import { useState } from "react";

import { type Appearance, type Mode, type ProseFont, apply, load } from "../appearance";
import { useSession } from "../store";

/**
 * 全局设置，**只有两组**（D174/D178）。
 *
 * 「会话设置」整个没了：控制维度与能力开关上调到了提示词层（去 agent 管理），
 * 题材与尺度是 worldbook 条目（去设定库），标题在会话列表上改。剩下的两组是：
 *
 * - **界面** —— 色彩模式、正文字体与字号。这是「这台机器上的这个人怎么看得
 *   舒服」，所以它落在浏览器本地，和后端一个字节都不来往。
 * - **服务** —— 流式、trace、开发者模式。**只读**：它们在 `.env` 里，
 *   启动服务时就定了（D156）。做成可写的话，「这台服务是拿来查问题的还是拿来
 *   玩的」就成了一个每局故事都要重答一遍的问题。
 */
export function SettingsPage(): JSX.Element {
  const meta = useSession((s) => s.meta);
  const [look, setLook] = useState<Appearance>(load);

  function change(patch: Partial<Appearance>): void {
    const next = { ...look, ...patch };
    setLook(next);
    apply(next);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">全局设置</span>
        <span className="text-[11px] text-dim">这台机器怎么看，和这台服务怎么跑</span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto max-w-2xl space-y-7">
          <Group name="界面" hint="存在这台浏览器里，不跟着故事走。">
            <Row label="色彩模式">
              <Pick<Mode>
                now={look.mode}
                options={[
                  ["system", "跟随系统"],
                  ["dark", "深色"],
                  ["light", "浅色"],
                ]}
                onPick={(mode) => change({ mode })}
              />
            </Row>
            <Row label="正文字体" hint="只管正文那一块；界面别处一直是无衬线。">
              <Pick<ProseFont>
                now={look.font}
                options={[
                  ["serif", "衬线"],
                  ["sans", "无衬线"],
                ]}
                onPick={(font) => change({ font })}
              />
            </Row>
            <Row label="正文字号">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  min={13}
                  max={22}
                  value={look.size}
                  onChange={(e) => change({ size: Number(e.target.value) })}
                  className="w-48 accent-accent"
                />
                <span className="font-mono text-xs text-dim">{look.size}px</span>
              </div>
            </Row>
            <p className="prose-body max-w-lg border-l-2 border-line pl-3">
              雨还在下。她把伞收了，抖了两下，靠在门边——这一段是给你看当前设置长什么样的。
            </p>
          </Group>

          <Group name="服务" hint="在 .env 里定，启动时就固定了。这里只读。">
            <Row label="模型">
              <span className="font-mono text-xs text-fg">{meta?.model || "（未配置）"}</span>
            </Row>
            <Row label="边写边显示" hint="关掉则一轮写完才整段出现，产出完全一样。">
              <State on={meta?.stream ?? true} />
            </Row>
            <Row label="开发者模式" hint="额外记录每次模型往返的完整输入输出，并强制 trace 落盘。">
              <State on={meta?.developer ?? false} />
            </Row>
          </Group>
        </div>
      </div>
    </div>
  );
}

function Group({
  name,
  hint,
  children,
}: {
  name: string;
  hint: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <section>
      <h2 className="text-xs font-medium text-fg">{name}</h2>
      <p className="mb-3 mt-0.5 text-[11px] text-dim">{hint}</p>
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div className="flex items-start gap-4">
      <div className="w-44 shrink-0 pt-1">
        <div className="text-xs text-fg">{label}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-snug text-dim">{hint}</div>}
      </div>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

function Pick<T extends string>({
  now,
  options,
  onPick,
}: {
  now: T;
  options: [T, string][];
  onPick: (next: T) => void;
}): JSX.Element {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map(([value, label]) => (
        <button
          key={value}
          onClick={() => onPick(value)}
          className={`btn ${now === value ? "border-accent text-fg" : ""}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function State({ on }: { on: boolean }): JSX.Element {
  return <span className="text-xs text-dim">{on ? "开" : "关"}</span>;
}
