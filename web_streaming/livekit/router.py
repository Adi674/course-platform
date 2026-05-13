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
from web_streaming.livekit import classroom_list_service
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
from web_streaming.livekit import recording_service
from web_streaming.livekit.schemas import RecordingOut

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

# ---------------------------------------------------------------------------
# Phase 6 — Classroom listing (for BatchDetailPage & ClassDetailPage)
# ---------------------------------------------------------------------------
 
@router.get("/batch/{batch_id}", response_model=List[dict])
async def list_classrooms_for_batch(
    batch_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """
    Lists all classrooms in a batch.
    Teachers must own the batch; students must be enrolled.
    """
    return await classroom_list_service.get_classrooms_for_batch(batch_id, current_user)
 
@router.get("/{classroom_id}/mic/my-state")
async def get_my_mic_state(
    classroom_id: UUID,
    user: UserOut = Depends(get_current_user),
):
    """Student polls their own mic permission state from Redis."""
    return await service.get_student_mic_state(classroom_id, user)
 
@router.get("/{classroom_id}/detail")
async def get_classroom_detail(
    classroom_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """
    Returns classroom fields + recordings list.
    Teachers must own; students must be enrolled in the batch.
    """
    return await classroom_list_service.get_classroom_detail(classroom_id, current_user)
 
 

# ---------------------------------------------------------------------------
# Recording — Phase 5
# ---------------------------------------------------------------------------
@router.get("/recordings/{recording_id}/url")
async def get_recording_url(
    recording_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """
    Generates and returns a fresh pre-signed S3 URL for recording playback.
    Valid for AWS_S3_PRESIGNED_URL_EXPIRY seconds (default 1 hour).
    Access-gated: teacher owns classroom, or student is enrolled in the batch.
    """
    url = await recording_service.get_recording_url(recording_id, current_user)
    return {"url": url, "expires_in_seconds": settings.AWS_S3_PRESIGNED_URL_EXPIRY}


@router.post(
    "/{classroom_id}/recording/start",
    response_model=RecordingOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_recording(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Teacher starts recording the live classroom.
    LiveKit egress writes an MP4 directly to S3.
    Returns the recording row (status="recording", no url yet).
    """
    return await recording_service.start_recording(classroom_id, teacher)
 
 
@router.post("/{classroom_id}/recording/stop", response_model=RecordingOut)
async def stop_recording(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Teacher stops the active recording.
    LiveKit finalises the S3 file asynchronously after this call.
    Returns the updated recording row (status="completed").
    """
    return await recording_service.stop_recording(classroom_id, teacher)
 
 
@router.get("/{classroom_id}/recordings", response_model=List[RecordingOut])
async def get_recordings(
    classroom_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """
    Lists all recordings for a classroom.
    Teachers must own it; students must be enrolled in the batch.
    url field is None — use GET /recordings/{id}/url for a playback link.
    """
    return await recording_service.get_recordings(classroom_id, current_user)
 
 
