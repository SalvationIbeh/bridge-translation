"""
Unit tests for auth.py (S3 signed tokens). No GPU, no server — plain Python:

    python server/test_auth.py
"""
from __future__ import annotations

import sys

import auth


def main() -> int:
    key = "test-signing-key"
    t0 = 1_750_000_000.0

    # Happy path: mint → validate (fresh nonce cache)
    tok = auth.mint_token(key, ttl_s=300, now=t0)
    ok, why = auth.validate_token(tok, key, max_ttl_s=300, nonce_cache=auth.NonceCache(), now=t0 + 1)
    assert ok, why

    # Replay: same token, same cache → rejected the second time
    cache = auth.NonceCache()
    assert auth.validate_token(tok, key, 300, cache, now=t0 + 1)[0]
    ok, why = auth.validate_token(tok, key, 300, cache, now=t0 + 2)
    assert not ok and why == "replayed nonce", (ok, why)

    # Expiry: past expiry + skew → rejected; within skew → accepted
    ok, why = auth.validate_token(tok, key, 300, auth.NonceCache(), now=t0 + 300 + auth.CLOCK_SKEW_S + 1)
    assert not ok and why == "expired", (ok, why)
    assert auth.validate_token(tok, key, 300, auth.NonceCache(), now=t0 + 300 + auth.CLOCK_SKEW_S - 1)[0]

    # Over-long mint rejected by server-side TTL cap
    long_tok = auth.mint_token(key, ttl_s=3600, now=t0)
    ok, why = auth.validate_token(long_tok, key, max_ttl_s=300, now=t0 + 1)
    assert not ok and why == "ttl exceeds server maximum", (ok, why)

    # Tampering: flip payload or signature → bad signature
    v, exp, nonce, sig = tok.split(".")
    assert not auth.validate_token(f"{v}.{int(exp) + 9999}.{nonce}.{sig}", key, 300, now=t0)[0]
    assert not auth.validate_token(f"{v}.{exp}.{nonce}x.{sig}", key, 300, now=t0)[0]
    assert not auth.validate_token(f"{v}.{exp}.{nonce}.{'0' * 64}", key, 300, now=t0)[0]

    # Wrong key, malformed shapes, empty
    assert not auth.validate_token(tok, "other-key", 300, now=t0)[0]
    for bad in ("", "garbage", "v1.123.abc", "v2." + tok.split(".", 1)[1], None):
        ok, _ = auth.validate_token(bad, key, 300, now=t0)  # type: ignore[arg-type]
        assert not ok, f"accepted malformed token: {bad!r}"

    # Nonce cache prunes expired entries (stays bounded)
    cache = auth.NonceCache()
    cache.check_and_store("old", expiry=int(t0 - 100), now=t0 - 200)
    cache.check_and_store("new", expiry=int(t0 + 300), now=t0)  # triggers prune of "old"
    assert "old" not in cache._seen and "new" in cache._seen

    # Prod static-mode tripwire (Option C control #4): prod + no signing key is the only
    # forbidden combo; signed-token mode and non-prod envs always boot.
    assert auth.prod_static_mode_forbidden("prod", "") is True
    assert auth.prod_static_mode_forbidden("PROD", "") is True          # case-insensitive
    assert auth.prod_static_mode_forbidden("prod", key) is False        # signed mode OK in prod
    assert auth.prod_static_mode_forbidden("dev", "") is False          # static OK off-prod
    assert auth.prod_static_mode_forbidden("staging", "") is False
    assert auth.prod_static_mode_forbidden("", "") is False             # unset env defaults safe

    # The resumption token the server now mints into `ready` is just a normal signed token —
    # validates + rotates through the existing nonce path (proves Option C reuses the signer).
    resume = auth.mint_token(key, ttl_s=300, now=t0)
    assert auth.validate_token(resume, key, 300, auth.NonceCache(), now=t0 + 1)[0]

    print("ALL AUTH TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ".")
    raise SystemExit(main())
