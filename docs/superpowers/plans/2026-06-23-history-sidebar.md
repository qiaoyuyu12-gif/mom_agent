# History Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在聊天页面左侧增加会话历史侧边栏，按 user_id 隔离，点击历史可恢复并继续聊天，每条会话可单独删除。

**Architecture:** 新增 `app/api/history.py` 提供三个 REST 端点；历史查询逻辑作为辅助函数追加到 `app/memory/long_term.py`；`ShortTermMemory` 增加 `clear()` 方法供删除时清除 Redis；前端重建为含侧边栏的单页 `web/index.html`。

**Tech Stack:** FastAPI, SQLAlchemy ORM, PostgreSQL (现有), Redis (现有), 纯 HTML/CSS/JS（无构建工具）

---

## File Structure

| 文件 | 类型 | 职责 |
|------|------|------|
| `app/memory/short_term.py` | 修改 | 新增 `ShortTermMemory.clear()` |
| `app/memory/long_term.py` | 修改 | 新增 `list_sessions` / `get_session_messages` / `delete_session_record` |
| `app/api/history.py` | 新建 | `/history` 路由：GET sessions, GET messages, DELETE session |
| `app/main.py` | 修改 | 注册 `history_router` |
| `web/index.html` | 新建 | 含历史侧边栏的完整聊天 UI |
| `tests/test_history.py` | 新建 | Tasks 1-2 的单元测试（不依赖外部服务） |

---

## Task 1: ShortTermMemory.clear()

**Files:**
- Modify: `app/memory/short_term.py`
- Create: `tests/test_history.py`

- [ ] **Step 1: 创建 `tests/test_history.py`，写两个失败测试**

```python
# tests/test_history.py
"""历史功能单元测试（不依赖外部服务）。"""
import logging
from unittest.mock import MagicMock


# ── Task 1: ShortTermMemory.clear() ──────────────────────────

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
    assert "test-sess" in caplog.text
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd mom_agent
pytest tests/test_history.py -v
```

期望输出：`FAILED ... ImportError` 或 `AttributeError: 'ShortTermMemory' object has no attribute 'clear'`

- [ ] **Step 3: 在 `short_term.py` 顶部加 logger，末尾加 `clear()` 方法**

在 `short_term.py` 的 `import` 区末尾加：
```python
import logging
logger = logging.getLogger(__name__)
```

在 `ShortTermMemory` 类末尾追加（在最后一个方法之后）：
```python
    def clear(self) -> None:
        """删除该 session 的所有 Redis 键。Redis 不可达时只记日志，不抛异常。"""
        try:
            pipe = self._cli.pipeline()
            pipe.delete(self._msg_key)
            pipe.delete(self._sum_key)
            pipe.execute()
        except Exception:
            logger.warning("ShortTermMemory.clear 失败 session=%s", self.session_id)
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
pytest tests/test_history.py -v
```

期望输出：`2 passed`

- [ ] **Step 5: 提交**

```bash
git add app/memory/short_term.py tests/test_history.py
git commit -m "feat: add ShortTermMemory.clear() + tests"
```

---

## Task 2: long_term.py 历史辅助函数

**Files:**
- Modify: `app/memory/long_term.py`
- Modify: `tests/test_history.py`（追加测试）

- [ ] **Step 1: 在 `tests/test_history.py` 末尾追加失败测试**

