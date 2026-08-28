import { useState } from "react";

import { useSession } from "../store";

/**
 * 长任务（goal，R24 / S92 / S129）。**它跑起来之后会自己开下一轮**，所以
 * 界面上必须一直看得见「推到哪儿了」和「怎么叫停」。
 *
 * 摆在侧栏而不是弹窗：goal 跑的时候用户是在**看**，不是在操作——弹窗要么挡着
 * 正文，要么关掉之后就再也不知道进度了。
 */
export function GoalPanel(): JSX.Element {
  const session = useSession((s) => s.session);
  const progress = useSession((s) => s.goal);
  const send = useSession((s) => s.send);

  const goal = session?.meta?.goal;
  const run = session?.goal_run;

  if (!goal) return <NewGoal />;

  // 进度优先用推过来的那份（每轮都更新）；还没跑过一轮时退回落盘的那份。
  const subgoals = progress?.subgoals ?? run?.plan?.subgoals ?? [];
  const spentRuns = progress?.spent_runs ?? run?.spent_runs ?? 0;
  const spentTokens = progress?.spent_tokens ?? run?.spent_tokens ?? 0;
  // **`null` 是"不设上限"，不是 0**（S97）。`??` 在这里会把它悄悄换成
  // 另一条的值，所以按"有没有推过进度"来选，不按值是否为空。
  const maxRuns = (progress ? progress.max_runs : goal.max_runs) ?? null;
  const maxTokens = (progress ? progress.max_total_tokens : goal.max_total_tokens) ?? null;
  const paused = run?.paused ?? false;
  const done = subgoals.filter((s) => s.done).length;

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
      <p className="text-xs leading-relaxed text-fg">{goal.description}</p>

      {paused && (
        <p className="mt-2 rounded border border-line px-2 py-1 text-[11px] text-dim">
          已暂停{run?.paused_reason ? `：${run.paused_reason}` : ""}
        </p>
      )}

      <div className="mt-3 flex gap-2 text-[11px] text-dim">
        <Meter label="轮" used={spentRuns} cap={maxRuns} />
        <Meter label="token" used={spentTokens} cap={maxTokens} compact />
      </div>

      <div className="mt-4">
        <div className="mb-1.5 text-[11px] uppercase tracking-wider text-dim">
          中间状态 {subgoals.length > 0 && `${done}/${subgoals.length}`}
        </div>
        {subgoals.length === 0 ? (
          <p className="text-[11px] text-dim">还没拆。跑第一轮之后会有。</p>
        ) : (
          <ol className="space-y-1.5">
            {subgoals.map((sub, i) => (
              <li key={i} className="flex gap-2 text-xs">
                <span className={`shrink-0 ${sub.done ? "text-accent" : "text-dim"}`}>
                  {sub.done ? "✓" : "○"}
                </span>
                <span className={`min-w-0 flex-1 ${sub.done ? "text-dim" : "text-fg"}`}>
                  {sub.description}
                </span>
              </li>
            ))}
          </ol>
        )}
        {/* **它们没有先后**（S93）：可以被跳过、以不同顺序实现、同时实现，
            也可能因剧情变化而不再必要。序号只用来指代。 */}
        <p className="mt-2 text-[10px] leading-snug text-dim/70">
          中间状态是脚手架，不是清单——它们没有先后，也可能因剧情变化而不再必要。
        </p>
      </div>

      <div className="mt-4 flex flex-wrap gap-1.5">
        <button
          className="btn"
          onClick={() => send({ type: "goal_control", action: paused ? "resume" : "pause" })}
        >
          {paused ? "继续" : "暂停"}
        </button>
        <AddBudget maxRuns={maxRuns} maxTokens={maxTokens} />
        <button
          className="btn"
          onClick={() => {
            // **放弃是唯一能换目标的路**：目标一旦定下就不能改，因为
            // goal_manager 的整份对话是围着它建起来的。
            if (confirm(`放弃目标「${goal.description.slice(0, 20)}…」？\n对话会一并清掉。`))
              send({ type: "goal_control", action: "abandon" });
          }}
        >
          放弃
        </button>
      </div>
    </div>
  );
}

/** 一条预算。**`cap` 为 `null` 就是没设上限**（S97）：只报已经花了多少，
 * 不画进度条——没有分母的进度条只会误导。 */
