"""
应用配置模块。

职责:
- 集中读取 .env 中的运行时配置(LLM/vLLM、RAGFlow、Redis、PostgreSQL、压缩阈值等)。
- 使用 pydantic-settings 做类型校验,启动即失败优于运行时 KeyError。

推理模型配置(2026-06 起):
- 引入统一的 `LLM_*` 配置,支持任意 **OpenAI 兼容** 端点
  (本地 vLLM、DeepSeek、Moonshot、Qwen DashScope、OpenAI 等)。
- 历史 `VLLM_*` 字段保留作为回退默认值,只配置老变量也能继续工作。
- `LLM_PROVIDER` 仅用于决定是否注入 Qwen3 专用的
  `chat_template_kwargs.enable_thinking`(vLLM 才注入),与 base_url 无关。
"""

import json
from functools import lru_cache
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。所有字段经 .env 注入。"""

    # ---------- 应用 ----------
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # ---------- vLLM / Qwen3(历史变量,保留作为 LLM_* 的回退默认) ----------
    # OpenAI 兼容端点 base_url,例如 http://host:8000/v1
    VLLM_BASE_URL: str
    # vLLM 通常不校验 key,占位即可
    VLLM_API_KEY: str = "EMPTY"
    # 模型名(--served-model-name)
    VLLM_MODEL: str
    VLLM_TEMPERATURE: float = 0.3
    VLLM_MAX_TOKENS: int = 2048

    # ---------- 通用 LLM(支持第三方 OpenAI 兼容 API) ----------
    # provider 取值:
    #   - "vllm"        : 本地 vLLM(默认,会注入 Qwen3 的 enable_thinking 开关)
    #   - "openai"      : OpenAI 官方 / 任意 OpenAI 兼容第三方(DeepSeek/Moonshot/
    #                     DashScope/智谱/MiniMax 等),默认不注入 enable_thinking
    # 与 base_url 解耦:provider 只影响 extra_body 注入策略与日志标签。
    LLM_PROVIDER: str = "vllm"
    # 以下字段若留空,则回退到对应 VLLM_* 字段,保证旧 .env 仍可用
    LLM_BASE_URL: Optional[str] = None
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: Optional[str] = None
    LLM_TEMPERATURE: Optional[float] = None
    LLM_MAX_TOKENS: Optional[int] = None
    # 是否在每次请求附加 Qwen3 思考开关
    # `chat_template_kwargs.enable_thinking`。留空时按 provider 自动判定:
    # vllm → True,其它 → False。第三方 API 多数不识别此参数,强行附加会被忽略
    # 或报错(如 DashScope 严格模式),所以默认关闭。
    LLM_ENABLE_THINKING_PARAM: Optional[bool] = None
    # 额外透传给底层 OpenAI 客户端的 extra_body(JSON 字符串),
    # 用于供应商特定参数(如 DeepSeek 的 `enable_reasoning`、OpenAI 的 `reasoning_effort`)。
    # 与系统自动注入的 enable_thinking 合并,本字段优先级更高。
    LLM_EXTRA_BODY_JSON: str = ""

    # ---------- RAGFlow ----------
    RAGFLOW_BASE_URL: str
    RAGFLOW_API_KEY: str
    # 逗号分隔的 dataset_id 列表
    RAGFLOW_DATASET_IDS: str = ""
    RAGFLOW_TOP_K: int = 6
    RAGFLOW_SIMILARITY_THRESHOLD: float = 0.2
    RAGFLOW_VECTOR_WEIGHT: float = 0.3

    # ---------- Redis 短期记忆 ----------
    REDIS_URL: str
    REDIS_TTL_SECONDS: int = 7 * 24 * 3600

    # ---------- PostgreSQL 长期记忆 ----------
    DATABASE_URL: str

    # ---------- 上下文压缩(给 SummarizationMiddleware 用) ----------
    # 触发摘要前允许的最大 token 数
    MAX_HISTORY_TOKENS: int = 3000
    # 触发后保留的最新消息条数
    KEEP_RECENT_MESSAGES: int = 6

    # ---------- 模型调用次数限制(ModelCallLimitMiddleware) ----------
    # 单次 /chat 请求最多调用模型多少次
    MODEL_CALL_RUN_LIMIT: int = 4
    # 一个 session(thread)累计最多调用模型多少次
    MODEL_CALL_THREAD_LIMIT: int = 200

    # ---------- Harness 中间件开关(app/core/middleware.py) ----------
    # 进度事件:模型调用前后通过自定义事件下发「生成中/已生成」,提升流式体验
    ENABLE_PROGRESS_EVENTS: bool = True
    # 模型重试:vLLM 瞬时错误(超时/5xx)指数退避重试
    ENABLE_MODEL_RETRY: bool = True
    MODEL_RETRY_MAX: int = 2
    # PII 脱敏:对输入做脱敏。注意运维问答常含 IP,故默认不脱敏 ip;
    # 仅脱敏邮箱与信用卡。需要时在 .env 用逗号分隔扩展(email/credit_card/ip/mac_address/url)
    ENABLE_PII_REDACTION: bool = True
    PII_TYPES: str = "email,credit_card"

    # ---------- Skill ----------
    SKILLS_DIR: str = "./skills"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("RAGFLOW_DATASET_IDS")
    @classmethod
    def _strip(cls, v: str) -> str:
        # 允许空串;实际取列表时再 split
        return v.strip()

    @property
    def ragflow_dataset_id_list(self) -> List[str]:
        """把逗号分隔字符串解析为 dataset_id 列表(去空白)。"""
        if not self.RAGFLOW_DATASET_IDS:
            return []
        return [x.strip() for x in self.RAGFLOW_DATASET_IDS.split(",") if x.strip()]

    @property
    def pii_type_list(self) -> List[str]:
        """把逗号分隔的 PII 类型解析为列表(去空白、去空项)。"""
        return [x.strip() for x in (self.PII_TYPES or "").split(",") if x.strip()]

    # ============================================================
    # LLM 解析属性:统一对外暴露「最终生效」的推理模型配置
    # 优先级:LLM_* > VLLM_*(回退)
    # ============================================================

    @property
    def llm_provider(self) -> str:
        """provider 标识,小写归一化,未识别时按 openai 兼容处理。"""
        p = (self.LLM_PROVIDER or "vllm").strip().lower()
        return p if p in {"vllm", "openai"} else "openai"

    @property
    def llm_base_url(self) -> str:
        """最终使用的 base_url(LLM_BASE_URL 优先,空则回退到 VLLM_BASE_URL)。"""
        return (self.LLM_BASE_URL or self.VLLM_BASE_URL).strip()

    @property
    def llm_api_key(self) -> str:
        """最终使用的 api_key(空则回退到 VLLM_API_KEY,vLLM 通常占位即可)。"""
        return (self.LLM_API_KEY or self.VLLM_API_KEY or "EMPTY").strip()

    @property
    def llm_model(self) -> str:
        """最终使用的 model 名(LLM_MODEL 优先,空则回退到 VLLM_MODEL)。"""
        return (self.LLM_MODEL or self.VLLM_MODEL).strip()

    @property
    def llm_temperature(self) -> float:
        """温度(LLM_TEMPERATURE 优先,空则回退到 VLLM_TEMPERATURE)。"""
        return self.LLM_TEMPERATURE if self.LLM_TEMPERATURE is not None else self.VLLM_TEMPERATURE

    @property
    def llm_max_tokens(self) -> int:
        """最大生成 token 数(LLM_MAX_TOKENS 优先,空则回退到 VLLM_MAX_TOKENS)。"""
        return self.LLM_MAX_TOKENS if self.LLM_MAX_TOKENS is not None else self.VLLM_MAX_TOKENS

    @property
    def llm_inject_enable_thinking(self) -> bool:
        """
        是否注入 Qwen3 专用的 `chat_template_kwargs.enable_thinking`。
        - LLM_ENABLE_THINKING_PARAM 显式设置时按其值
        - 否则:provider=vllm 时 True,其它 False
        """
        if self.LLM_ENABLE_THINKING_PARAM is not None:
            return bool(self.LLM_ENABLE_THINKING_PARAM)
        return self.llm_provider == "vllm"

    @property
    def llm_extra_body(self) -> Dict[str, Any]:
        """解析 LLM_EXTRA_BODY_JSON 为 dict;非法 JSON 视为空(并不抛错以避免线上中断)。"""
        raw = (self.LLM_EXTRA_BODY_JSON or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


@lru_cache
def get_settings() -> Settings:
    """单例配置,避免每次请求重复解析环境变量。"""
    return Settings()  # type: ignore[call-arg]
