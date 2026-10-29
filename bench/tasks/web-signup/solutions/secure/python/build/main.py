"""Sign-up and login API: FastAPI, SQLite, scrypt password hashes and HMAC-signed tokens."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

DB_PATH = os.environ["DB_PATH"]
SECRET = os.environ["APP_SECRET"].encode()
MAX_BODY = 64 * 1024
TOKEN_SECONDS = 3600
EMAIL = re.compile(r"[^@\s]+@[^@\s]*\.[^@\s]*")
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


@contextmanager
def database():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


with database() as db:
    db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,"
               " name TEXT NOT NULL, password_hash TEXT NOT NULL)")


class BodyLimit:
    """Refuse a body over MAX_BODY with 413: by its declared length before reading, or once it streams past."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > MAX_BODY:
            return await JSONResponse({"error": "request body too large"}, status_code=413)(scope, receive, send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > MAX_BODY:
                raise HTTPException(413, "request body too large")
            return message

        await self.app(scope, limited_receive, send)


class Signup(BaseModel):
    model_config = ConfigDict(strict=True)
    email: str
    password: str
    name: str


class Login(BaseModel):
    model_config = ConfigDict(strict=True)
    email: str
    password: str


app = FastAPI()
app.add_middleware(BodyLimit)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def invalid_body(request, exc):
    return JSONResponse({"error": "invalid request body"}, status_code=400)


def utf8(text: str) -> bytes | None:
    try:
        return text.encode()
    except UnicodeEncodeError:
        return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def password_matches(password: str, stored: str) -> bool:
    _, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex()
    return hmac.compare_digest(candidate, digest)


UNKNOWN_USER_HASH = hash_password(secrets.token_hex(16))


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signature(payload: str) -> str:
    return b64(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())


def issue_token(user_id: int) -> str:
    claims = {"uid": user_id, "exp": int(time.time()) + TOKEN_SECONDS, "jti": secrets.token_urlsafe(16)}
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{signature(payload)}"


def read_token(token: str) -> dict | None:
    payload, dot, sig = token.partition(".")
    if not dot or utf8(token) is None or not hmac.compare_digest(sig.encode(), signature(payload).encode()):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("uid"), int) \
            or not isinstance(claims.get("exp"), int) or claims["exp"] < time.time():
        return None
    return claims


def current_user(authorization: str | None = Header(default=None)) -> dict:
    scheme, _, token = (authorization or "").partition(" ")
    claims = read_token(token) if scheme == "Bearer" else None
    row = None
    if claims:
        with database() as db:
            row = db.execute("SELECT id, email, name FROM users WHERE id = ?", (claims["uid"],)).fetchone()
    if not row:
        raise HTTPException(401, "missing or invalid token")
    return {"id": row[0], "email": row[1], "name": row[2]}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", status_code=201)
def register(body: Signup):
    email, name = body.email.strip().lower(), body.name.strip()
    if any(utf8(text) is None for text in (email, body.password, name)) \
            or len(email) > 254 or not EMAIL.fullmatch(email) \
            or not 8 <= len(body.password) <= 128 or not 1 <= len(name) <= 40:
        raise HTTPException(400, "invalid email, password or name")
    password_hash = hash_password(body.password)
    try:
        with database() as db:
            user_id = db.execute("INSERT INTO users (email, name, password_hash) VALUES (?, ?, ?)",
                                 (email, name, password_hash)).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "email already registered") from None
    return {"id": user_id}


@app.post("/login")
def login(body: Login):
    email = body.email.strip().lower()
    row = None
    if utf8(email) is not None and utf8(body.password) is not None:
        with database() as db:
            row = db.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    matches = utf8(body.password) is not None and password_matches(body.password, row[1] if row else UNKNOWN_USER_HASH)
    if not row or not matches:
        raise HTTPException(401, "wrong email or password")
    return {"token": issue_token(row[0])}


@app.get("/me")
def me(user: dict = Depends(current_user)):
    return user


@app.get("/users/{user_id}")
def user_by_id(user_id: str, user: dict = Depends(current_user)):
    if user_id != str(user["id"]):
        raise HTTPException(403, "not your account")
    return user
