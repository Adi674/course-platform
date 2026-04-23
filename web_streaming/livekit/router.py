from fastapi import APIRouter, Depends, status
from typing import List
from uuid import UUID
from datetime import datetime

from schemas import UserOut, LiveKitTokenResponse, ParticipantOut
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


@router.get("/{classroom_id}/participants", response_model=List[ParticipantOut])
async def get_live_participants(
    classroom_id: UUID,
    current_user: UserOut = Depends(get_current_user),
):
    """Get a list of live participants in a classroom."""
    return await service.get_live_participants(classroom_id)