```python
# ── Task 2: long_term.py 辅助函数 ────────────────────────────

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
    """user_id 为空时直接返回空列表，不查 DB。"""
    from app.memory.long_term import list_sessions
    db = MagicMock()
    assert list_sessions(db, "") == []
    db.execute.assert_not_called()


def test_list_sessions_returns_formatted_results():
    """正常情况：返回含 session_id / title / updated_at / message_count 的列表。"""
    from app.memory.long_term import list_sessions

    db = MagicMock()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    sess = _make_session("sess-1", "user-1", now)

    exec1 = MagicMock()
    exec1.scalars.return_value.all.return_value = [sess]
    exec2 = MagicMock()
    exec2.scalar.return_value = "如何重启网关？"
    exec3 = MagicMock()
    exec3.scalar.return_value = 4
    db.execute.side_effect = [exec1, exec2, exec3]

    result = list_sessions(db, "user-1")

    assert len(result) == 1
    assert result[0]["session_id"] == "sess-1"
    assert result[0]["title"] == "如何重启网关？"
    assert result[0]["message_count"] == 4


def test_list_sessions_truncates_title_to_30_chars():
    """第一条消息超 30 字时截断。"""
    from app.memory.long_term import list_sessions

    db = MagicMock()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    sess = _make_session("sess-2", "user-1", now)

    exec1 = MagicMock()
    exec1.scalars.return_value.all.return_value = [sess]
    exec2 = MagicMock()
    exec2.scalar.return_value = "A" * 50
    exec3 = MagicMock()
    exec3.scalar.return_value = 2
    db.execute.side_effect = [exec1, exec2, exec3]

    result = list_sessions(db, "user-1")
    assert len(result[0]["title"]) == 30


def test_list_sessions_fallback_title_when_no_user_msg():
    """无 user 消息时标题回退为'新对话'。"""
    from app.memory.long_term import list_sessions

    db = MagicMock()
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    sess = _make_session("sess-3", "user-1", now)

    exec1 = MagicMock()
    exec1.scalars.return_value.all.return_value = [sess]
    exec2 = MagicMock()
    exec2.scalar.return_value = None   # 无用户消息
    exec3 = MagicMock()
    exec3.scalar.return_value = 0
    db.execute.side_effect = [exec1, exec2, exec3]

    result = list_sessions(db, "user-1")
    assert result[0]["title"] == "新对话"


# get_session_messages

def test_get_session_messages_raises_when_not_found():
    """session 不存在时抛 ValueError。"""
    from app.memory.long_term import get_session_messages
    db = MagicMock()
    db.get.return_value = None
    try:
        get_session_messages(db, "sess-x", "user-1")
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_get_session_messages_raises_when_wrong_user():
    """session 存在但 user_id 不匹配时抛 ValueError。"""
    from app.memory.long_term import get_session_messages
    db = MagicMock()
    sess = _make_session("sess-1", "other-user", datetime.now(timezone.utc))
    db.get.return_value = sess
    try:
        get_session_messages(db, "sess-1", "user-1")
        assert False, "应抛 ValueError"
    except ValueError:
        pass


def test_get_session_messages_returns_user_and_assistant_messages():
    """正常情况：返回 user + assistant 消息列表。"""
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
    """session 不存在时抛 ValueError，不调用 db.delete。"""
    from app.memory.long_term import delete_session_record
    db = MagicMock()
    db.get.return_value = None
    try:
        delete_session_record(db, "sess-x", "user-1")
        assert False, "应抛 ValueError"
    except ValueError:
        pass
    db.delete.assert_not_called()


def test_delete_session_record_ok():
    """session 存在且 user_id 匹配时删除并返回 True。"""
    from app.memory.long_term import delete_session_record
    db = MagicMock()
    sess = _make_session("sess-1", "user-1", datetime.now(timezone.utc))
    db.get.return_value = sess

    result = delete_session_record(db, "sess-1", "user-1")

    assert result is True
    db.delete.assert_called_once_with(sess)
    db.commit.assert_called_once()
```

- [ ] **Step 2: 运行，确认新增的测试全部失败**

```bash
pytest tests/test_history.py -v
```

期望：前 2 个 pass（Task 1），新增的全部 FAILED（ImportError: cannot import name...）

- [ ] **Step 3: 在 `app/memory/long_term.py` 末尾追加三个辅助函数**

在文件最末尾（`format_facts_for_prompt` 函数之后）追加：

