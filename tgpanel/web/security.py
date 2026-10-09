"""Authentication primitives: signed stateless sessions, CSRF, login rate limiting.

Sessions are stateless signed cookies (itsdangerous, secret from ``TGPANEL_SECRET_KEY``). A
session is valid while it is not expired AND its version equals the ``panel_session_version``
setting AND its login equals ``panel_login``. Bumping the version (password change, "log out
everywhere") invalidates every session at once.

The login limiter is IN-MEMORY and per process: a restart forgets the counters. That is
acceptable for a single-admin panel behind a random path; the attempts are counted per client
IP (``X-Forwarded-For`` is trusted only from the local reverse proxy).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from itsdangerous import BadData, URLSafeSerializer

SESSION_COOKIE = "tgp_session"
LOGIN_COOKIE = "tgp_login"
FLASH_COOKIE = "tgp_flash"
COLS_COOKIE = "tgp_cols"
SESSION_TTL_S = 12 * 3600
LOGIN_TOKEN_TTL_S = 3600
MIN_SECRET_KEY_LEN = 32


@dataclass(frozen=True, slots=True)
class Session:
    login: str
    version: int
    exp: int  # unix seconds
    csrf: str


class Signer:
    """Signed JSON payloads with per-purpose salts."""

    def __init__(self, secret_key: str) -> None:
        if len(secret_key) < MIN_SECRET_KEY_LEN:
            raise ValueError("TGPANEL_SECRET_KEY must be at least 32 characters")
        self._key = secret_key

    def _ser(self, purpose: str) -> URLSafeSerializer:
        return URLSafeSerializer(
            self._key,
            salt=f"tgpanel.web.{purpose}",
            signer_kwargs={"digest_method": hashlib.sha256, "key_derivation": "hmac"},
        )

    def dumps(self, purpose: str, payload: dict[str, Any]) -> str:
        return str(self._ser(purpose).dumps(payload))

    def loads(self, purpose: str, token: str | None) -> dict[str, Any] | None:
        if not token or len(token) > 4096:
            return None
        try:
            data = self._ser(purpose).loads(token)
        except BadData:
            return None
        return data if isinstance(data, dict) else None

    # sessions -----------------------------------------------------------------------------

    def make_session(self, login: str, version: int, now_s: int) -> Session:
        return Session(login, version, now_s + SESSION_TTL_S, secrets.token_urlsafe(24))

    def dump_session(self, s: Session) -> str:
        return self.dumps("session", {"l": s.login, "v": s.version, "e": s.exp, "c": s.csrf})

    def load_session(self, token: str | None, now_s: int) -> Session | None:
        data = self.loads("session", token)
        if data is None:
            return None
        try:
            session = Session(str(data["l"]), int(data["v"]), int(data["e"]), str(data["c"]))
        except (KeyError, TypeError, ValueError):
            return None
        return session if session.exp > now_s else None

    # pre-login CSRF token (double submit: cookie + hidden field) --------------------------

    def make_login_token(self, now_s: int) -> str:
        return self.dumps("login", {"n": secrets.token_urlsafe(16), "e": now_s + LOGIN_TOKEN_TTL_S})

    def login_token_ok(self, cookie: str | None, field_value: str | None, now_s: int) -> bool:
        if not cookie or not field_value or not _same(cookie, field_value):
            return False
        data = self.loads("login", cookie)
        return data is not None and int(data.get("e", 0)) > now_s


def _same(a: str, b: str) -> bool:
    """Constant-time comparison that also accepts non-ASCII input (it is simply unequal)."""
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


def csrf_ok(expected: str, *candidates: str | None) -> bool:
    return any(c is not None and c != "" and _same(c, expected) for c in candidates)


@dataclass(slots=True)
class _State:
    attempts: list[float] = field(default_factory=list)
    blocked_until: float = 0.0
    strikes: int = 0
    last_seen: float = 0.0


class LoginLimiter:
    """5 attempts per minute per IP, then a pause that doubles with each repeated block."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        max_attempts: int = 5,
        window_s: float = 60.0,
        base_penalty_s: float = 60.0,
        max_penalty_s: float = 3600.0,
    ) -> None:
        self._clock = clock
        self._max = max_attempts
        self._window = window_s
        self._base = base_penalty_s
        self._cap = max_penalty_s
        self._state: dict[str, _State] = {}
        self._new_block = False

    def allow(self, ip: str) -> float:
        """0.0 when the attempt may proceed (and is counted); otherwise seconds to wait."""
        now = self._clock()
        self._new_block = False
        self._prune(now)
        st = self._state.setdefault(ip, _State())
        st.last_seen = now
        if st.blocked_until > now:
            return st.blocked_until - now
        st.attempts = [t for t in st.attempts if now - t < self._window]
        if len(st.attempts) >= self._max:
            st.strikes += 1
            penalty = min(self._cap, self._base * float(2 ** (st.strikes - 1)))
            st.blocked_until = now + penalty
            st.attempts.clear()
            self._new_block = True
            return penalty
        st.attempts.append(now)
        return 0.0

    @property
    def newly_blocked(self) -> bool:
        """True if the last ``allow`` call started a new block."""
        return self._new_block

    def success(self, ip: str) -> None:
        self._state.pop(ip, None)

    def _prune(self, now: float) -> None:
        if len(self._state) < 2048:
            return
        stale = [k for k, s in self._state.items() if now - s.last_seen > 86400]
        for k in stale:
            del self._state[k]


class GlobalFailureLimiter:
    """Failed logins from all clients together: slows logins down, never locks anybody out.

    An outsider can always produce failures from many addresses, so a hard global block would
    let them lock the administrator out. Instead, once ``free_failures`` failures happened in the
    window, every new login attempt waits an artificial delay that grows by ``step_s`` per extra
    failure up to ``max_delay_s`` (per-client /64 limits stay in force separately).
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        *,
        free_failures: int = 10,
        window_s: float = 60.0,
        step_s: float = 0.1,
        max_delay_s: float = 2.0,
    ) -> None:
        self._clock = clock
        self._free = free_failures
        self._window = window_s
        self._step = step_s
        self._max = max_delay_s
        self._failures: list[float] = []

    def _recent(self) -> list[float]:
        now = self._clock()
        self._failures = [t for t in self._failures if now - t < self._window]
        return self._failures

    def delay(self) -> float:
        """Seconds a new login attempt must wait before it is processed (0 when calm)."""
        extra = len(self._recent()) - self._free
        return 0.0 if extra <= 0 else min(self._max, extra * self._step)

    def record_failure(self) -> bool:
        """Count a failure; True when this one made the delay start."""
        recent = self._recent()
        recent.append(self._clock())
        return len(recent) == self._free + 1
