import redis.asyncio as redis
from typing import Optional
from config import settings

_redis_client: Optional[redis.Redis] = None

async def init_redis():
    global _redis_client
    _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    await _redis_client.ping()
    print("Redis client initialized")

async def close_redis():
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        print("Redis client closed")

def get_redis() -> redis.Redis:
    if not _redis_client:
        raise ConnectionError("Redis client not initialized")
    return _redis_client

# Key builders
def room_active_key(classroom_id: str) -> str:
    """'true' string when class is live."""
    return f"classroom:{classroom_id}:active"

def room_participants_key(classroom_id: str) -> str:
    """Redis SET of user_id strings currently in the room."""
    return f"classroom:{classroom_id}:participants"

def room_mic_open_key(classroom_id: str) -> str:
    """'true' string when teacher has opened mic for ALL students."""
    return f"classroom:{classroom_id}:mic_open"
  
 
def room_mic_allowed_key(classroom_id: str) -> str:
    """
    Per-student mic allowlist — Redis SET of user_id strings.
    A student present in this set has been individually granted mic access by the teacher.
    Used alongside room_mic_open_key: a student can publish audio if EITHER the global
    mic is open OR their user_id is in this per-student set.
    """
    return f"classroom:{classroom_id}:mic_allowed"