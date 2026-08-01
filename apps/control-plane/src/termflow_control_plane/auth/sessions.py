"""Hashed, process-local browser sessions and browser-origin policy."""

from __future__ import annotations

import hmac
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import Request, WebSocket

from termflow_control_plane.auth.tokens import hash_token
from termflow_control_plane.config import Settings

PRODUCTION_COOKIE_NAME = "__Host-termflow_session"
DEVELOPMENT_COOKIE_NAME = "termflow_session"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class BrowserCookiePolicy:
    name: str
    secure: bool


class BrowserSessionStore:
    """Keep only session-secret digests, bounded by expiry and capacity."""

    def __init__(
        self,
        *,
        ttl: timedelta,
        capacity: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sessions: OrderedDict[str, datetime] = OrderedDict()

    def __repr__(self) -> str:
        return f"BrowserSessionStore(live_count={self.live_count}, capacity={self._capacity})"

    def _prune(self, now: datetime) -> None:
        expired = [digest for digest, expiry in self._sessions.items() if expiry <= now]
        for digest in expired:
            self._sessions.pop(digest, None)

    def create(self) -> tuple[str, datetime]:
        now = self._clock()
        self._prune(now)
        while len(self._sessions) >= self._capacity:
            self._sessions.popitem(last=False)
        secret = secrets.token_urlsafe(32)
        expires_at = now + self._ttl
        self._sessions[hash_token(secret)] = expires_at
        return secret, expires_at

    def authenticate(self, secret: str | None) -> datetime | None:
        now = self._clock()
        self._prune(now)
        if not secret:
            return None
        return self._sessions.get(hash_token(secret))

    def invalidate(self, secret: str | None) -> bool:
        if not secret:
            return False
        return self._sessions.pop(hash_token(secret), None) is not None

    @property
    def live_count(self) -> int:
        self._prune(self._clock())
        return len(self._sessions)


def browser_cookie_policy(settings: Settings) -> BrowserCookiePolicy:
    parsed = urlsplit(str(settings.public_base_url))
    if parsed.scheme == "https":
        return BrowserCookiePolicy(PRODUCTION_COOKIE_NAME, secure=True)
    if (
        parsed.scheme == "http"
        and parsed.hostname in _LOOPBACK_HOSTS
        and settings.allow_insecure_loopback
    ):
        return BrowserCookiePolicy(DEVELOPMENT_COOKIE_NAME, secure=False)
    raise RuntimeError("browser sessions require HTTPS or explicit loopback development mode")


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    return origin is not None and origin in settings.allowed_web_origins


def request_cookie_session(
    request: Request,
    settings: Settings,
    store: BrowserSessionStore,
) -> datetime | None:
    policy = browser_cookie_policy(settings)
    return store.authenticate(request.cookies.get(policy.name))


def _websocket_bearer(websocket: WebSocket) -> str | None:
    authorization = websocket.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        return None
    return token


def websocket_admin_authenticated(
    websocket: WebSocket,
    settings: Settings,
    store: BrowserSessionStore,
) -> bool:
    """Browser traffic needs exact Origin; Origin-less native traffic needs Bearer."""

    return websocket_admin_close_code(websocket, settings, store) is None


def websocket_admin_close_code(
    websocket: WebSocket,
    settings: Settings,
    store: BrowserSessionStore,
) -> int | None:
    """Return an authentication/policy close code, or ``None`` when allowed."""

    bearer = _websocket_bearer(websocket)
    origin = websocket.headers.get("origin")
    expected = settings.admin_token.get_secret_value()
    if origin is None:
        return None if bearer is not None and hmac.compare_digest(bearer, expected) else 4401
    if not origin_allowed(origin, settings):
        return 4403
    if bearer is not None and hmac.compare_digest(bearer, expected):
        return None
    policy = browser_cookie_policy(settings)
    return None if store.authenticate(websocket.cookies.get(policy.name)) is not None else 4401
