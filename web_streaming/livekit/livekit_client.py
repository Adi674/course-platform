from livekit.api import LiveKitAPI, AccessToken, VideoGrants, TrackSource
from config import settings
from typing import List, Dict

api = LiveKitAPI(settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
room_service = api.room

async def create_room(room_name: str, max_participants: int = 20) -> Dict:
    """Creates a LiveKit room (idempotent)."""
    try:
        room = await room_service.create_room(room_name=room_name, max_participants=max_participants)
        return room.as_dict()
    except Exception as e:
        # LiveKit API returns error if room already exists, which is fine for idempotency
        # We can fetch the existing room instead
        if "already exists" in str(e):
            room = await room_service.get_room(room_name=room_name)
            return room.as_dict()
        raise e

async def delete_room(room_name: str) -> None:
    """Closes a LiveKit room, disconnecting all participants."""
    await room_service.delete_room(room_name=room_name)

def generate_token(
    room_name: str,
    identity: str,
    participant_name: str,
    can_publish: bool,
    can_publish_audio: bool,
    can_publish_video: bool,
    can_share_screen: bool,
    can_subscribe: bool,
    ttl_seconds: int = 3600
) -> str:
    """Builds a signed LiveKit JWT with exact track permissions."""
    grant = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_publish_data=True,
        can_publish_sources=[TrackSource.MICROPHONE, TrackSource.CAMERA, TrackSource.SCREEN_SHARE],
        can_subscribe=can_subscribe,
        can_update_own_metadata=True,
        room_admin=False,
        hidden=False,
        recorder=False,
        agent=False,
    )
    
    access_token = (
        AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_grants(grant)
        .with_identity(identity)
        .with_name(participant_name)
        .with_ttl(ttl_seconds)
    )
    return access_token.to_jwt()

async def mute_participant_track(room_name: str, identity: str, track_sid: str) -> None:
    """Server-side mutes a specific participant's track immediately."""
    await room_service.mute_published_track(room=room_name, identity=identity, track_sid=track_sid, muted=True)

async def unmute_participant_track(room_name: str, identity: str, track_sid: str) -> None:
    """Server-side unmutes a specific participant's track immediately."""
    await room_service.mute_published_track(room=room_name, identity=identity, track_sid=track_sid, muted=False)

async def list_participants(room_name: str) -> List[Dict]:
    """Fetches live participant list from LiveKit."""
    participants = await room_service.list_participants(room_name=room_name)
    return [p.as_dict() for p in participants.participants]
