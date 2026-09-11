"""HS256 token minting for the demo and local tools. Production tokens come from your IdP."""

from __future__ import annotations

import time
from typing import Any

import jwt


def mint_token(
    user_id: str,
    secret: str,
    *,
    ttl_seconds: int = 3600,
    user_claim: str = "sub",
    issuer: str | None = None,
    audience: str | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {user_claim: user_id, "iat": now, "exp": now + ttl_seconds}
    if issuer:
        claims["iss"] = issuer
    if audience:
        claims["aud"] = audience
    return jwt.encode(claims, secret, algorithm="HS256")
