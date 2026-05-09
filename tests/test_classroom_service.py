"""
tests/test_classroom_service.py
--------------------------------
Unit tests for web_streaming/livekit/service.py

Run with:
    pip install pytest pytest-asyncio pytest-mock --break-system-packages
    pytest tests/test_classroom_service.py -v
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
from uuid import UUID, uuid4
from datetime import datetime

# ── Fixtures ──────────────────────────────────────────────────────────────────

TEACHER_ID = uuid4()
STUDENT_ID = uuid4()
CLASSROOM_ID = uuid4()
BATCH_ID = uuid4()
ROOM_NAME = f"classroom_{uuid4().hex}"
JOIN_TOKEN = "abc123"

def make_teacher():
    from schemas import UserOut, UserRole
    return UserOut(
        id=TEACHER_ID,
        name="Prof. Test",
        email="teacher@test.com",
        role=UserRole.TEACHER,
        created_at=datetime.utcnow(),
    )

def make_student():
    from schemas import UserOut, UserRole
    return UserOut(
        id=STUDENT_ID,
        name="Student Test",
        email="student@test.com",
        role=UserRole.STUDENT,
        created_at=datetime.utcnow(),
    )

def make_classroom(status="live"):
    return {
        "id": str(CLASSROOM_ID),
        "teacher_id": str(TEACHER_ID),
        "batch_id": str(BATCH_ID),
        "title": "Test Class",
        "description": None,
        "room_name": ROOM_NAME,
        "join_token": JOIN_TOKEN,
        "status": status,
        "scheduled_at": None,
        "started_at": datetime.utcnow().isoformat(),
        "ended_at": None,
        "duration_minutes": 60,
        "created_at": datetime.utcnow().isoformat(),
    }


def _mock_supabase_chain(return_data, count=None):
    execute_mock = MagicMock()
    execute_mock.data = return_data
    execute_mock.count = count

    chain = MagicMock()
    chain.execute.return_value = execute_mock
    chain.single.return_value = chain
    chain.eq.return_value = chain
    chain.neq.return_value = chain
    chain.is_.return_value = chain
    chain.in_.return_value = chain
    chain.order.return_value = chain
    chain.select.return_value = chain
    chain.insert.return_value = chain
    chain.update.return_value = chain
    chain.delete.return_value = chain

    supabase = MagicMock()
    supabase.table.return_value = chain
    return supabase, chain, execute_mock


def make_redis_get_side_effect(active=True, mic_open=False):
    """
    Returns a side_effect function for redis.get that distinguishes
    room_active_key from room_mic_open_key by the key string.

    This prevents the test from accidentally granting mic permission
    when it only intends to mock the room-active check.
    """
    from redis_client import room_active_key, room_mic_open_key

    active_key = room_active_key(str(CLASSROOM_ID))
    mic_open_key = room_mic_open_key(str(CLASSROOM_ID))

    async def _side_effect(key):
        if key == active_key:
            return "true" if active else None
        if key == mic_open_key:
            return "true" if mic_open else None
        return None

    return _side_effect


# ── open_mics tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_mics_success():
    """Teacher can open mics on their own classroom."""
    from web_streaming.livekit import service

    classroom_data = make_classroom(status="live")
    supabase, chain, _ = _mock_supabase_chain(classroom_data)

    redis = AsyncMock()
    redis.set = AsyncMock()

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis):
        await service.open_mics(CLASSROOM_ID, make_teacher())

    redis.set.assert_called_once()
    call_args = redis.set.call_args[0]
    assert str(CLASSROOM_ID) in call_args[0]
    assert call_args[1] == "true"


@pytest.mark.asyncio
async def test_open_mics_wrong_teacher_raises_403():
    """A teacher who doesn't own the classroom gets ForbiddenError."""
    from web_streaming.livekit import service
    from exceptions import ForbiddenError

    other_teacher_classroom = make_classroom()
    other_teacher_classroom["teacher_id"] = str(uuid4())
    supabase, _, _ = _mock_supabase_chain(other_teacher_classroom)

    redis = AsyncMock()

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis):
        with pytest.raises(ForbiddenError):
            await service.open_mics(CLASSROOM_ID, make_teacher())


@pytest.mark.asyncio
async def test_open_mics_classroom_not_found_raises_404():
    """Missing classroom raises NotFoundError (404)."""
    from web_streaming.livekit import service
    from exceptions import NotFoundError

    supabase, _, execute_mock = _mock_supabase_chain(None)
    execute_mock.data = None

    redis = AsyncMock()

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis):
        with pytest.raises(NotFoundError):
            await service.open_mics(CLASSROOM_ID, make_teacher())


