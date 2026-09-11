"""Pure-function unit tests: id generation, cursors, grapheme counting, settings."""

from __future__ import annotations

import time
import uuid

import pytest

from app.config import Settings
from app.ids import decode_cursor, encode_cursor, uuid7
from app.models import Conversation
from app.text import grapheme_length


def test_uuid7_is_version_7_and_time_ordered() -> None:
    a = uuid7()
    time.sleep(0.002)
    b = uuid7()
    assert a.version == 7 and b.version == 7
    assert a < b
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_cursor_round_trips() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    mid = uuid.uuid4()
    created, decoded_id = decode_cursor(encode_cursor(now, mid))
    assert created == now and decoded_id == mid


@pytest.mark.parametrize("bad", ["", "!!!!", "not-base64", "YWJj"])
def test_decode_cursor_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_cursor(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hello", 5),
        ("café", 4),  # e + combining acute = one grapheme
        ("\U0001f44d", 1),  # thumbs up
        ("\U0001f469‍\U0001f469‍\U0001f467", 1),  # family emoji ZWJ sequence
        ("\U0001f1ee\U0001f1f3", 1),  # flag
    ],
)
def test_grapheme_length(text: str, expected: int) -> None:
    assert grapheme_length(text) == expected


def test_conversation_peer_of() -> None:
    conv = Conversation(user_a="alice", user_b="bob")
    assert conv.peer_of("alice") == "bob"
    assert conv.peer_of("bob") == "alice"


def test_settings_reject_short_hs256_secret() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        Settings(auth_mode="hs256", jwt_secret="short")


def test_settings_jwks_requires_url() -> None:
    with pytest.raises(ValueError, match="JWKS_URL"):
        Settings(auth_mode="jwks", jwks_url=None)


def test_settings_split_csv() -> None:
    s = Settings(
        auth_mode="hs256",
        jwt_secret="x" * 40,
        jwt_algorithms="RS256, ES256",
        attachment_allowed_mime="image/png,application/pdf",
    )
    assert s.jwt_algorithms == ["RS256", "ES256"]
    assert s.attachment_allowed_mime == ["image/png", "application/pdf"]
