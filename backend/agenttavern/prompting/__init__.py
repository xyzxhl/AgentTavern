"""提示词组装器：**每轮第一次给模型的输入，由这里装配。**

一个独立组件，四个 agent 共用。分两层：

- `assembler.py` —— 装配机器本身，**不认识 director 和 writer**。
- `prompts.py`   —— 谁收到什么，也就是 D33 的那把刀。

**YAML 的写法与全部装配约定见 `README.md`。** 那份文件是唯一说明——它以前抄在
每份 YAML 的头部，四份各一遍，改一处就有三处过时。
"""

from .assembler import Prompt, assemble, choice, choices_of, fill, load, text, titled
from .prompts import (
    STATE_INLINE_LIMIT,
    director_prompt,
    player_character,
    render_delta,
    render_recent,
    render_state,
    render_worldinfo,
    state_manager_prompt,
    writer_prompt,
)

__all__ = [
    "STATE_INLINE_LIMIT",
    "Prompt",
    "assemble",
    "choice",
    "choices_of",
    "director_prompt",
    "player_character",
    "fill",
    "load",
    "render_delta",
    "render_recent",
    "render_state",
    "render_worldinfo",
    "state_manager_prompt",
    "text",
    "titled",
    "writer_prompt",
]
