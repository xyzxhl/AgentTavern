"""配置。从 ``.env`` 读取，见 ``.env.example``。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_base_url: str = "https://api.openai.com/v1"
    """OpenAI 兼容端点。任何实现该格式的服务都能接——OpenRouter、DeepSeek、
    本地 llama.cpp / vLLM 等。不含 ``/chat/completions`` 后缀。
    """

    llm_api_key: str = ""
    llm_model: str = ""

    llm_timeout_seconds: float = 300.0
    """director 的 tool loop 可能跑很久，且 D7 已放弃流式——超时要给足。"""

    llm_max_retries: int = 2
    """仅对瞬时错误（429 / 5xx / 网络）重试。"""

    data_dir: Path = Path("data")


def load_settings() -> Settings:
    return Settings()
