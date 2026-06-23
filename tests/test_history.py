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
