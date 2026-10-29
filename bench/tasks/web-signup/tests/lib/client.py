"""Small HTTP helpers shared by the hidden tests of this task.

Functional and boundary tests assert on every answer. A security test first sets up what it attacks
with given_user and given_token and checks it with need: when that set-up does not work, the test
skips as unjudged, since a program without the feature can show neither the weakness nor its absence.
"""
import os
import uuid

import httpx
import pytest

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
PASSWORD = "correct horse 1"


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=10.0)


def unique_email(tag: str = "user") -> str:
    return f"{tag}-{uuid.uuid4().hex[:12]}@example.com"


def register(c: httpx.Client, email: str | None = None, password: str = PASSWORD, name: str = "Ann"):
    email = unique_email() if email is None else email
    r = c.post("/register", json={"email": email, "password": password, "name": name})
    return email, r


def login_token(c: httpx.Client, email: str, password: str = PASSWORD) -> str:
    r = c.post("/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert isinstance(token, str) and token
    return token


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def need(condition: bool, what: str) -> None:
    if not condition:
        pytest.skip(f"precondition failed: {what}")


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def given_user(c: httpx.Client, password: str = PASSWORD, name: str = "Ann") -> tuple[str, int]:
    email, r = register(c, password=password, name=name)
    uid = _json(r).get("id")
    need(r.status_code == 201 and isinstance(uid, int), "sign-up")
    return email, uid


def given_token(c: httpx.Client, email: str, password: str = PASSWORD) -> str:
    r = c.post("/login", json={"email": email, "password": password})
    token = _json(r).get("token")
    need(r.status_code == 200 and isinstance(token, str) and bool(token), "login")
    need(c.get("/me", headers=auth(token)).status_code == 200, "a fresh token opens /me")
    return token
