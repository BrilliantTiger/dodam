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
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Response
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
    db.execute("CREATE TABLE IF NOT EXISTS revoked_tokens (jti TEXT PRIMARY KEY, expires INTEGER NOT NULL)")


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


class NameChange(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str


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


def valid_name(name: str) -> bool:
    return utf8(name) is not None and 1 <= len(name) <= 40


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
            or not isinstance(claims.get("exp"), int) or claims["exp"] < time.time() \
            or not isinstance(claims.get("jti"), str):
        return None
    return claims


@dataclass
class Session:
    user: dict
    jti: str
    expires: int


def current_session(authorization: str | None = Header(default=None)) -> Session:
    scheme, _, token = (authorization or "").partition(" ")
    claims = read_token(token) if scheme == "Bearer" else None
    row = None
    if claims:
        with database() as db:
            revoked = db.execute("SELECT 1 FROM revoked_tokens WHERE jti = ?", (claims["jti"],)).fetchone()
            if not revoked:
                row = db.execute("SELECT id, email, name FROM users WHERE id = ?", (claims["uid"],)).fetchone()
    if not row:
        raise HTTPException(401, "missing or invalid token")
    return Session({"id": row[0], "email": row[1], "name": row[2]}, claims["jti"], claims["exp"])


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", status_code=201)
def register(body: Signup):
    email, name = body.email.strip().lower(), body.name.strip()
    if any(utf8(text) is None for text in (email, body.password, name)) \
            or len(email) > 254 or not EMAIL.fullmatch(email) \
            or not 8 <= len(body.password) <= 128 or not valid_name(name):
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
def me(session: Session = Depends(current_session)):
    return session.user


@app.patch("/me")
def change_name(body: NameChange, session: Session = Depends(current_session)):
    name = body.name.strip()
    if not valid_name(name):
        raise HTTPException(400, "invalid name")
    with database() as db:
        db.execute("UPDATE users SET name = ? WHERE id = ?", (name, session.user["id"]))
    return {**session.user, "name": name}


@app.post("/logout", status_code=204)
def logout(session: Session = Depends(current_session)):
    with database() as db:
        db.execute("INSERT OR IGNORE INTO revoked_tokens (jti, expires) VALUES (?, ?)", (session.jti, session.expires))
    return Response(status_code=204)


@app.get("/users/{user_id}")
def user_by_id(user_id: str, session: Session = Depends(current_session)):
    if user_id != str(session.user["id"]):
        raise HTTPException(403, "not your account")
    return session.user
