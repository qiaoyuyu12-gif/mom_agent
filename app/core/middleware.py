"""
自定义中间件与护栏 hook(基于 LangChain 1.x 的 middleware 框架)。

本模块是本项目的「harness 工程」集中地,装配在 app/core/agent.py 的 _build_agent 中。
包含三类东西:

1. ProgressMiddleware(自定义类中间件)
   - 在每次模型调用前/后通过 `adispatch_custom_event` 下发结构化进度事件。
   - 这些事件在 `astream_events(version="v2")` 流里以 `on_custom_event`(name="progress")
     出现,被 agent.py 转成 SSE 的 `status` 事件,让前端看到「生成中/已生成」。

2. CitationGuardMiddleware(护栏 hook,after_model)
   - 确定性地复用 guards.audit_source_citations 做引用审计(遥测用),
     命中「检索到资料却未标注 [来源N]」时下发 `on_custom_event`(name="audit")。
   - 注意:用户可见的「引用缺失提示」仍由 agent.py 主路径负责(已被单测覆盖、稳妥),
     本 hook 只做确定性校验与事件遥测,不改写消息状态,避免重复处理。

3. build_harness_middleware()
   - 工厂函数:按配置组装本项目的全部中间件(含内置 Summarization / ModelCallLimit /
     ModelRetry / PII),返回给 create_agent 使用。

进度/审计事件统一结构:{"event": "status"|"audit", "stage": str, ...}
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    PIIMiddleware,
    SummarizationMiddleware,
)
from langchain_core.callbacks import adispatch_custom_event, dispatch_custom_event

from app.config import get_settings
from app.core.guards import SOURCE_CITATION_RE
from app.core.llm import get_summarizer_llm

logger = logging.getLogger(__name__)

# 自定义事件名:agent.py 据此从 astream_events 里捞出并转成 SSE
PROGRESS_EVENT = "progress"
AUDIT_EVENT = "audit"


def _last_ai_text(state: Any) -> str:
    """从 agent 状态里取最后一条 AI 消息的纯文本(兼容多模态分段 content)。"""
    messages = (state or {}).get("messages") or []
    if not messages:
        return ""
    last = messages[-1]
    content = getattr(last, "content", "")
    if isinstance(content, list):
        return "".join(
            seg.get("text", "")
            for seg in content
            if isinstance(seg, dict) and seg.get("type") in (None, "text")
        )
    return str(content or "")


def _cited_numbers(text: str) -> list[int]:
    """解析答复中的 [来源N] 编号(去重升序)。无状态,无并发副作用。"""
    return sorted({int(m.group(1)) for m in SOURCE_CITATION_RE.finditer(text or "")})


class ProgressMiddleware(AgentMiddleware):
    """模型调用前/后下发进度事件,提升流式可观测性与用户体验。"""

    # 同步路径(create_agent 同步调用时用)
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        dispatch_custom_event(PROGRESS_EVENT, {"event": "status", "stage": "generating"})
        return None

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        dispatch_custom_event(PROGRESS_EVENT, {"event": "status", "stage": "generated"})
        return None

    # 异步路径(本项目 /chat 走 astream_events,实际用这两个)
    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        await adispatch_custom_event(PROGRESS_EVENT, {"event": "status", "stage": "generating"})
        return None

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        await adispatch_custom_event(PROGRESS_EVENT, {"event": "status", "stage": "generated"})
        return None


class CitationGuardMiddleware(AgentMiddleware):
    """after_model 护栏:**无状态**地解析答复中的 [来源N] 引用,下发 audit 事件(遥测)。

    设计取舍:`required/missing` 的权威判定需要「本轮检索命中了几条」这一请求态信息,
    放在 agent.py 主路径里完成(已被单测覆盖)。本 hook 不持有任何请求态,故并发安全,
    只负责把「答复实际引用了哪些来源编号」作为确定性遥测事件吐出。
    """

    def _audit_payload(self, state: Any) -> dict[str, Any] | None:
        text = _last_ai_text(state)
        if not text:
            return None
        cited = _cited_numbers(text)
        return {"event": "audit", "cited_sources": cited, "has_citation": bool(cited)}

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        payload = self._audit_payload(state)
        if payload:
            dispatch_custom_event(AUDIT_EVENT, payload)
        return None

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        payload = self._audit_payload(state)
        if payload:
            await adispatch_custom_event(AUDIT_EVENT, payload)
        return None


def build_harness_middleware() -> list[AgentMiddleware]:
    """
    按配置组装本项目全部中间件,供 create_agent 使用。

    顺序约定(before_* 先到后执行;wrap_* 由外到内嵌套):
      PII(先脱敏输入) → Citation 护栏 → Progress → Summarization(压缩) →
      ModelCallLimit(限额) → ModelRetry(重试,包裹模型调用)
    """
    s = get_settings()
    mw: list[AgentMiddleware] = []

    # 1) PII 脱敏(输入侧)。运维问答常含 IP,默认只脱敏 email/credit_card。
    if s.ENABLE_PII_REDACTION:
        for pii_type in s.pii_type_list:
            # 信用卡用 mask(保留后四位),其余用 redact(整体替换)
            strategy = "mask" if pii_type == "credit_card" else "redact"
            mw.append(PIIMiddleware(pii_type, strategy=strategy, apply_to_input=True))

    # 2) 引用审计护栏(after_model 遥测,无状态)
    mw.append(CitationGuardMiddleware())

    # 3) 进度事件
    if s.ENABLE_PROGRESS_EVENTS:
        mw.append(ProgressMiddleware())

    # 4) 上下文压缩(沿用原配置,专属非流式摘要器)
    mw.append(
        SummarizationMiddleware(
            model=get_summarizer_llm(),
            trigger=("tokens", s.MAX_HISTORY_TOKENS),
            keep=("messages", s.KEEP_RECENT_MESSAGES),
        )
    )

    # 5) 模型调用次数限制(防失控循环)
    mw.append(
        ModelCallLimitMiddleware(
            thread_limit=s.MODEL_CALL_THREAD_LIMIT,
            run_limit=s.MODEL_CALL_RUN_LIMIT,
            exit_behavior="end",
        )
    )

    # 6) 模型重试(vLLM 瞬时错误)。on_failure="error" 让最终失败上抛到 SSE error
    if s.ENABLE_MODEL_RETRY:
        mw.append(
            ModelRetryMiddleware(
                max_retries=s.MODEL_RETRY_MAX,
                on_failure="error",
            )
        )

    return mw

