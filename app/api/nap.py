"""
NoDeskClaw Agent Protocol (NAP) v1.0 接入层。

实现平台要求的三个端点：
  GET  /meta    — 返回 Agent 元数据，平台 sync 时拉取
  POST /stream  — 流式对话（SSE），event: message / done / error
  （GET /health 在 main.py 中实现，格式已符合 NAP 规范）

与内部 /chat 的差异：
  - 入参格式遵循 NAP 协议（messages 数组 + request_id / session_id 等）
  - SSE 事件格式使用 NAP 规范（event: message，data 为纯文本；而非 JSON）
  - 内部 meta/thought 事件不透传给平台，思考内容被静默丢弃
  - 可选 Bearer token 鉴权（NAP_API_KEY 配置后启用）
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import AsyncIterator, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.core.agent import AgentRequest, astream_agent_response
from app.core.guards import MAX_MESSAGE_CHARS, MAX_SESSION_ID_CHARS, MAX_USER_ID_CHARS
from app.db.session import get_db
from app.memory.short_term import ShortTermMemory
from app.rag.ragflow_client import RagflowClient

logger = logging.getLogger(__name__)
router = APIRouter(tags=["nap"])

# 与 /chat 共享同一 session 并发计数器，防止同一 session 并发请求
_active_sessions: defaultdict[str, int] = defaultdict(int)


# ─────────────────────────────────────────────────────────
# Pydantic 数据模型
# ─────────────────────────────────────────────────────────

class NapMessage(BaseModel):
    """NAP 协议中 messages 数组的单条消息。"""
    role: str          # user / assistant / system / tool
    content: str = ""


class NapStreamRequest(BaseModel):
    """POST /stream 的请求体，遵循 NAP v1.0 规范。"""

    protocol_version: str = Field("1.0", description="协议版本，固定 '1.0'")
    request_id: str = Field(..., description="本次请求唯一 UUID")
    session_id: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SESSION_ID_CHARS,
        description="多轮会话 ID，同一对话保持一致",
    )
    user_id: Optional[str] = Field(
        None,
        max_length=MAX_USER_ID_CHARS,
        description="平台用户 ID",
    )
    organization_id: Optional[str] = Field(None, description="平台组织 ID（可选）")
    messages: List[NapMessage] = Field(..., min_length=1, description="完整对话历史")
    metadata: Optional[dict] = Field(None, description="附加元信息，如 source=nodeskclaw")


# ─────────────────────────────────────────────────────────
# 鉴权依赖（NAP_API_KEY 留空则跳过）
# ─────────────────────────────────────────────────────────

def _require_api_key(authorization: Optional[str] = Header(None, alias="Authorization")) -> None:
    """校验 Bearer token；NAP_API_KEY 未配置时直接放行。"""
    settings = get_settings()
    expected = settings.NAP_API_KEY
    if not expected:
        return  # 未配置密钥，不鉴权

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer <api_key>")

    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="API Key 不匹配")


# ─────────────────────────────────────────────────────────
# GET /meta
# ─────────────────────────────────────────────────────────

@router.get("/meta")
def nap_meta(_: None = Depends(_require_api_key)) -> dict:
    """
    返回 Agent 元数据。
    平台执行 sync 时自动拉取，capabilities 与 description 写入卡片展示。
    """
    settings = get_settings()
    return {
        "protocol_version": "1.0",
        "agent_id": "mom-agent",
        "name": settings.NAP_AGENT_NAME,
        "description": settings.NAP_AGENT_DESCRIPTION,
        "version": "1.0.0",
        "runtime": "langgraph",
        "capabilities": [
            "生产计划查询",
            "质量管理分析",
            "设备管理问答",
            "物料追踪",
            "MOM 业务知识问答",
        ],
    }


# ─────────────────────────────────────────────────────────
# POST /stream
# ─────────────────────────────────────────────────────────

@router.post("/stream")
async def nap_stream(
    payload: NapStreamRequest,
    db: OrmSession = Depends(get_db),
    _: None = Depends(_require_api_key),
) -> StreamingResponse:
    """
    NAP 流式聊天端点（SSE）。

    事件格式：
      event: message  — 纯文本回复片段，平台逐块拼接
      event: done     — 结束信号（data: complete）
      event: error    — 错误信息（data: JSON {"code": ..., "message": ...}）
    """
    # 取 messages 中最后一条 user 消息
    user_messages = [m for m in payload.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="messages 中缺少 role=user 的消息")

    last_user_message = user_messages[-1].content.strip()
    if not last_user_message:
        raise HTTPException(status_code=400, detail="最后一条 user 消息内容为空")

    if len(last_user_message) > MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail=f"消息长度超过上限 {MAX_MESSAGE_CHARS}")

    if _active_sessions[payload.session_id] >= 1:
        raise HTTPException(status_code=429, detail="同一 session 已有请求处理中，请稍后再试")

    short_term = ShortTermMemory(payload.session_id)
    ragflow = RagflowClient()
    req = AgentRequest(
        session_id=payload.session_id,
        message=last_user_message,
        user_id=payload.user_id,
        skill_name=None,   # NAP 协议暂不传递 skill 名，使用默认
        thinking=False,    # NAP 协议暂不传递 thinking 开关
    )

    async def _generate() -> AsyncIterator[str]:
        """将内部 agent 事件转换为 NAP SSE 格式的字节流。"""
        _active_sessions[payload.session_id] += 1
        try:
            async for ev_type, ev_data in astream_agent_response(
                req, db=db, short_term=short_term, ragflow=ragflow
            ):
                if ev_type == "answer":
                    # NAP 要求 message 事件的 data 是纯文本
                    yield f"event: message\ndata: {ev_data}\n\n"
                elif ev_type == "done":
                    # 完成信号
                    yield "event: done\ndata: complete\n\n"
                elif ev_type == "error":
                    # 错误信号，data 为 JSON
                    msg = ev_data.get("error", str(ev_data)) if isinstance(ev_data, dict) else str(ev_data)
                    yield f"event: error\ndata: {json.dumps({'code': 'AGENT_ERROR', 'message': msg}, ensure_ascii=False)}\n\n"
                # meta / thought 事件静默丢弃，不透传给平台
        except Exception as exc:  # noqa: BLE001
            logger.exception("NAP /stream 出错 request_id=%s", payload.request_id)
            payload_json = json.dumps(
                {"code": "INTERNAL_ERROR", "message": str(exc)}, ensure_ascii=False
            )
            yield f"event: error\ndata: {payload_json}\n\n"
        finally:
            _active_sessions[payload.session_id] -= 1
            if _active_sessions[payload.session_id] <= 0:
                _active_sessions.pop(payload.session_id, None)

    return StreamingResponse(_generate(), media_type="text/event-stream")
