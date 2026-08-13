import { useSession } from "../store";

/** 一行通知条。**打断、失败、目标终止都走这里**——它们都是"你需要知道的事"。 */
export function Notice(): JSX.Element | null {
  const notice = useSession((s) => s.notice);
  const dismiss = useSession((s) => s.dismissNotice);
  if (!notice) return null;

  return (
    <div className="flex items-center gap-3 border-b border-line bg-ink-700 px-5 py-2 text-xs text-fg">
      <span className="flex-1">{notice}</span>
      <button className="btn" onClick={dismiss}>
        知道了
      </button>
    </div>
  );
}
