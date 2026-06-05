"""
/chat 接口:接收一次对话请求,以 SSE 形式流式返回 agent 事件。

事件类型(SSE event 字段):
- meta     :一次性元信息(命中来源、是否触发压缩、token 等)
- thought  :思考流(深度思考模式才会出现)
- answer   :答复流
- done     :收尾(assistant_message_id)
- error    :异常(同时关闭流)
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session as OrmSession
from sse_starlette.sse import EventSourceResponse

from app.core.guards import (
    MAX_MESSAGE_CHARS,
    MAX_SESSION_ID_CHARS,
    MAX_SKILL_NAME_CHARS,
    MAX_USER_ID_CHARS,
    SESSION_ID_RE,
)
from app.core.agent import AgentRequest, astream_agent_response
from app.db.session import get_db
from app.memory.long_term import (
    delete_session_history,
    get_session_messages,
    list_sessions,
)
from app.memory.short_term import ShortTermMemory
from app.rag.ragflow_client import RagflowClient
from app.skills.registry import get_skill

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])
_active_sessions: defaultdict[str, int] = defaultdict(int)


class ChatIn(BaseModel):
    """/chat 请求体。"""

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SESSION_ID_CHARS,
        description="会话 id,由前端生成(uuid)",
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=MAX_MESSAGE_CHARS,
        description="本轮用户消息",
    )
    user_id: str | None = Field(
        None,
        max_length=MAX_USER_ID_CHARS,
        description="可选业务用户标识,用于长期记忆召回",
    )
    skill: str | None = Field(
        None,
        max_length=MAX_SKILL_NAME_CHARS,
        description="选中的 skill name(可由前端 / 自动补全填入)",
    )
    thinking: bool = Field(False, description="是否开启深度思考模式")

    @field_validator("session_id")
    @classmethod
    def _valid_session_id(cls, v: str) -> str:
        v = v.strip()
        if not SESSION_ID_RE.fullmatch(v):
            raise ValueError("session_id 只能包含字母、数字、下划线、点、冒号或连字符")
        return v

    @field_validator("message")
    @classmethod
    def _valid_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message 不能为空")
        return v

    @field_validator("user_id", "skill")
    @classmethod
    def _strip_optional(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


@router.post("/chat")
async def chat_sse(payload: ChatIn, db: OrmSession = Depends(get_db)) -> EventSourceResponse:
    """流式对话接口。"""
    if payload.skill and get_skill(db, payload.skill) is None:
        raise HTTPException(status_code=400, detail=f"未知 skill: {payload.skill}")

    if _active_sessions[payload.session_id] >= 1:
        raise HTTPException(status_code=429, detail="同一 session 已有请求处理中,请稍后再试")

    short_term = ShortTermMemory(payload.session_id)
    ragflow = RagflowClient()
    req = AgentRequest(
        session_id=payload.session_id,
        message=payload.message,
        user_id=payload.user_id,
        skill_name=payload.skill,
        thinking=payload.thinking,
    )

    async def event_gen() -> AsyncIterator[dict]:
        """把 agent 事件转为 SSE 事件字典。"""
        _active_sessions[payload.session_id] += 1
        try:
            async for ev_type, ev_data in astream_agent_response(
                req, db=db, short_term=short_term, ragflow=ragflow
            ):
                # text 类型(thought/answer)直接以字符串发,前端拼接成块
                if ev_type in ("thought", "answer"):
                    yield {"event": ev_type, "data": ev_data}
                else:
                    yield {"event": ev_type, "data": json.dumps(ev_data, ensure_ascii=False)}
        except Exception as e:  # noqa: BLE001
            logger.exception("chat 流式出错")
            yield {"event": "error", "data": json.dumps({"error": str(e)}, ensure_ascii=False)}
        finally:
            _active_sessions[payload.session_id] -= 1
            if _active_sessions[payload.session_id] <= 0:
                _active_sessions.pop(payload.session_id, None)

    return EventSourceResponse(event_gen())


class SessionItem(BaseModel):
    """会话列表条目(供侧边栏)。"""

    session_id: str
    title: str
    message_count: int
    updated_at: str | None = None


@router.get("/sessions", response_model=list[SessionItem])
def list_user_sessions(
    user_id: str = Query(..., min_length=1, max_length=MAX_USER_ID_CHARS),
    db: OrmSession = Depends(get_db),
) -> list[SessionItem]:
    """列出某用户的全部会话(按最近活跃倒序),供前端会话切换。"""
    rows = list_sessions(db, user_id.strip())
    return [SessionItem(**r) for r in rows]


class HistoryMessage(BaseModel):
    """历史消息条目(供右上角「历史聊天记录」查看)。"""

    role: str
    content: str
    thinking: str | None = None
    skill: str | None = None
    created_at: str | None = None


@router.get("/sessions/{session_id}/messages", response_model=list[HistoryMessage])
def session_history(session_id: str, db: OrmSession = Depends(get_db)) -> list[HistoryMessage]:
    """读取某会话归档的全部历史消息(PG messages 表,按时间正序)。"""
    session_id = session_id.strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="非法 session_id")
    rows = get_session_messages(db, session_id)
    return [
        HistoryMessage(
            role=m.role,
            content=m.content,
            thinking=m.thinking,
            skill=m.skill_name,
            created_at=m.created_at.isoformat() if m.created_at else None,
        )
        for m in rows
    ]


@router.delete("/sessions/{session_id}/messages")
def delete_session(session_id: str, db: OrmSession = Depends(get_db)) -> dict:
    """删除某会话的历史聊天记录:PG 归档消息 + 滚动摘要 + Redis 短期窗口。

    保留 session_id 本身,删除后可继续在同一会话对话。
    """
    session_id = session_id.strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="非法 session_id")

    if _active_sessions.get(session_id, 0) >= 1:
        raise HTTPException(status_code=409, detail="该 session 正在对话中,请稍后再删")

    deleted = delete_session_history(db, session_id)
    try:
        ShortTermMemory(session_id).clear()
    except Exception:  # noqa: BLE001
        logger.warning("清空 Redis 短期记忆失败 session=%s", session_id, exc_info=True)
    return {"deleted": deleted}
