import { describe, expect, it } from "vitest";

import { splitAtProse } from "../src/components/Process";
import { mergeOlderTracePage, nextVisibleChars } from "../src/components/DeveloperPanel";
import { hasCommittedLiveNode, isNodeProseVisible } from "../src/components/Transcript";
import type { Node, TraceEvent } from "../src/protocol";
import { liveFromRunStarted, pathBeforeReroll } from "../src/store";

describe("story timeline regressions", () => {
  it("hides the target turn and its descendants as soon as reroll begins", () => {
    const path: Node[] = [
      { id: "run_1", prose: "第一轮正文" },
      { id: "run_2", parent_id: "run_1", prose: "第二轮正文" },
      { id: "run_3", parent_id: "run_2", prose: "第三轮正文" },
    ];

    expect(pathBeforeReroll(path, "run_2")?.map((node) => node.id)).toEqual(["run_1"]);
  });

  it("uses the dequeued input carried by run_started", () => {
    expect(
      liveFromRunStarted({ type: "run_started", run_id: "run_a", input: "当前轮输入" }),
    ).toEqual({ runId: "run_a", input: "当前轮输入" });
  });

  it("replaces the reading prose with the node editor", () => {
    expect(isNodeProseVisible("run_edit", "run_edit")).toBe(false);
    expect(isNodeProseVisible("run_edit", null)).toBe(true);
  });

  it("does not render a second turn when reconnecting during post-processing", () => {
    const path: Node[] = [{ id: "run_committed", prose: "正文已经原子落盘" }];

    expect(hasCommittedLiveNode(path, "run_committed")).toBe(true);
    expect(hasCommittedLiveNode(path, "run_pending")).toBe(false);
  });

  it("puts post-writer work below the prose without moving goal setup there", () => {
    const events = [
      { actor: "goal_manager", label: "开场规划" },
      { actor: "director", label: "细纲" },
      { actor: "writer", label: "正文" },
      { actor: "orchestrator", label: "正文结束" },
      { actor: "state_manager", label: "状态" },
      { actor: "goal_manager", label: "目标判定" },
    ];

    const [before, after] = splitAtProse(events);
    expect(before.map((event) => event.label)).toEqual(["开场规划", "细纲", "正文"]);
    expect(after.map((event) => event.label)).toEqual(["正文结束", "状态", "目标判定"]);
  });

  it("prepends older trace pages without changing their chronological order", () => {
    const event = (seq: number): TraceEvent => ({
      run_id: "run_trace",
      seq,
      type: "model_request",
      actor: "director",
      label: `事件 ${seq}`,
    });

    expect(mergeOlderTracePage([event(3), event(4)], [event(1), event(2)]).map((e) => e.seq)).toEqual([
      1, 2, 3, 4,
    ]);
  });

  it("reveals long trace fields in bounded chunks", () => {
    expect(nextVisibleChars(4_000, 20_000)).toBe(8_000);
    expect(nextVisibleChars(18_000, 20_000)).toBe(20_000);
  });
});
