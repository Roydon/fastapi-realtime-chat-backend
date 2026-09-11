"""Identifier helpers: UUIDv7 (time-ordered) and opaque pagination cursors."""

from __future__ import annotations

import base64
import os
import time
import uuid
from datetime import datetime


def uuid7() -> uuid.UUID:
    """Return an RFC 9562 UUIDv7: 48-bit unix ms timestamp, version, variant, random bits."""
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & 0x3FFF_FFFF_FFFF_FFFF
    value = (ts_ms & 0xFFFF_FFFF_FFFF) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def encode_cursor(created_at: datetime, message_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{message_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Raise ValueError when the cursor is malformed."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        created_raw, id_raw = base64.urlsafe_b64decode(padded).decode().split("|", 1)
        created_at = datetime.fromisoformat(created_raw)
        return created_at, uuid.UUID(id_raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid cursor") from exc
