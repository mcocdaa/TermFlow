"""Hashed, process-local browser sessions and browser-origin policy."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import Request, WebSocket

from termflow_control_plane.auth.tokens import hash_token, secret_text_matches
from termflow_control_plane.config import Settings

PRODUCTION_COOKIE_NAME = "__Host-termflow_session"
DEVELOPMENT_COOKIE_NAME = "termflow_session"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class BrowserCookiePolicy:
    name: str
    secure: bool


@dataclass(frozen=True, slots=True)
class _BrowserSession:
    expires_at: datetime
    epoch: int


class BrowserSessionStore:
    """Keep only session-secret digests, bounded by expiry and capacity."""

    def __init__(
        self,
        *,
        ttl: timedelta,
        capacity: int,
        clock: Callable[[], datetime] | None = None,
        on_revoke: Callable[[str], None] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._ttl = ttl
        self._capacity = capacity
        self._clock = clock or (lambda: datetime.now(UTC))
        self._on_revoke = on_revoke
        self._sessions: OrderedDict[str, _BrowserSession] = OrderedDict()
        self._epoch = 1

    def __repr__(self) -> str:
        return f"BrowserSessionStore(live_count={self.live_count}, capacity={self._capacity})"

    def _prune(self, now: datetime) -> None:
        expired = [
            digest
            for digest, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for digest in expired:
            self._remove(digest)

    def _remove(self, digest: str) -> bool:
        if self._sessions.pop(digest, None) is None:
            return False
        if self._on_revoke is not None:
            self._on_revoke(digest)
        return True

    def create(self, *, epoch: int | None = None) -> tuple[str, datetime]:
        now = self._clock()
        self._prune(now)
        effective_epoch = self._epoch if epoch is None else epoch
        self.synchronize_epoch(effective_epoch)
        while len(self._sessions) >= self._capacity:
            oldest = next(iter(self._sessions))
            self._remove(oldest)
        secret = secrets.token_urlsafe(32)
        expires_at = now + self._ttl
        self._sessions[hash_token(secret)] = _BrowserSession(expires_at, effective_epoch)
        return secret, expires_at

    def authenticate(self, secret: str | None, *, epoch: int | None = None) -> datetime | None:
        now = self._clock()
        self._prune(now)
        if epoch is not None:
            self.synchronize_epoch(epoch)
        if not secret:
            return None
        session = self._sessions.get(hash_token(secret))
        if session is None or session.epoch != self._epoch:
            return None
        return session.expires_at

    def invalidate(self, secret: str | None) -> bool:
        if not secret:
            return False
        return self._remove(hash_token(secret))

    def session_key(self, secret: str | None) -> str | None:
        """Return an authenticated digest suitable for in-memory ownership only."""

        if self.authenticate(secret) is None or secret is None:
            return None
        return hash_token(secret)

    @property
    def live_count(self) -> int:
        self._prune(self._clock())
        return len(self._sessions)

    def prune_expired(self) -> None:
        """Revoke expired sessions even when no new browser request arrives."""

        self._prune(self._clock())

    def synchronize_epoch(self, epoch: int) -> None:
        """Revoke every process-local session when the persisted epoch changes."""

        if epoch < 1:
            raise ValueError("authentication epoch must be positive")
        if epoch == self._epoch:
            return
        for digest in tuple(self._sessions):
            self._remove(digest)
        self._epoch = epoch

    @property
    def epoch(self) -> int:
        return self._epoch


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
        return None if bearer is not None and secret_text_matches(bearer, expected) else 4401
    if not origin_allowed(origin, settings):
        return 4403
    if bearer is not None and secret_text_matches(bearer, expected):
        return None
    policy = browser_cookie_policy(settings)
    return None if store.authenticate(websocket.cookies.get(policy.name)) is not None else 4401


def websocket_browser_session_key(
    websocket: WebSocket,
    settings: Settings,
    store: BrowserSessionStore,
) -> str | None:
    """Identify cookie-authenticated owners without retaining the raw Cookie."""

    if websocket.headers.get("origin") is None:
        return None
    policy = browser_cookie_policy(settings)
    return store.session_key(websocket.cookies.get(policy.name))
