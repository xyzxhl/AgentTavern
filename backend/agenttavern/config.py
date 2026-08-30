"""配置。**分两份，判据是「换台机器要不要改」**。

- `Settings` —— 从 ``.env`` 读（见 ``.env.example``）：端点、key、模型、超时、
  数据目录。**秘密与机器相关**，不进版本控制。
- `Limits` —— 从 ``config.yaml`` 读：各种上限、预算、窗口。它们跟机器无关，
  是**调过就该重测**的数，所以进版本控制，且每个值旁边都记着当初为什么是它。

**曾经的第三处（`Capabilities`，session 级）已经没了**（S80）：那些开关整体
上调到了提示词层——它们全都在改「模型收到什么」，就该和提示词住在同一份 YAML 里。
只有两条落在这里：`stream`（关于交付）与 `trace`（关于运维），两条都跟模型无关。

**代码里不再写这些字面量。** 以前它们散在八个模块的模块级常量里：想知道
"director 到底几轮上限"得先猜它在哪个文件；想调一个数得改代码、跑一遍格式化。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # **三把 key 都 `repr=False`**：pytest 的断言自省会把整个 `Settings` 打出来，
    # 于是任何一次涉及它的失败都把 key 写进日志——CI 的日志是留档的。
    # 踩过一次：加完 RERANK_* 之后一条测试失败，key 就在输出里。
    # 用 `repr=False` 而不是 `SecretStr`，是为了不动任何一处调用点（`.get_secret_value()`
    # 会散到十几个地方，而漏改一处的症状是把字面量 `**********` 发去做鉴权）。

    llm_base_url: str = "https://api.openai.com/v1"
    """OpenAI 兼容端点。任何实现该格式的服务都能接——OpenRouter、DeepSeek、
    本地 llama.cpp / vLLM 等。不含 ``/chat/completions`` 后缀。
    """

    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = ""

    llm_timeout_seconds: float = 300.0
    """director 的 tool loop 可能跑很久，且 P21 已放弃流式——超时要给足。"""

    llm_max_retries: int = 2
    """仅对瞬时错误（429 / 5xx / 网络）重试。"""

    embed_base_url: str = ""
    """embeddings 的端点。**独立的一组**（S140）——多数人的 chat 与 embedding
    未必在同一家。留空则回落到 `llm_base_url`。
    """

    embed_api_key: str = Field(default="", repr=False)
    embed_model: str = ""
    """**留空 = 不启用向量那一路**，整条链路走纯 BM25（S140）。

    不去拿 `llm_model` 猜一个：猜错的后果是每次检索都对着一个不存在的模型报错，
    而症状只是"检索总是查不到"。
    """

    rerank_provider: str = ""
    """哪一家的 reranker：`cohere` / `jina` / `voyage` / `qwen`（S141）。

    **留空 = 用 chat 模型重排**（`corpus/rerank.py`），那一版只能排序、不能否决，
    但它不需要任何额外端点。同 embeddings 那条：**降级不是拒绝启用**。

    OpenAI SDK 没有 rerank 这个面，各家形状自己定，所以这里要指名道姓；
    写了不认得的名字**当场报错**，不猜。
    """

    rerank_base_url: str = ""
    """留空则用那一家的官方地址。自建或代理才需要填。"""

    rerank_api_key: str = Field(default="", repr=False)
    rerank_model: str = ""
    """**留空同样是不启用**——不去拿别处的模型名猜一个。"""

    data_dir: Path = Path("data")

    stream: bool = True
    """**边生成边推**（S118）。关掉则一轮写完才整段出现。

    **它在服务级，不在 session 上**（S80）：S118 自己就写着「这是这组里唯一一条
    关于交付而不是关于能力的」——开关它不改变模型收到什么、也不改变产出，
    只改变用户什么时候看到。别的开关全都上调到了提示词层，而这一条上调到服务。

    P21 说的是「不为保住逐 token 流式而扭曲 agent 结构」。这条不扭曲任何结构：
    四个 agent 的顺序、工具循环、契约、重放全都不变，流式只是多了一条旁路。
    """

    trace: bool = False
    """trace 落不落盘。**运维，跟模型完全无关**（S80），所以也在服务级。

    `developer` 开着时强制落盘（S113）——查问题总是事后，记了不给读回来的路
    等于没记。
    """

    developer: bool = False
    """**开发者模式，启动服务时选**（S113）。开了才额外记录每次模型往返的完整
    输入输出（`Tracer.exchange` → `MODEL_REQUEST` 事件），并强制 trace 落盘。

    **它是服务级的**：「这台服务是拿来查问题的还是拿来玩的」不该每个故事各答
    一遍。上面那条 `trace` 管的是另一件事——它决定要不要留 trace 文件，
    这条决定 trace 里有没有原文。

    默认关闭的理由是体量：一次往返的 messages 动辄几万字，四个 agent 加起来
    一轮上十万。前端的开发者面板也据此显隐。
    """


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
    max_rounds: int = 4
    # `max_runs` / `max_total_tokens` **已删除**（S97）：预算不再有默认值，
    # 留空就是不设上限。一个配置里的默认数只会在用户没要求的时候把他拦下来。


class _Context(BaseModel):
    budget_chars: int = 96_000
    state_inline_limit: int = 200
    tool_read_chars: int = 8_000


class _Segment(BaseModel):
    max_runs: int = 6
    min_runs: int = 1


class _Sampling(BaseModel):
    expansion_min_ratio: float = 1.6
    state_value_max_chars: int = 60


class _Windows(BaseModel):
    upstream_runs: int = 3
    state_recent_runs: int = 3


class _Corpus(BaseModel):
    chunk_chars: int = 400
    chunk_overlap: int = 80
    embed_batch: int = 10
    candidates: int = 20
    top_k: int = 5
    min_coverage: float = 0.5
    min_similarity: float = 0.35
    rrf_k: int = 60


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
    corpus: _Corpus = _Corpus()


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