# ── grant_student_mic tests ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_student_mic_pushes_sse():
    """grant_student_mic must call push_mic_granted after updating Redis."""
    from web_streaming.livekit import service

    classroom_data = make_classroom(status="live")
    enrollment_data = [{"id": str(uuid4())}]

    supabase = MagicMock()

    def table_side_effect(table_name):
        chain = MagicMock()
        execute_result = MagicMock()

        if table_name == "classrooms":
            execute_result.data = classroom_data
        elif table_name == "batch_enrollments":
            execute_result.data = enrollment_data
        else:
            execute_result.data = []

        chain.execute.return_value = execute_result
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.single.return_value = chain
        chain.is_.return_value = chain
        return chain

    supabase.table.side_effect = table_side_effect

    redis = AsyncMock()
    redis.sadd = AsyncMock()

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis), \
         patch("web_streaming.livekit.service.push_mic_granted") as mock_push, \
         patch("web_streaming.livekit.service.livekit_client.list_participants",
               return_value=[]):
        mock_push.return_value = None
        await service.grant_student_mic(CLASSROOM_ID, make_teacher(), STUDENT_ID)

    mock_push.assert_called_once_with(str(CLASSROOM_ID), str(STUDENT_ID))
    redis.sadd.assert_called_once()


@pytest.mark.asyncio
async def test_revoke_student_mic_pushes_sse():
    """revoke_student_mic must call push_mic_revoked after updating Redis."""
    from web_streaming.livekit import service

    classroom_data = make_classroom(status="live")
    supabase, _, _ = _mock_supabase_chain(classroom_data)

    redis = AsyncMock()
    redis.srem = AsyncMock()

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis), \
         patch("web_streaming.livekit.service.push_mic_revoked") as mock_push, \
         patch("web_streaming.livekit.service.livekit_client.list_participants",
               return_value=[]):
        mock_push.return_value = None
        await service.revoke_student_mic(CLASSROOM_ID, make_teacher(), STUDENT_ID)

    mock_push.assert_called_once_with(str(CLASSROOM_ID), str(STUDENT_ID))
    redis.srem.assert_called_once()


# ── join_classroom permission tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_join_classroom_student_can_subscribe():
    """
    Students with no mic grant must get:
      - can_subscribe=True  (so they receive teacher's video/screen/audio)
      - can_publish=False   (listen-only until teacher grants mic)
    """
    from web_streaming.livekit import service

    classroom_data = make_classroom(status="live")
    enrollment_data = [{"id": str(uuid4()), "batch_id": str(BATCH_ID), "user_id": str(STUDENT_ID)}]

    supabase = MagicMock()

    def table_side_effect(table_name):
        chain = MagicMock()
        execute_result = MagicMock()
        if table_name == "classrooms":
            execute_result.data = classroom_data
        elif table_name == "batch_enrollments":
            execute_result.data = enrollment_data
        elif table_name == "classroom_participants":
            execute_result.data = []
        else:
            execute_result.data = []
        chain.execute.return_value = execute_result
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.single.return_value = chain
        chain.is_.return_value = chain
        chain.insert.return_value = chain
        return chain

    supabase.table.side_effect = table_side_effect

    redis = AsyncMock()
    # KEY FIX: use side_effect to return "true" for active key, None for mic_open key
    redis.get = AsyncMock(side_effect=make_redis_get_side_effect(active=True, mic_open=False))
    redis.sismember = AsyncMock(return_value=False)  # no individual grant
    redis.sadd = AsyncMock()

    captured_token_args = {}

    def fake_generate_token(**kwargs):
        captured_token_args.update(kwargs)
        return "fake.livekit.token"

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis), \
         patch("web_streaming.livekit.service.livekit_client.generate_token",
               side_effect=fake_generate_token):
        result = await service.join_classroom(JOIN_TOKEN, make_student())

    assert captured_token_args["can_subscribe"] is True, \
        "can_subscribe must be True so student receives teacher's video/screen/audio"
    assert captured_token_args["can_publish"] is False, \
        "Student with no mic grant must not be able to publish"


@pytest.mark.asyncio
async def test_join_classroom_teacher_gets_full_publish():
    """Teacher must get can_publish=True and can_subscribe=True."""
    from web_streaming.livekit import service

    classroom_data = make_classroom(status="live")

    supabase = MagicMock()

    def table_side_effect(table_name):
        chain = MagicMock()
        execute_result = MagicMock()
        if table_name == "classrooms":
            execute_result.data = classroom_data
        elif table_name == "classroom_participants":
            execute_result.data = []
        else:
            execute_result.data = []
        chain.execute.return_value = execute_result
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.single.return_value = chain
        chain.is_.return_value = chain
        chain.insert.return_value = chain
        return chain

    supabase.table.side_effect = table_side_effect

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=make_redis_get_side_effect(active=True))
    redis.sadd = AsyncMock()

    captured = {}

    def fake_generate_token(**kwargs):
        captured.update(kwargs)
        return "fake.livekit.token"

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis), \
         patch("web_streaming.livekit.service.livekit_client.generate_token",
               side_effect=fake_generate_token):
        await service.join_classroom(JOIN_TOKEN, make_teacher())

    assert captured["can_publish"] is True
    assert captured["can_subscribe"] is True


