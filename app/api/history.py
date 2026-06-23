"""
历史会话 API。

GET  /history/sessions                       — 列出指定用户的会话（分页）
GET  /history/sessions/{session_id}/messages  — 获取某会话的完整消息
DELETE /history/sessions/{session_id}         — 删除会话（DB + Redis）
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session as OrmSession

from app.db.session import get_db
from app.memory.long_term import (
    delete_session_record,
    get_session_messages,
    list_sessions,
)
from app.memory.short_term import ShortTermMemory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/history", tags=["history"])


class SessionOut(BaseModel):
    """会话列表条目。"""
    session_id: str
    title: str
    updated_at: str
    message_count: int


class MessageOut(BaseModel):
    """消息条目。"""
    id: int
    role: str
    content: str
    thinking: Optional[str] = None
    created_at: str


class DeleteResult(BaseModel):
    deleted: str


@router.get("/sessions", response_model=List[SessionOut])
def api_list_sessions(
    user_id: str = Query("", description="用户 ID，为空时返回空列表"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: OrmSession = Depends(get_db),
) -> List[SessionOut]:
    """列出指定用户的历史会话，按最近活跃时间降序。"""
    rows = list_sessions(db, user_id, limit=limit, offset=offset)
    return [SessionOut(**r) for r in rows]


@router.get("/sessions/{session_id}/messages", response_model=List[MessageOut])
def api_get_messages(
    session_id: str,
    user_id: str = Query(..., description="用户 ID，用于隔离校验"),
    db: OrmSession = Depends(get_db),
) -> List[MessageOut]:
    """获取指定会话的全部 user/assistant 消息。"""
    try:
        msgs = get_session_messages(db, session_id, user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")

    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            thinking=m.thinking,
            created_at=m.created_at.isoformat(),
        )
        for m in msgs
    ]


@router.delete("/sessions/{session_id}", response_model=DeleteResult)
def api_delete_session(
    session_id: str,
    user_id: str = Query(..., description="用户 ID，用于隔离校验"),
    db: OrmSession = Depends(get_db),
) -> DeleteResult:
    """删除会话（DB CASCADE + Redis 清除）。"""
    try:
        delete_session_record(db, session_id, user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 清除 Redis 短期记忆；失败只记日志不影响响应
    try:
        ShortTermMemory(session_id).clear()
    except Exception:
        logger.warning("清除 Redis 短期记忆失败 session=%s", session_id, exc_info=True)

    return DeleteResult(deleted=session_id)
