"""
RAGFlow 检索客户端。

只调用 RAGFlow 的检索 API:`POST {base}/api/v1/retrieval`,
不做生成。生成由本项目的 agent 结合记忆/skill/思考开关完成。

请求参数与响应字段以 RAGFlow 官方 HTTP API 为准;字段命名做了
防御性兼容(content / content_with_weight 等)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

import httpx

from app.config import get_settings
from app.core.guards import wrap_untrusted_context


@dataclass
class RetrievedChunk:
    """检索命中的一个片段。"""

    content: str                 # 片段正文
    doc_name: str                # 来源文档名(便于溯源)
    document_id: str             # 文档 id
    similarity: float            # 综合相似度
    dataset_id: str = ""         # 所属知识库 id

    def render(self, idx: int) -> str:
        """把片段渲染为 prompt 中的一段带编号文本(供 LLM 引用)。"""
        head = f"[来源{idx}: {self.doc_name} | sim={self.similarity:.3f}]"
        return f"{head}\n{self.content.strip()}"


class RagflowClient:
    """RAGFlow HTTP 客户端(异步)。"""

    def __init__(self, timeout: float = 30.0) -> None:
        s = get_settings()
        self._base = s.RAGFLOW_BASE_URL.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {s.RAGFLOW_API_KEY}",
            "Content-Type": "application/json",
        }
        self._dataset_ids = s.ragflow_dataset_id_list
        self._top_k = s.RAGFLOW_TOP_K
        self._sim_threshold = s.RAGFLOW_SIMILARITY_THRESHOLD
        self._vec_weight = s.RAGFLOW_VECTOR_WEIGHT
        self._timeout = timeout

    async def retrieve(
        self,
        question: str,
        dataset_ids: list[str] | None = None,
        top_k: int | None = None,
    ) -> List[RetrievedChunk]:
        """
        调 RAGFlow 检索,返回 top-k 片段。

        - 若未传 dataset_ids,使用 .env 中配置的默认列表。
        - 若知识库未配置或 question 为空,直接返回空列表(不报错)。
        """
        ds = self._dataset_ids if dataset_ids is None else dataset_ids
        question = (question or "").strip()
        if not question or not ds:
            return []

        payload: dict[str, Any] = {
            "question": question,
            "dataset_ids": ds,
            "page": 1,
            "page_size": top_k or self._top_k,
            "similarity_threshold": self._sim_threshold,
            "vector_similarity_weight": self._vec_weight,
            "top_k": top_k or self._top_k,
            "keyword": False,
            "highlight": False,
        }

        async with httpx.AsyncClient(timeout=self._timeout) as cli:
            r = await cli.post(
                f"{self._base}/api/v1/retrieval",
                headers=self._headers,
                json=payload,
            )
            r.raise_for_status()
            body = r.json()

        # 防御性解析:RAGFlow 不同版本字段略有差异
        data = body.get("data") or {}
        raw_chunks = data.get("chunks") or []
        chunks: List[RetrievedChunk] = []
        for c in raw_chunks:
            content = (
                c.get("content")
                or c.get("content_with_weight")
                or c.get("content_ltks")
                or ""
            )
            if not content:
                continue
            chunks.append(
                RetrievedChunk(
                    content=content,
                    doc_name=c.get("document_keyword") or c.get("docnm_kwd") or c.get("document_name") or "未知文档",
                    document_id=c.get("document_id") or c.get("doc_id") or "",
                    similarity=float(c.get("similarity") or c.get("score") or 0.0),
                    dataset_id=c.get("kb_id") or c.get("dataset_id") or "",
                )
            )
        return chunks


def format_chunks_for_prompt(chunks: List[RetrievedChunk]) -> str:
    """把片段列表拼为 prompt 中的"参考资料"段。无命中则返回空串。"""
    if not chunks:
        return ""
    rendered = "\n\n".join(c.render(i + 1) for i, c in enumerate(chunks))
    return wrap_untrusted_context(
        "以下是从系统操作手册中检索到的相关内容。",
        rendered,
        "回答涉及这些资料时,请优先依据资料内容,并用 [来源N] 标注出处。",
    )
