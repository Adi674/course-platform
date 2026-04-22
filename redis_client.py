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
    return f"classroom:{classroom_id}:active"

def room_participants_key(classroom_id: str) -> str:
    return f"classroom:{classroom_id}:participants"

def room_mic_allowed_key(classroom_id: str) -> str:
    return f"classroom:{classroom_id}:mic_allowed"
