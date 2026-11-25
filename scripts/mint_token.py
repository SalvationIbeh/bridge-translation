"""
mint_token.py — mint a short-lived signed token for /ws/translate (S3 auth).

Reads AUTH_SIGNING_KEY from the environment or .env (never passed as an argument —
keeps the key out of shell history). Prints the token to stdout so it composes:

    $env:AUTH_TOKEN = & python scripts/mint_token.py          # PowerShell
    AUTH_TOKEN=$(python scripts/mint_token.py) python scripts/benchmark_s1_telephony.py

The benchmark/test clients are unchanged — a signed token is just a string in the
init frame's `auth` field. Default TTL 300s (server rejects anything longer than
its TOKEN_TTL_S). Each token is single-use per connection (nonce); mint per session.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

from auth import mint_token  # noqa: E402 — needs the sys.path bootstrap above


def _load_dotenv_key(name: str) -> str:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ttl", type=int, default=300, help="token lifetime in seconds (default 300)")
    args = ap.parse_args()

    key = os.getenv("AUTH_SIGNING_KEY") or _load_dotenv_key("AUTH_SIGNING_KEY")
    if not key:
        print("ERROR: AUTH_SIGNING_KEY not set (env or .env).", file=sys.stderr)
        return 1
    print(mint_token(key, ttl_s=args.ttl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
