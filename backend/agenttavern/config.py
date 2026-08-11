"""配置。**分两份，判据是「换台机器要不要改」**。

- `Settings` —— 从 ``.env`` 读（见 ``.env.example``）：端点、key、模型、超时、
  数据目录。**秘密与机器相关**，不进版本控制。
- `Limits` —— 从 ``config.yaml`` 读：各种上限、预算、窗口。它们跟机器无关，
  是**调过就该重测**的数，所以进版本控制，且每个值旁边都记着当初为什么是它。

第三处是 `Capabilities` / `Goal`（session 级，每个故事不一样）——它们的默认值
从 `Limits` 取，单个 session 仍可覆盖。

**代码里不再写这些字面量。** 以前它们散在八个模块的模块级常量里：想知道
"director 到底几轮上限"得先猜它在哪个文件；想调一个数得改代码、跑一遍格式化。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel
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


# --------------------------------------------------------------------------
# config.yaml —— 调参
# --------------------------------------------------------------------------


class _Director(BaseModel):
    max_rounds: int = 24
    soft_rounds: int = 16


class _StateManager(BaseModel):
    max_rounds: int = 4


class _GoalManager(BaseModel):
    max_runs: int = 50
    max_total_tokens: int = 1_000_000
    max_revisions: int = 3


class _Context(BaseModel):
    budget_chars: int = 96_000
    state_inline_limit: int = 200
    tool_read_chars: int = 8_000


class _Segment(BaseModel):
    summary_max_chars: int = 200
    max_runs: int = 6
    min_runs: int = 1


class _Sampling(BaseModel):
    expansion_min_ratio: float = 1.6
    state_value_max_chars: int = 60


class _Windows(BaseModel):
    upstream_runs: int = 3
    state_recent_runs: int = 3


class Limits(BaseModel):
    """`config.yaml` 的形状。

    **这里的默认值只是兜底**——真值在 `config.yaml` 里，那份文件才带着每个数
    当初为什么是它的理由。两处写着同一个数是有意的：文件损坏或缺失时进程仍能起来，
    而不是在 import 阶段炸掉。
    """

    director: _Director = _Director()
    state_manager: _StateManager = _StateManager()
    goal_manager: _GoalManager = _GoalManager()
    context: _Context = _Context()
    segment: _Segment = _Segment()
    sampling: _Sampling = _Sampling()
    windows: _Windows = _Windows()


def load_limits(path: Path | None = None) -> Limits:
    """读 `config.yaml`。**进程内只读一次**（见下面的 `LIMITS`），它不随请求变。"""
    path = path or Path(__file__).with_name("config.yaml")
    if not path.exists():
        return Limits()
    return Limits.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


LIMITS = load_limits()
"""进程级单例。

**模块级常量从它取值**（`MAX_ROUNDS = LIMITS.director.max_rounds`），
调用点因此一个都不用改，测试也仍然能显式传 `max_rounds=` 覆盖单次调用。
"""
