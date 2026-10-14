"""Email + password accounts with server-side sessions in an HttpOnly cookie."""

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .config import settings
from .db import get_conn

COOKIE_NAME = "talk2me_session"
SESSION_TTL = timedelta(days=30)

_hasher = PasswordHasher()
# Verified against when the email is unknown, so response timing doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

router = APIRouter(prefix="/api/auth", tags=["auth"])


class _RateLimiter:
    """In-process sliding window. Good enough for one instance; use Redis if you scale out."""

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                raise HTTPException(429, "Too many attempts, try again later")
            hits.append(now)


_login_limiter = _RateLimiter(max_attempts=10, window_seconds=15 * 60)  # per email
# Per IP, looser: a household or office shares one address.
_ip_limiter = _RateLimiter(max_attempts=30, window_seconds=15 * 60)


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _valid_timezone(tz: str | None) -> str:
    try:
        return ZoneInfo(tz).key if tz else "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


def _start_session(response: Response, request: Request, user_id: int) -> None:
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, user_agent) VALUES (%s, %s, %s, %s)",
            (
                _token_hash(token),
                user_id,
                datetime.now(timezone.utc) + SESSION_TTL,
                request.headers.get("user-agent", "")[:200],
            ),
        )
    secure = settings.cookie_secure if settings.cookie_secure is not None else request.url.scheme == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


class User(BaseModel):
    id: int
    email: str
    display_name: str
    timezone: str


def current_user(request: Request) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "Not signed in")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT u.id, u.email, u.display_name, u.timezone FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash = %s AND s.expires_at > now()",
            (_token_hash(token),),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Session expired")
    return User(**row)


CurrentUser = Annotated[User, Depends(current_user)]


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=10, max_length=256)
    display_name: str = Field(default="", max_length=100)
    timezone: str | None = None


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    timezone: str | None = None


@router.get("/status")
def status():
    """Lets the UI decide whether to offer sign-up."""
    with get_conn() as conn:
        has_accounts = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM users WHERE password_hash IS NOT NULL) AS v"
        ).fetchone()["v"]
    return {"signup_open": settings.allow_signup or not has_accounts}


@router.post("/signup", status_code=201)
def signup(body: Credentials, request: Request, response: Response) -> User:
    _ip_limiter.check(f"signup:{request.client.host if request.client else ''}")
    email = body.email.strip().lower()
    password_hash = _hasher.hash(body.password)
    tz = _valid_timezone(body.timezone)

    with get_conn() as conn:
        # Serialize signups so two concurrent "first user" requests can't both succeed.
        conn.execute("SELECT pg_advisory_xact_lock(7242002)")
        has_accounts = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM users WHERE password_hash IS NOT NULL) AS v"
        ).fetchone()["v"]
        if has_accounts and not settings.allow_signup:
            raise HTTPException(403, "Sign-up is closed")
        if conn.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
            raise HTTPException(409, "An account with that email already exists")

        placeholder = None
        if not has_accounts:
            # First real account inherits anything created before auth existed.
            placeholder = conn.execute(
                "SELECT id FROM users WHERE email = %s AND password_hash IS NULL",
                (settings.legacy_user_email,),
            ).fetchone()
        if placeholder:
            row = conn.execute(
                "UPDATE users SET email = %s, display_name = %s, password_hash = %s, timezone = %s "
                "WHERE id = %s RETURNING id, email, display_name, timezone",
                (email, body.display_name, password_hash, tz, placeholder["id"]),
            ).fetchone()
        else:
            row = conn.execute(
                "INSERT INTO users (email, display_name, password_hash, timezone) VALUES (%s, %s, %s, %s) "
                "RETURNING id, email, display_name, timezone",
                (email, body.display_name, password_hash, tz),
            ).fetchone()

    _start_session(response, request, row["id"])
    return User(**row)


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> User:
    email = body.email.strip().lower()
    _login_limiter.check(f"login:{email}")
    _ip_limiter.check(f"login:{request.client.host if request.client else ''}")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, display_name, timezone, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()
    try:
        _hasher.verify(row["password_hash"] if row and row["password_hash"] else _DUMMY_HASH, body.password)
        if not row or not row["password_hash"]:
            raise VerifyMismatchError
    except (VerifyMismatchError, InvalidHashError):
        raise HTTPException(401, "Incorrect email or password") from None

    tz = _valid_timezone(body.timezone) if body.timezone else row["timezone"]
    with get_conn() as conn:
        updates = {"timezone": tz}
        if _hasher.check_needs_rehash(row["password_hash"]):
            updates["password_hash"] = _hasher.hash(body.password)
        conn.execute(
            "UPDATE users SET timezone = %(timezone)s"
            + (", password_hash = %(password_hash)s" if "password_hash" in updates else "")
            + " WHERE id = %(id)s",
            {**updates, "id": row["id"]},
        )

    _start_session(response, request, row["id"])
    return User(id=row["id"], email=row["email"], display_name=row["display_name"], timezone=tz)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_token_hash(token),))
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me")
def me(user: CurrentUser) -> User:
    return user
