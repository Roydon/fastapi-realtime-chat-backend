"""Authentication: HS256 verification, configurable claim, and JWKS mode."""

from __future__ import annotations

import time

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import TokenVerifier
from app.config import Settings
from app.errors import AppError
from app.tokens import mint_token

pytestmark = pytest.mark.asyncio


def client_for(app: object) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    )


async def test_missing_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/v1/conversations")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == "unauthorized"
    assert "request_id" in body


async def test_expired_token_is_rejected(client: AsyncClient, token_factory) -> None:
    token = token_factory("alice", ttl_seconds=-120)  # past the 30s verification leeway
    resp = await client.get("/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"


async def test_token_signed_with_wrong_secret(client: AsyncClient) -> None:
    forged = jwt.encode({"sub": "alice", "exp": int(time.time()) + 60}, "wrong-secret", "HS256")
    resp = await client.get("/v1/conversations", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_token_without_exp_is_rejected(settings: Settings) -> None:
    verifier = TokenVerifier(settings)
    token = jwt.encode({"sub": "alice"}, settings.jwt_secret, "HS256")
    with pytest.raises(AppError):
        await verifier.verify(token)


async def test_bad_user_id_claim_is_rejected(settings: Settings) -> None:
    verifier = TokenVerifier(settings)
    token = jwt.encode(
        {"sub": "has spaces and $$$", "exp": int(time.time()) + 60}, settings.jwt_secret, "HS256"
    )
    with pytest.raises(AppError, match="user id"):
        await verifier.verify(token)


async def test_custom_user_claim(make_app, settings: Settings) -> None:
    app = await make_app(jwt_user_claim="uid")
    token = mint_token("alice", settings.jwt_secret, user_claim="uid")
    async with client_for(app) as c:
        resp = await c.get("/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


async def test_jwks_mode_verifies_rs256(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        auth_mode="jwks",
        jwks_url="https://idp.example.test/jwks.json",
        jwt_algorithms="RS256",
        jwt_audience="chat-api",
        jwt_issuer="https://idp.example.test/",
    )

    class FakeKey:
        key = private_key.public_key()

    monkeypatch.setattr("jwt.PyJWKClient.get_signing_key_from_jwt", lambda self, token: FakeKey())
    verifier = TokenVerifier(settings)

    good = jwt.encode(
        {
            "sub": "alice",
            "aud": "chat-api",
            "iss": "https://idp.example.test/",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
    )
    assert await verifier.verify(good) == "alice"

    wrong_aud = jwt.encode(
        {
            "sub": "alice",
            "aud": "other",
            "iss": "https://idp.example.test/",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
    )
    with pytest.raises(AppError):
        await verifier.verify(wrong_aud)
