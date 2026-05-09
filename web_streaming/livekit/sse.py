"""
web_streaming/livekit/sse.py
----------------------------
Server-Sent Events broadcaster for per-classroom mic permission changes.

Architecture:
  - One asyncio.Queue per connected student (keyed by classroom_id + user_id).
  - grant_student_mic / revoke_student_mic call push_mic_granted / push_mic_revoked.
  - stream_classroom_events is an async generator consumed by StreamingResponse.
  - Keepalive ping every 25 s prevents proxy timeouts.
"""

import asyncio
import json
from typing import AsyncGenerator, Dict

from schemas import UserOut

# ---------------------------------------------------------------------------
# In-process event bus: { classroom_id: { user_id: asyncio.Queue } }
# ---------------------------------------------------------------------------

_queues: Dict[str, Dict[str, asyncio.Queue]] = {}


def _get_or_create_queue(classroom_id: str, user_id: str) -> asyncio.Queue:
    if classroom_id not in _queues:
        _queues[classroom_id] = {}
    if user_id not in _queues[classroom_id]:
        _queues[classroom_id][user_id] = asyncio.Queue(maxsize=20)
    return _queues[classroom_id][user_id]


def _remove_queue(classroom_id: str, user_id: str) -> None:
    if classroom_id in _queues and user_id in _queues[classroom_id]:
        del _queues[classroom_id][user_id]
        if not _queues[classroom_id]:
            del _queues[classroom_id]


# ---------------------------------------------------------------------------
# Push helpers — called from service.py after updating Redis
# ---------------------------------------------------------------------------

async def push_mic_granted(classroom_id: str, student_id: str) -> None:
    """Push mic_granted to the student's live SSE connection (best-effort)."""
    q = _queues.get(classroom_id, {}).get(student_id)
    if q:
        try:
            q.put_nowait({"event": "mic_granted", "data": {"student_id": student_id}})
        except asyncio.QueueFull:
            pass  # Student will get updated permissions on next token refresh


async def push_mic_revoked(classroom_id: str, student_id: str) -> None:
    """Push mic_revoked to the student's live SSE connection (best-effort)."""
    q = _queues.get(classroom_id, {}).get(student_id)
    if q:
        try:
            q.put_nowait({"event": "mic_revoked", "data": {"student_id": student_id}})
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# SSE stream generator
# ---------------------------------------------------------------------------

PING_INTERVAL = 25  # seconds — keep below typical proxy idle timeout (30–60 s)


def _fmt(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream_classroom_events(
    classroom_id: str,
    user: UserOut,
) -> AsyncGenerator[str, None]:
    """
    Async generator yielding SSE-formatted strings.

    Used as:
        StreamingResponse(stream_classroom_events(id, user), media_type="text/event-stream")

    Lifecycle:
        1. Register a queue for this (classroom_id, user_id) pair.
        2. Wait for events with PING_INTERVAL timeout; send ping on timeout.
        3. On client disconnect (GeneratorExit / CancelledError) clean up queue.
    """
    user_id = str(user.id)
    q = _get_or_create_queue(classroom_id, user_id)

    try:
        # Immediate ping confirms the stream is alive to the client
        yield _fmt("ping", {})

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=PING_INTERVAL)
                yield _fmt(msg["event"], msg["data"])
            except asyncio.TimeoutError:
                yield _fmt("ping", {})  # keepalive
    except (GeneratorExit, asyncio.CancelledError):
        pass
    finally:
        _remove_queue(classroom_id, user_id)