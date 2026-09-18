"""提示词组装器：**每轮第一次给模型的输入，由这里装配。**

一个独立组件，四个 agent 加两次单次调用共用（`AGENTS` / `CALLS`，S147）。分两层：

- `assembler.py` —— 装配机器本身，**不认识 director 和 writer**。
- `prompts.py`   —— 谁收到什么，也就是 S3 的那把刀。

**一份 YAML = 一个 agent 收到的全部文字**（S3/S76/R31）：`sections`（消息串）、
`tools`（工具描述）、`specials`（运行时另外追加的整条消息）、`rendering`（嵌在
渲染里的短句）、`enabled`（这个 agent 跑不跑）。写法见 `assembler.py` 开头。
"""

from .assembler import (
    AGENTS,
    CALLS,
    PROMPT_FILES,
    Prompt,
    PromptSnapshot,
    assemble,
    call_of,
    capture_snapshot,
    current_snapshot,
    enabled,
    fill,
    load,
    param,
    path_of,
    rendering,
    save,
    selected,
    special,
    special_messages,
    special_on,
    special_text,
    tool_description,
    tool_enabled,
    using_snapshot,
)
from .lab import PromptSetup, prompts_as
from .prompts import (
    STATE_INLINE_LIMIT,
    director_prompt,
    fold_depth,
    player_character,
    render_delta,
    render_recent,
    render_state,
    render_voices,
    render_worldinfo,
    state_manager_prompt,
    state_recent_runs,
    upstream_runs,
    writer_prompt,
)

__all__ = [
    "AGENTS",
    "CALLS",
    "PROMPT_FILES",
    "PromptSetup",
    "STATE_INLINE_LIMIT",
    "Prompt",
    "PromptSnapshot",
    "assemble",
    "capture_snapshot",
    "call_of",
    "current_snapshot",
    "director_prompt",
    "fold_depth",
    "enabled",
    "fill",
    "load",
    "param",
    "path_of",
    "player_character",
    "prompts_as",
    "render_delta",
    "render_recent",
    "render_state",
    "render_voices",
    "render_worldinfo",
    "rendering",
    "save",
    "selected",
    "special",
    "special_messages",
    "special_on",
    "special_text",
    "state_manager_prompt",
    "state_recent_runs",
    "tool_description",
    "tool_enabled",
    "using_snapshot",
    "upstream_runs",
    "writer_prompt",
]
