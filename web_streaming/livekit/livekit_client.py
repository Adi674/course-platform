from livekit.api import (
    LiveKitAPI,
    AccessToken,
    VideoGrants,
    TrackSource,
    CreateRoomRequest,
    DeleteRoomRequest,
    ListParticipantsRequest,
    MuteRoomTrackRequest,
)
from config import settings
from typing import List, Dict
from datetime import timedelta

api = LiveKitAPI(settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
room_service = api.room


async def create_room(room_name: str, max_participants: int = 200) -> Dict:
    """Creates a LiveKit room (idempotent — safe to call even if room already exists)."""
    try:
        room = await room_service.create_room(
            CreateRoomRequest(name=room_name, max_participants=max_participants)
        )
        return room
    except Exception as e:
        if "already exists" in str(e).lower():
            return {"name": room_name}
        raise


async def delete_room(room_name: str) -> None:
    """Closes a LiveKit room, disconnecting all participants."""
    await room_service.delete_room(DeleteRoomRequest(room=room_name))


def generate_token(
    room_name: str,
    identity: str,
    participant_name: str,
    can_publish: bool,
    can_publish_audio: bool,
    can_publish_video: bool,
    can_share_screen: bool,
    can_subscribe: bool,
    ttl_seconds: int = 3600,
) -> str:
    """
    Builds a signed LiveKit JWT with exact per-track permissions.

    Teachers  -> can_publish=True, all sources allowed.
    Students  -> can_publish=False (subscribe only) unless teacher grants mic,
                in which case can_publish=True but only MICROPHONE source allowed.
    """
    allowed_sources = []
    if can_publish_audio:
        allowed_sources.append(TrackSource.MICROPHONE)
    if can_publish_video:
        allowed_sources.append(TrackSource.CAMERA)
    if can_share_screen:
        allowed_sources.append(TrackSource.SCREEN_SHARE)
        allowed_sources.append(TrackSource.SCREEN_SHARE_AUDIO)

    grant = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_publish_data=True,
        can_publish_sources=allowed_sources,
        can_subscribe=can_subscribe,
        can_update_own_metadata=True,
    )

    access_token = (
        AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_grants(grant)
        .with_identity(identity)
        .with_name(participant_name)
        .with_ttl(timedelta(seconds=ttl_seconds))
    )
    return access_token.to_jwt()


async def mute_participant_track(room_name: str, identity: str, track_sid: str) -> None:
    """Server-side mutes a specific participant track immediately."""
    await room_service.mute_published_track(
        MuteRoomTrackRequest(room=room_name, identity=identity, track_sid=track_sid, muted=True)
    )


async def unmute_participant_track(room_name: str, identity: str, track_sid: str) -> None:
    """Server-side unmutes a specific participant track immediately."""
    await room_service.mute_published_track(
        MuteRoomTrackRequest(room=room_name, identity=identity, track_sid=track_sid, muted=False)
    )


async def list_participants(room_name: str) -> List:
    """Fetches live participant list from LiveKit, returns list of ParticipantInfo objects."""
    response = await room_service.list_participants(
        ListParticipantsRequest(room=room_name)
    )
    return response.participants