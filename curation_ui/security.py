"""Authentication, failed-auth throttling and CSRF for the curation UI.

These live outside main.py so health.py can put a route behind require_auth
without importing main, and so both protections can be tested without booting
the app.
"""

import hashlib
import hmac
import logging
import secrets
import time
from collections import deque
from ipaddress import ip_address, ip_network

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from src.shared.config import get_settings

logger = logging.getLogger(__name__)

security = HTTPBasic()

# Failed authentications are allowed per client per window. Only failures count,
# so a curator who is already signed in is never locked out of their own tab.
AUTH_WINDOW_SECONDS = 60.0
AUTH_MAX_FAILED = 10
# Ceiling on tracked clients. Buckets expire on their own, but an attacker who
# can vary its source address must not be able to grow the map without bound.
AUTH_MAX_CLIENTS = 10_000

# Parsed CURATION_TRUSTED_PROXIES, cached per raw value so the env var is read
# once per change rather than once per request.
_trusted_networks_cache: tuple[str, tuple] = ("", ())


def _trusted_networks() -> tuple:
    """Networks whose X-Forwarded-For we believe, from CURATION_TRUSTED_PROXIES.

    Empty by default: that header is client-controlled unless a proxy we control
    is the one that set it.
    """
    global _trusted_networks_cache
    raw = get_settings().curation_trusted_proxies.strip()
    cached_raw, networks = _trusted_networks_cache
    if raw == cached_raw:
        return networks
    parsed = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            parsed.append(ip_network(part, strict=False))
        except ValueError:
            logger.warning("Ignoring unparseable CURATION_TRUSTED_PROXIES entry %r", part)
    _trusted_networks_cache = (raw, tuple(parsed))
    return _trusted_networks_cache[1]


def _is_trusted_proxy(address: str) -> bool:
    try:
        parsed = ip_address(address.strip())
    except ValueError:
        return False
    return any(parsed in network for network in _trusted_networks())


def client_key(request: Request) -> str:
    """The key failures are counted under for this request.

    X-Forwarded-For is only consulted when the direct peer is a trusted proxy,
    and the chain is then walked right to left: the last hop that is not one of
    ours is the caller. Trusting the header unconditionally, or taking its first
    entry blindly, lets a client pick its own bucket by rotating the header,
    which is the same as having no limit at all.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    chain = [hop.strip() for hop in request.headers.get("X-Forwarded-For", "").split(",")]
    for hop in reversed(chain):
        if hop and not _is_trusted_proxy(hop):
            return hop
    return peer


class FailureLimiter:
    """Counts failed authentications per client over a sliding window.

    Each client gets a deque of failure timestamps, so a bucket shrinks by itself
    as the window slides. Reading never creates a bucket, a success clears one,
    and the client count is capped.
    """

    def __init__(
        self,
        window_seconds: float,
        max_failures: int,
        max_clients: int = AUTH_MAX_CLIENTS,
        clock=time.monotonic,
    ):
        self.window_seconds = window_seconds
        self.max_failures = max_failures
        self.max_clients = max_clients
        self.clock = clock
        self._failures: dict[str, deque] = {}

    def _bucket(self, key: str, now: float) -> deque:
        """This client's in-window timestamps, forgetting the bucket when empty."""
        bucket = self._failures.get(key)
        if bucket is None:
            return deque()
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            del self._failures[key]
            return deque()
        return bucket

    def blocked(self, key: str, now: float | None = None) -> bool:
        """True when this client already failed max_failures times in the window."""
        now = self.clock() if now is None else now
        return len(self._bucket(key, now)) >= self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        """Count one failure. The only path that creates a bucket."""
        now = self.clock() if now is None else now
        bucket = self._bucket(key, now)
        if not bucket:
            if len(self._failures) >= self.max_clients:
                self._evict_one(now)
            bucket = deque()
            self._failures[key] = bucket
        bucket.append(now)

    def record_success(self, key: str) -> None:
        """Forget the bucket: this caller proved they were not guessing."""
        self._failures.pop(key, None)

    def _evict_one(self, now: float) -> None:
        """Free a slot at the client cap, preferring buckets nothing is using."""
        cutoff = now - self.window_seconds
        for key, bucket in list(self._failures.items()):
            if not bucket or bucket[-1] <= cutoff:
                del self._failures[key]
            if len(self._failures) < self.max_clients:
                return
        if self._failures:
            del self._failures[next(iter(self._failures))]

    def reset(self) -> None:
        self._failures.clear()


auth_limiter = FailureLimiter(AUTH_WINDOW_SECONDS, AUTH_MAX_FAILED)


async def require_auth(request: Request, creds: HTTPBasicCredentials = Depends(security)) -> str:
    """Require HTTP Basic credentials, throttling repeated failures.

    Failures are counted per client (see client_key) and only failures count: a
    correct password always gets through, so brute forcing a bucket shut does not
    lock out the curator.
    """
    settings = get_settings()
    if not settings.has_curation_auth:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Curation UI not configured: CURATION_USER and CURATION_PASSWORD must be set",
        )

    key = client_key(request)
    ok_user = secrets.compare_digest(creds.username, settings.curation_user)
    ok_pass = secrets.compare_digest(creds.password, settings.curation_password)

    if ok_user and ok_pass:
        auth_limiter.record_success(key)
        return creds.username

    if auth_limiter.blocked(key):
        retry_after = int(auth_limiter.window_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many failed authentication attempts. "
                f"Try again in {retry_after} seconds."
            ),
            headers={"Retry-After": str(retry_after)},
        )
    auth_limiter.record_failure(key)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


# CSRF.
#
# Browsers cache HTTP Basic credentials and re-attach them to cross-origin
# requests, so an attacker's page can POST to the triage routes as the curator
# unless the request also proves it came from a page this app rendered.
CSRF_HEADER = "X-CSRF-Token"


def _csrf_key() -> bytes:
    """Signing key for CSRF tokens, derived from the curation password.

    Stateless on purpose: serverless instances share no memory, so a token has to
    verify on any of them. Deriving it from the password means no extra secret to
    deploy, and it invalidates every outstanding token when credentials rotate.
    """
    return hashlib.sha256(f"csrf:{get_settings().curation_password}".encode()).digest()


def issue_csrf_token() -> str:
    """Mint the token for one rendered page: a random nonce and its signature."""
    nonce = secrets.token_urlsafe(16)
    signature = hmac.new(_csrf_key(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


async def require_csrf(request: Request) -> None:
    """Reject a state-changing request that does not carry this app's token.

    htmx sends the token minted with the page in CSRF_HEADER (see the hx-headers
    attribute in the templates), which covers the button posts and the keyboard
    shortcut posts alike.
    """
    token = request.headers.get(CSRF_HEADER, "")
    nonce, _, signature = token.partition(".")
    expected = (
        hmac.new(_csrf_key(), nonce.encode(), hashlib.sha256).hexdigest() if nonce else ""
    )
    if not nonce or not hmac.compare_digest(signature, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token",
        )