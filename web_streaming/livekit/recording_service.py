"""
web_streaming/livekit/recording_service.py
------------------------------------------
Phase 5 — Recording & Egress

Service layer for classroom recording via LiveKit egress → AWS S3.

Flow:
  1. Teacher calls start_recording → LiveKit starts a RoomComposite egress
     that writes an MP4 directly to S3. A `recordings` row is inserted with
     status="recording" and the egress_id stored for later stop.

  2. Teacher calls stop_recording → LiveKit finalises the file on S3.
     The DB row is updated: status="completed", ended_at=now().
     (The S3 key was set at start time so we already know where the file lands.)

  3. Anyone with access calls get_recordings → returns recording rows.
     Callers that need a playback URL hit get_recording_url which generates
     a pre-signed S3 GET URL valid for AWS_S3_PRESIGNED_URL_EXPIRY seconds.

Design decisions:
  - s3_key is deterministic: "recordings/{classroom_id}/{egress_id}.mp4"
    This means we know the key before the file exists, so we can store it
    immediately and generate URLs without waiting for egress to complete.
  - One active recording per classroom enforced at service level.
  - Pre-signed URLs generated on demand (not stored) so they're always fresh.
"""

from datetime import datetime
from typing import List
from uuid import UUID

from database import get_supabase
from exceptions import BadRequestError, ForbiddenError, NotFoundError
from s3_client import generate_presigned_url
from schemas import UserOut, UserRole
from web_streaming.livekit import livekit_client
from web_streaming.livekit.schemas import RecordingOut


# ── Helpers ────────────────────────────────────────────────────────────────────

def _s3_key(classroom_id: str, egress_id: str) -> str:
    """
    Builds the S3 object key for a recording.
    Pattern: recordings/{classroom_id}/{egress_id}.mp4

    Args:
        classroom_id: UUID string of the classroom.
        egress_id:    LiveKit egress ID string.

    Returns:
        S3 key string, e.g. "recordings/abc-123/eg_xyz.mp4"
    """
    return f"recordings/{classroom_id}/{egress_id}.mp4"


async def _assert_teacher_owns_classroom(classroom_id: str, teacher: UserOut) -> dict:
    """
    Fetches the classroom row and raises ForbiddenError if the teacher doesn't own it.

    Args:
        classroom_id: UUID string.
        teacher:      Authenticated teacher UserOut.

    Returns:
        classroom dict on success.

    Raises:
        NotFoundError: classroom row missing.
        ForbiddenError: teacher_id mismatch.
    """
    supabase = get_supabase()
    resp = supabase.table("classrooms").select("*").eq("id", classroom_id).single().execute()
    if not resp.data:
        raise NotFoundError("Classroom not found")
    if resp.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You do not own this classroom")
    return resp.data


# ── Public service functions ───────────────────────────────────────────────────

async def start_recording(classroom_id: UUID, teacher: UserOut) -> RecordingOut:
    """
    Starts a RoomComposite egress for the classroom and persists a recording row.

    Preconditions:
      - Teacher must own the classroom.
      - Classroom must be LIVE (status="live").
      - No other recording may already be active (status="recording") for this classroom.

    Steps:
      1. Fetch + validate classroom ownership and status.
      2. Check for duplicate active recording.
      3. Call livekit_client.start_egress → get egress_id.
      4. Compute deterministic s3_key from classroom_id + egress_id.
      5. Insert `recordings` row (status="recording").
      6. Return RecordingOut.

    Args:
        classroom_id: UUID of the classroom to record.
        teacher:      Authenticated teacher performing the action.

    Returns:
        RecordingOut with status="recording", no url yet (file not complete).

    Raises:
        NotFoundError: classroom not found.
        ForbiddenError: teacher doesn't own classroom.
        BadRequestError: classroom not live, or recording already active.
    """
    supabase = get_supabase()
    classroom = await _assert_teacher_owns_classroom(str(classroom_id), teacher)

    if classroom["status"] != "live":
        raise BadRequestError("Cannot record a classroom that is not live")

    # Prevent duplicate active recordings
    active = (
        supabase.table("recordings")
        .select("id")
        .eq("classroom_id", str(classroom_id))
        .eq("status", "recording")
        .execute()
    )
    if active.data:
        raise BadRequestError("A recording is already in progress for this classroom")

    from web_streaming.livekit import livekit_client as _lk

    participants = await _lk.list_participants(classroom["room_name"])
    publishing = [
        p for p in participants
        if any(t for t in p.tracks)  # has at least one published track
    ]
    if not publishing:
        raise BadRequestError(
            "No participants are publishing. "
            "Teacher must enable camera or microphone before recording."
        )

    import uuid as _uuid
    # Start LiveKit egress — we get egress_id back
    pre_key = f"recordings/{classroom_id}/{_uuid.uuid4().hex}.mp4"

    egress_info = await livekit_client.start_egress(
        room_name=classroom["room_name"],
        s3_key=pre_key,   # real key sent to LiveKit from the start
    )
    egress_id = egress_info.egress_id

    # Now we know egress_id — build final s3_key
    key = pre_key

    row = {
        "classroom_id": str(classroom_id),
        "egress_id": egress_id,
        "s3_key": key,
        "status": "recording",
    }
    resp = supabase.table("recordings").insert(row).execute()
    if not resp.data:
        raise Exception("Failed to insert recording row")

    return RecordingOut(**resp.data[0])


