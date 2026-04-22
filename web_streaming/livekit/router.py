from datetime import datetime, timedelta
from typing import List, Optional
from uuid import UUID

from livekit.protocol import room as livekit_room_models

from config import settings
from database import get_supabase
from exceptions import BadRequestError, ForbiddenError, NotFoundError, UnauthorizedError
from redis_client import get_redis, room_active_key, room_mic_allowed_key, room_participants_key
from schemas import (ClassroomStatus, AccessTokenResponse, UserOut, UserRole)
from . import livekit_client


async def create_classroom(teacher: UserOut, title: str, description: Optional[str], batch_id: UUID, scheduled_at: Optional[datetime]):
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can create classrooms")

    supabase = get_supabase()
    
    # Generate a unique room name and join token
    room_name = f"classroom_{UUID.uuid4().hex}"
    join_token = UUID.uuid4().hex[:10] # Short unique slug

    # Create LiveKit room (idempotent)
    await livekit_client.create_room(room_name=room_name)

    # Insert into Supabase
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
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can start classes")

    supabase = get_supabase()
    redis_client = get_redis()

    # Fetch classroom
    response = supabase.table("classrooms").select("*").eq("id", str(classroom_id)).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data

    if classroom["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")
    
    if classroom["status"] == ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is already live")

    # Update status in DB
    update_data = {"status": ClassroomStatus.LIVE.value, "started_at": datetime.utcnow().isoformat()}
    response = supabase.table("classrooms").update(update_data).eq("id", str(classroom_id)).execute()
    if not response.data:
        raise Exception("Failed to update classroom status")
    
    # Set active in Redis
    await redis_client.set(room_active_key(str(classroom_id)), "true")
    
    return response.data[0]

async def end_class(classroom_id: UUID, teacher: UserOut):
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can end classes")

    supabase = get_supabase()
    redis_client = get_redis()

    # Fetch classroom
    response = supabase.table("classrooms").select("*").eq("id", str(classroom_id)).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found")
    classroom = response.data

    if classroom["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")
    
    if classroom["status"] == ClassroomStatus.ENDED.value:
        raise BadRequestError("Classroom is already ended")

    # Update status in DB
    update_data = {"status": ClassroomStatus.ENDED.value, "ended_at": datetime.utcnow().isoformat()}
    response = supabase.table("classrooms").update(update_data).eq("id", str(classroom_id)).execute()
    if not response.data:
        raise Exception("Failed to update classroom status")
    
    # Delete LiveKit room
    await livekit_client.delete_room(room_name=classroom["room_name"])

    # Clear Redis keys
    await redis_client.delete(room_active_key(str(classroom_id)))
    await redis_client.delete(room_participants_key(str(classroom_id)))
    await redis_client.delete(room_mic_allowed_key(str(classroom_id)))
    
    return response.data[0]

async def join_classroom(join_token: str, user: UserOut) -> AccessTokenResponse:
    supabase = get_supabase()
    redis_client = get_redis()

    # Fetch classroom by join_token
    response = supabase.table("classrooms").select("*").eq("join_token", join_token).single().execute()
    if not response.data:
        raise NotFoundError("Classroom not found or invalid join token")
    classroom = response.data
    classroom_id = UUID(classroom["id"])

    # Check if classroom is live
    is_active = await redis_client.get(room_active_key(str(classroom_id)))
    if not is_active and classroom["status"] != ClassroomStatus.LIVE.value:
        raise BadRequestError("Classroom is not currently live")

    # Check batch enrollment for students
    if user.role == UserRole.STUDENT:
        enrollment_response = supabase.table("batch_enrollments").select("*").eq("user_id", str(user.id)).eq("batch_id", classroom["batch_id"]).execute()
        if not enrollment_response.data:
            raise ForbiddenError("Student not enrolled in this classroom's batch")

    # Determine LiveKit permissions based on role
    can_publish = user.role == UserRole.TEACHER
    can_publish_audio = user.role == UserRole.TEACHER # Students can only publish audio if explicitly allowed
    can_publish_video = user.role == UserRole.TEACHER
    can_share_screen = user.role == UserRole.TEACHER
    can_subscribe = True # All participants can subscribe

    # If student, check if mic is allowed
    if user.role == UserRole.STUDENT:
        mic_allowed = await redis_client.sismember(room_mic_allowed_key(str(classroom_id)), str(user.id))
        if mic_allowed:
            can_publish_audio = True

    # Generate LiveKit token
    livekit_token = livekit_client.generate_token(
        room_name=classroom["room_name"],
        identity=str(user.id),
        participant_name=user.name,
        can_publish=can_publish,
        can_publish_audio=can_publish_audio,
        can_publish_video=can_publish_video,
        can_share_screen=can_share_screen,
        can_subscribe=can_subscribe,
    )

    # Log participant join in Supabase
    participant_data = {"classroom_id": str(classroom_id), "user_id": str(user.id)}
    # Check if user is already in classroom_participants with left_at IS NULL
    existing_participant_response = supabase.table("classroom_participants").select("*").eq("classroom_id", str(classroom_id)).eq("user_id", str(user.id)).is_("left_at", None).execute()
    if not existing_participant_response.data:
        supabase.table("classroom_participants").insert(participant_data).execute()
    
    # Add participant to Redis set
    await redis_client.sadd(room_participants_key(str(classroom_id)), str(user.id))

    return LiveKitTokenResponse(token=livekit_token)

