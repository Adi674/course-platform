"""
web_streaming/livekit/classroom_list_service.py
------------------------------------------------
Phase 6 — Classroom listing for Dashboard

Provides list/detail queries for classrooms that power the
BatchesPage → BatchDetailPage → ClassDetailPage flow.

Functions:
  get_classrooms_for_batch  — teacher or enrolled student lists classrooms in a batch
  get_classroom_detail      — single classroom with recording list; role-gated
"""

from typing import List
from uuid import UUID

from database import get_supabase
from exceptions import ForbiddenError, NotFoundError
from schemas import UserOut, UserRole


async def get_classrooms_for_batch(batch_id: UUID, requester: UserOut) -> List[dict]:
    """
    Returns all classrooms for a given batch, ordered by scheduled_at desc.

    Access rules:
      - Teacher must own the batch.
      - Students must be enrolled in the batch.

    Each item is the raw classroom row: id, title, description, status,
    scheduled_at, started_at, ended_at, duration_minutes, join_token,
    room_name, teacher_id, batch_id, created_at.

    Args:
        batch_id:   UUID of the batch.
        requester:  Authenticated user.

    Returns:
        List of classroom dicts (newest first by scheduled_at / created_at).

    Raises:
        NotFoundError:  batch not found.
        ForbiddenError: requester has no access.
    """
    supabase = get_supabase()

    # Verify batch exists
    batch_resp = (
        supabase.table("batches")
        .select("teacher_id")
        .eq("id", str(batch_id))
        .single()
        .execute()
    )
    if not batch_resp.data:
        raise NotFoundError("Batch not found")

    if requester.role == UserRole.TEACHER:
        if batch_resp.data["teacher_id"] != str(requester.id):
            raise ForbiddenError("You do not own this batch")
    elif requester.role == UserRole.STUDENT:
        enroll = (
            supabase.table("batch_enrollments")
            .select("id")
            .eq("batch_id", str(batch_id))
            .eq("user_id", str(requester.id))
            .execute()
        )
        if not enroll.data:
            raise ForbiddenError("You are not enrolled in this batch")
    else:
        raise ForbiddenError("Access denied")

    classrooms_resp = (
        supabase.table("classrooms")
        .select("*")
        .eq("batch_id", str(batch_id))
        .order("created_at", desc=True)
        .execute()
    )
    return classrooms_resp.data or []


async def get_classroom_detail(classroom_id: UUID, requester: UserOut) -> dict:
    """
    Returns a single classroom row plus its list of recordings.

    Access rules (same as above — teacher owns, student enrolled).

    Returns a dict:
      { ...classroom_fields, "recordings": [ ...RecordingOut dicts ] }

    Args:
        classroom_id:  UUID of the classroom.
        requester:     Authenticated user.

    Returns:
        Classroom dict with `recordings` list appended.

    Raises:
        NotFoundError:  classroom not found.
        ForbiddenError: requester has no access.
    """
    supabase = get_supabase()

    classroom_resp = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not classroom_resp.data:
        raise NotFoundError("Classroom not found")

    classroom = classroom_resp.data

    if requester.role == UserRole.TEACHER:
        if classroom["teacher_id"] != str(requester.id):
            raise ForbiddenError("You do not own this classroom")
    elif requester.role == UserRole.STUDENT:
        enroll = (
            supabase.table("batch_enrollments")
            .select("id")
            .eq("batch_id", classroom["batch_id"])
            .eq("user_id", str(requester.id))
            .execute()
        )
        if not enroll.data:
            raise ForbiddenError("You are not enrolled in this classroom's batch")
    else:
        raise ForbiddenError("Access denied")

    recordings_resp = (
        supabase.table("recordings")
        .select("*")
        .eq("classroom_id", str(classroom_id))
        .order("started_at", desc=True)
        .execute()
    )
    classroom["recordings"] = recordings_resp.data or []
    return classroom