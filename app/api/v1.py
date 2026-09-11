"""REST API, version 1."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import get_service
from app.auth import get_current_user
from app.config import Settings
from app.errors import AppError
from app.ids import uuid7
from app.schemas import (
    ConversationList,
    ErrorOut,
    MessageOut,
    MessagePage,
    PresignRequest,
    PresignResponse,
    ReadRequest,
    ReadResult,
    SendMessageRequest,
    TokenOut,
)
from app.services.chat import ChatService
from app.storage import AttachmentStorage
from app.tokens import mint_token

_ERRORS: dict[int | str, dict[str, object]] = {
    code: {"model": ErrorOut} for code in (400, 401, 404, 409, 413, 415, 422, 503)
}

router = APIRouter(prefix="/v1", responses=_ERRORS)

CurrentUser = Annotated[str, Depends(get_current_user)]
Service = Annotated[ChatService, Depends(get_service)]

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


@router.post(
    "/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    tags=["messages"],
    summary="Send a message (idempotent on client_msg_id)",
    responses={200: {"model": MessageOut, "description": "Replay of an earlier send"}},
)
async def send_message(
    payload: SendMessageRequest, response: Response, user: CurrentUser, service: Service
) -> MessageOut:
    message, created = await service.send_message(user, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return message


@router.post(
    "/messages/{message_id}/delivered",
    response_model=MessageOut,
    tags=["receipts"],
    summary="Confirm delivery of a message (recipient only)",
)
async def mark_delivered(message_id: uuid.UUID, user: CurrentUser, service: Service) -> MessageOut:
    return await service.mark_delivered(user, message_id)


@router.get(
    "/conversations",
    response_model=ConversationList,
    tags=["conversations"],
    summary="List conversations with last message and unread count",
)
async def list_conversations(
    user: CurrentUser, service: Service, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> ConversationList:
    return ConversationList(items=await service.list_conversations(user, limit=limit))


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePage,
    tags=["conversations"],
    summary="Message history (before=) or catch-up sync (after=)",
)
async def list_messages(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(max_length=200)] = None,
    after: Annotated[str | None, Query(max_length=200)] = None,
) -> MessagePage:
    return await service.list_messages(
        user, conversation_id, limit=limit, before=before, after=after
    )


@router.post(
    "/conversations/{conversation_id}/read",
    response_model=ReadResult,
    tags=["receipts"],
    summary="Mark incoming messages as read, up to and including a message",
)
async def mark_read(
    conversation_id: uuid.UUID, payload: ReadRequest, user: CurrentUser, service: Service
) -> ReadResult:
    return await service.mark_read(user, conversation_id, payload.up_to_message_id)


@router.post(
    "/attachments/presign",
    response_model=PresignResponse,
    tags=["attachments"],
    summary="Get a presigned POST to upload an attachment directly to storage",
)
async def presign_attachment(
    payload: PresignRequest, request: Request, user: CurrentUser
) -> PresignResponse:
    settings: Settings = request.app.state.settings
    storage: AttachmentStorage = request.app.state.storage
    content_type = payload.content_type.split(";", 1)[0].strip().lower()
    if content_type not in settings.attachment_allowed_mime:
        allowed = ", ".join(settings.attachment_allowed_mime)
        raise AppError(415, "unsupported_media_type", f"Allowed types: {allowed}")
    if payload.size > settings.attachment_max_bytes:
        raise AppError(
            413, "payload_too_large", f"Limit is {settings.attachment_max_bytes} bytes"
        )
    key = f"u/{user}/{uuid7()}{_EXTENSIONS.get(content_type, '')}"
    post = storage.presign_upload(key, content_type)
    return PresignResponse(
        url=post["url"],
        fields=post["fields"],
        key=key,
        expires_in=settings.presign_expiry_seconds,
        max_bytes=settings.attachment_max_bytes,
    )


demo_router = APIRouter(prefix="/v1/demo", tags=["demo"])


@demo_router.get(
    "/token", response_model=TokenOut, summary="Demo only: token for alice or bob"
)
async def demo_token(request: Request, user: Literal["alice", "bob"]) -> TokenOut:
    settings: Settings = request.app.state.settings
    ttl = 12 * 3600
    token = mint_token(
        user,
        settings.jwt_secret,
        ttl_seconds=ttl,
        user_claim=settings.jwt_user_claim,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    return TokenOut(access_token=token, user_id=user, expires_in=ttl)
