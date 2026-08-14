import { useState } from "react";

import { api } from "../api";
import type { Capabilities, ControlDimensions, SessionMeta } from "../protocol";
import { useSession } from "../store";

/** 四类配置里的两类（D38 / D78）。**它们性质不同，混成一张表是概念膨胀的来源。** */
const CAP_LABEL: Record<string, [string, string]> = {
  state: ["结构化状态", "关掉则 state_manager 整个不跑，读写工具都不注册。"],
  state_query: [
    "状态按需展开",
    "关掉则状态每轮全量注入。**两件事必须绑在一起**：注入时折叠、再让模型展开是无损降维；没了展开的路，折叠就成了有损筛选。",
  ],
  worldinfo_read: ["设定按需展开", "关掉则所有条目（含标为按需的）一律全文进上下文。"],
  history_search: ["历史检索", "关掉则只有近期全文 + 更早的摘要，模型不能自己回头查。"],
  ask_question: [
    "停下来问用户",
    "关掉则模型只能自己拿主意。**这与控制权不同**：控制权调的是积极性，这里是有没有这个动作。",
  ],
  nudge: ["空手回来时推一句", "模型只回文本没调工具时提醒它。关掉则那一轮就算完。"],
  trace: ["留下 trace 文件", "只影响落盘。事件照常推给界面——两者可以独立开关。"],
};

const HANDLING: Record<string, string> = {
  narrate_and_advance: "转述 + 推进",
  narrate_only: "只转述",
  advance_only: "只推进",
};
const AUTHORITY: Record<string, string> = { low: "用户主导", medium: "均衡", high: "模型主导" };
const DENSITY: Record<string, string> = { sparse: "紧凑", normal: "适中", rich: "铺陈" };
const POV: Record<string, string> = { first: "第一人称", second: "第二人称", third: "第三人称" };
const SUMMARIZE: Record<string, string> = { auto: "自动归段", manual: "只手动" };

/**
 * 会话设置：**元指令 / 控制维度 / 能力开关**三类分开摆。
 *
 * 三者性质不同（D38/D78），混成一张表是概念膨胀的来源：元指令是「这个世界是
 * 什么」，控制维度是「怎么写」，能力开关是「系统能做什么」。
 *
 * 文风与视角的档位**从服务端现读**，不在这里写死——它们定义在提示词 YAML 里，
 * 写死一份的话用户加一档就得等谁想起来同步。
 */
