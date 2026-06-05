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

from sqlalchemy import delete, desc, func, select, text
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


def list_sessions(db: OrmSession, user_id: str, limit: int = 100) -> List[dict]:
    """列出某用户的全部会话(供侧边栏会话列表)。

    返回按 updated_at 倒序的 [{session_id, title, message_count, updated_at}],
    title 取该会话第一条用户消息的前若干字符。只列出已有归档消息的会话。
    """
    sess_rows = list(
        db.execute(
            select(SessionRow.id, SessionRow.updated_at)
            .where(SessionRow.user_id == user_id)
            .order_by(desc(SessionRow.updated_at))
            .limit(limit)
        ).all()
    )
    if not sess_rows:
        return []
    ids = [r[0] for r in sess_rows]

    # 每会话第一条用户消息(DISTINCT ON 取每组最早一条)做标题
    title_rows = db.execute(
        select(Message.session_id, Message.content)
        .where(Message.session_id.in_(ids), Message.role == "user")
        .distinct(Message.session_id)
        .order_by(Message.session_id, Message.created_at, Message.id)
    ).all()
    titles = {sid: content for sid, content in title_rows}

    # 每会话消息计数
    count_rows = db.execute(
        select(Message.session_id, func.count())
        .where(Message.session_id.in_(ids), Message.role.in_(("user", "assistant")))
        .group_by(Message.session_id)
    ).all()
    counts = {sid: n for sid, n in count_rows}

    out: list[dict] = []
    for sid, updated_at in sess_rows:
        n = counts.get(sid, 0)
        if n == 0:
            continue  # 没有任何归档消息的空会话不展示
        title = (titles.get(sid) or "").strip().replace("\n", " ")
        if len(title) > 40:
            title = title[:40] + "…"
        out.append(
            {
                "session_id": sid,
                "title": title or "(无标题)",
                "message_count": n,
                "updated_at": updated_at.isoformat() if updated_at else None,
            }
        )
    return out


def get_session_messages(db: OrmSession, session_id: str, limit: int = 500) -> List[Message]:
    """按时间正序返回某会话归档的全部消息(供「历史聊天记录」查看)。

    只取用户/助手消息(过滤掉内部 system 注入),最多 limit 条。
    """
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .where(Message.role.in_(("user", "assistant")))
        .order_by(Message.created_at, Message.id)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def delete_session_history(db: OrmSession, session_id: str) -> int:
    """删除某会话的全部归档消息与滚动摘要,返回删除的消息条数。

    保留 sessions 主表行(session_id 仍有效,可继续对话)。
    """
    n = db.execute(delete(Message).where(Message.session_id == session_id)).rowcount or 0
    db.execute(delete(SessionSummary).where(SessionSummary.session_id == session_id))
    db.commit()
    return int(n)


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
