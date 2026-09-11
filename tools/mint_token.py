"""Mint demo HS256 tokens.

    uv run python tools/mint_token.py alice
    uv run python tools/mint_token.py bob --ttl 86400

Reads JWT_SECRET (and optional JWT_ISSUER / JWT_AUDIENCE / JWT_USER_CLAIM) from the
environment or `.env`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.tokens import mint_token


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("users", nargs="*", default=["alice", "bob"], help="user ids")
    parser.add_argument("--ttl", type=int, default=12 * 3600, help="lifetime in seconds")
    args = parser.parse_args()

    settings = Settings(auth_mode="hs256")
    for user in args.users:
        token = mint_token(
            user,
            settings.jwt_secret,
            ttl_seconds=args.ttl,
            user_claim=settings.jwt_user_claim,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )
        print(f"{user}\t{token}")


if __name__ == "__main__":
    main()
