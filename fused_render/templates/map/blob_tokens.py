"""Read tokens for Azure blob sources that refuse anonymous access.

Storage accounts behind Microsoft Planetary Computer (NASA HLS, Sentinel,
Landsat, NAIP) have public access disabled: every request comes back
``409 Public access is not permitted on this storage account``. Planetary
Computer issues short-lived read-only SAS tokens for those containers, so a URL
that a browser cannot open still opens here.

Signing is learned, not assumed. Plenty of Azure containers are public, and
asking the token endpoint before each of those opens would add a round trip for
nothing; the store only starts signing a container once that container has
actually turned us away.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import urllib.request
from urllib.parse import urlsplit


BLOB_HOST_SUFFIX = ".blob.core.windows.net"
TOKEN_API = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
# Anonymous access disabled (409), no credentials (401), credentials rejected
# (403). Anything else — a missing object above all — is not ours to retry.
REFUSAL_MARKERS = (
    "response code: 401",
    "response code: 403",
    "response code: 409",
    "public access is not permitted",
)
REFRESH_MARGIN = 300.0
ASSUMED_LIFETIME = 1800.0


def container_of(url: str) -> tuple[str, str] | None:
    """Return the ``(account, container)`` an Azure blob URL addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname or ""
    if not host.lower().endswith(BLOB_HOST_SUFFIX):
        return None
    account = host[: -len(BLOB_HOST_SUFFIX)]
    container = parts.path.lstrip("/").split("/")[0]
    return (account.lower(), container) if account and container else None


def is_signed(url: str) -> bool:
    return "sig=" in urlsplit(url).query


def refused_access(message: str) -> bool:
    lowered = str(message).lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)


def _fetch_token(account: str, container: str) -> tuple[str, str]:
    with urllib.request.urlopen(
        f"{TOKEN_API}/{account}/{container}", timeout=20
    ) as response:
        payload = json.load(response)
    return payload["token"], payload.get("msft:expiry", "")


def _lifetime(expiry: str) -> float:
    """Seconds of validity left in an ISO-8601 expiry, read off the wall clock."""
    try:
        stamp = dt.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return ASSUMED_LIFETIME
    remaining = stamp.timestamp() - time.time()
    return remaining if remaining > 0 else ASSUMED_LIFETIME


class TokenStore:
    """Per-container tokens, kept until they are close to expiring."""

    def __init__(self, fetch=_fetch_token, now=time.monotonic) -> None:
        self._fetch = fetch
        self._now = now
        self._lock = threading.Lock()
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._needed: set[tuple[str, str]] = set()

    def sign(self, url: str) -> str:
        """Append a read token to *url*, fetching one if none is current."""
        key = container_of(url)
        if key is None:
            return url
        token = self._token(key)
        separator = "&" if urlsplit(url).query else "?"
        return url + separator + token

    def needs_signing(self, url: str) -> bool:
        """Whether *url*'s container has refused us and is now being signed."""
        return container_of(url) in self._needed

    def signed_if_needed(self, url: str) -> str:
        """Sign *url* only if its container has already refused us."""
        key = container_of(url)
        if key is None or key not in self._needed or is_signed(url):
            return url
        try:
            return self.sign(url)
        except Exception:
            return url

    def learn(self, url: str, message: str) -> bool:
        """Record that *url*'s container needs signing; return whether to retry.

        False means the caller's error stands: a public-access refusal is worth
        one retry with a token, a 404 is not, and a URL we already signed has
        had its chance.
        """
        key = container_of(url)
        if key is None or is_signed(url) or not refused_access(message):
            return False
        try:
            self._token(key)
        except Exception:
            return False
        with self._lock:
            self._needed.add(key)
        return True

    def _token(self, key: tuple[str, str]) -> str:
        with self._lock:
            current = self._tokens.get(key)
            if current and current[1] - self._now() > REFRESH_MARGIN:
                return current[0]
        token, expiry = self._fetch(*key)
        # The endpoint reports wall-clock expiry while the store measures
        # against whatever clock it was given, so keep the lifetime rather than
        # the timestamp.
        with self._lock:
            self._tokens[key] = (token, self._now() + _lifetime(expiry))
        return token


TOKENS = TokenStore()