async def leave_classroom(classroom_id: UUID, user: UserOut):
    supabase = get_supabase()
    redis_client = get_redis()

    # Update left_at in Supabase
    update_data = {"left_at": datetime.utcnow().isoformat()}
    response = supabase.table("classroom_participants").update(update_data).eq("classroom_id", str(classroom_id)).eq("user_id", str(user.id)).is_("left_at", None).execute()
    if not response.data:
        raise BadRequestError("User not found as active participant in this classroom")

    # Remove participant from Redis set
    await redis_client.srem(room_participants_key(str(classroom_id)), str(user.id))

async def allow_student_mic(classroom_id: UUID, teacher: UserOut, student_id: UUID):
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can control student microphones")

    supabase = get_supabase()
    redis_client = get_redis()

    # Verify teacher owns classroom
    classroom_response = supabase.table("classrooms").select("teacher_id").eq("id", str(classroom_id)).single().execute()
    if not classroom_response.data or classroom_response.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")

    # Add student to mic_allowed set in Redis
    await redis_client.sadd(room_mic_allowed_key(str(classroom_id)), str(student_id))

    # TODO: Implement LiveKit server-side unmute if student is currently in the room
    # This would require getting the participant's track SID from LiveKit
    # For now, the token generation logic will grant mic on rejoin if allowed

async def revoke_student_mic(classroom_id: UUID, teacher: UserOut, student_id: UUID):
    if teacher.role != UserRole.TEACHER:
        raise ForbiddenError("Only teachers can control student microphones")

    supabase = get_supabase()
    redis_client = get_redis()

    # Verify teacher owns classroom
    classroom_response = supabase.table("classrooms").select("teacher_id").eq("id", str(classroom_id)).single().execute()
    if not classroom_response.data or classroom_response.data["teacher_id"] != str(teacher.id):
        raise ForbiddenError("You are not the teacher of this classroom")

    # Remove student from mic_allowed set in Redis
    await redis_client.srem(room_mic_allowed_key(str(classroom_id)), str(student_id))

    # TODO: Implement LiveKit server-side mute if student is currently in the room
    # This would require getting the participant's track SID from LiveKit
    # For now, the token generation logic will revoke mic on rejoin if not allowed

async def get_live_participants(classroom_id: UUID) -> List[ParticipantOut]:
    supabase = get_supabase()
    redis_client = get_redis()

    # Get participant IDs from Redis
    participant_ids = await redis_client.smembers(room_participants_key(str(classroom_id)))
    if not participant_ids:
        return []

    # Fetch user details from Supabase
    response = supabase.table("users").select("id, name, email, role").in_("id", list(participant_ids)).execute()
    if not response.data:
        return []
    
    participants = []
    for user_data in response.data:
        participants.append(ParticipantOut(id=user_data["id"], name=user_data["name"], email=user_data["email"], role=user_data["role"]))
    
    return participants