function Meter({
  label,
  used,
  cap,
  compact,
}: {
  label: string;
  used: number;
  cap: number | null;
  compact?: boolean;
}): JSX.Element {
  const show = (n: number) => (compact && n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
  const ratio = cap ? Math.min(1, used / cap) : 0;
  return (
    <div className="flex-1">
      <div className="flex justify-between">
        <span>{label}</span>
        <span className={cap !== null && ratio >= 1 ? "text-accent" : ""}>
          {show(used)}
          {cap === null ? " · 不限" : `/${show(cap)}`}
        </span>
      </div>
      <div className="mt-0.5 h-0.5 rounded bg-ink-600">
        {cap !== null && (
          <div className="h-full rounded bg-accent" style={{ width: `${ratio * 100}%` }} />
        )}
      </div>
    </div>
  );
}

/** **预算耗尽是暂停，不是失败**（S96）——调大上限就能接着跑。
 *
 * 没设上限时它整个不出现：没有可加的东西，多一个按不动的按钮只是噪音。 */
function AddBudget({
  maxRuns,
  maxTokens,
}: {
  maxRuns: number | null;
  maxTokens: number | null;
}): JSX.Element | null {
  const send = useSession((s) => s.send);
  if (maxRuns === null && maxTokens === null) return null;
  return (
    <button
      className="btn"
      onClick={() => {
        const more = prompt("再给几轮？（留空 = 从此不设上限）", "10");
        if (more === null) return;
        if (!more.trim()) {
          send({ type: "set_goal_budget", unlimited: true });
          return;
        }
        const n = Number(more);
        if (!Number.isInteger(n) || n < 1) return;
        send({
          type: "set_goal_budget",
          max_runs: maxRuns === null ? null : maxRuns + n,
          // token 上限按同样的比例跟着放，否则加了轮数还是会立刻撞上另一条。
          max_total_tokens:
            maxTokens === null ? null : maxTokens + Math.round((maxTokens / (maxRuns || 1)) * n),
        });
      }}
    >
      加预算
    </button>
  );
}

function NewGoal(): JSX.Element {
  const send = useSession((s) => s.send);
  const [text, setText] = useState("");
  const [runs, setRuns] = useState("");

  // **留空 = 不设上限**（S97）。`Number("") === 0` 会被服务端当场拒掉，
  // 所以空串必须显式变成 `null`，不能靠 `Number(runs) || null` 蒙混过去——
  // 那个写法把 "0" 和 "" 混成同一件事，而它们的意思正好相反。
  const cap = runs.trim() ? Number(runs) : null;
  const bad = cap !== null && (!Number.isInteger(cap) || cap < 1);

  function submit(): void {
    const said = text.trim();
    if (!said || bad) return;
    send({ type: "set_goal", description: said, max_runs: cap, max_total_tokens: null });
    setText("");
    setRuns("");
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
      <p className="mb-2 text-[11px] leading-snug text-dim">
        设一个目标，系统会自己往那儿推：每轮结束判一次，没到就替你写下一条输入。
        <b className="font-medium text-fg">写成可判定的剧情状态</b>
        ，别写抽象的进度——它是判定的唯一依据。
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        placeholder="例：林昭得知照片上那人的姓名与死因，且陈屿明确承认认识他。"
        className="w-full resize-none rounded border border-line bg-ink-900 px-2 py-1.5 text-xs text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
      />
      <div className="mt-2 flex items-center gap-2">
        <input
          value={runs}
          onChange={(e) => setRuns(e.target.value)}
          type="number"
          min={1}
          placeholder="轮数上限（留空不限）"
          className={`w-40 rounded border bg-ink-900 px-2 py-1 text-xs text-fg placeholder:text-dim/60 focus:outline-none ${
            bad ? "border-accent" : "border-line focus:border-dim"
          }`}
        />
        <button className="btn" onClick={submit} disabled={!text.trim() || bad}>
          开始
        </button>
      </div>
      <p className="mt-2 text-[10px] leading-snug text-dim/70">
        {/* **留空是在表态，不是没填**（S97）：不设上限就一直推到达成、失败，
            或者你自己按停。想止损就填个数。 */}
        <b className="font-medium text-fg">留空就是不设上限</b>
        ，一直推到达成、失败或你按暂停。填了数就是止损——模型可能永远判不达标，
        目标本身也可能不可达；到顶是暂停不是失败，随时可以加预算接着跑。
      </p>
      <p className="mt-2 text-[10px] leading-snug text-dim/70">
        设完<b className="font-medium text-fg">立刻开始</b>
        ：一轮都还没写过也一样。它先拆出中间状态、写好第一条输入，再开始推——之后每一轮的输入都由它替你写。
      </p>
    </div>
  );
}
