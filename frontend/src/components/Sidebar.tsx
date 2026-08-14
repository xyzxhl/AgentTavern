import { StatePanel } from "./StatePanel";

/**
 * 侧栏只剩**世界状态**。
 *
 * 过程搬进正文流了：它是每一轮自己的一部分（输入 → 过程 → 正文），放在右边
 * 等于要求读者一边读正文一边扭头。状态不一样——它是**跨轮的当前快照**，
 * 不属于任何一轮，所以留在这里。
 */
export function Sidebar(): JSX.Element {
  return (
    <aside className="flex w-72 shrink-0 flex-col border-l border-line bg-ink-800">
      <div className="border-b border-line">
        <span className="panel-title block">世界状态</span>
      </div>
      <StatePanel />
    </aside>
  );
}
