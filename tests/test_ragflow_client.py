"""单测:RAGFlow 检索客户端的响应解析(用 httpx mock,避免真打外部服务)。"""

import os

import httpx
import pytest

# 在导入 client 之前,先设置必要环境变量,避免 Settings 校验失败
os.environ.setdefault("VLLM_BASE_URL", "http://localhost:9/v1")
os.environ.setdefault("VLLM_MODEL", "qwen")
os.environ.setdefault("RAGFLOW_BASE_URL", "http://ragflow.local")
os.environ.setdefault("RAGFLOW_API_KEY", "k")
os.environ.setdefault("RAGFLOW_DATASET_IDS", "ds1,ds2")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://u:p@localhost:5432/x")

from app.rag.ragflow_client import RagflowClient, format_chunks_for_prompt  # noqa: E402


@pytest.mark.asyncio
async def test_retrieve_parses_chunks(monkeypatch):
    """模拟 RAGFlow 返回两条 chunk,验证字段映射与默认值兜底。"""

    sample = {
        "code": 0,
        "data": {
            "chunks": [
                {
                    "content": "片段A正文",
                    "document_keyword": "手册1.pdf",
                    "document_id": "d1",
                    "similarity": 0.91,
                    "kb_id": "ds1",
                },
                {
                    "content_with_weight": "片段B正文",
                    "docnm_kwd": "手册2.pdf",
                    "doc_id": "d2",
                    "score": 0.55,
                },
            ]
        },
    }

    async def fake_post(self, url, headers=None, json=None):  # noqa: ARG001
        return httpx.Response(200, json=sample, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    client = RagflowClient()
    chunks = await client.retrieve("如何重启服务?")
    assert len(chunks) == 2
    assert chunks[0].content == "片段A正文"
    assert chunks[0].doc_name == "手册1.pdf"
    assert chunks[0].similarity == pytest.approx(0.91)
    assert chunks[1].content == "片段B正文"
    assert chunks[1].doc_name == "手册2.pdf"


def test_format_chunks_for_prompt_empty():
    assert format_chunks_for_prompt([]) == ""


@pytest.mark.asyncio
async def test_retrieve_empty_inputs():
    """空 question 或没配 dataset 时直接返回空,不访问网络。"""
    client = RagflowClient()
    assert await client.retrieve("") == []
    assert await client.retrieve("x", dataset_ids=[]) == []
