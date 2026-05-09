"""
web_streaming/livekit/router.py  — full replacement
Adds Phase 4 SSE /events endpoint and optional-auth dependency for EventSource compatibility.
"""

import asyncio
from datetime import datetime
from typing import List
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from database import get_supabase
from dependencies import get_current_user, require_teacher
from exceptions import UnauthorizedError
from schemas import (
    ParticipantOut,
    StudentMicStatusOut,
    TokenRefreshResponse,
    UserOut,
    LiveKitTokenResponse,
)
from web_streaming.livekit import service
from web_streaming.livekit.sse import stream_classroom_events

router = APIRouter(prefix="/classrooms", tags=["classrooms"])


# ---------------------------------------------------------------------------
# Optional-auth dependency — needed for SSE because EventSource cannot send
# Authorization headers. We accept the JWT as a ?token= query param instead.
# ---------------------------------------------------------------------------

async def get_current_user_from_token_param(
    token: str = Query(default=None, description="Bearer JWT (used by SSE clients)"),
) -> UserOut:
    """
    Validates a JWT passed as a query parameter.
    Used exclusively by the SSE /events endpoint where EventSource cannot
    set custom request headers.
    """
    if not token:
        raise UnauthorizedError("Missing token")
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        raise UnauthorizedError("Token has expired")
    except jwt.InvalidTokenError:
        raise UnauthorizedError("Invalid token")

    user_id = payload.get("user_id")
    if not user_id:
        raise UnauthorizedError("Invalid token payload")

    supabase = get_supabase()
    result = supabase.table("users").select("*").eq("id", user_id).single().execute()
    if not result.data:
        raise UnauthorizedError("User not found")

    return UserOut(**result.data)


# ---------------------------------------------------------------------------
# Classroom lifecycle
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_classroom(
    title: str,
    batch_id: UUID,
    description: str = None,
    scheduled_at: datetime = None,
    duration_minutes: int = 60,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher creates a new classroom."""
    return await service.create_classroom(
        teacher, title, description, batch_id, scheduled_at, duration_minutes
    )


@router.post("/{classroom_id}/start")
async def start_class(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher starts a scheduled class, making it live."""
    return await service.start_class(classroom_id, teacher)


@router.post("/{classroom_id}/end")
async def end_class(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher ends a live class."""
    return await service.end_class(classroom_id, teacher)


@router.post("/join/{join_token}", response_model=LiveKitTokenResponse)
async def join_classroom(
    join_token: str,
    user: UserOut = Depends(get_current_user),
):
    """User joins a classroom via a join token."""
    return await service.join_classroom(join_token, user)


@router.post("/{classroom_id}/leave")
async def leave_classroom(
    classroom_id: UUID,
    user: UserOut = Depends(get_current_user),
):
    """User leaves a classroom."""
    await service.leave_classroom(classroom_id, user)
    return {"message": "Left classroom successfully"}


# ---------------------------------------------------------------------------
# Mic control
# ---------------------------------------------------------------------------

@router.post("/{classroom_id}/mic/open")
async def open_mics(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher opens mic for ALL students in the classroom."""
    await service.open_mics(classroom_id, teacher)
    return {"message": "Microphones opened for all students"}


@router.post("/{classroom_id}/mic/close")
async def close_mics(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher closes mic for ALL students."""
    await service.close_mics(classroom_id, teacher)
    return {"message": "Microphones closed for all students"}


@router.post("/{classroom_id}/mic/grant/{student_id}")
async def grant_student_mic(
    classroom_id: UUID,
    student_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher grants mic access to a specific student."""
    await service.grant_student_mic(classroom_id, teacher, student_id)
    return {"message": "Mic granted", "student_id": str(student_id)}


@router.post("/{classroom_id}/mic/revoke/{student_id}")
async def revoke_student_mic(
    classroom_id: UUID,
    student_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Teacher revokes mic access from a specific student."""
    await service.revoke_student_mic(classroom_id, teacher, student_id)
    return {"message": "Mic revoked", "student_id": str(student_id)}


@router.get("/{classroom_id}/mic/status", response_model=List[StudentMicStatusOut])
async def get_mic_status(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """Returns active students annotated with their individual mic grant status."""
    return await service.get_mic_status(classroom_id, teacher)


# ---------------------------------------------------------------------------
# Token & participants
# ---------------------------------------------------------------------------

@router.get("/{classroom_id}/token/refresh", response_model=TokenRefreshResponse)
async def refresh_token(
    classroom_id: UUID,
    user: UserOut = Depends(get_current_user),
):
    """Issues a fresh LiveKit JWT reflecting the caller's current mic permissions."""
    return await service.refresh_token(classroom_id, user)


@router.get("/{classroom_id}/participants", response_model=List[ParticipantOut])
async def get_live_participants(
    classroom_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """Get a list of live participants in a classroom."""
    return await service.get_live_participants(classroom_id)


# ---------------------------------------------------------------------------
# Phase 4 — SSE real-time mic notifications
# ---------------------------------------------------------------------------

@router.get("/{classroom_id}/events")
async def classroom_events(
    classroom_id: UUID,
    user: UserOut = Depends(get_current_user_from_token_param),
):
    """
    SSE stream — pushes mic_granted / mic_revoked / ping events to the student.

    EventSource cannot send Authorization headers, so the JWT is passed as
    ?token=<jwt> query param and validated by get_current_user_from_token_param.

    Event format:
        event: mic_granted
        data: {"student_id": "<uuid>"}

        event: mic_revoked
        data: {"student_id": "<uuid>"}

        event: ping
        data: {}
    """
    return StreamingResponse(
        stream_classroom_events(str(classroom_id), user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
        },
    )