```python
# ─── 历史会话查询 ──────────────────────────────────

def list_sessions(
    db: OrmSession,
    user_id: str,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """
    按 user_id 列出会话，按 updated_at 降序。
    返回含 session_id / title / updated_at / message_count 的 dict 列表。
    user_id 为空时返回空列表。
    """
    if not user_id:
        return []

    sessions = list(
        db.execute(
            select(SessionRow)
            .where(SessionRow.user_id == user_id)
            .order_by(desc(SessionRow.updated_at))
            .limit(limit)
            .offset(offset)
        ).scalars().all()
    )

    result = []
    for s in sessions:
        first_content = db.execute(
            select(Message.content)
            .where(Message.session_id == s.id, Message.role == "user")
            .order_by(Message.created_at)
            .limit(1)
        ).scalar()

        msg_count = db.execute(
            select(func.count())
            .select_from(Message)
            .where(
                Message.session_id == s.id,
                Message.role.in_(["user", "assistant"]),
            )
        ).scalar() or 0

        result.append({
            "session_id": s.id,
            "title": first_content[:30] if first_content else "新对话",
            "updated_at": s.updated_at.isoformat(),
            "message_count": int(msg_count),
        })

    return result


def get_session_messages(
    db: OrmSession,
    session_id: str,
    user_id: str,
) -> list[Message]:
    """
    返回指定会话的 user/assistant 消息列表（按 created_at 升序）。
    若 session 不存在或 user_id 不匹配，抛 ValueError。
    """
    s = db.get(SessionRow, session_id)
    if s is None or s.user_id != user_id:
        raise ValueError(f"session {session_id!r} not found for user {user_id!r}")

    return list(
        db.execute(
            select(Message)
            .where(
                Message.session_id == session_id,
                Message.role.in_(["user", "assistant"]),
            )
            .order_by(Message.created_at)
        ).scalars().all()
    )


def delete_session_record(
    db: OrmSession,
    session_id: str,
    user_id: str,
) -> bool:
    """
    删除指定会话（DB 层 CASCADE 自动清除 messages / session_summaries）。
    若 session 不存在或 user_id 不匹配，抛 ValueError。
    """
    s = db.get(SessionRow, session_id)
    if s is None or s.user_id != user_id:
        raise ValueError(f"session {session_id!r} not found for user {user_id!r}")

    db.delete(s)
    db.commit()
    return True
```

- [ ] **Step 4: 运行全部测试，确认全部通过**

```bash
pytest tests/test_history.py -v
```

期望：`12 passed`

- [ ] **Step 5: 提交**

```bash
git add app/memory/long_term.py tests/test_history.py
git commit -m "feat: add list_sessions / get_session_messages / delete_session_record"
```

---

## Task 3: history.py API 路由 + main.py 注册

**Files:**
- Create: `app/api/history.py`
- Modify: `app/main.py`

- [ ] **Step 1: 创建 `app/api/history.py`**

```python
"""
历史会话 API。

GET  /history/sessions                      — 列出指定用户的会话（分页）
GET  /history/sessions/{session_id}/messages — 获取某会话的完整消息
DELETE /history/sessions/{session_id}        — 删除会话（DB + Redis）
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
        logger.warning("清除 Redis 短期记忆失败 session=%s", session_id)

    return DeleteResult(deleted=session_id)
```

- [ ] **Step 2: 在 `app/main.py` 中注册 history_router**

在 `main.py` 的 import 区找到：
```python
from app.api.nap import router as nap_router
```
改为：
```python
from app.api.history import router as history_router
from app.api.nap import router as nap_router
```

在 `create_app()` 函数中，找到：
```python
    app.include_router(nap_router)
```
改为：
```python
    app.include_router(history_router)
    app.include_router(nap_router)
```

- [ ] **Step 3: 启动服务器，用 curl 验证三个端点**

```bash
# 启动（保持运行）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8090

# 另开终端测试（user_id 为空应返回 []）
curl "http://localhost:8090/history/sessions?user_id=" 
# 期望: []

# 不存在的 session 应返回 404
curl "http://localhost:8090/history/sessions/nonexistent/messages?user_id=u1"
# 期望: {"detail":"会话不存在"}

# 删除不存在的 session 应返回 404
curl -X DELETE "http://localhost:8090/history/sessions/nonexistent?user_id=u1"
# 期望: {"detail":"会话不存在"}
```

- [ ] **Step 4: 提交**

```bash
git add app/api/history.py app/main.py
git commit -m "feat: add history API (list/get/delete sessions)"
```

---

## Task 4: web/index.html 聊天前端

**Files:**
- Create: `web/index.html`

- [ ] **Step 1: 创建 `web/` 目录和 `index.html`**

```bash
mkdir -p mom_agent/web
```

