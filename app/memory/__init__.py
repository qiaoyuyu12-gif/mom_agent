"""记忆子包:短期(Redis 存储)+ 长期(PostgreSQL 持久化)。

上下文压缩由 LangChain 的 `SummarizationMiddleware` 接管,
在 `app/core/agent.py` 装配 agent 时挂入。
"""
