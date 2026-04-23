from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from database import get_supabase
from exceptions import BadRequestError, ForbiddenError, NotFoundError
from redis_client import get_redis, room_active_key, room_mic_open_key, room_participants_key
from schemas import ClassroomStatus, LiveKitTokenResponse, ParticipantOut, UserOut, UserRole
from web_streaming.livekit import livekit_client

DEFAULT_CLASS_DURATION_MINUTES = 60  # fallback if teacher doesn't set duration


async def _cleanup_ended_class(classroom_id: str, room_name: str):
    """
    Idempotent cleanup — safe to call multiple times.
    Deletes LiveKit room, clears all Redis keys, flips DB status to ENDED.
    Called by end_class (manual) and join_classroom (TTL expiry detection).
    """
    supabase = get_supabase()
    redis_client = get_redis()

    await livekit_client.delete_room(room_name=room_name)
    await redis_client.delete(room_active_key(classroom_id))
    await redis_client.delete(room_participants_key(classroom_id))
    await redis_client.delete(room_mic_open_key(classroom_id))

    classroom = supabase.table("classrooms").select("status").eq("id", classroom_id).single().execute()
    if classroom.data and classroom.data["status"] != ClassroomStatus.ENDED.value:
        supabase.table("classrooms").update({
            "status": ClassroomStatus.ENDED.value,
            "ended_at": datetime.utcnow().isoformat()
        }).eq("id", classroom_id).execute()


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

    return LiveKitTokenResponse(token=livekit_token, room_name=classroom["room_name"])


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