创建 `web/index.html`，完整内容如下：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MOM 智能问答</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; height: 100vh; display: flex; overflow: hidden; background: #f5f5f5; }

    /* ── 侧边栏 ── */
    #sidebar {
      width: 260px; min-width: 260px;
      background: #1a1a2e; color: #eee;
      display: flex; flex-direction: column;
    }
    #sidebar-header { padding: 14px 12px; border-bottom: 1px solid #2e2e4e; }
    #new-chat-btn {
      width: 100%; padding: 9px 12px;
      background: #16213e; border: 1px solid #3a3a5e;
      color: #ddd; border-radius: 6px; cursor: pointer; font-size: 13px;
      text-align: left;
    }
    #new-chat-btn:hover { background: #0f3460; color: #fff; }
    #session-list { flex: 1; overflow-y: auto; padding: 6px; }
    .session-item {
      position: relative; padding: 9px 10px;
      border-radius: 6px; cursor: pointer; margin-bottom: 2px;
    }
    .session-item:hover { background: #2a2a4e; }
    .session-item.active { background: #0f3460; }
    .session-title { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding-right: 22px; }
    .session-meta { font-size: 11px; color: #888; margin-top: 2px; }
    .delete-btn {
      display: none; position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
      background: transparent; border: none; color: #f87; cursor: pointer; font-size: 14px; padding: 2px 4px;
    }
    .session-item:hover .delete-btn { display: block; }
    #load-more {
      padding: 8px; text-align: center; font-size: 12px; color: #666; cursor: pointer; display: none;
    }
    #load-more:hover { color: #aaa; }

    /* ── 聊天区 ── */
    #chat-area { flex: 1; display: flex; flex-direction: column; min-width: 0; background: #fff; }
    #chat-header { padding: 13px 20px; border-bottom: 1px solid #e5e7eb; font-size: 15px; font-weight: 600; color: #111; flex-shrink: 0; }
    #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 14px; }
    .msg { max-width: 74%; }
    .msg.user { align-self: flex-end; }
    .msg.assistant { align-self: flex-start; }
    .msg-bubble { padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; }
    .msg.user .msg-bubble { background: #0f3460; color: #fff; border-bottom-right-radius: 2px; }
    .msg.assistant .msg-bubble { background: #f1f5f9; color: #111; border-bottom-left-radius: 2px; }
    .think-block {
      font-size: 12px; color: #888; background: #f8f8f8;
      border-left: 3px solid #ddd; padding: 6px 10px; margin-bottom: 6px;
      border-radius: 4px; cursor: pointer; user-select: none;
    }
    .think-content { display: none; margin-top: 4px; white-space: pre-wrap; color: #666; }
    .think-block.open .think-content { display: block; }

    /* ── 输入区 ── */
    #input-area { padding: 14px 20px; border-top: 1px solid #e5e7eb; background: #fff; flex-shrink: 0; }
    #toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
    #skill-tag {
      display: none; align-items: center; gap: 4px;
      background: #e0f2fe; color: #0369a1;
      padding: 2px 8px; border-radius: 4px; font-size: 12px;
    }
    #skill-tag .rm { cursor: pointer; color: #666; margin-left: 2px; }
    #upload-label {
      font-size: 12px; color: #888; cursor: pointer;
      padding: 3px 8px; border: 1px dashed #d1d5db; border-radius: 4px;
    }
    #upload-label:hover { color: #555; }
    #upload-input { display: none; }
    #input-row { display: flex; gap: 8px; align-items: flex-end; }
    #wrap { position: relative; flex: 1; }
    #user-input {
      width: 100%; padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 8px;
      font-size: 14px; font-family: inherit; resize: none; min-height: 40px; max-height: 160px; overflow-y: auto;
    }
    #user-input:focus { outline: none; border-color: #0f3460; }
    #thinking-lbl { display: flex; align-items: center; gap: 4px; font-size: 13px; color: #555; white-space: nowrap; cursor: pointer; }
    #send-btn {
      padding: 9px 20px; background: #0f3460; color: #fff;
      border: none; border-radius: 8px; font-size: 14px; cursor: pointer; white-space: nowrap;
    }
    #send-btn:hover:not(:disabled) { background: #16213e; }
    #send-btn:disabled { opacity: 0.55; cursor: not-allowed; }

    /* ── Skill 下拉 ── */
    #skill-dd {
      display: none; position: absolute; bottom: calc(100% + 4px); left: 0;
      background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
      box-shadow: 0 4px 12px rgba(0,0,0,.1); max-height: 200px; overflow-y: auto; z-index: 100; min-width: 240px;
    }
    .skill-opt { padding: 8px 12px; cursor: pointer; font-size: 13px; }
    .skill-opt:hover { background: #f1f5f9; }
    .skill-opt-name { font-weight: 500; }
    .skill-opt-desc { font-size: 11px; color: #888; margin-top: 1px; }
  </style>
</head>
<body>

<aside id="sidebar">
  <div id="sidebar-header">
    <button id="new-chat-btn">＋ 新对话</button>
  </div>
  <div id="session-list"></div>
  <div id="load-more">加载更多…</div>
</aside>

<div id="chat-area">
  <div id="chat-header">MOM 智能问答助手</div>
  <div id="messages"></div>
  <div id="input-area">
    <div id="toolbar">
      <div id="skill-tag">
        <span id="skill-name"></span>
        <span class="rm" onclick="clearSkill()">×</span>
      </div>
      <label id="upload-label" for="upload-input">上传 Skill</label>
      <input id="upload-input" type="file" accept=".md">
    </div>
    <div id="input-row">
      <div id="wrap">
        <textarea id="user-input" rows="1" placeholder="输入消息… (/ 选择 Skill，Shift+Enter 换行)"></textarea>
        <div id="skill-dd"></div>
      </div>
      <label id="thinking-lbl"><input type="checkbox" id="thinking-cb"> 深度思考</label>
      <button id="send-btn">发送</button>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════
// 状态
// ═══════════════════════════════════════════════
const S = {
  userId: null,
  sessionId: null,
  skills: [],
  sessionsOffset: 0,
  sessionsLimit: 20,
  hasMore: false,
  sending: false,
  answerEl: null,   // 当前流式回复的气泡 DOM 节点
};

// ═══════════════════════════════════════════════
// 启动
// ═══════════════════════════════════════════════
function init() {
  S.userId    = localStorage.getItem('mom_user_id')    || (localStorage.setItem('mom_user_id',    crypto.randomUUID()), localStorage.getItem('mom_user_id'));
  S.sessionId = localStorage.getItem('mom_session_id') || (localStorage.setItem('mom_session_id', crypto.randomUUID()), localStorage.getItem('mom_session_id'));

  loadSessions();
  loadSkills();
  setupInputEvents();

  document.getElementById('new-chat-btn').addEventListener('click', startNewChat);
  document.getElementById('load-more').addEventListener('click', () => loadSessions(false));
  document.getElementById('send-btn').addEventListener('click', sendMessage);
  document.getElementById('upload-input').addEventListener('change', uploadSkill);
}

// ═══════════════════════════════════════════════
// 会话侧边栏
// ═══════════════════════════════════════════════
async function loadSessions(reset = true) {
  if (reset) { S.sessionsOffset = 0; }
  const res = await fetch(`/history/sessions?user_id=${enc(S.userId)}&limit=${S.sessionsLimit}&offset=${S.sessionsOffset}`);
  if (!res.ok) return;
  const data = await res.json();
  if (reset) {
    renderSidebar(data);
  } else {
    appendSidebar(data);
  }
  S.sessionsOffset += data.length;
  S.hasMore = data.length === S.sessionsLimit;
  document.getElementById('load-more').style.display = S.hasMore ? 'block' : 'none';
}

function renderSidebar(sessions) {
  const list = document.getElementById('session-list');
  list.innerHTML = '';
  sessions.forEach(s => list.appendChild(makeSessionItem(s)));
}

function appendSidebar(sessions) {
  const list = document.getElementById('session-list');
  sessions.forEach(s => list.appendChild(makeSessionItem(s)));
}

function makeSessionItem(s) {
  const div = document.createElement('div');
  div.className = 'session-item' + (s.session_id === S.sessionId ? ' active' : '');
  div.dataset.sid = s.session_id;
  div.innerHTML = `
    <div class="session-title">${esc(s.title)}</div>
    <div class="session-meta">${relTime(s.updated_at)} · ${s.message_count} 条</div>
    <button class="delete-btn" title="删除此对话">🗑</button>`;
  div.addEventListener('click', e => { if (!e.target.closest('.delete-btn')) switchSession(s.session_id); });
  div.querySelector('.delete-btn').addEventListener('click', e => { e.stopPropagation(); confirmDelete(s.session_id); });
  return div;
}

function highlightSession(sid) {
  document.querySelectorAll('.session-item').forEach(el => el.classList.toggle('active', el.dataset.sid === sid));
}

async function switchSession(sessionId) {
  if (sessionId === S.sessionId) return;
  S.sessionId = sessionId;
  localStorage.setItem('mom_session_id', sessionId);
  highlightSession(sessionId);

  const res = await fetch(`/history/sessions/${sessionId}/messages?user_id=${enc(S.userId)}`);
  if (!res.ok) return;
  const msgs = await res.json();

  const container = document.getElementById('messages');
  container.innerHTML = '';
  msgs.forEach(m => appendBubble(m.role, m.content, m.thinking));
  scrollToBottom();
}

function startNewChat() {
  S.sessionId = crypto.randomUUID();
  localStorage.setItem('mom_session_id', S.sessionId);
  document.getElementById('messages').innerHTML = '';
  // 把新会话加到侧边栏顶部（实际数据写入在第一次发消息后 ensure_session 完成）
  loadSessions();
}

async function confirmDelete(sessionId) {
  if (!confirm('确认删除这条对话？')) return;
  const res = await fetch(`/history/sessions/${sessionId}?user_id=${enc(S.userId)}`, { method: 'DELETE' });
  if (!res.ok) { alert('删除失败'); return; }
  if (sessionId === S.sessionId) startNewChat();
  loadSessions();
}

// ═══════════════════════════════════════════════
// 消息渲染
// ═══════════════════════════════════════════════
function appendBubble(role, content, thinking) {
  const container = document.getElementById('messages');
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;

  if (thinking) {
    const think = document.createElement('div');
    think.className = 'think-block';
    think.innerHTML = `💭 思考过程 <div class="think-content">${esc(thinking)}</div>`;
    think.addEventListener('click', () => think.classList.toggle('open'));
    wrap.appendChild(think);
  }

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.textContent = content;
  wrap.appendChild(bubble);
  container.appendChild(wrap);
  scrollToBottom();
  return bubble;
}

function startStreamBubble() {
  const container = document.getElementById('messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  wrap.appendChild(bubble);
  container.appendChild(wrap);
  S.answerEl = bubble;
  S.thinkWrap = wrap;
  scrollToBottom();
}

function scrollToBottom() {
  const c = document.getElementById('messages');
  c.scrollTop = c.scrollHeight;
}

// ═══════════════════════════════════════════════
// 发送消息 + SSE
// ═══════════════════════════════════════════════
async function sendMessage() {
  if (S.sending) return;
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  if (!text) return;

  const thinking = document.getElementById('thinking-cb').checked;
  const skillEl  = document.getElementById('skill-name');
  const skill    = skillEl.textContent.trim() || null;

  input.value = '';
  input.style.height = 'auto';
  S.sending = true;
  document.getElementById('send-btn').disabled = true;

  appendBubble('user', text);
  startStreamBubble();

  let thinkBuf = '';
  let answerBuf = '';
  let thinkDiv = null;

  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: S.sessionId, user_id: S.userId, message: text, skill, thinking }),
    });

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();

      for (const chunk of parts) {
        let event = 'message', data = '';
        for (const line of chunk.split('\n')) {
          if (line.startsWith('event: ')) event = line.slice(7).trim();
          else if (line.startsWith('data: '))  data  = line.slice(6);
        }

        if (event === 'thought') {
          thinkBuf += data;
          if (!thinkDiv) {
            thinkDiv = document.createElement('div');
            thinkDiv.className = 'think-block';
            thinkDiv.innerHTML = '💭 思考过程 <div class="think-content"></div>';
            thinkDiv.addEventListener('click', () => thinkDiv.classList.toggle('open'));
            S.thinkWrap.insertBefore(thinkDiv, S.answerEl);
          }
          thinkDiv.querySelector('.think-content').textContent = thinkBuf;
        } else if (event === 'answer') {
          answerBuf += data;
          S.answerEl.textContent = answerBuf;
          scrollToBottom();
        } else if (event === 'error') {
          try { S.answerEl.textContent = '[错误] ' + JSON.parse(data).error; }
          catch { S.answerEl.textContent = '[错误] ' + data; }
        }
      }
    }
  } catch (e) {
    S.answerEl.textContent = '[网络错误] ' + e.message;
  } finally {
    S.sending = false;
    document.getElementById('send-btn').disabled = false;
    loadSessions(); // 刷新侧边栏排序
  }
}

