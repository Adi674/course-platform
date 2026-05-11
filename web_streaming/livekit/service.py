from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID, uuid4

from config import settings
from database import get_supabase
from exceptions import BadRequestError, ForbiddenError, NotFoundError
from redis_client import (
    get_redis,
    room_active_key,
    room_mic_allowed_key,
    room_mic_open_key,
    room_participants_key,
)
from schemas import (
    ClassroomStatus,
    LiveKitTokenResponse,
    ParticipantOut,
    StudentMicStatusOut,
    TokenRefreshResponse,
    UserOut,
    UserRole,
)
from web_streaming.livekit.sse import push_mic_granted, push_mic_revoked
from . import livekit_client


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_classroom(classroom_id: UUID) -> dict:
    """Fetch classroom row by ID — no status check."""
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
    return response.data


async def _get_live_classroom(classroom_id: UUID) -> dict:
    """Fetch classroom row and assert it is currently LIVE."""
    classroom = await _get_classroom(classroom_id)
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
    - Clears all Redis keys for this classroom.
    - Flips DB status to ENDED if not already.
    """
    supabase = get_supabase()
    redis_client = get_redis()

    try:
        await livekit_client.delete_room(room_name=room_name)
    except Exception:
        pass

    await redis_client.delete(room_active_key(classroom_id))
    await redis_client.delete(room_participants_key(classroom_id))
    await redis_client.delete(room_mic_open_key(classroom_id))
    await redis_client.delete(room_mic_allowed_key(classroom_id))

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


# ── Service functions ──────────────────────────────────────────────────────────

async def create_classroom(
    teacher: UserOut,
    title: str,
    description: Optional[str],
    batch_id: UUID,
    scheduled_at: Optional[datetime],
    duration_minutes: int = 60,
):
    supabase = get_supabase()

    room_name = f"classroom_{uuid4().hex}"
    join_token = uuid4().hex[:10]

    await livekit_client.create_room(room_name=room_name)

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

    return response.data[0]


async def start_class(classroom_id: UUID, teacher: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    classroom = await _get_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)

    if classroom["status"] == ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is already live")

    await livekit_client.create_room(room_name=classroom["room_name"])


    update_data = {
        "status": ClassroomStatus.LIVE.value,
        "started_at": datetime.utcnow().isoformat(),
    }
    response = (
        supabase.table("classrooms")
        .update(update_data)
        .eq("id", str(classroom_id))
        .execute()
    )
    if not response.data:
        raise Exception("Failed to update classroom status")

    await redis_client.set(room_active_key(str(classroom_id)), "true")

    return response.data[0]


async def end_class(classroom_id: UUID, teacher: UserOut):
    supabase = get_supabase()

    classroom = await _get_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)

    if classroom["status"] == ClassroomStatus.ENDED.value:
        raise BadRequestError("Classroom is already ended")

    await _cleanup_ended_class(str(classroom_id), classroom["room_name"])

    updated = (
        supabase.table("classrooms")
        .select("*")
        .eq("id", str(classroom_id))
        .single()
        .execute()
    )
    return updated.data


async def join_classroom(join_token: str, user: UserOut) -> LiveKitTokenResponse:
    supabase = get_supabase()
    redis_client = get_redis()

    response = (
        supabase.table("classrooms")
        .select("*")
        .eq("join_token", join_token)
        .single()
        .execute()
    )
    if not response.data:
        raise NotFoundError("Classroom not found or invalid join token")
    classroom = response.data
    classroom_id = str(classroom["id"])

    is_active = await redis_client.get(room_active_key(classroom_id))
    if not is_active and classroom["status"] != ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is not currently live")

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

    # Determine publish permissions
    can_publish = user.role == UserRole.TEACHER
    can_publish_video = user.role == UserRole.TEACHER
    can_share_screen = user.role == UserRole.TEACHER

    # Audio: teacher always yes; student depends on global OR per-student flag
    can_publish_audio = user.role == UserRole.TEACHER
    if user.role == UserRole.STUDENT:
        mic_open = await redis_client.get(room_mic_open_key(classroom_id))
        mic_individually_granted = await redis_client.sismember(
            room_mic_allowed_key(classroom_id), str(user.id)
        )
        can_publish_audio = _student_can_publish_audio(
            bool(mic_open), bool(mic_individually_granted)
        )
        # Only set can_publish if audio is explicitly allowed
        can_publish = can_publish_audio

    livekit_token = livekit_client.generate_token(
        room_name=classroom["room_name"],
        identity=str(user.id),
        participant_name=user.name,
        can_publish=can_publish,
        can_publish_audio=can_publish_audio,
        can_publish_video=can_publish_video,
        can_share_screen=can_share_screen,
        can_subscribe=True,  # Always allow subscribing so students receive teacher tracks
    )

    # Log join in Supabase (idempotent)
    existing = (
        supabase.table("classroom_participants")
        .select("*")
        .eq("classroom_id", classroom_id)
        .eq("user_id", str(user.id))
        .is_("left_at", None)
        .execute()
    )
    if not existing.data:
        supabase.table("classroom_participants").insert(
            {"classroom_id": classroom_id, "user_id": str(user.id)}
        ).execute()

    await redis_client.sadd(room_participants_key(classroom_id), str(user.id))

    return LiveKitTokenResponse(
        token=livekit_token,
        room_name=classroom["room_name"],
        classroom_id=classroom_id,
        classroom_title=classroom["title"],
        can_publish=can_publish,
        role=user.role,
    )


async def leave_classroom(classroom_id: UUID, user: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    response = (
        supabase.table("classroom_participants")
        .update({"left_at": datetime.utcnow().isoformat()})
        .eq("classroom_id", str(classroom_id))
        .eq("user_id", str(user.id))
        .is_("left_at", None)
        .execute()
    )
    if not response.data:
        raise BadRequestError("User not found as active participant in this classroom")

    await redis_client.srem(room_participants_key(str(classroom_id)), str(user.id))


async def open_mics(classroom_id: UUID, teacher: UserOut):
    """
    Globally open mic for all students.
    Uses _get_classroom (no status check) so this works even if the Redis
    active key wasn't set — the DB status is the source of truth here.
    """
    classroom = await _get_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)
    redis_client = get_redis()
    await redis_client.set(room_mic_open_key(str(classroom_id)), "true")

    try:
        participants = await livekit_client.list_participants(classroom["room_name"])
        for p in participants:
            # Skip the teacher; update all others (students)
            if p.identity != str(teacher.id):
                await livekit_client.update_participant_permissions(
                    room_name=classroom["room_name"],
                    identity=p.identity,
                    can_publish=True  # Allow them to start their mics
                )
    except Exception as e:
        print(f"Failed to push global mic permissions: {e}")


async def close_mics(classroom_id: UUID, teacher: UserOut):
    """Globally close mic for all students."""
    classroom = await _get_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)
    redis_client = get_redis()
    await redis_client.delete(room_mic_open_key(str(classroom_id)))

    try:
        participants = await livekit_client.list_participants(classroom["room_name"])
        for p in participants:
            if p.identity != str(teacher.id):
                # Check if they have an individual grant before revoking global access
                is_granted = await redis_client.sismember(
                    room_mic_allowed_key(str(classroom_id)), p.identity
                )
                if not is_granted:
                    await livekit_client.update_participant_permissions(
                        room_name=classroom["room_name"],
                        identity=p.identity,
                        can_publish=False
                    )
    except Exception as e:
        print(f"Failed to revoke global mic permissions: {e}")


async def grant_student_mic(classroom_id: UUID, teacher: UserOut, student_id: UUID):
    """
    Teacher individually grants mic access to a specific student.
    1. Validate teacher owns the LIVE classroom.
    2. Confirm student_id is enrolled in the classroom's batch.
    3. Add student_id to the Redis mic_allowed SET.
    4. Push SSE mic_granted event to the student.
    5. Best-effort server-side LiveKit unmute on their audio tracks.
    """
    classroom = await _get_live_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)

    supabase = get_supabase()
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

    # Notify student via SSE so they can refresh their token
    await push_mic_granted(str(classroom_id), str(student_id))

    # Best-effort server-side unmute
    try:
        await livekit_client.update_participant_permissions(
            room_name=classroom["room_name"],
            identity=str(student_id),
            can_publish=True  # This enables the mic button for them instantly
        )
    except Exception as e:
        print(f"LiveKit permission update failed: {e}")


async def revoke_student_mic(classroom_id: UUID, teacher: UserOut, student_id: UUID):
    """
    Teacher individually revokes mic access from a specific student.
    1. Validate teacher owns the classroom.
    2. Remove student_id from the Redis mic_allowed SET.
    3. Push SSE mic_revoked event to the student.
    4. Immediate server-side LiveKit mute on their active audio tracks.
    """
    classroom = await _get_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)

    redis_client = get_redis()
    await redis_client.srem(room_mic_allowed_key(str(classroom_id)), str(student_id))

    # Notify student via SSE
    await push_mic_revoked(str(classroom_id), str(student_id))

    # Immediate server-side mute
    try:
        await livekit_client.update_participant_permissions(
            room_name=classroom["room_name"],
            identity=str(student_id),
            can_publish=False
        )
        # 3. Force-mute them if they were currently speaking
        participants = await livekit_client.list_participants(classroom["room_name"])
        for p in participants:
            if p.identity == str(student_id):
                for track in p.tracks:
                    if "MICROPHONE" in str(track.source):
                        await livekit_client.mute_participant_track(
                            classroom["room_name"], str(student_id), track.sid
                        )
    except Exception:
        pass


async def refresh_token(classroom_id: UUID, user: UserOut) -> TokenRefreshResponse:
    """
    Issues a fresh LiveKit JWT reflecting the caller's current mic permissions.
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
        can_publish = can_publish_audio

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
    Returns active participants with their individual mic grant status.
    """
    classroom = await _get_classroom(classroom_id)
    await _assert_teacher_owns(classroom, teacher)

    supabase = get_supabase()
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
    supabase = get_supabase()
    redis_client = get_redis()

    participant_ids = await redis_client.smembers(room_participants_key(str(classroom_id)))
    if not participant_ids:
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
        for uid in participant_ids:
            await redis_client.sadd(room_participants_key(str(classroom_id)), uid)

    response = (
        supabase.table("users")
        .select("id, name, email, role")
        .in_("id", list(participant_ids))
        .execute()
    )
    if not response.data:
        return []

    return [
        ParticipantOut(
            id=u["id"], name=u["name"], email=u["email"], role=u["role"]
        )
        for u in response.data
    ]