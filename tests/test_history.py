# tests/test_history.py
"""历史功能单元测试（不依赖外部服务）。"""
import logging
from unittest.mock import MagicMock


def test_clear_deletes_both_redis_keys():
    """clear() 应当删除 messages 和 summary 两个 key。"""
    from app.memory.short_term import ShortTermMemory

    mock_redis = MagicMock()
    mock_pipe = MagicMock()
    mock_redis.pipeline.return_value = mock_pipe

    mem = ShortTermMemory("test-sess", client=mock_redis)
    mem.clear()

    mock_redis.pipeline.assert_called_once()
    mock_pipe.delete.assert_any_call("mom:session:test-sess:messages")
    mock_pipe.delete.assert_any_call("mom:session:test-sess:summary")
    mock_pipe.execute.assert_called_once()


def test_clear_does_not_raise_on_redis_error(caplog):
    """clear() Redis 出错时只记录日志，不抛异常。"""
    from app.memory.short_term import ShortTermMemory

    mock_redis = MagicMock()
    mock_redis.pipeline.side_effect = Exception("redis down")

    mem = ShortTermMemory("test-sess", client=mock_redis)
    with caplog.at_level(logging.WARNING):
        mem.clear()  # 不应抛出
    assert any(
        r.levelno == logging.WARNING and "test-sess" in r.message
        for r in caplog.records
    )


# ── Task 2: long_term.py 辅助函数 ────────────────────────────
import pytest
from datetime import datetime, timezone


def _make_session(id_, user_id, updated_at):
    s = MagicMock()
    s.id = id_
    s.user_id = user_id
    s.updated_at = updated_at
    return s


def _make_message(id_, session_id, role, content, thinking=None):
    m = MagicMock()
    m.id = id_
    m.session_id = session_id
    m.role = role
    m.content = content
    m.thinking = thinking
    m.created_at = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    return m


# list_sessions

def test_list_sessions_empty_user_id_returns_empty():
    from app.memory.long_term import list_sessions
    db = MagicMock()
    assert list_sessions(db, "") == []
    db.execute.assert_not_called()


def test_list_sessions_returns_formatted_results():
    from app.memory.long_term import list_sessions
    db = MagicMock()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    sess = _make_session("sess-1", "user-1", now)

    # 单次查询返回 (SessionRow, first_content, cnt) 三元组列表
    mock_row = (sess, "如何重启网关？", 4)
    db.execute.return_value.all.return_value = [mock_row]

    result = list_sessions(db, "user-1")
    assert len(result) == 1
    assert result[0]["session_id"] == "sess-1"
    assert result[0]["title"] == "如何重启网关？"
    assert result[0]["message_count"] == 4
    assert isinstance(result[0]["updated_at"], str)


def test_list_sessions_truncates_title_to_30_chars():
    from app.memory.long_term import list_sessions
    db = MagicMock()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    sess = _make_session("sess-2", "user-1", now)

    mock_row = (sess, "A" * 50, 2)
    db.execute.return_value.all.return_value = [mock_row]

    result = list_sessions(db, "user-1")
    assert len(result[0]["title"]) == 30


def test_list_sessions_fallback_title_when_no_user_msg():
    from app.memory.long_term import list_sessions
    db = MagicMock()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    sess = _make_session("sess-3", "user-1", now)

    mock_row = (sess, None, 0)
    db.execute.return_value.all.return_value = [mock_row]

    result = list_sessions(db, "user-1")
    assert result[0]["title"] == "新对话"


# get_session_messages

def test_get_session_messages_raises_when_not_found():
    from app.memory.long_term import get_session_messages
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(ValueError):
        get_session_messages(db, "sess-x", "user-1")


def test_get_session_messages_raises_when_wrong_user():
    from app.memory.long_term import get_session_messages
    db = MagicMock()
    sess = _make_session("sess-1", "other-user", datetime.now(timezone.utc))
    db.get.return_value = sess
    with pytest.raises(ValueError):
        get_session_messages(db, "sess-1", "user-1")


def test_get_session_messages_returns_user_and_assistant_messages():
    from app.memory.long_term import get_session_messages
    db = MagicMock()
    sess = _make_session("sess-1", "user-1", datetime.now(timezone.utc))
    db.get.return_value = sess

    msg1 = _make_message(1, "sess-1", "user", "你好")
    msg2 = _make_message(2, "sess-1", "assistant", "你好！")
    db.execute.return_value.scalars.return_value.all.return_value = [msg1, msg2]

    result = get_session_messages(db, "sess-1", "user-1")
    assert len(result) == 2
    assert result[0].role == "user"
    assert result[1].role == "assistant"


# delete_session_record

def test_delete_session_record_raises_when_not_found():
    from app.memory.long_term import delete_session_record
    db = MagicMock()
    db.get.return_value = None
    with pytest.raises(ValueError):
        delete_session_record(db, "sess-x", "user-1")
    db.delete.assert_not_called()


def test_delete_session_record_raises_when_wrong_user():
    from app.memory.long_term import delete_session_record
    db = MagicMock()
    sess = _make_session("sess-1", "other-user", datetime.now(timezone.utc))
    db.get.return_value = sess
    with pytest.raises(ValueError):
        delete_session_record(db, "sess-1", "user-1")
    db.delete.assert_not_called()


def test_delete_session_record_ok():
    from app.memory.long_term import delete_session_record
    db = MagicMock()
    sess = _make_session("sess-1", "user-1", datetime.now(timezone.utc))
    db.get.return_value = sess

    result = delete_session_record(db, "sess-1", "user-1")
    assert result is True
    db.delete.assert_called_once_with(sess)
    db.commit.assert_called_once()
