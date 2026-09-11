"""Request/response models for the public API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.models import MessageStatus

UserId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.@:-]{1,64}$")]


class ErrorOut(BaseModel):
    code: str = Field(examples=["validation_error"])
    message: str
    request_id: str | None = None


class SendMessageRequest(BaseModel):
    recipient_id: UserId
    client_msg_id: Annotated[str, StringConstraints(min_length=1, max_length=64)] = Field(
        description="Client-generated id. Retrying with the same value never duplicates."
    )
    body: str | None = Field(default=None, description="UTF-8 text, up to 4000 characters")
    attachment_key: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = Field(
        default=None, description="Key returned by POST /v1/attachments/presign"
    )

    @model_validator(mode="after")
    def _has_content(self) -> SendMessageRequest:
        if self.body is not None:
            if "\x00" in self.body:
                raise ValueError("body must not contain NUL characters")
            if not self.body.strip():
                self.body = None
        if self.body is None and self.attachment_key is None:
            raise ValueError("either body or attachment_key is required")
        return self


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    sender_id: str
    recipient_id: str
    client_msg_id: str
    body: str | None
    attachment_key: str | None
    attachment_mime: str | None
    attachment_url: str | None = Field(description="Short-lived download URL")
    status: MessageStatus
    created_at: datetime
    delivered_at: datetime | None
    read_at: datetime | None
    cursor: str = Field(description="Opaque pagination cursor for this message")


class ConversationOut(BaseModel):
    id: uuid.UUID
    peer_id: str
    created_at: datetime
    last_message: MessageOut | None
    unread_count: int


class ConversationList(BaseModel):
    items: list[ConversationOut]


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None = Field(
        description="With `after`: pass back as `after` to continue syncing. "
        "Otherwise: pass back as `before` to load older messages."
    )
    has_more: bool


class ReadRequest(BaseModel):
    up_to_message_id: uuid.UUID


class ReadResult(BaseModel):
    conversation_id: uuid.UUID
    message_ids: list[uuid.UUID]
    read_at: datetime | None


class PresignRequest(BaseModel):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content_type: Annotated[str, StringConstraints(min_length=3, max_length=100)]
    size: int = Field(gt=0, description="File size in bytes")


class PresignResponse(BaseModel):
    url: str
    fields: dict[str, str] = Field(description="Form fields to send with the multipart POST")
    key: str = Field(description="Pass as attachment_key when sending the message")
    expires_in: int
    max_bytes: int


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token_type label, not a credential
    user_id: str
    expires_in: int
