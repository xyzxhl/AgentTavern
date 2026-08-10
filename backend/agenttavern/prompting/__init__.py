"""提示词组装器：**每轮第一次给模型的输入，由这里装配。**

一个独立组件，两个 agent 共用。它的边界很清楚：

- **管**：这一轮发给模型的第一份消息列表——身份、写法规则、世界设定、状态、
  前情、上文、细纲、破限，谁在前谁在后、各用什么 role。
- **不管**：这之后 loop 边跑边追加的东西——模型的回复、`tool_calls`、
  `role=tool` 的结果、编排层的催促、用户的插话。**那些的位置由协议决定**
  （工具结果必须紧跟它的调用），编排不了，也不该编排。

分两层：

- `assembler.py` —— 装配机器本身，**不认识 director 和 writer**。
- `prompts.py`   —— 谁收到什么，也就是 D33 的那把刀。

## 分文件的判据：按接收方分，不按主题分

`writer.yaml` / `director.yaml` 各自是一个 agent 收到的全部文字。
**这样才看得出隔离有没有被破坏**——文风混进 director、正文混进哪一侧，
翻开对应文件就知道。按主题分（比如「对白」一个文件）会把两侧的内容搅在一起，
而两侧的分工正是整个拓扑的地基。
"""

from .assembler import Prompt, assemble, choice, fill, load, text, titled
from .prompts import (
    STATE_INLINE_LIMIT,
    director_prompt,
    render_state,
    render_worldinfo,
    writer_prompt,
)

__all__ = [
    "STATE_INLINE_LIMIT",
    "Prompt",
    "assemble",
    "choice",
    "director_prompt",
    "fill",
    "load",
    "render_state",
    "render_worldinfo",
    "text",
    "titled",
    "writer_prompt",
]
