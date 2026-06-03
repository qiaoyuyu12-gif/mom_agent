"""
Agent 编排:用 LangChain 1.x 的 `create_agent` + Middleware 框架。

中间件:
- SummarizationMiddleware:上下文超阈值自动摘要旧消息(替代我们之前自写的滚动摘要)。
- ModelCallLimitMiddleware:限制单次请求 / 单 session 的模型调用次数,防止失控循环。

事件流:agent 的 `astream_events(version="v2")` 是 LangChain 的标准事件流,我们:
- 监听 `on_chat_model_stream` 取主答复 chunk(带 `summarizer` tag 的来自摘要器,跳过)。
- 监听 `on_chat_model_end` 兜底拿完整答复(用于流被中途切断时收尾)。
- 把 chunk 文本喂给 `StreamThinkSplitter`,分流"思考"与"答复"两路。

事件输出:
    ("meta",    dict)   — 路由阶段元信息(命中片段、skill、call-limit 设置等)
    ("thought", str)    — 思考流(深度思考模式时才有)
    ("answer",  str)    — 答复流
    ("done",    dict)   — 收尾(assistant_message_id)
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
from typing import Any, AsyncIterator, Tuple

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.core.guards import audit_source_citations
from app.core.llm import StreamThinkSplitter, _make_llm
from app.core.middleware import build_harness_middleware
from app.core.prompts import SYSTEM_PROMPT
from app.memory.long_term import (
    archive_message,
    ensure_session,
    format_facts_for_prompt,
    search_facts,
)
from app.memory.short_term import ShortTermMemory
from app.rag.ragflow_client import RagflowClient, format_chunks_for_prompt
from app.skills.runner import load_runtime_skill, render_skill_as_instruction


@dataclass
class AgentRequest:
    """一次对话请求的入参。"""

    session_id: str
    message: str
    user_id: str | None = None
    skill_name: str | None = None
    thinking: bool = False


# ============================================================
# Agent 工厂(按"是否开思考"缓存两个 agent 实例)
# ============================================================


def _build_agent(thinking: bool):
    """
    构造一个 LangChain Agent。

    - 主模型:开/关思考各一份(其余参数共享)。
    - SummarizationMiddleware:用专属非流式摘要器,避免 token 混入主流式。
    - ModelCallLimitMiddleware:run_limit 控制单次请求最多调用模型次数,
      thread_limit 控制 session 累计上限,超限时 exit_behavior="end" 优雅停止。
    """
    s = get_settings()

    main_model = _make_llm(thinking)
    summarizer = get_summarizer_llm()

    summarization = SummarizationMiddleware(
        model=summarizer,
        # 触发条件:历史 token 数 ≥ 阈值
        trigger=("tokens", s.MAX_HISTORY_TOKENS),
        # 摘要后保留最近 N 条消息(其余被替换为摘要)
        keep=("messages", s.KEEP_RECENT_MESSAGES),
    )
    call_limit = ModelCallLimitMiddleware(
        thread_limit=s.MODEL_CALL_THREAD_LIMIT,
        run_limit=s.MODEL_CALL_RUN_LIMIT,
        exit_behavior="end",
    )

    return create_agent(
        model=main_model,
        tools=[],                       # 当前阶段无外部工具
        system_prompt=SYSTEM_PROMPT,
        middleware=[summarization, call_limit],
    )


@lru_cache(maxsize=2)
def get_agent(thinking: bool):
    """进程级缓存:深度思考开/关两份 agent。"""
    return _build_agent(bool(thinking))


# ============================================================
# 事件流主入口
# ============================================================


def _extract_text(content: Any) -> str:
    """从 chat model chunk 的 content 中抽出纯文本(兼容多模态分段)。"""
    if isinstance(content, list):
        return "".join(
            seg.get("text", "")
            for seg in content
            if isinstance(seg, dict) and seg.get("type") in (None, "text")
        )
    return str(content or "")


def _is_summarizer_event(event: dict) -> bool:
    """事件是否由摘要器中间件产生(应跳过,不计入主答复)。"""
    tags = event.get("tags") or []
    if "summarizer" in tags:
        return True
    # langgraph 节点名兜底:摘要节点通常名字含 "summariz"
    md = event.get("metadata") or {}
    node = str(md.get("langgraph_node") or md.get("checkpoint_ns") or "")
    return "summariz" in node.lower()


async def astream_agent_response(
    req: AgentRequest,
    *,
    db: OrmSession,
    short_term: ShortTermMemory,
    ragflow: RagflowClient | None = None,
) -> AsyncIterator[Tuple[str, Any]]:
    """
    主流程:
    1. 确保 session 存在 → 归档用户消息到 PG。
    2. 从 Redis 取短期历史;调 RAGFlow 取参考片段;按需召回长期事实;按需加载 skill。
    3. 拼输入 messages(系统提示由 create_agent 自动注入,不在这里加)。
    4. 发 meta 事件(来源、skill、调用上限等)。
    5. 调 `agent.astream_events(version="v2")`,按事件类型分流:
       - on_chat_model_stream:主模型 chunk → 经 splitter 分发 thought/answer。
       - on_chat_model_end:摘要器结束等;忽略对应主模型也补一次,避免漏尾。
    6. 收尾:归档助手消息 + 写回 Redis 短期窗口。
    """
    ragflow = ragflow or RagflowClient()
    s = get_settings()

    # 1. 会话 + 用户消息入库
    ensure_session(db, req.session_id, req.user_id)
    user_msg_id = archive_message(
        db, req.session_id, "user", req.message, skill_name=req.skill_name
    )

    # 2. 短期历史 + RAG + 长期事实 + skill
    history: list[BaseMessage] = short_term.messages()

    rag_started = perf_counter()
    rag_error: str | None = None
    try:
        chunks = await ragflow.retrieve(req.message)
    except Exception as e:  # noqa: BLE001
        chunks = []
        rag_error = f"{type(e).__name__}: {e}"
    rag_ms = round((perf_counter() - rag_started) * 1000, 1)
    rag_context = format_chunks_for_prompt(chunks)

    facts = search_facts(db, req.user_id, req.message, limit=5) if req.user_id else []
    facts_context = format_facts_for_prompt(facts)

    skill_instruction = ""
    if req.skill_name:
        skill = load_runtime_skill(db, req.skill_name)
        if skill is not None:
            skill_instruction = render_skill_as_instruction(skill)

    # 3. 构建输入 messages(SYSTEM_PROMPT 由 create_agent 自动注入)
    input_messages: list[BaseMessage] = list(history)
    if facts_context:
        input_messages.append(SystemMessage(content=facts_context))
    if rag_context:
        input_messages.append(SystemMessage(content=rag_context))
    if skill_instruction:
        input_messages.append(SystemMessage(content=skill_instruction))
    input_messages.append(HumanMessage(content=req.message))

    # 4. 发 meta
    yield (
        "meta",
        {
            "thinking": req.thinking,
            "skill": req.skill_name,
            "sources": [
                {
                    "idx": i + 1,
                    "doc_name": c.doc_name,
                    "document_id": c.document_id,
                    "similarity": c.similarity,
                }
                for i, c in enumerate(chunks)
            ],
            "rag": {
                "ok": rag_error is None,
                "error": rag_error,
                "duration_ms": rag_ms,
            },
            "facts_count": len(facts),
            "user_message_id": user_msg_id,
            "input_messages": len(input_messages),
            "limits": {
                "run_limit": s.MODEL_CALL_RUN_LIMIT,
                "thread_limit": s.MODEL_CALL_THREAD_LIMIT,
                "summary_threshold_tokens": s.MAX_HISTORY_TOKENS,
                "summary_keep_recent": s.KEEP_RECENT_MESSAGES,
            },
        },
    )

    # 5. 流式调用 agent
    agent = get_agent(req.thinking)
    splitter = StreamThinkSplitter()
    full_thought: list[str] = []
    full_answer: list[str] = []
    final_message_seen = False
    llm_started = perf_counter()
    first_answer_ms: float | None = None

    async for event in agent.astream_events(
        {"messages": input_messages},
        config={"configurable": {"thread_id": req.session_id}},
        version="v2",
    ):
        ev = event.get("event")

        # 跳过摘要器自身的事件(防御性,主要靠 streaming=False)
        if _is_summarizer_event(event):
            continue

        if ev == "on_chat_model_stream":
            chunk = (event.get("data") or {}).get("chunk")
            text = _extract_text(getattr(chunk, "content", ""))
            if not text:
                continue
            for ch, seg in splitter.feed(text):
                if not seg:
                    continue
                if ch == "answer" and first_answer_ms is None:
                    first_answer_ms = round((perf_counter() - llm_started) * 1000, 1)
                (full_thought if ch == "thought" else full_answer).append(seg)
                yield ch, seg

        elif ev == "on_chat_model_end" and not final_message_seen:
            # 兜底:若没收到任何 stream chunk(模型非流式返回),用 end 事件取最终消息
            if not full_answer and not full_thought:
                output = (event.get("data") or {}).get("output")
                content = _extract_text(getattr(output, "content", ""))
                if content:
                    for ch, seg in splitter.feed(content):
                        if seg:
                            if ch == "answer" and first_answer_ms is None:
                                first_answer_ms = round((perf_counter() - llm_started) * 1000, 1)
                            (full_thought if ch == "thought" else full_answer).append(seg)
                            yield ch, seg
            final_message_seen = True

    # splitter 缓冲区收尾
    for ch, seg in splitter.flush():
        if seg:
            if ch == "answer" and first_answer_ms is None:
                first_answer_ms = round((perf_counter() - llm_started) * 1000, 1)
            (full_thought if ch == "thought" else full_answer).append(seg)
            yield ch, seg

    thought_text = "".join(full_thought).strip()
    answer_text = "".join(full_answer).strip()
    citation_audit = audit_source_citations(answer_text, len(chunks))
    if citation_audit.missing and answer_text:
        citation_note = "\n\n注: 本轮检索到了参考资料,但回答未明确标注来源编号,请结合来源列表谨慎核对。"
        answer_text = f"{answer_text}{citation_note}"
        yield "answer", citation_note

    # 6. 归档助手消息 + 写回 Redis 短期
    assistant_msg_id = archive_message(
        db,
        req.session_id,
        "assistant",
        answer_text,
        thinking=thought_text or None,
        skill_name=req.skill_name,
    )
    short_term.append_pair(
        HumanMessage(content=req.message), AIMessage(content=answer_text)
    )

    yield (
        "done",
        {
            "assistant_message_id": assistant_msg_id,
            "audit": {
                "citation_required": citation_audit.required,
                "citation_missing": citation_audit.missing,
                "cited_sources": citation_audit.cited_source_numbers,
            },
            "timings": {
                "rag_ms": rag_ms,
                "llm_total_ms": round((perf_counter() - llm_started) * 1000, 1),
                "first_answer_ms": first_answer_ms,
            },
        },
    )
