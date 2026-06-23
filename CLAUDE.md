# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

`mom_agent` 是 **MOM(Manufacturing Operations Management,制造运营管理)系统的 AI 问答助手**:只负责检索之后的「记忆 + skill + 思考开关 + 生成」,回答用户关于 MOM 系统的业务问题(生产计划、质量管理、设备管理、物料追踪等)。知识库的手册上传、切片、索引、检索全部交给外部已部署的 **RAGFlow**;大模型交给外部已部署的 **vLLM(Qwen3,OpenAI 兼容端点)**。本仓库不启动也不实现这两者。

技术栈:FastAPI + LangChain 1.x(`create_agent` + middleware) + Redis(短期记忆) + PostgreSQL(长期记忆,无 pgvector)。

完整需求/设计见 [docs/需求文档.md](docs/需求文档.md)。

## 常用命令

```bash
# 1) 起本地依赖(仅 Postgres + Redis;vLLM/RAGFlow 是外部服务)
docker compose up -d          # PG 首启自动执行 migrations/init.sql 建表

# 2) 装依赖 + 配置
cp .env.example .env          # 必填 VLLM_* / RAGFLOW_*(尤其 RAGFLOW_DATASET_IDS)
pip install -r requirements.txt

# 3) 启动(静态前端由后端挂载在 /)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000   # 打开 http://localhost:8000

# 测试
pytest -q                                          # 全部
pytest tests/test_llm_split.py -q                  # 单文件
pytest tests/test_llm_split.py::test_split_thinking_with_block   # 单用例
```

- 未配置 linter/formatter;`pytest.ini` 设 `asyncio_mode=auto`、`testpaths=tests`。
- 单测**不依赖**外部服务(RAGFlow/vLLM/PG/Redis 均被 mock 或不触达);改动 `core/llm.py`、`core/guards.py`、`rag/`、`skills/loader.py` 后务必跑对应测试。

## 架构要点(需跨文件理解的部分)

**一切始于 `app/core/agent.py::astream_agent_response`** —— 这是整个 `/chat` 的编排中枢,理解它就理解了系统。它是一个 async 生成器,产出 `(event_type, data)` 元组,由 `app/api/chat.py` 包装成 SSE。事件顺序固定:`meta → thought* → answer* → done`(异常 `error`)。流程:归档用户消息(PG)→ 取 Redis 历史 → 并联 RAGFlow 检索 / 长期事实召回 / skill 加载 → 拼 `input_messages` → 流式调 agent → 引用审计 → 归档助手消息 + 写回 Redis。

**System prompt 由 `create_agent` 自动注入**,不要在 `input_messages` 里手动加 `SYSTEM_PROMPT`。`input_messages` 的拼装顺序是:历史 → facts(SystemMessage)→ RAG(SystemMessage)→ skill(SystemMessage)→ 用户问题(HumanMessage)。

**两层 middleware 在 `_build_agent` 装配**:`SummarizationMiddleware`(历史 token 超 `MAX_HISTORY_TOKENS` 自动摘要旧消息,用**专属非流式**摘要器,tag=`summarizer`,其事件被 `_is_summarizer_event` 过滤,不混入主答复流)和 `ModelCallLimitMiddleware`(run/thread 双限,超限 `exit_behavior="end"` 优雅停止)。agent 与 LLM 实例都按 `thinking` 用 `@lru_cache` 缓存。

**深度思考是 vLLM 透传 + 流式切分两件事**(`app/core/llm.py`):`_make_llm` 通过 `extra_body={"chat_template_kwargs":{"enable_thinking":bool}}` 控制 Qwen3 输出 `<think>…</think>`;`StreamThinkSplitter` 是个状态机,逐 chunk 喂入、按 `<think>` 标签把文本分流成 `thought`/`answer` 两路,并保留 `len(tag)-1` 字符以处理**跨 chunk 的标签边界**。改动切分逻辑必看 `tests/test_llm_split.py`。

**三层记忆**:短期=Redis(`app/memory/short_term.py`,键 `mom:session:{sid}:messages` LIST + `:summary` STRING,每次写刷新 TTL);长期=PG(`app/memory/long_term.py`,`messages` 全量归档、`session_summaries` 带 `last_compressed_message_id` 做**增量压缩 checkpoint**、`memory_facts` 用 **PG 全文检索 `to_tsvector` + ILIKE 兜底**做跨会话召回,**无向量**)。表结构见 `migrations/init.sql` 与 `app/db/models.py`。

**RAG 是纯消费方**(`app/rag/ragflow_client.py`):只调 RAGFlow `POST /api/v1/retrieval`。解析**刻意兼容多版本字段名**(content/content_with_weight/content_ltks 等),改这里别收窄兼容分支。检索失败被捕获降级为空,不中断对话。

**Skill 三段式**:`loader`(frontmatter 解析 + 注入检测)→ `registry`(元数据 upsert 到 PG,**body 只留磁盘**;启动时 `sync_disk_to_db` 对齐)→ `runner`(运行时按名加载并渲染成 SystemMessage 注入)。`load_runtime_skill` 任一步失败返回 `None` 优雅降级。

**安全护栏集中在 `app/core/guards.py`**:`assert_safe_skill_text` 在 skill 上传时拦截提示注入;`wrap_untrusted_context` 给所有外部内容(RAG/facts)加信任边界;`audit_source_citations` 在生成后检查 `[来源N]`,缺失则给答复追加提示。新增任何「把外部文本喂给模型」的路径都应走 `wrap_untrusted_context`。

**并发**:`app/api/chat.py` 用 `_active_sessions` 计数对**同一 session_id 串行化**,二次请求返回 429。

**配置**:全部经 `app/config.py` 的 pydantic-settings 从 `.env` 加载,`get_settings()` 带 `@lru_cache`——改环境变量需重启进程。

## 已知现状(改动时注意)

- `web/index.html` 的 `setMeta` 读取 `compression_triggered/tokens_before/tokens_after`,但后端 `meta` 事件未下发这三个字段(见需求文档 10.9)。
- `add_fact` 已实现但当前无自动抽取管道调用它,长期事实需手工写入(需求文档 10.3)。
- `create_agent(tools=[])` 当前无工具(需求文档 10.7)。
- 推理模型支持本地 vLLM 与第三方 OpenAI 兼容 API(DeepSeek / Moonshot / DashScope / OpenAI 等):配置统一走 `LLM_*`,留空则回退到旧 `VLLM_*`(完全向后兼容);见 `.env.example` 与 `app/config.py` 的 `llm_*` 解析属性。`enable_thinking` 仅在 `LLM_PROVIDER=vllm` 时默认注入。运行时按请求切换模型仍未支持(需求文档 10.11)。
