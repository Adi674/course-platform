from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from database import get_supabase
from exceptions import BadRequestError, ForbiddenError, NotFoundError
from redis_client import get_redis, room_active_key, room_mic_open_key, room_mic_allowed_key, room_participants_key
from schemas import ClassroomStatus, LiveKitTokenResponse, ParticipantOut, UserOut, UserRole, StudentMicStatusOut,TokenRefreshResponse
from web_streaming.livekit import livekit_client

DEFAULT_CLASS_DURATION_MINUTES = 60  # fallback if teacher doesn't set duration


async def _get_live_classroom(classroom_id: UUID):
    """Fetch classroom row and assert it is currently LIVE."""
    supabase = get_supabase()
    response = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data
    if classroom["status"] != ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is not currently live")
    return classroom
 
 
async def _assert_teacher_owns(classroom: dict, teacher: UserOut):
    if classroom["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")
 
 
async def _cleanup_ended_class(classroom_id: str, room_name: str):
    """
    Idempotent teardown helper.
    - Deletes the LiveKit room (kicks all participants).
    - Clears all 3 Redis keys for this classroom.
    - Flips DB status to ENDED if not already.
    """
    supabase = get_supabase()
    redis_client = get_redis()
 
    # Delete LiveKit room — safe on non-existent rooms
    try:
        await livekit_client.delete_room(room_name=room_name)
    except Exception:
        pass  # already gone is fine
 
    # Clear Redis keys
    await redis_client.delete(room_active_key(classroom_id))
    await redis_client.delete(room_participants_key(classroom_id))
    await redis_client.delete(room_mic_open_key(classroom_id))
    await redis_client.delete(room_mic_allowed_key(classroom_id))
 
    # Flip status in DB (idempotent)
    supabase.table("classrooms").update(
        {"status": ClassroomStatus.ENDED.value, "ended_at": datetime.utcnow().isoformat()}
    ).eq("id", classroom_id).neq("status", ClassroomStatus.ENDED.value).execute()
 
 
def _student_can_publish_audio(mic_open: bool, mic_individually_granted: bool) -> bool:
    """
    A student may publish audio if EITHER:
      - The teacher has globally opened the mic for all students, OR
      - The teacher has individually granted this student mic access.
    """
    return mic_open or mic_individually_granted


async def create_classroom(
    teacher: UserOut,
    title: str,
    description: Optional[str],
    batch_id: UUID,
    scheduled_at: Optional[datetime],
    duration_minutes: int,
):
    """
    Teacher schedules a classroom. Stores in DB only.
    LiveKit room is NOT created here — created lazily at start_class.
    Redis TTL key is set here with the scheduled expiry so the join link
    has a natural deadline even before the class goes live.
    """
    supabase = get_supabase()
    redis_client = get_redis()

    room_name = f"classroom_{uuid4().hex}"
    join_token = uuid4().hex[:10]

    data = {
        "teacher_id": str(teacher.id),
        "batch_id": str(batch_id),
        "title": title,
        "description": description,
        "room_name": room_name,
        "join_token": join_token,
        "status": ClassroomStatus.SCHEDULED.value,
        "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        "duration_minutes": duration_minutes,
    }
    response = supabase.table("classrooms").insert(data).execute()
    if not response.data:
        raise Exception("Failed to create classroom in DB")

    classroom = response.data[0]

    # Pre-set Redis TTL so the join link expires at scheduled_at + duration_minutes
    # even if teacher never manually ends the class.
    # TTL = time until scheduled_at + duration, or just duration if no scheduled_at.
    if scheduled_at:
        now = datetime.utcnow()
        seconds_until_expiry = int((scheduled_at - now).total_seconds()) + (duration_minutes * 60)
        ttl = max(seconds_until_expiry, duration_minutes * 60)  # at least the duration
    else:
        ttl = duration_minutes * 60

    # We store a "scheduled" marker (not "true") so join_classroom can distinguish
    # scheduled-but-not-live from live.
    await redis_client.set(room_active_key(str(classroom["id"])), "scheduled", ex=ttl)

    return classroom


