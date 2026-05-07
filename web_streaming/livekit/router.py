from fastapi import APIRouter, Depends, status
from typing import List
from uuid import UUID
from datetime import datetime

from schemas import UserOut, LiveKitTokenResponse, ParticipantOut, TokenRefreshResponse, StudentMicStatusOut
from dependencies import get_current_user, require_teacher
from web_streaming.livekit import service

router = APIRouter(prefix="/classrooms", tags=["classrooms"])


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
    return await service.create_classroom(teacher, title, description, batch_id, scheduled_at, duration_minutes)


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


@router.post("/{classroom_id}/mic/open")
async def open_mics(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Teacher opens mic for ALL students in the classroom.
    Any student who joins (or rejoins) after this will get a token
    with microphone publish permission.
    """
    await service.open_mics(classroom_id, teacher)
    return {"message": "Microphones opened for all students"}


@router.post("/{classroom_id}/mic/close")
async def close_mics(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Teacher closes mic for ALL students.
    Students who rejoin after this will be back to listen-only.
    """
    await service.close_mics(classroom_id, teacher)
    return {"message": "Microphones closed for all students"}

@router.post("/{classroom_id}/mic/grant/{student_id}")
async def grant_student_mic(
    classroom_id: UUID,
    student_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Teacher grants mic access to a specific student.
 
    - Adds student_id to the Redis mic_allowed SET.
    - Attempts a best-effort server-side LiveKit unmute on their current audio tracks.
    - The student must call GET /classrooms/{id}/token/refresh to get a new token
      with can_publish=True before LiveKit will accept new tracks from them.
    """
    await service.grant_student_mic(classroom_id, teacher, student_id)
    return {"message": "Mic granted", "student_id": str(student_id)}
 
 
@router.post("/{classroom_id}/mic/revoke/{student_id}")
async def revoke_student_mic(
    classroom_id: UUID,
    student_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Teacher revokes mic access from a specific student.
 
    - Removes student_id from the Redis mic_allowed SET.
    - Performs an immediate server-side LiveKit mute on their active audio tracks.
      This takes effect instantly — the student cannot speak even with their current token.
    - Their next token refresh will also reflect the revocation.
    """
    await service.revoke_student_mic(classroom_id, teacher, student_id)
    return {"message": "Mic revoked", "student_id": str(student_id)}
 
 
@router.get("/{classroom_id}/mic/status", response_model=List[StudentMicStatusOut])
async def get_mic_status(
    classroom_id: UUID,
    teacher: UserOut = Depends(require_teacher),
):
    """
    Returns a list of all currently active student participants with their
    individual mic grant status. Useful for the teacher dashboard to show
    a real-time mic permission panel.
    """
    return await service.get_mic_status(classroom_id, teacher)
 
 
# ── Phase 3 — Token refresh ────────────────────────────────────────────────────
 
@router.get("/{classroom_id}/token/refresh", response_model=TokenRefreshResponse)
async def refresh_token(
    classroom_id: UUID,
    user: UserOut = Depends(get_current_user),
):
    """
    Issues a fresh LiveKit JWT reflecting the caller's current mic permissions.
 
    When to call this (client-side):
    - After teacher grants the student mic (student needs can_publish=True token).
    - After teacher revokes the student mic (student should re-connect with restricted token).
    - Periodically before the current token's TTL expires (TTL = 1 hour).
 
    Client flow on receiving new token:
    1. Disconnect from LiveKit room.
    2. Swap old token for new token.
    3. Reconnect to LiveKit room using the same room_name.
 
    The response includes can_publish and can_publish_audio so the client
    can update its UI without connecting first.
    """
    return await service.refresh_token(classroom_id, user)
 


@router.get("/{classroom_id}/participants", response_model=List[ParticipantOut])
async def get_live_participants(
    classroom_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """Get a list of live participants in a classroom."""
    return await service.get_live_participants(classroom_id)