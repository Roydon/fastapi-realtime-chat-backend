"""Pluggable bearer-token authentication.

Two modes, selected with AUTH_MODE:
  * hs256 - tokens signed with a shared secret (used by the demo and tests).
  * jwks  - tokens signed by an existing identity provider; keys come from JWKS_URL.
The user id is read from a configurable claim (JWT_USER_CLAIM, default `sub`).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import jwt
from fastapi import Depends, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.errors import AppError

USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.@:-]{1,64}$")
WS_SUBPROTOCOL = "bearer"

_bearer = HTTPBearer(auto_error=False, description="JWT issued by your identity provider")


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwk_client: jwt.PyJWKClient | None = None
        if settings.auth_mode == "jwks" and settings.jwks_url:
            self._jwk_client = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=300)

    async def _key_and_algorithms(self, token: str) -> tuple[Any, list[str]]:
        if self._jwk_client is None:
            return self._settings.jwt_secret, ["HS256"]
        # PyJWKClient does blocking HTTP on a cache miss; keep it off the event loop.
        signing_key = await asyncio.to_thread(self._jwk_client.get_signing_key_from_jwt, token)
        return signing_key.key, self._settings.jwt_algorithms

    async def verify(self, token: str) -> str:
        s = self._settings
        try:
            key, algorithms = await self._key_and_algorithms(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=s.jwt_audience,
                issuer=s.jwt_issuer,
                leeway=s.jwt_leeway_seconds,
                options={"require": ["exp"], "verify_aud": s.jwt_audience is not None},
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            raise AppError(401, "invalid_token", f"Token rejected: {exc}") from exc
        user_id = claims.get(s.jwt_user_claim)
        if not isinstance(user_id, str) or not USER_ID_PATTERN.match(user_id):
            raise AppError(401, "invalid_token", f"Claim '{s.jwt_user_claim}' is not a user id")
        return user_id


async def get_current_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
) -> str:
    if credentials is None:
        raise AppError(401, "unauthorized", "Missing bearer token")
    verifier: TokenVerifier = request.app.state.token_verifier
    return await verifier.verify(credentials.credentials)


def websocket_token(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Return (token, subprotocol_to_echo).

    Browsers cannot set headers on a WebSocket, so the token is accepted either as
    `?token=...` or as the second entry of `Sec-WebSocket-Protocol: bearer, <token>`.
    """
    protocols = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    if len(protocols) >= 2 and protocols[0] == WS_SUBPROTOCOL and protocols[1]:
        return protocols[1], WS_SUBPROTOCOL
    return websocket.query_params.get("token"), None
