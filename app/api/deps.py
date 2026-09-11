"""Shared FastAPI dependencies."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette

from app.db import get_session
from app.services.chat import ChatService


def build_service(app: Starlette, session: AsyncSession) -> ChatService:
    state = app.state
    return ChatService(session, state.settings, state.storage, on_commit=state.relay.notify)


async def get_service(
    request: Request, session: AsyncSession = Depends(get_session)
) -> ChatService:
    return build_service(request.app, session)