// ═══════════════════════════════════════════════
// Skill 补全
// ═══════════════════════════════════════════════
async function loadSkills() {
  const res = await fetch('/skills');
  if (res.ok) S.skills = await res.json();
}

function setupInputEvents() {
  const input = document.getElementById('user-input');
  const dd    = document.getElementById('skill-dd');

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';

    const val = input.value;
    if (val.startsWith('/')) {
      const q = val.slice(1).toLowerCase();
      const hits = S.skills.filter(s => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q));
      if (hits.length) {
        dd.innerHTML = hits.map(s => `
          <div class="skill-opt" data-name="${escAttr(s.name)}">
            <div class="skill-opt-name">/${esc(s.name)}</div>
            <div class="skill-opt-desc">${esc(s.description)}</div>
          </div>`).join('');
        dd.querySelectorAll('.skill-opt').forEach(el =>
          el.addEventListener('click', () => selectSkill(el.dataset.name))
        );
        dd.style.display = 'block';
      } else {
        dd.style.display = 'none';
      }
    } else {
      dd.style.display = 'none';
    }
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  document.addEventListener('click', e => {
    if (!e.target.closest('#wrap')) dd.style.display = 'none';
  });
}

function selectSkill(name) {
  document.getElementById('user-input').value = '';
  document.getElementById('skill-dd').style.display = 'none';
  document.getElementById('skill-name').textContent = name;
  document.getElementById('skill-tag').style.display = 'flex';
}

