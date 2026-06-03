"""单测:/chat 请求体输入约束。"""

import os

import pytest
from pydantic import ValidationError

os.environ.setdefault("VLLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("VLLM_MODEL", "qwen")
os.environ.setdefault("RAGFLOW_BASE_URL", "http://ragflow.local")
os.environ.setdefault("RAGFLOW_API_KEY", "k")
os.environ.setdefault("RAGFLOW_DATASET_IDS", "ds1,ds2")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api.chat import ChatIn


def test_chat_in_strips_message_and_optional_fields():
    payload = ChatIn(session_id="abc-123", message="  你好  ", user_id=" u1 ", skill=" s1 ")
    assert payload.message == "你好"
    assert payload.user_id == "u1"
    assert payload.skill == "s1"


def test_chat_in_rejects_blank_message():
    with pytest.raises(ValidationError):
        ChatIn(session_id="abc-123", message="   ")


def test_chat_in_rejects_bad_session_id():
    with pytest.raises(ValidationError):
        ChatIn(session_id="../bad", message="你好")