export function SettingsPanel({ onClose }: { onClose: () => void }): JSX.Element {
  const session = useSession((s) => s.session);
  const options = useSession((s) => s.meta?.options);
  const refreshState = useSession((s) => s.refreshState);
  const sessionId = useSession((s) => s.sessionId);
  const [busy, setBusy] = useState(false);

  const meta = session?.meta ?? {};
  const control = session?.control ?? {};
  const caps = session?.capabilities ?? {};

  async function patch(body: {
    meta?: Partial<SessionMeta>;
    control?: Partial<ControlDimensions>;
    capabilities?: Partial<Capabilities>;
  }): Promise<void> {
    if (!sessionId) return;
    setBusy(true);
    try {
      // 服务端整体替换这几个子对象，所以要把当前值带上，只覆盖改动的那几项。
      await api.patchSession(sessionId, {
        ...(body.meta && { meta: { ...meta, ...body.meta } }),
        ...(body.control && { control: { ...control, ...body.control } }),
        ...(body.capabilities && { capabilities: { ...caps, ...body.capabilities } }),
      });
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="absolute inset-0 z-10 flex flex-col bg-ink-900">
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">会话设置</span>
        <div className="flex-1" />
        <button className="btn" onClick={onClose}>
          关闭
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <fieldset disabled={busy} className="mx-auto max-w-2xl space-y-7">
          <Group name="元指令" hint="这个世界是什么。创建时设定，很少改。">
            <Text
              label="标题"
              value={meta.title ?? ""}
              onChange={(title) => void patch({ meta: { title } })}
            />
            <Text
              label="题材与基调"
              value={meta.genre_tone ?? ""}
              onChange={(genre_tone) => void patch({ meta: { genre_tone } })}
            />
            <Text
              label="剧情尺度"
              value={meta.content_scale ?? ""}
              onChange={(content_scale) => void patch({ meta: { content_scale } })}
            />
          </Group>

          <Group name="控制维度" hint="怎么写。每轮都可以被单轮覆盖。">
            <Pick
              label="文风"
              hint="只进 writer。它不参与检索，参与每一个 token。"
              value={control.prose_style ?? ""}
              options={options?.prose_style ?? []}
              onChange={(prose_style) => void patch({ control: { prose_style } })}
            />
            <Pick
              label="视角"
              value={control.pov ?? ""}
              options={options?.pov ?? []}
              labels={POV}
              onChange={(pov) => void patch({ control: { pov } })}
            />
            <Pick
              label="输入处理"
              hint="用户那句话怎么落：要不要转述、要不要推进剧情。"
              value={control.input_handling ?? ""}
              options={Object.keys(HANDLING)}
              labels={HANDLING}
              onChange={(v) =>
                void patch({ control: { input_handling: v as ControlDimensions["input_handling"] } })
              }
            />
            <Pick
              label="剧情控制权"
              hint="直接反映为 ask_question 的调用积极性——越高越少问。"
              value={control.model_authority ?? ""}
              options={Object.keys(AUTHORITY)}
              labels={AUTHORITY}
              onChange={(v) =>
                void patch({
                  control: { model_authority: v as ControlDimensions["model_authority"] },
                })
              }
            />
            <Pick
              label="铺陈密度"
              hint="每单位故事时间写多少字。与故事跨度正交。"
              value={control.expansion_density ?? ""}
              options={Object.keys(DENSITY)}
              labels={DENSITY}
              onChange={(v) =>
                void patch({
                  control: { expansion_density: v as ControlDimensions["expansion_density"] },
                })
              }
            />
            <Toggle
              label="允许替用户的角色说话"
              value={control.allow_speaking_for_user ?? false}
              onChange={(allow_speaking_for_user) =>
                void patch({ control: { allow_speaking_for_user } })
              }
            />
          </Group>

          <Group name="能力开关" hint="系统能做什么。**关闭的能力其工具不注册**——模型看不到。">
            <Pick
              label="摘要"
              hint="自动：段落收束时由系统压缩。手动：摘要由你维护。"
              value={caps.summarize ?? "auto"}
              options={Object.keys(SUMMARIZE)}
              labels={SUMMARIZE}
              onChange={(v) =>
                void patch({ capabilities: { summarize: v as Capabilities["summarize"] } })
              }
            />
            {Object.entries(CAP_LABEL).map(([key, [label, hint]]) => (
              <Toggle
                key={key}
                label={label}
                hint={hint}
                value={Boolean(caps[key as keyof Capabilities] ?? false)}
                onChange={(on) => void patch({ capabilities: { [key]: on } })}
              />
            ))}
            <Number
              label="给 writer 几轮正文上文"
              hint="只用来接住语气，长程记忆是 director 的事。"
              value={caps.upstream_runs ?? 3}
              onChange={(upstream_runs) => void patch({ capabilities: { upstream_runs } })}
            />
            <Number
              label="给 state_manager 几轮示范"
              hint="**整个系统里最贵的一段上下文**：滑动窗口，每轮全变，一点缓存都吃不到。"
              value={caps.state_recent_runs ?? 3}
              onChange={(state_recent_runs) => void patch({ capabilities: { state_recent_runs } })}
            />
          </Group>
        </fieldset>
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

function Text({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
}): JSX.Element {
  const [draft, setDraft] = useState(value);
  return (
    <Row label={label}>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        // 每敲一个字发一次请求太吵，失焦时提交。
        onBlur={() => draft !== value && onChange(draft)}
        className="w-full rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg focus:border-dim focus:outline-none"
      />
    </Row>
  );
}

function Pick({
  label,
  hint,
  value,
  options,
  labels,
  onChange,
}: {
  label: string;
  hint?: string;
  value: string;
  options: string[];
  labels?: Record<string, string>;
  onChange: (next: string) => void;
}): JSX.Element {
  return (
    <Row label={label} hint={hint}>
      <div className="flex flex-wrap gap-1.5">
        {options.map((option) => (
          <button
            key={option}
            onClick={() => onChange(option)}
            className={`btn ${value === option ? "border-accent text-fg" : ""}`}
          >
            {labels?.[option] ?? option}
          </button>
        ))}
      </div>
    </Row>
  );
}

function Toggle({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  value: boolean;
  onChange: (next: boolean) => void;
}): JSX.Element {
  return (
    <Row label={label} hint={hint}>
      <button
        onClick={() => onChange(!value)}
        className={`btn ${value ? "border-accent text-fg" : ""}`}
      >
        {value ? "开" : "关"}
      </button>
    </Row>
  );
}

function Number_({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  onChange: (next: number) => void;
}): JSX.Element {
  return (
    <Row label={label} hint={hint}>
      <input
        type="number"
        min={0}
        value={value}
        onChange={(e) => onChange(globalThis.Number(e.target.value) || 0)}
        className="w-24 rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg focus:border-dim focus:outline-none"
      />
    </Row>
  );
}

const Number = Number_;