async def start_class(classroom_id: UUID, teacher: UserOut):
    """
    Teacher starts the class. Creates LiveKit room now (lazy).
    Overwrites the Redis key from "scheduled" → "true", preserving the original TTL
    by reading remaining TTL and re-setting with it.
    """
    supabase = get_supabase()
    redis_client = get_redis()

    response = supabase.table("classrooms").select("*").eq("id", str(classroom_id)).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data

    if classroom["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")
    if classroom["status"] == ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is already live")
    if classroom["status"] == ClassroomStatus.ENDED.value:
        raise BadRequestError("Classroom has already ended")

    # Get remaining TTL from the scheduled key
    remaining_ttl = await redis_client.ttl(room_active_key(str(classroom_id)))
    # ttl() returns -2 if key doesn't exist, -1 if no expiry — fall back to duration
    if remaining_ttl < 0:
        remaining_ttl = classroom.get("duration_minutes", DEFAULT_CLASS_DURATION_MINUTES) * 60

    await livekit_client.create_room(room_name=classroom["room_name"])

    update_data = {"status": ClassroomStatus.LIVE.value, "started_at": datetime.utcnow().isoformat()}
    response = supabase.table("classrooms").update(update_data).eq("id", str(classroom_id)).execute()
    if not response.data:
        raise Exception("Failed to update classroom status")

    # Overwrite "scheduled" → "true", keeping the remaining TTL
    await redis_client.set(room_active_key(str(classroom_id)), "true", ex=remaining_ttl)

    return response.data[0]


async def end_class(classroom_id: UUID, teacher: UserOut):
    """
    Teacher manually ends the class.
    Cleanup always runs first (LiveKit + Redis) regardless of DB status,
    so participants never stay stuck in Redis.
    """
    supabase = get_supabase()

    response = supabase.table("classrooms").select("*").eq("id", str(classroom_id)).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data

    if classroom["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")

    # Always clean up first — even if status is already ENDED
    await _cleanup_ended_class(str(classroom_id), classroom["room_name"])

    if classroom["status"] == ClassroomStatus.ENDED.value:
        raise BadRequestError("Classroom is already ended")

    return supabase.table("classrooms").select("*").eq("id", str(classroom_id)).single().execute().data


async def join_classroom(join_token: str, user: UserOut) -> LiveKitTokenResponse:
    """
    User joins via join link token.
    - Checks Redis room_active_key: "true" = live, "scheduled" = not started yet, missing = expired
    - Redis SET is the participant cache; DB is the source of truth for history
    """
    supabase = get_supabase()
    redis_client = get_redis()

    response = supabase.table("classrooms").select("*").eq("join_token", join_token).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found or invalid join token")
    classroom = response.data
    classroom_id = UUID(classroom["id"])

    active_flag = await redis_client.get(room_active_key(str(classroom_id)))

    if active_flag is None:
        # Key expired (TTL hit) or never set — class is over
        await _cleanup_ended_class(str(classroom_id), classroom["room_name"])
        raise BadRequestError("Classroom session has expired")

    if active_flag == "scheduled":
        raise BadRequestError("Classroom has not started yet")

    # active_flag == "true" — class is live, proceed
    if user.role == UserRole.STUDENT:
        enrollment_response = (
            supabase.table("batch_enrollments")
            .select("*")
            .eq("user_id", str(user.id))
            .eq("batch_id", classroom["batch_id"])
            .execute()
        )
        if not enrollment_response.data:
            raise ForbiddenError("Student not enrolled in this classroom's batch")

    is_teacher = user.role == UserRole.TEACHER
    mic_open = bool(await redis_client.get(room_mic_open_key(str(classroom_id))))

    can_publish_audio = is_teacher or mic_open
    can_publish_video = is_teacher
    can_share_screen = is_teacher
    can_publish = can_publish_audio or can_publish_video or can_share_screen

    livekit_token = livekit_client.generate_token(
        room_name=classroom["room_name"],
        identity=str(user.id),
        participant_name=user.name,
        can_publish=can_publish,
        can_publish_audio=can_publish_audio,
        can_publish_video=can_publish_video,
        can_share_screen=can_share_screen,
        can_subscribe=True,
    )

    # Write to DB (source of truth for history)
    existing = (
        supabase.table("classroom_participants")
        .select("id")
        .eq("classroom_id", str(classroom_id))
        .eq("user_id", str(user.id))
        .is_("left_at", None)
        .execute()
    )
    if not existing.data:
        supabase.table("classroom_participants").insert(
            {"classroom_id": str(classroom_id), "user_id": str(user.id)}
        ).execute()

    # Write to Redis cache (fast participant list)
    await redis_client.sadd(room_participants_key(str(classroom_id)), str(user.id))

    return LiveKitTokenResponse(
        token=livekit_token,
        room_name=classroom["room_name"],
        classroom_id=str(classroom_id),
        classroom_title=classroom["title"],
        can_publish=can_publish
    )


