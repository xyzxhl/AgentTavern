"""给 bench 换一份提示词跑（S80）。

取值上调到 YAML 之后，`Variant` 原先那套「往 session 上写 `capabilities` /
`control`」失去了全部着力点——那两个类都没了。而 P19 要求同一批素材跑多档
（五个场景各有各的文风），回归集不能就这么散掉。

**补法是换一份 YAML，不是开一个 `overrides` 后门。** 一个"只有程序能用"的第二
取值入口迟早会有第二个调用者，而那正是 R31 要消掉的东西。这里做的是：把
`prompting/` 整个复制到一个临时目录、在**副本**上改那几档、让装配器读副本。
取值仍然只住在 YAML 里。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .assembler import PROMPT_FILES, path_of, use_dir


@dataclass(frozen=True)
class PromptSetup:
    """一次消融要改的那几处。空的表示原样用仓库里那份。

    只有三种改法，因为消融真正需要的只有这三种：**换一档**（文风、视角、节奏、
    工具描述）、**开关一个工具**（检索、按需展开）、**关掉一个 agent**。
    要改别的就直接编辑副本——这里不做成一套通用的补丁语言，那会变成第二套配置格式。
    """

    choices: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """``{agent: {choice 组名: 选哪一档}}``。"""

    tools: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)
    """``{agent: {工具名: 开不开}}``。"""

    agents: Mapping[str, bool] = field(default_factory=dict)
    """``{agent: 跑不跑}``。关掉一个 agent 是最粗的一档消融——**state_manager
    是唯一真的能关的那个**（原 `Capabilities.state`）。"""

    calls: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """``{名字: {model / stream / extra: …}}``——**这一次调用的旋钮**（S159）。

    它是第四种改法：换个模型、关掉流式、改一下思考参数再跑同一批素材。
    P19 要的正是这个——`writer_lab` 那个「thinking 开 / 关各跑一遍」的实验，
    以前只能靠一个 `complete(thinking=...)` 参数，现在和别的消融同一条路。
    """

    tool_choices: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """``{agent: {工具名: 描述选哪一档}}``。

    **工具描述本身也可以是一个 `choice`**（R31）：`ask_question` 就是，控制权
    三档讲的正是「这个工具该多积极地调用」（S10②）。所以消融要够得着它。
    """

    def empty(self) -> bool:
        return not (self.choices or self.tools or self.tool_choices or self.agents or self.calls)

    def _apply(self, root: Path) -> None:
        for agent, on in self.agents.items():
            path = root / f"{agent}.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["enabled"] = on
            _dump(path, data)

        for agent, groups in self.choices.items():
            path = root / f"{agent}.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            for item in data.get("sections") or []:
                if isinstance(item, dict) and item.get("name") in groups:
                    picked = groups[str(item["name"])]
                    if picked not in (item.get("options") or {}):
                        raise KeyError(f"{agent}.yaml 的 `{item['name']}` 里没有 `{picked}` 这一档")
                    item["selected"] = picked
            _dump(path, data)

        for name, knobs in self.calls.items():
            path = root / f"{name}.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            for key, value in knobs.items():
                if key not in ("model", "stream", "extra"):
                    raise KeyError(f"{name}.yaml 顶上没有 `{key}` 这个旋钮")
                data[key] = value
            _dump(path, data)

        for agent in {*self.tools, *self.tool_choices}:
            path = root / f"{agent}.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            for tool, on in self.tools.get(agent, {}).items():
                _tool_spec(data, agent, tool)["enabled"] = on
            for tool, picked in self.tool_choices.get(agent, {}).items():
                spec = _tool_spec(data, agent, tool)
                if picked not in (spec.get("options") or {}):
                    raise KeyError(f"{agent}.yaml 的 tools.{tool} 里没有 `{picked}` 这一档")
                spec["selected"] = picked
            _dump(path, data)


def _tool_spec(data: Any, agent: str, tool: str) -> dict[str, Any]:
    spec = (data.get("tools") or {}).get(tool)
    if not isinstance(spec, dict):
        raise KeyError(f"{agent}.yaml 的 tools 里没有 `{tool}`")
    return spec


def _dump(path: Path, data: Any) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=10_000),
        encoding="utf-8",
    )


@contextmanager
def prompts_as(setup: PromptSetup) -> Iterator[None]:
    """在这段代码里，装配器读的是按 ``setup`` 改过的那一份副本。

    **副本，不是原件**——bench 跑完不该在仓库里留下改动，而消融最常见的用法
    正是"同一批素材连跑三档"。退出时目录删掉、装配器切回默认。
    """
    if setup.empty():
        yield
        return

    tmp = Path(tempfile.mkdtemp(prefix="prompts-"))
    try:
        # 只复制**当前生效套装**的五份文件到临时根目录。用户选了自定义套装时，
        # 继续复制整个 prompting/ 会连同 `.active-set` 一起带过去，而 `_apply`
        # 改的是根目录默认文件——最终运行反而仍读未修改的自定义子目录。
        for name in PROMPT_FILES:
            shutil.copy2(path_of(name), tmp / f"{name}.yaml")
        setup._apply(tmp)
        use_dir(tmp)
        yield
    finally:
        use_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = ["PromptSetup", "prompts_as"]
