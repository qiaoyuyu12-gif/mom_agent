# 跨会话历史记录侧边栏 — 设计文档

**日期：** 2026-06-22
**状态：** 已批准，待实现

---

## 1. 问题背景

数据库 `sessions` / `messages` 表已完整归档所有对话，但缺少：
1. 读取历史的后端 API 端点
2. 前端聊天页（`web/` 目录不存在）

用户无法在前端页面查阅、恢复或继续历史对话。

---

## 2. 目标

- 在聊天页面左侧新增会话历史侧边栏
- 按 `user_id` 隔离，只展示当前用户的会话
- 点击历史会话后还原消息、可继续聊天
- 每条会话提供删除按钮，级联清除 DB + Redis 数据

---

## 3. 后端设计

### 3.1 新增文件：`app/api/history.py`

路由前缀 `/history`，在 `main.py` 中注册。

#### `GET /history/sessions`

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | str (query, 必填) | 过滤该用户的会话 |
| `limit` | int (query, 默认 20) | 分页大小 |
| `offset` | int (query, 默认 0) | 分页偏移 |

**逻辑：**
1. 查 `sessions` 表，`WHERE user_id = ?`，`ORDER BY updated_at DESC`
2. 对每条 session，子查询取第一条 `role='user'` 消息的 `content`，截取前 30 字作为标题；若无则用 `created_at` 格式化字符串兜底
3. 同时返回该 session 的 `message_count`（`role IN ('user','assistant')` 的条数）

**响应体：**
```json
[
  {
    "session_id": "uuid",
    "title": "如何重启生产线网关？",
    "updated_at": "2026-06-22T10:30:00Z",
    "message_count": 6
  }
]
```

---

#### `GET /history/sessions/{session_id}/messages`

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | str (query, 必填) | 隔离校验 |

**逻辑：**
1. 查 `sessions` 表，确认 `id = session_id AND user_id = ?`，不匹配返回 404
2. 查 `messages` 表，`WHERE session_id = ? AND role IN ('user','assistant')`，`ORDER BY created_at ASC`

**响应体：**
```json
[
  {
    "id": 1,
    "role": "user",
    "content": "如何重启网关？",
    "thinking": null,
    "created_at": "2026-06-22T10:00:00Z"
  }
]
```

---

#### `DELETE /history/sessions/{session_id}`

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | str (query, 必填) | 隔离校验 |

**逻辑：**
1. 查 `sessions`，确认 `user_id` 匹配，否则 404
2. 删除 `sessions` 行（CASCADE 自动清除 `messages`、`session_summaries`）
3. 调用 `ShortTermMemory(session_id).clear()` 清除 Redis 短期记忆

**响应体：**
```json
{ "deleted": "uuid" }
```

---

### 3.2 `app/memory/short_term.py` 新增 `clear()` 方法

```python
def clear(self) -> None:
    """删除该 session 的所有 Redis 键。"""
    pipe = self._cli.pipeline()
    pipe.delete(self._msg_key)
    pipe.delete(self._sum_key)
    pipe.execute()
```

---

## 4. 前端设计

### 4.1 文件：`web/index.html`（单文件，无构建工具）

#### 布局
```
┌──────────────────────────────────────────────────┐
│  ┌──────────────┐  ┌───────────────────────────┐ │
│  │  侧边栏 260px │  │       聊天区（flex-1）     │ │
│  │              │  │                           │ │
│  │ [+ 新对话]   │  │  消息气泡列表（滚动）      │ │
│  │ ──────────── │  │                           │ │
│  │ 会话标题     │  │                           │ │
│  │ 2小时前  [🗑]│  │  ───────────────────────  │ │
│  │              │  │  [输入框] [思考开关] [发送] │ │
│  └──────────────┘  └───────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

#### localStorage 键
| 键 | 说明 |
|----|------|
| `mom_user_id` | 首次访问自动生成 UUID，后续复用 |
| `mom_session_id` | 当前活跃 session_id |

#### 交互行为

| 操作 | 行为 |
|------|------|
| 页面加载 | 读取/生成 `user_id`；读取/生成 `session_id`；拉取会话列表 |
| 点击"新对话" | 生成新 UUID 作为 session_id，清空聊天区，侧边栏新增项并高亮 |
| 点击历史会话 | 切换 session_id，GET messages，还原气泡，高亮对应侧边栏项 |
| 发送消息 | POST /chat SSE，流式追加 answer 气泡；完成后刷新侧边栏排序 |
| 删除按钮悬停 | 仅在鼠标悬停时显示删除按钮 |
| 确认删除 | `window.confirm()` 确认后 DELETE；若删除当前会话则自动新建 |
| 滚动到底部 | 侧边栏追加加载更多会话（offset += 20） |

#### 保留现有功能
- 深度思考开关（`thinking` 参数）
- 输入 `/` 触发 Skill 下拉补全
- Skill 上传入口

---

## 5. 文件改动清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `app/api/history.py` | 新建 | 三个历史端点 |
| `app/memory/short_term.py` | 修改 | 新增 `clear()` 方法 |
| `app/main.py` | 修改 | 注册 `history_router` |
| `web/index.html` | 新建 | 含侧边栏的完整聊天 UI |

---

## 6. 不在本次范围内

- 会话重命名（标题目前只取第一条消息）
- 消息搜索
- 批量删除
- 登录/认证（user_id 仍为 localStorage 自生成，无服务端校验）
