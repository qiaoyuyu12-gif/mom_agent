-- ============================================================
-- mom_agent 初始化建表脚本(纯 PostgreSQL,无 pgvector)
-- 长期记忆:消息归档 + 滚动摘要 + 抽取事实 + skill 元数据
-- ============================================================

-- 会话表:每个 session_id 对应一次连续对话
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,                       -- session_id(由客户端生成)
    user_id      TEXT,                                   -- 可选:绑定的业务用户
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 全量消息归档(每条用户/助手消息都入库,做长期溯源)
CREATE TABLE IF NOT EXISTS messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content      TEXT NOT NULL,                          -- 正式回复(不含 <think>)
    thinking     TEXT,                                   -- 思考过程(可选)
    skill_name   TEXT,                                   -- 本轮使用的 skill(若有)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at);

-- 滚动摘要:每个会话一行,持续覆盖
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id   TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    summary      TEXT NOT NULL DEFAULT '',
    -- 已被压缩进摘要的最后一条消息 id;后续只增量摘要更新部分
    last_compressed_message_id BIGINT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 长期事实库:从对话中抽取的关键事实,跨会话召回
CREATE TABLE IF NOT EXISTS memory_facts (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    user_id      TEXT,
    fact         TEXT NOT NULL,
    keywords     TEXT,                                   -- 空格分隔的关键词,便于 LIKE 召回
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_facts_user_created
    ON memory_facts (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_facts_keywords
    ON memory_facts USING gin (to_tsvector('simple', coalesce(keywords, '')));

-- skill 元数据(实际正文存盘上 .md 文件)
CREATE TABLE IF NOT EXISTS skills (
    name         TEXT PRIMARY KEY,                       -- skill 唯一名(来自 frontmatter)
    description  TEXT,
    trigger      TEXT,                                   -- 可选:触发词/正则
    file_path    TEXT NOT NULL,                          -- 磁盘相对路径
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
