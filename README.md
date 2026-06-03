# mom_agent

嵌入式知识库 Agent 助手:**LangChain + FastAPI + vLLM(Qwen3) + RAGFlow + Redis + PostgreSQL**。

- 知识库问答:复用已部署的 **RAGFlow** 做检索;本项目只负责检索之后的"记忆 + skill + 思考开关 + 生成"。
- 短期记忆:**Redis**(按 session,带 TTL)。
- 长期记忆:**PostgreSQL**(消息归档 + 滚动摘要 + 跨会话事实,无 pgvector)。
- 自定义**上下文压缩中间件**:超阈值自动把旧消息摘要进滚动摘要。
- 大模型:**vLLM 部署的 Qwen3**(OpenAI 兼容接口),对话支持**深度思考 / 普通模式**切换。
- Skill 系统:`markdown + frontmatter` 格式,可上传,前端输入 `/` 选中运行。

---

## 目录结构

```
app/                # 后端
├─ api/             # /chat (SSE)、/skills
├─ core/            # llm 封装、agent 编排、提示词
├─ memory/          # 短期(Redis)、长期(PG)、压缩中间件
├─ rag/             # RAGFlow 检索客户端
├─ skills/          # skill 加载/注册/运行
├─ db/              # SQLAlchemy 引擎 + ORM
├─ config.py
└─ main.py          # FastAPI 入口
web/index.html      # 最小聊天 UI(/ 补全、深度思考开关、skill 上传)
skills/             # 上传的 skill 文件
migrations/init.sql # 建表脚本
docker-compose.yml  # 本地 Postgres + Redis
tests/              # 单测(切分器、skill 解析、RAGFlow 客户端)
```

---

## 快速开始

### 1) 准备外部依赖
- **vLLM**:已部署、可访问,启动时 `--served-model-name Qwen3`(或自定),OpenAI 兼容端点。
- **RAGFlow**:已部署,在其上传操作手册到知识库,记录下 `dataset_id` 与 API Key。

### 2) 启动本地依赖
```bash
docker compose up -d   # 启动 postgres + redis
```
Postgres 容器首启会自动执行 `migrations/init.sql` 完成建表。

### 3) 配置环境
```bash
cp .env.example .env
# 编辑 .env:填入 VLLM_BASE_URL / VLLM_MODEL、RAGFLOW_BASE_URL / RAGFLOW_API_KEY /
# RAGFLOW_DATASET_IDS、(必要时调整 DATABASE_URL / REDIS_URL)
```

### 4) 安装依赖并启动服务
```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

浏览器打开 `http://localhost:8000`,即可看到聊天页。

---

## 使用要点

- **多轮记忆**:同一浏览器会复用 `localStorage` 中的 `session_id`;清除即开启新会话。
- **深度思考**:右上角开关。开启后请求会带 `thinking=true`,vLLM 透传给 Qwen3 chat template
  (`enable_thinking=true`),模型输出中的 `<think>…</think>` 段会被服务器拆分,
  前端在「思考过程」折叠块展示。
- **/ 选 Skill**:在输入框敲 `/` 出现下拉,选中后顶部出现 skill 标签,本次发送将注入该 skill 指令。
- **上传 Skill**:点击「上传 Skill」选择 `.md` 文件;必须含 `name` 与 `description` 的 frontmatter。
  示例见 `skills/sample_troubleshoot.md`。
- **手册上传**:**不在本项目做**,请到 RAGFlow 平台上传/重建索引。

---

## API

### `POST /chat`(SSE)
请求体:
```json
{
  "session_id": "uuid",
  "user_id": "可选业务用户",
  "message": "如何重启网关?",
  "skill": "故障排查",
  "thinking": true
}
```
返回事件:`meta` → `thought*` → `answer*` → `done`(或 `error`)。

### `GET /skills`
返回 `[{name, description, trigger}]`,供前端 `/` 补全。

### `POST /skills/upload`
multipart `file=<.md>`,落 `skills/` 目录并 upsert 到 DB。

---

## 测试

```bash
pytest -q
```
单测覆盖:`<think>` 流式/同步切分、skill 解析与文件名安全化、RAGFlow 客户端响应解析。
依赖外部服务的集成测试需要本机起好 Postgres/Redis/vLLM/RAGFlow,按需扩展。

---

## 端到端验证清单

1. `docker compose up -d`,确认 Postgres/Redis 起来;vLLM/RAGFlow 外部可达。
2. `pip install -r requirements.txt && uvicorn app.main:app --reload`。
3. 打开网页 → 提问知识库内容 → 看到答复 + 来源标签 `[来源N]`。
4. 连续多轮提问,确认引用前文;刷新页面后同 `session_id` 仍记得。
5. 制造长对话(>20 条或 token 超 `MAX_HISTORY_TOKENS`)→ meta 行出现「已触发上下文压缩」,
   window tokens 明显下降,答复仍连贯。
6. 切「深度思考」开关 → 折叠块出现思考过程,关闭后无思考段且更快。
7. 上传 `skills/sample_troubleshoot.md`(或自己写一个),输入 `/` 选中 → 答复按 skill 结构组织。