async def stop_recording(classroom_id: UUID, teacher: UserOut) -> RecordingOut:
    """
    Stops the active egress for a classroom and marks the recording as completed.

    Preconditions:
      - Teacher must own the classroom.
      - An active recording (status="recording") must exist for this classroom.

    Steps:
      1. Validate ownership.
      2. Find the active recording row by (classroom_id, status="recording").
      3. Call livekit_client.stop_egress(egress_id).
      4. Update row: status="completed", ended_at=now().
      5. Return RecordingOut (url is still None; caller fetches via get_recording_url).

    Args:
        classroom_id: UUID of the classroom.
        teacher:      Authenticated teacher performing the action.

    Returns:
        RecordingOut with status="completed".

    Raises:
        NotFoundError: no active recording found.
        ForbiddenError: teacher doesn't own classroom.
    """
    supabase = get_supabase()
    await _assert_teacher_owns_classroom(str(classroom_id), teacher)

    active = (
        supabase.table("recordings")
        .select("*")
        .eq("classroom_id", str(classroom_id))
        .eq("status", "recording")
        .execute()
    )
    if not active.data:
        raise NotFoundError("No active recording found for this classroom")

    recording = active.data[0]

    # Stop LiveKit egress (LiveKit finalises + uploads to S3 async)
    try:
        await livekit_client.stop_egress(recording["egress_id"])
    except Exception as e:
        # If egress already stopped (e.g. class ended), just mark completed
        if "not found" not in str(e).lower() and "already" not in str(e).lower():
            raise

    update = {
        "status": "completed",
        "ended_at": datetime.utcnow().isoformat(),
    }
    resp = (
        supabase.table("recordings")
        .update(update)
        .eq("id", recording["id"])
        .execute()
    )
    if not resp.data:
        raise Exception("Failed to update recording row")

    return RecordingOut(**resp.data[0])


async def get_recordings(classroom_id: UUID, requester: UserOut) -> List[RecordingOut]:
    """
    Returns all recording rows for a classroom.

    Access rules:
      - Teacher must own the classroom.
      - Students must be enrolled in the classroom's batch.

    Args:
        classroom_id: UUID of the classroom.
        requester:    Authenticated user (teacher or student).

    Returns:
        List of RecordingOut objects (url field is None; use get_recording_url for playback).

    Raises:
        NotFoundError: classroom not found.
        ForbiddenError: access denied.
    """
    supabase = get_supabase()

    classroom = (
        supabase.table("classrooms")
        .select("teacher_id, batch_id")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not classroom.data:
        raise NotFoundError("Classroom not found")

    if requester.role == UserRole.TEACHER:
        if classroom.data["teacher_id"] != str(requester.id):
            raise ForbiddenError("You do not own this classroom")
    elif requester.role == UserRole.STUDENT:
        enrollment = (
            supabase.table("batch_enrollments")
            .select("id")
            .eq("user_id", str(requester.id))
            .eq("batch_id", classroom.data["batch_id"])
            .execute()
        )
        if not enrollment.data:
            raise ForbiddenError("You are not enrolled in this classroom's batch")
    else:
        raise ForbiddenError("Access denied")

    resp = (
        supabase.table("recordings")
        .select("*")
        .eq("classroom_id", str(classroom_id))
        .order("started_at", desc=True)
        .execute()
    )
    return [RecordingOut(**r) for r in (resp.data or [])]


async def get_recording_url(recording_id: UUID, requester: UserOut) -> str:
    supabase = get_supabase()

    # Step 1: fetch the recording row on its own (no join)
    rec_resp = (
        supabase.table("recordings")
        .select("*")
        .eq("id", str(recording_id))
        .single()
        .execute()
    )
    if not rec_resp.data:
        raise NotFoundError("Recording not found")

    recording = rec_resp.data

    # Step 2: fetch the classroom separately
    classroom_resp = (
        supabase.table("classrooms")
        .select("teacher_id, batch_id")
        .eq("id", recording["classroom_id"])
        .single()
        .execute()
    )
    if not classroom_resp.data:
        raise NotFoundError("Classroom not found")

    classroom = classroom_resp.data

    # Step 3: access check
    if requester.role == UserRole.TEACHER:
        if classroom["teacher_id"] != str(requester.id):
            raise ForbiddenError("You do not own this recording")
    elif requester.role == UserRole.STUDENT:
        enrollment = (
            supabase.table("batch_enrollments")
            .select("id")
            .eq("user_id", str(requester.id))
            .eq("batch_id", classroom["batch_id"])
            .execute()
        )
        if not enrollment.data:
            raise ForbiddenError("You are not enrolled in this classroom's batch")
    else:
        raise ForbiddenError("Access denied")

    if recording["status"] != "completed" or not recording.get("s3_key"):
        raise BadRequestError("Recording is not yet available (still processing or failed)")

    return generate_presigned_url(recording["s3_key"])