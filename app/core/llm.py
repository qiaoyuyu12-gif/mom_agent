"""
LLM 封装:把任意 **OpenAI 兼容** 推理服务包装成可被 agent 调用的 ChatOpenAI。

支持两类后端(由 `LLM_PROVIDER` 决定行为差异):
1. 本地 vLLM(Qwen3) —— 默认。会注入 Qwen3 chat template 的
   `chat_template_kwargs.enable_thinking` 以按请求切换深度思考。
2. 第三方 OpenAI 兼容 API(DeepSeek / Moonshot / DashScope / 智谱 / OpenAI 等)
   —— 不注入 `enable_thinking`(多数供应商不识别该参数)。

要点:
1. 通过 `app.config.Settings.llm_*` 解析属性统一读取配置(LLM_* 优先,VLLM_* 回退)。
2. 深度思考开关:vLLM/Qwen3 用 `extra_body={"chat_template_kwargs":{"enable_thinking":<bool>}}`;
   第三方 API 想加供应商专属参数(如 OpenAI `reasoning_effort`)可通过
   `LLM_EXTRA_BODY_JSON` 写入 .env,会被合并到 extra_body。
3. 模型若仍按 `<think>…</think>` 输出(如 DeepSeek-R1、Qwen3 思考模式),
   下面的 `StreamThinkSplitter` 状态机仍能把"思考"和"答复"拆成两路。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, AsyncIterator, Dict, Iterable, Literal, Tuple

from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from app.config import get_settings

# `<think>` 段落的开始/结束标签(Qwen3 / DeepSeek-R1 等思考模式输出)
THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def _build_extra_body(enable_thinking: bool) -> Dict[str, Any]:
    """
    构造透传给底层 OpenAI 客户端的 extra_body。

    规则:
    - 若配置允许注入 enable_thinking(默认 vllm 才允许),按本次调用的 enable_thinking 写入
      `chat_template_kwargs.enable_thinking`。
    - 与用户 .env 中的 LLM_EXTRA_BODY_JSON 合并,后者优先覆盖(高级用户兜底口子)。
    """
    s = get_settings()
    extra: Dict[str, Any] = {}
    if s.llm_inject_enable_thinking:
        extra["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    # 合并用户自定义(供应商专属参数),用户字段优先
    user_extra = s.llm_extra_body
    if user_extra:
        # chat_template_kwargs 做深合并,其它键浅覆盖
        if "chat_template_kwargs" in user_extra and isinstance(user_extra["chat_template_kwargs"], dict):
            merged = dict(extra.get("chat_template_kwargs") or {})
            merged.update(user_extra["chat_template_kwargs"])
            extra["chat_template_kwargs"] = merged
            rest = {k: v for k, v in user_extra.items() if k != "chat_template_kwargs"}
            extra.update(rest)
        else:
            extra.update(user_extra)
    return extra


@lru_cache(maxsize=2)
def _make_llm(enable_thinking: bool) -> ChatOpenAI:
    """构造一个开/关思考两种状态各自的 ChatOpenAI 实例(进程级缓存)。

    通过统一的 `s.llm_*` 解析属性读取配置,从而同时支持本地 vLLM 与第三方
    OpenAI 兼容 API(DeepSeek/Moonshot/DashScope/OpenAI 等)。
    """
    s = get_settings()
    return ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        temperature=s.llm_temperature,
        max_tokens=s.llm_max_tokens,
        streaming=True,
        extra_body=_build_extra_body(enable_thinking),
    )


def llm_with_thinking(thinking: bool) -> ChatOpenAI:
    """按需返回"开/关"思考的 LLM 句柄(两种状态各一个缓存实例)。"""
    return _make_llm(bool(thinking))


def get_llm() -> ChatOpenAI:
    """默认句柄(关闭思考)。"""
    return _make_llm(False)


@lru_cache(maxsize=1)
def get_summarizer_llm() -> ChatOpenAI:
    """
    专供 SummarizationMiddleware 使用的非流式句柄。

    设 `streaming=False` 是为了让摘要器的 token 不混入主答复的
    `on_chat_model_stream` 事件流(避免摘要内容被前端当成答复输出)。
    另外把它打上 `summarizer` tag,作为双重保险供事件过滤。

    摘要任务恒定关闭思考模式(无需深度推理,且要省 token)。
    """
    s = get_settings()
    base = ChatOpenAI(
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        model=s.llm_model,
        temperature=0.0,
        max_tokens=512,
        streaming=False,
        extra_body=_build_extra_body(enable_thinking=False),
    )
    return base.with_config({"tags": ["summarizer"]})  # type: ignore[return-value]


# ============================================================
# `<think>` 段落解析
# ============================================================

_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def split_thinking(text: str) -> Tuple[str, str]:
    """
    同步切分:从一段完整模型输出中提取思考与答复。

    返回 `(thinking, answer)`。若没有 `<think>` 段,thinking 为空字符串。
    """
    thoughts = _THINK_BLOCK_RE.findall(text)
    thinking = "\n".join(t.strip() for t in thoughts).strip()
    # 去掉 think 段后剩余即正式答复
    answer = _THINK_BLOCK_RE.sub("", text).strip()
    return thinking, answer


class StreamThinkSplitter:
    """
    流式切分器:逐 chunk 喂入,逐段产出 `("thought"|"answer", text)`。

    Qwen3 思考模式下模型可能先吐 `<think>…</think>`,再吐正式答复;
    也可能交替出现。我们按状态机处理两种标签的跨 chunk 拼接。
    """

    Channel = Literal["thought", "answer"]

    def __init__(self) -> None:
        self._buffer = ""        # 当前还未确定归属的字符
        self._in_think = False   # 当前是否处在 <think> 段内

    def feed(self, chunk: str) -> Iterable[Tuple["StreamThinkSplitter.Channel", str]]:
        """
        往切分器灌入新文本,返回可立即输出的若干段。

        约定:对 `<think>` / `</think>` 标签可能横跨多个 chunk,
        所以最多缓存 len(THINK_OPEN/CLOSE)-1 个字符等待下一片。
        """
        self._buffer += chunk
        outs: list[Tuple[StreamThinkSplitter.Channel, str]] = []

        while True:
            if not self._in_think:
                idx = self._buffer.find(THINK_OPEN)
                if idx == -1:
                    # 没看到开标签:除最后可能是部分标签前缀的字符,其余都是答复
                    safe_len = max(len(self._buffer) - (len(THINK_OPEN) - 1), 0)
                    if safe_len > 0:
                        outs.append(("answer", self._buffer[:safe_len]))
                        self._buffer = self._buffer[safe_len:]
                    break
                # 看到 <think>:把之前的文本作为 answer 输出,切到 think 状态
                if idx > 0:
                    outs.append(("answer", self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(THINK_OPEN):]
                self._in_think = True
                continue
            # 处在 <think> 段内,找闭标签
            idx = self._buffer.find(THINK_CLOSE)
            if idx == -1:
                safe_len = max(len(self._buffer) - (len(THINK_CLOSE) - 1), 0)
                if safe_len > 0:
                    outs.append(("thought", self._buffer[:safe_len]))
                    self._buffer = self._buffer[safe_len:]
                break
            if idx > 0:
                outs.append(("thought", self._buffer[:idx]))
            self._buffer = self._buffer[idx + len(THINK_CLOSE):]
            self._in_think = False
        return outs

    def flush(self) -> Iterable[Tuple["StreamThinkSplitter.Channel", str]]:
        """流结束时把缓冲区残留按当前状态吐尽。"""
        if not self._buffer:
            return []
        ch: "StreamThinkSplitter.Channel" = "thought" if self._in_think else "answer"
        out = [(ch, self._buffer)]
        self._buffer = ""
        return out


async def astream_split(
    messages: list[BaseMessage], thinking: bool
) -> AsyncIterator[Tuple[StreamThinkSplitter.Channel, str]]:
    """
    异步流式调用 LLM,按 (thought/answer, chunk) 产出。

    内部使用 LangChain 的标准事件流 `astream_events(version="v2")`:
    - 监听 `on_chat_model_stream` 事件,取 `data.chunk.content` 作为新增文本;
    - 把文本交给 `StreamThinkSplitter` 状态机,按 thought / answer 切两路;
    - 兼容多模态 content 为 list 的情况(只抽其中的 text 段)。

    用法见 `app/api/chat.py` 中的 SSE 端点。
    """
    splitter = StreamThinkSplitter()
    llm = llm_with_thinking(thinking)

    async for event in llm.astream_events(messages, version="v2"):
        # 只关心 chat 模型的流式 chunk;链/工具等其它事件忽略
        if event.get("event") != "on_chat_model_stream":
            continue
        chunk = (event.get("data") or {}).get("chunk")
        content = getattr(chunk, "content", "") or ""

        # 多模态返回可能是 [{"type":"text","text":"..."}] 的分段列表
        if isinstance(content, list):
            text = "".join(
                seg.get("text", "")
                for seg in content
                if isinstance(seg, dict) and seg.get("type") in (None, "text")
            )
        else:
            text = str(content)

        if not text:
            continue
        for ch, seg in splitter.feed(text):
            if seg:
                yield ch, seg

    for ch, seg in splitter.flush():
        if seg:
            yield ch, seg
