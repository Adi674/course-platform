from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from database import get_supabase
from exceptions import BadRequestError, ForbiddenError, NotFoundError
from redis_client import get_redis, room_active_key, room_mic_open_key, room_participants_key
from schemas import ClassroomStatus, LiveKitTokenResponse, ParticipantOut, UserOut, UserRole
from web_streaming.livekit import livekit_client


async def create_classroom(
    teacher: UserOut,
    title: str,
    description: Optional[str],
    batch_id: UUID,
    scheduled_at: Optional[datetime],
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
    }
    response = supabase.table("classrooms").insert(data).execute()
    if not response.data:
        raise Exception("Failed to create classroom in DB")

    return response.data[0]


async def start_class(classroom_id: UUID, teacher: UserOut):
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

    update_data = {"status": ClassroomStatus.LIVE.value, "started_at": datetime.utcnow().isoformat()}
    response = supabase.table("classrooms").update(update_data).eq("id", str(classroom_id)).execute()
    if not response.data:
        raise Exception("Failed to update classroom status")

    await redis_client.set(room_active_key(str(classroom_id)), "true")

    return response.data[0]


async def end_class(classroom_id: UUID, teacher: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    response = supabase.table("classrooms").select("*").eq("id", str(classroom_id)).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data

    if classroom["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")
    if classroom["status"] == ClassroomStatus.ENDED.value:
        raise BadRequestError("Classroom is already ended")

    update_data = {"status": ClassroomStatus.ENDED.value, "ended_at": datetime.utcnow().isoformat()}
    response = supabase.table("classrooms").update(update_data).eq("id", str(classroom_id)).execute()
    if not response.data:
        raise Exception("Failed to update classroom status")

    await livekit_client.delete_room(room_name=classroom["room_name"])

    # Clear all Redis keys for this classroom
    await redis_client.delete(room_active_key(str(classroom_id)))
    await redis_client.delete(room_participants_key(str(classroom_id)))
    await redis_client.delete(room_mic_open_key(str(classroom_id)))

    return response.data[0]


async def join_classroom(join_token: str, user: UserOut) -> LiveKitTokenResponse:
    supabase = get_supabase()
    redis_client = get_redis()

    response = supabase.table("classrooms").select("*").eq("join_token", join_token).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found or invalid join token")
    classroom = response.data
    classroom_id = UUID(classroom["id"])

    is_active = await redis_client.get(room_active_key(str(classroom_id)))
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

    # Permissions: teacher gets full publish; student gets mic only if global mic is open
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

    # Log join — only insert if not already active in this session
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

    await redis_client.srem(room_participants_key(str(classroom_id)), str(user.id))


async def open_mics(classroom_id: UUID, teacher: UserOut):
    """
    Open mic for ALL students in the classroom.
    Sets a global Redis flag — every student who joins or rejoins
    gets a token with can_publish_audio=True automatically.
    Already-connected students need to rejoin to get the new token.
    """
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
    """
    Close mic for ALL students in the classroom.
    Removes the global Redis flag — students who rejoin get subscribe-only tokens.
    """
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
    supabase = get_supabase()
    redis_client = get_redis()

    participant_ids = await redis_client.smembers(room_participants_key(str(classroom_id)))
    if not participant_ids:
        return []

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