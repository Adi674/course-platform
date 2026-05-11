from livekit.api import (
    LiveKitAPI,
    AccessToken,
    VideoGrants,
    CreateRoomRequest,
    DeleteRoomRequest,
    ListParticipantsRequest,
    MuteRoomTrackRequest,
    UpdateParticipantRequest,
)

# from livekit.api.egress_service import (
#     RoomCompositeEgressRequest,
#     StopEgressRequest,
#     EncodedFileOutput,
#     S3Upload,
# )
try:
    from livekit.api.proto.egress_pb2 import (  # type: ignore[import]
        RoomCompositeEgressRequest,
        StopEgressRequest,
        EncodedFileOutput,
        S3Upload,
    )
except ImportError:
    try:
        from livekit.api.egress_service import (  # type: ignore[import]
            RoomCompositeEgressRequest,
            StopEgressRequest,
            EncodedFileOutput,
            S3Upload,
        )
    except ImportError:
        # Newest SDK (≥ 0.7) — everything lives directly in livekit.api
        from livekit.api import (  # type: ignore[import]
            RoomCompositeEgressRequest,
            StopEgressRequest,
            EncodedFileOutput,
            S3Upload,
        )
        
from config import settings
from typing import List, Dict
from datetime import timedelta

api = LiveKitAPI(settings.LIVEKIT_URL, settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
room_service = api.room
egress_service = api.egress


# ── Room management ────────────────────────────────────────────────────────────

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
    """Closes a LiveKit room. Safe to call even if room no longer exists."""
    try:
        await room_service.delete_room(DeleteRoomRequest(room=room_name))
    except Exception as e:
        if "not_found" not in str(e).lower() and "does not exist" not in str(e).lower():
            raise


# ── Token generation ──────────────────────────────────────────────────────────

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
    Builds and signs a LiveKit JWT granting the specified permissions.

    Args:
        room_name:         LiveKit room identifier.
        identity:          Unique participant identity (user UUID as string).
        participant_name:  Display name shown to other participants.
        can_publish:       Master publish flag (audio + video + data).
        can_publish_audio: Granular audio-publish permission.
        can_publish_video: Granular video-publish permission.
        can_share_screen:  Allow screen-share tracks.
        can_subscribe:     Allow receiving other participants' tracks.
        ttl_seconds:       Token lifetime in seconds (default 1 hour).

    Returns:
        Signed JWT string for use with LiveKit SDK `room.connect()`.
    """
    grant = VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_publish_data=True,
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


# ── Participant control ────────────────────────────────────────────────────────

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
    """Fetches live participant list from LiveKit. Returns list of ParticipantInfo protobuf objects."""
    response = await room_service.list_participants(
        ListParticipantsRequest(room=room_name)
    )
    return response.participants


async def update_participant_permissions(
    room_name: str,
    identity: str,
    can_publish: bool,
    can_subscribe: bool,
    can_publish_data: bool = True,
) -> None:
    """
    Server-side live permission update — pushes updated grants to an active
    participant without requiring them to disconnect and rejoin.

    Args:
        room_name:        LiveKit room identifier.
        identity:         Participant identity (user UUID string).
        can_publish:      Whether participant may publish tracks.
        can_subscribe:    Whether participant may receive tracks.
        can_publish_data: Whether participant may send data messages (default True).
    """
    from livekit.api import ParticipantPermission
    await room_service.update_participant(
        UpdateParticipantRequest(
            room=room_name,
            identity=identity,
            permission=ParticipantPermission(
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                can_publish_data=can_publish_data,
            ),
        )
    )


# ── Egress / Recording ────────────────────────────────────────────────────────

async def start_egress(room_name: str, s3_key: str) -> object:
    """
    Starts a RoomComposite egress that records the full room (video + audio mix)
    and writes the output MP4 directly to S3.

    The S3 path is: s3://<AWS_S3_BUCKET_NAME>/<s3_key>

    Args:
        room_name: LiveKit room to record.
        s3_key:    S3 object key for the output file,
                   e.g. "recordings/classroom_abc/rec_xyz.mp4".

    Returns:
        EgressInfo protobuf object. Callers should store `.egress_id` (str)
        in the DB for use with `stop_egress`.

    Raises:
        Exception: propagated from LiveKit API on auth/config errors.
    """
    output = EncodedFileOutput(
    filepath=s3_key,         # ← object key inside the bucket
    s3=S3Upload(
        access_key=settings.AWS_ACCESS_KEY_ID,
        secret=settings.AWS_SECRET_ACCESS_KEY,
        region=settings.AWS_REGION,
        bucket=settings.AWS_S3_BUCKET_NAME,
        )
    )

    request = RoomCompositeEgressRequest(
        room_name=room_name,
        layout="speaker",          # "speaker" | "grid" | "pin" — best for lecture format
        audio_only=False,
        file=output,
    )

    egress_info = await egress_service.start_room_composite_egress(request)
    return egress_info


async def stop_egress(egress_id: str) -> object:
    """
    Stops an active egress by its ID. LiveKit finalises the file and writes it
    to S3 — this may take a few seconds after the call returns.

    Args:
        egress_id: The egress_id string returned by `start_egress`.

    Returns:
        Updated EgressInfo protobuf with final status.

    Raises:
        Exception: propagated from LiveKit API if egress not found or already stopped.
    """
    egress_info = await egress_service.stop_egress(
        StopEgressRequest(egress_id=egress_id)
    )
    return egress_info