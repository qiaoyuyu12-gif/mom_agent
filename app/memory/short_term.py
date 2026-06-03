"""
短期记忆:Redis 存储的会话消息历史。

为何不直接用 langchain-redis 的 RedisChatMessageHistory:
- 我们需要做 trim / replace / 缓存滚动摘要 等定制操作,
  直接用 redis-py 维护一个 list + 一个 string 更可控、可测。
- 对外仍以 LangChain BaseMessage 提供消息。

数据结构:
- mom:session:{sid}:messages  -> Redis LIST,LPUSH 入队 / RPOP 取尾(我们用 RPUSH + LRANGE)
- mom:session:{sid}:summary   -> Redis STRING,滚动摘要的缓存
两者均设 TTL,任一写入会刷新 TTL。
"""

from __future__ import annotations

import json
from typing import List

import redis
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.config import get_settings


# 角色 → 工厂函数,把存储记录还原为 LangChain 消息
_ROLE_TO_MSG = {
    "user": HumanMessage,
    "assistant": AIMessage,
    "system": SystemMessage,
}


def _msg_to_record(m: BaseMessage) -> dict:
    """LangChain 消息 → 可入 Redis 的 JSON 记录。"""
    if isinstance(m, HumanMessage):
        role = "user"
    elif isinstance(m, AIMessage):
        role = "assistant"
    else:
        role = "system"
    return {"role": role, "content": m.content}


def _record_to_msg(r: dict) -> BaseMessage:
    """JSON 记录 → LangChain 消息。"""
    cls = _ROLE_TO_MSG.get(r.get("role", "user"), HumanMessage)
    return cls(content=r.get("content", ""))


class ShortTermMemory:
    """单一 session 的短期记忆,封装 Redis 读写。"""

    def __init__(self, session_id: str, client: redis.Redis | None = None) -> None:
        s = get_settings()
        self.session_id = session_id
        self._ttl = s.REDIS_TTL_SECONDS
        self._cli = client or redis.Redis.from_url(s.REDIS_URL, decode_responses=True)

    # ---------- key 命名 ----------
    @property
    def _msg_key(self) -> str:
        return f"mom:session:{self.session_id}:messages"

    @property
    def _sum_key(self) -> str:
        return f"mom:session:{self.session_id}:summary"

    # ---------- 消息读写 ----------
    def append(self, message: BaseMessage) -> None:
        """追加一条消息(尾部)。"""
        self._cli.rpush(self._msg_key, json.dumps(_msg_to_record(message), ensure_ascii=False))
        self._cli.expire(self._msg_key, self._ttl)

    def append_pair(self, user: BaseMessage, assistant: BaseMessage) -> None:
        """常用便捷:同时追加一对用户/助手消息。"""
        pipe = self._cli.pipeline()
        pipe.rpush(
            self._msg_key,
            json.dumps(_msg_to_record(user), ensure_ascii=False),
            json.dumps(_msg_to_record(assistant), ensure_ascii=False),
        )
        pipe.expire(self._msg_key, self._ttl)
        pipe.execute()

    def messages(self) -> List[BaseMessage]:
        """读取当前短期窗口里的所有消息(按时间顺序)。"""
        raw = self._cli.lrange(self._msg_key, 0, -1)
        out: list[BaseMessage] = []
        for item in raw:
            try:
                out.append(_record_to_msg(json.loads(item)))
            except json.JSONDecodeError:
                continue
        return out

    def trim_keep_last(self, n: int) -> None:
        """只保留最近 n 条消息(压缩后调用,把更早的消息丢弃)。"""
        if n <= 0:
            self._cli.delete(self._msg_key)
            return
        # LTRIM(key, -n, -1) 保留尾部 n 条
        self._cli.ltrim(self._msg_key, -n, -1)
        self._cli.expire(self._msg_key, self._ttl)

    def clear(self) -> None:
        """清空(用于测试/调试)。"""
        self._cli.delete(self._msg_key, self._sum_key)

    # ---------- 滚动摘要缓存 ----------
    def get_summary(self) -> str:
        return self._cli.get(self._sum_key) or ""

    def set_summary(self, summary: str) -> None:
        self._cli.set(self._sum_key, summary, ex=self._ttl)
