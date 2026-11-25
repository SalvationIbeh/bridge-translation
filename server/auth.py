"""
Stateless HMAC signed-token auth for /ws/translate (S3 — council decision
docs/decisions/2026-06-11-s3-ws-auth-hmac-tokens.md).

Token format:   v1.<expiry_unix>.<nonce>.<hmac_sha256_hex>
Signed payload: "v1.<expiry_unix>.<nonce>" with AUTH_SIGNING_KEY.

Properties (and their honest limits):
- Expiry caps the blast radius of a leaked token (vs the old standing AUTH_TOKEN).
- The server also rejects tokens minted with a lifetime beyond its configured TTL,
  so a misconfigured mint can't create long-lived credentials.
- Single-use nonces stop replay within the validity window. The nonce cache is
  IN-MEMORY: single-pod only, resets on restart — acceptable because expiry
  bounds the replay window anyway (Skeptic's dissent, accepted in the decision).
- ±CLOCK_SKEW_S tolerance covers founder-laptop vs pod clock drift.

No imports from config — callers pass the key/TTL so scripts/mint_token.py and a
future token-broker can mint the exact same token shape (mint-source swap, not a
protocol change).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

TOKEN_VERSION = "v1"
CLOCK_SKEW_S = 30


def _sign(key: str, payload: str) -> str:
    return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def prod_static_mode_forbidden(env: str, signing_key: str) -> bool:
    """True when the config is the forbidden combo: a prod deployment running the
    legacy static-token path (no AUTH_SIGNING_KEY). The server refuses to boot in
    this state, so the insecure mode is *structurally absent* in prod rather than
    merely policed (docs/decisions/2026-06-20-ws-auth-resumption-token.md, control #4)."""
    return (env or "").lower() == "prod" and not signing_key


def mint_token(key: str, ttl_s: int = 300, now: float | None = None) -> str:
    """Create a signed token valid for ttl_s seconds."""
    if not key:
        raise ValueError("signing key must be non-empty")
    expiry = int((now if now is not None else time.time()) + ttl_s)
    nonce = secrets.token_urlsafe(12)
    payload = f"{TOKEN_VERSION}.{expiry}.{nonce}"
    return f"{payload}.{_sign(key, payload)}"


class NonceCache:
    """In-memory single-use nonce tracking. Single-pod state; resets on restart."""

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}   # nonce → expiry

    def check_and_store(self, nonce: str, expiry: int, now: float) -> bool:
        """True if the nonce is fresh (and is now recorded); False if replayed."""
        # Prune expired entries so the cache stays bounded by tokens-in-flight.
        cutoff = now - CLOCK_SKEW_S
        for n in [n for n, exp in self._seen.items() if exp < cutoff]:
            del self._seen[n]
        if nonce in self._seen:
            return False
        self._seen[nonce] = expiry
        return True


def validate_token(token: str, key: str, max_ttl_s: int,
                   nonce_cache: NonceCache | None = None,
                   now: float | None = None) -> tuple[bool, str]:
    """Validate a signed token. Returns (ok, reason). Constant-time signature check."""
    now = now if now is not None else time.time()
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        return False, "malformed token"
    version, expiry_s, nonce, sig = parts
    payload = f"{version}.{expiry_s}.{nonce}"
    # Signature first: nothing after this line can be attacker-controlled garbage.
    if not hmac.compare_digest(_sign(key, payload), sig):
        return False, "bad signature"
    try:
        expiry = int(expiry_s)
    except ValueError:
        return False, "malformed expiry"
    if now > expiry + CLOCK_SKEW_S:
        return False, "expired"
    if expiry > now + max_ttl_s + CLOCK_SKEW_S:
        return False, "ttl exceeds server maximum"
    if nonce_cache is not None and not nonce_cache.check_and_store(nonce, expiry, now):
        return False, "replayed nonce"
    return True, "ok"