function clearSkill() {
  document.getElementById('skill-tag').style.display = 'none';
  document.getElementById('skill-name').textContent = '';
}

// ═══════════════════════════════════════════════
// Skill 上传
// ═══════════════════════════════════════════════
async function uploadSkill(e) {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  const res = await fetch('/skills/upload', { method: 'POST', body: form });
  if (res.ok) {
    alert('Skill 上传成功');
    loadSkills();
  } else {
    const err = await res.json().catch(() => ({}));
    alert('上传失败: ' + (err.detail || '未知错误'));
  }
  e.target.value = '';
}

// ═══════════════════════════════════════════════
// 工具函数
// ═══════════════════════════════════════════════
function esc(s)     { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return String(s).replace(/"/g,'&quot;'); }
function enc(s)     { return encodeURIComponent(s); }
function relTime(iso) {
  const d = (Date.now() - new Date(iso)) / 1000;
  if (d < 60)    return '刚刚';
  if (d < 3600)  return Math.floor(d / 60)   + ' 分钟前';
  if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
  return Math.floor(d / 86400) + ' 天前';
}

init();
</script>
</body>
</html>
```

- [ ] **Step 2: 启动服务并手动验证**

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8090
```

打开浏览器 `http://localhost:8090`，逐项检查：

| 检查项 | 期望结果 |
|--------|----------|
| 页面加载 | 左侧侧边栏 + 右侧聊天区正常渲染 |
| 发送消息 | 消息气泡出现，AI 流式回复，侧边栏出现该会话 |
| 点击"新对话" | 聊天区清空，sidebar 不变 |
| 再次发消息 | 新会话出现在侧边栏顶部 |
| 点击历史会话 | 历史消息加载，可继续发消息 |
| 删除按钮悬停 | 会话行右侧出现 🗑 图标 |
| 点击删除 | 弹出确认框，确认后该项从侧边栏消失 |
| 删除当前会话 | 自动切到新对话 |
| 输入 `/` | Skill 下拉出现（若有 skill） |

- [ ] **Step 3: 运行全部单元测试，确认无回归**

```bash
pytest -q
```

期望：全部 pass，无 FAILED

- [ ] **Step 4: 提交**

```bash
git add web/index.html
git commit -m "feat: add chat frontend with history sidebar"
```

---

## 完成标准

- [ ] `pytest -q` 全部通过
- [ ] `GET /history/sessions?user_id=` 返回 `[]`
- [ ] 前端侧边栏正确显示历史会话（按时间降序）
- [ ] 切换历史会话后可继续发消息
- [ ] 删除会话后侧边栏移除该项，删除当前会话后自动新建
