"""
长期记忆:PostgreSQL 持久化。

提供:
- ensure_session  : 确保 sessions 表中存在该会话(upsert)
- archive_message : 把一条消息写入 messages 归档表
- get_summary / upsert_summary : 滚动摘要(session_summaries)读写
- add_fact / search_facts      : 跨会话事实(memory_facts)读写

注意:本模块不使用 pgvector;事实召回靠
"用户/近期 + 关键词 ILIKE / to_tsvector 全文索引"。
"""

from __future__ import annotations

from typing import List

from sqlalchemy import desc, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as OrmSession

from app.core.guards import wrap_untrusted_context
from app.db.models import MemoryFact, Message, Session as SessionRow, SessionSummary


# ---------------- 会话与消息归档 ----------------

def ensure_session(db: OrmSession, session_id: str, user_id: str | None = None) -> None:
    """若会话不存在则插入;若存在则刷新 updated_at(以及 user_id 若给出)。"""
    stmt = pg_insert(SessionRow).values(id=session_id, user_id=user_id)
    update_cols = {"updated_at": func.now()}
    if user_id is not None:
        update_cols["user_id"] = user_id
    stmt = stmt.on_conflict_do_update(index_elements=[SessionRow.id], set_=update_cols)
    db.execute(stmt)
    db.commit()


def archive_message(
    db: OrmSession,
    session_id: str,
    role: str,
    content: str,
    thinking: str | None = None,
    skill_name: str | None = None,
) -> int:
    """把一条消息写入归档表,返回新 message_id。"""
    m = Message(
        session_id=session_id,
        role=role,
        content=content,
        thinking=thinking,
        skill_name=skill_name,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return int(m.id)


# ---------------- 滚动摘要 ----------------

def get_summary(db: OrmSession, session_id: str) -> tuple[str, int | None]:
    """返回 (summary, last_compressed_message_id)。无则 ("", None)。"""
    row = db.get(SessionSummary, session_id)
    if not row:
        return "", None
    return row.summary or "", row.last_compressed_message_id


def upsert_summary(
    db: OrmSession,
    session_id: str,
    summary: str,
    last_compressed_message_id: int | None,
) -> None:
    """覆写指定会话的滚动摘要。"""
    stmt = pg_insert(SessionSummary).values(
        session_id=session_id,
        summary=summary,
        last_compressed_message_id=last_compressed_message_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[SessionSummary.session_id],
        set_={
            "summary": summary,
            "last_compressed_message_id": last_compressed_message_id,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    db.commit()


# ---------------- 长期事实 ----------------

def add_fact(
    db: OrmSession,
    fact: str,
    user_id: str | None = None,
    session_id: str | None = None,
    keywords: str | None = None,
) -> int:
    """新增一条长期事实,返回 id。"""
    f = MemoryFact(fact=fact, user_id=user_id, session_id=session_id, keywords=keywords or "")
    db.add(f)
    db.commit()
    db.refresh(f)
    return int(f.id)


def search_facts(
    db: OrmSession,
    user_id: str | None,
    query: str,
    limit: int = 5,
) -> List[MemoryFact]:
    """
    长期事实召回:优先按 user_id 过滤,然后用全文检索 + ILIKE 兜底,
    最后按 created_at 倒序。简单稳健,无需向量。
    """
    q = query.strip()
    base = select(MemoryFact)
    if user_id:
        base = base.where(MemoryFact.user_id == user_id)

    if q:
        # PostgreSQL 全文检索(对 keywords 字段),ILIKE 兜底匹配 fact 正文
        ts = func.to_tsvector("simple", func.coalesce(MemoryFact.keywords, ""))
        tsq = func.plainto_tsquery("simple", q)
        base = base.where((ts.op("@@")(tsq)) | (MemoryFact.fact.ilike(f"%{q}%")))

    base = base.order_by(desc(MemoryFact.created_at)).limit(limit)
    return list(db.execute(base).scalars().all())


def format_facts_for_prompt(facts: List[MemoryFact]) -> str:
    """把长期事实拼为 prompt 段。"""
    if not facts:
        return ""
    lines = [f"- {f.fact.strip()}" for f in facts]
    return wrap_untrusted_context(
        "以下是关于该用户的长期记忆事实。",
        "\n".join(lines),
        "仅在与当前问题相关时参考;若与用户当前表达冲突,以当前表达为准。",
    )


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
    # user_id 为空直接返回空列表，避免无效查询
    if not user_id:
        return []

    # 查询该用户的所有会话，按最近更新时间降序排列
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
        # 取该会话第一条用户消息作为标题来源
        first_content = db.execute(
            select(Message.content)
            .where(Message.session_id == s.id, Message.role == "user")
            .order_by(Message.created_at)
            .limit(1)
        ).scalar()

        # 统计该会话的 user/assistant 消息总数
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
            # 标题截取前 30 字符；无用户消息时回退为"新对话"
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
    # 校验会话存在且属于该用户
    s = db.get(SessionRow, session_id)
    if s is None or s.user_id != user_id:
        raise ValueError(f"session {session_id!r} not found for user {user_id!r}")

    # 按时间升序返回对话消息（仅 user/assistant 角色）
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
    # 校验会话归属，防止越权删除
    s = db.get(SessionRow, session_id)
    if s is None or s.user_id != user_id:
        raise ValueError(f"session {session_id!r} not found for user {user_id!r}")

    # 删除会话，CASCADE 会自动清理关联的消息和摘要
    db.delete(s)
    db.commit()
    return True