@pytest.mark.asyncio
async def test_join_classroom_student_with_mic_grant_can_publish():
    """Student with individual mic grant must get can_publish=True."""
    from web_streaming.livekit import service

    classroom_data = make_classroom(status="live")
    enrollment_data = [{"id": str(uuid4())}]

    supabase = MagicMock()

    def table_side_effect(table_name):
        chain = MagicMock()
        execute_result = MagicMock()
        if table_name == "classrooms":
            execute_result.data = classroom_data
        elif table_name == "batch_enrollments":
            execute_result.data = enrollment_data
        elif table_name == "classroom_participants":
            execute_result.data = []
        else:
            execute_result.data = []
        chain.execute.return_value = execute_result
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.single.return_value = chain
        chain.is_.return_value = chain
        chain.insert.return_value = chain
        return chain

    supabase.table.side_effect = table_side_effect

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=make_redis_get_side_effect(active=True, mic_open=False))
    redis.sismember = AsyncMock(return_value=True)  # individually granted
    redis.sadd = AsyncMock()

    captured = {}

    def fake_generate_token(**kwargs):
        captured.update(kwargs)
        return "fake.livekit.token"

    with patch("web_streaming.livekit.service.get_supabase", return_value=supabase), \
         patch("web_streaming.livekit.service.get_redis", return_value=redis), \
         patch("web_streaming.livekit.service.livekit_client.generate_token",
               side_effect=fake_generate_token):
        result = await service.join_classroom(JOIN_TOKEN, make_student())

    assert captured["can_publish"] is True
    assert result.can_publish is True


# ── SSE push tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sse_push_mic_granted_delivers_to_queue():
    """push_mic_granted puts event in the right queue."""
    from web_streaming.livekit.sse import (
        push_mic_granted,
        _get_or_create_queue,
        _remove_queue,
    )

    cid = str(uuid4())
    uid = str(uuid4())
    q = _get_or_create_queue(cid, uid)

    await push_mic_granted(cid, uid)

    assert not q.empty()
    msg = q.get_nowait()
    assert msg["event"] == "mic_granted"
    assert msg["data"]["student_id"] == uid

    _remove_queue(cid, uid)


@pytest.mark.asyncio
async def test_sse_push_mic_revoked_delivers_to_queue():
    """push_mic_revoked puts event in the right queue."""
    from web_streaming.livekit.sse import (
        push_mic_revoked,
        _get_or_create_queue,
        _remove_queue,
    )

    cid = str(uuid4())
    uid = str(uuid4())
    q = _get_or_create_queue(cid, uid)

    await push_mic_revoked(cid, uid)

    assert not q.empty()
    msg = q.get_nowait()
    assert msg["event"] == "mic_revoked"

    _remove_queue(cid, uid)


@pytest.mark.asyncio
async def test_sse_push_no_subscriber_does_not_raise():
    """push_mic_granted when no student is connected must not raise."""
    from web_streaming.livekit.sse import push_mic_granted
    await push_mic_granted("nonexistent-room", "nonexistent-user")


@pytest.mark.asyncio
async def test_sse_stream_sends_immediate_ping_then_event():
    """stream_classroom_events yields a ping first, then queued events."""
    import asyncio
    from web_streaming.livekit.sse import (
        stream_classroom_events,
        _get_or_create_queue,
        _remove_queue,
        push_mic_granted,
    )
    from schemas import UserOut, UserRole

    cid = str(uuid4())
    uid = str(uuid4())

    user = UserOut(
        id=UUID(uid),
        name="Test Student",
        email="s@test.com",
        role=UserRole.STUDENT,
        created_at=datetime.utcnow(),
    )

    q = _get_or_create_queue(cid, uid)
    q.put_nowait({"event": "mic_granted", "data": {"student_id": uid}})

    gen = stream_classroom_events(cid, user)

    first = await gen.__anext__()
    assert "event: ping" in first

    second = await gen.__anext__()
    assert "event: mic_granted" in second
    assert uid in second

    await gen.aclose()


# ── Permission logic unit tests ───────────────────────────────────────────────

def test_student_can_publish_audio_either_flag():
    """_student_can_publish_audio returns True if either flag is set."""
    from web_streaming.livekit.service import _student_can_publish_audio

    assert _student_can_publish_audio(True, False) is True
    assert _student_can_publish_audio(False, True) is True
    assert _student_can_publish_audio(True, True) is True
    assert _student_can_publish_audio(False, False) is False