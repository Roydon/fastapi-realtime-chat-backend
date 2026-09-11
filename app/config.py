"""Application settings, loaded from environment variables (and `.env` when present)."""

from __future__ import annotations

import socket
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_ALLOWED_MIME = [
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
]

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- service ---
    replica_id: str = Field(default_factory=socket.gethostname)
    log_level: str = "INFO"
    log_json: bool = True

    # --- storage backends ---
    database_url: str = "postgresql+asyncpg://chat:chat@localhost:5432/chat"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    redis_url: str = "redis://localhost:6379/0"

    # --- auth ---
    auth_mode: Literal["hs256", "jwks"] = "hs256"
    jwt_secret: str = ""
    jwks_url: str | None = None
    jwt_algorithms: CsvList = ["RS256"]
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_user_claim: str = "sub"
    jwt_leeway_seconds: int = 30

    # --- attachments ---
    s3_endpoint_url: str | None = None
    s3_public_endpoint_url: str | None = None
    s3_bucket: str = "chat-attachments"
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    attachment_max_bytes: int = 5 * 1024 * 1024
    attachment_allowed_mime: CsvList = DEFAULT_ALLOWED_MIME
    presign_expiry_seconds: int = 600
    download_url_expiry_seconds: int = 3600

    # --- messaging / realtime ---
    message_max_graphemes: int = 4000
    outbox_poll_interval_seconds: float = 0.5
    outbox_batch_size: int = 200
    ws_heartbeat_seconds: float = 20.0
    ws_idle_timeout_seconds: float = 60.0

    # --- demo ---
    demo_enabled: bool = False

    @field_validator("jwt_algorithms", "attachment_allowed_mime", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _check_auth(self) -> Settings:
        if self.auth_mode == "hs256" and len(self.jwt_secret) < 32:
            raise ValueError("AUTH_MODE=hs256 requires JWT_SECRET of at least 32 characters")
        if self.auth_mode == "jwks" and not self.jwks_url:
            raise ValueError("AUTH_MODE=jwks requires JWKS_URL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