async def leave_classroom(classroom_id: UUID, user: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    update_data = {"left_at": datetime.utcnow().isoformat()}
    response = (
        supabase.table("classroom_participants")
        .update(update_data)
        .eq("classroom_id", str(classroom_id))
        .eq("user_id", str(user.id))
        .is_("left_at", None)
        .execute()
    )
    if not response.data:
        raise BadRequestError("User not found as active participant in this classroom")

    # Remove from Redis cache
    await redis_client.srem(room_participants_key(str(classroom_id)), str(user.id))


async def open_mics(classroom_id: UUID, teacher: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    classroom_response = (
        supabase.table("classrooms")
        .select("teacher_id, status")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not classroom_response.data:
        raise NotFoundError("Classroom not found")
    if classroom_response.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")
    if classroom_response.data["status"] != ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is not live")

    await redis_client.set(room_mic_open_key(str(classroom_id)), "true")


async def close_mics(classroom_id: UUID, teacher: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    classroom_response = (
        supabase.table("classrooms")
        .select("teacher_id, status")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not classroom_response.data:
        raise NotFoundError("Classroom not found")
    if classroom_response.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")

    await redis_client.delete(room_mic_open_key(str(classroom_id)))

async def grant_student_mic(classroom_id: UUID, teacher: UserOut, student_id: UUID):
    """
    Teacher individually grants mic access to a specific student.
 
    Steps:
    1. Validate teacher owns the LIVE classroom.
    2. Confirm student_id is enrolled in the classroom's batch.
    3. Add student_id to the Redis mic_allowed SET.
    4. Attempt a server-side LiveKit unmute on all their audio tracks
       (best-effort — works only if they are currently in the room).
 
    The student must call GET /classrooms/{id}/token/refresh to receive
    a new token with can_publish=True before LiveKit will accept their tracks.
    """
    classroom = await _get_live_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)
 
    supabase = get_supabase()
    # Confirm student is enrolled in this batch
    enrollment = (
        supabase.table("batch_enrollments")
        .select("id")
        .eq("user_id", str(student_id))
        .eq("batch_id", classroom["batch_id"])
        .execute()
    )
    if not enrollment.data:
        raise ForbiddenError("Student is not enrolled in this classroom's batch")
 
    redis_client = get_redis()
    await redis_client.sadd(room_mic_allowed_key(str(classroom_id)), str(student_id))
 
    # Best-effort server-side unmute via LiveKit API
    # This mutes/unmutes an existing published track; the student must refresh
    # their token to gain can_publish permission if they haven't yet.
    try:
        participants = await livekit_client.list_participants(classroom["room_name"])
        for p in participants:
            if p.get("identity") == str(student_id):
                for track in p.get("tracks", []):
                    if track.get("source") == "MICROPHONE":
                        await livekit_client.unmute_participant_track(
                            room_name=classroom["room_name"],
                            identity=str(student_id),
                            track_sid=track["sid"],
                        )
    except Exception:
        pass  # Participant may not be in room yet; token refresh will handle it
 
 
async def revoke_student_mic(classroom_id: UUID, teacher: UserOut, student_id: UUID):
    """
    Teacher individually revokes mic access from a specific student.
 
    Steps:
    1. Validate teacher owns the classroom (any status — revoke should work post-class too).
    2. Remove student_id from the Redis mic_allowed SET.
    3. Attempt a server-side LiveKit mute on all their active audio tracks.
 
    The revocation takes effect immediately via server-side mute. The student's
    current token still has can_publish set; on their next token refresh it will
    be stripped away.
    """
    supabase = get_supabase()
    response = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data
    await _assert_teacher_owns(classroom, teacher)
 
    redis_client = get_redis()
    await redis_client.srem(room_mic_allowed_key(str(classroom_id)), str(student_id))
 
    # Best-effort server-side mute via LiveKit API (immediate effect)
    try:
        participants = await livekit_client.list_participants(classroom["room_name"])
        for p in participants:
            if p.get("identity") == str(student_id):
                for track in p.get("tracks", []):
                    if track.get("source") == "MICROPHONE":
                        await livekit_client.mute_participant_track(
                            room_name=classroom["room_name"],
                            identity=str(student_id),
                            track_sid=track["sid"],
                        )
    except Exception:
        pass  # Room may be gone; Redis update is the source of truth
 
 
async def refresh_token(classroom_id: UUID, user: UserOut) -> TokenRefreshResponse:
    """
    Issues a fresh LiveKit JWT for a participant already inside a classroom,
    reflecting their current mic permissions without forcing a full re-join.
 
    Flow:
    - Re-reads Redis for current mic_open and mic_allowed state.
    - Recomputes can_publish / can_publish_audio for the calling user.
    - Signs and returns a new short-lived LiveKit token (TTL = 1 hour).
 
    Students call this endpoint after the teacher grants or revokes their mic.
    The client disconnects from LiveKit, swaps the token, and reconnects.
    TTL is intentionally kept at 1 hour (same as join) to avoid frequent refresh.
    """
    supabase = get_supabase()
    response = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data
 
    if classroom["status"] != ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is not currently live")
 
    # Verify requester is an active participant
    participant_check = (
        supabase.table("classroom_participants")
        .select("id")
        .eq("classroom_id", str(classroom_id))
        .eq("user_id", str(user.id))
        .is_("left_at", None)
        .execute()
    )
    if not participant_check.data:
        raise ForbiddenError("You are not an active participant in this classroom")
 
    redis_client = get_redis()
 
    can_publish = user.role == UserRole.TEACHER
    can_publish_audio = user.role == UserRole.TEACHER
    can_publish_video = user.role == UserRole.TEACHER
    can_share_screen = user.role == UserRole.TEACHER
 
    if user.role == UserRole.STUDENT:
        mic_open = await redis_client.get(room_mic_open_key(str(classroom_id)))
        mic_individually_granted = await redis_client.sismember(
            room_mic_allowed_key(str(classroom_id)), str(user.id)
        )
        can_publish_audio = _student_can_publish_audio(
            bool(mic_open), bool(mic_individually_granted)
        )
        if can_publish_audio:
            can_publish = True
 
    token = livekit_client.generate_token(
        room_name=classroom["room_name"],
        identity=str(user.id),
        participant_name=user.name,
        can_publish=can_publish,
        can_publish_audio=can_publish_audio,
        can_publish_video=can_publish_video,
        can_share_screen=can_share_screen,
        can_subscribe=True,
    )
 
    return TokenRefreshResponse(
        token=token,
        can_publish=can_publish,
        can_publish_audio=can_publish_audio,
    )
 
 
async def get_mic_status(classroom_id: UUID, teacher: UserOut) -> List[StudentMicStatusOut]:
    """
    Returns the list of currently active participants with their individual
    mic grant status, so the teacher dashboard can show who has mic access.
 
    Steps:
    1. Validate teacher owns the classroom.
    2. Read the mic_allowed SET from Redis.
    3. Fetch user details for all current participants from Redis SET + Supabase.
    4. Annotate each participant with mic_granted = (their id in mic_allowed).
    """
    supabase = get_supabase()
    response = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    if not response.data:
        raise NotFoundError("Classroom not found")
    await _assert_teacher_owns(response.data, teacher)
 
    redis_client = get_redis()
 
    participant_ids = await redis_client.smembers(room_participants_key(str(classroom_id)))
    mic_allowed_ids = await redis_client.smembers(room_mic_allowed_key(str(classroom_id)))
 
    if not participant_ids:
        return []
 
    users_response = (
        supabase.table("users")
        .select("id, name, email, role")
        .in_("id", list(participant_ids))
        .execute()
    )
    if not users_response.data:
        return []
 
    result = []
    for u in users_response.data:
        # Only include students (teachers always have mic; no need to show them here)
        if u["role"] == UserRole.STUDENT.value:
            result.append(
                StudentMicStatusOut(
                    student_id=UUID(u["id"]),
                    name=u["name"],
                    email=u["email"],
                    mic_granted=u["id"] in mic_allowed_ids,
                )
            )
    return result


async def get_live_participants(classroom_id: UUID) -> List[ParticipantOut]:
    """
    Redis-first participant lookup.
    Cache hit  → Redis SET has members → fetch user details from Supabase users table
    Cache miss → Redis SET empty → query classroom_participants in Supabase
                 where left_at IS NULL → repopulate Redis SET → return
    """
    supabase = get_supabase()
    redis_client = get_redis()

    # --- Cache hit path ---
    participant_ids = await redis_client.smembers(room_participants_key(str(classroom_id)))

    if not participant_ids:
        # --- Cache miss path: rebuild from DB ---
        db_response = (
            supabase.table("classroom_participants")
            .select("user_id")
            .eq("classroom_id", str(classroom_id))
            .is_("left_at", None)
            .execute()
        )
        if not db_response.data:
            return []

        participant_ids = {row["user_id"] for row in db_response.data}

        # Repopulate Redis cache
        if participant_ids:
            await redis_client.sadd(room_participants_key(str(classroom_id)), *participant_ids)

    if not participant_ids:
        return []

    # Fetch user details for the IDs we have (same for both paths)
    response = (
        supabase.table("users")
        .select("id, name, email, role")
        .in_("id", list(participant_ids))
        .execute()
    )
    if not response.data:
        return []

    return [
        ParticipantOut(id=u["id"], name=u["name"], email=u["email"], role=u["role"])
        for u in response.data
    ]