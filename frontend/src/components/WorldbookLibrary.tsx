import { useEffect, useRef, useState } from "react";

import { api, type WorldbookCard } from "../api";
import type { Worldbook, WorldbookBundle } from "../protocol";
import { useSession } from "../store";
import { EditableTitle } from "./EditableTitle";
import { WorldbookPanel } from "./WorldbookPanel";

/**
 * 设定库：**worldbook 是一级资产，不属于任何 session**（S63）。
 *
 * 所以它有自己的入口，而不是只能从某局故事里点进去。一份设定能开出好几局故事，
 * 那件事在这里必须看得见——列表上写着「几局在用」，删之前也拦得住。
 *
 * 从这里开新故事是「先有世界，再有故事」那条路；反过来（先开故事、系统配一份
 * 空设定、边写边补）也走得通，那条在会话列表上。**两条都要留**：要求先建设定
 * 才能开会话，等于在最没有想法的那一刻逼人做决定。
 */
export function WorldbookLibrary(): JSX.Element {
  const [cards, setCards] = useState<WorldbookCard[]>([]);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const newSession = useSession((s) => s.newSession);
  // **要打开哪一份写在路由上**，不写在这个组件的局部状态里——头部那个
  // `〔旧城区 ↗〕` 标签就是这么直接跳进某一份的（R17）。
  const editing = useSession((s) => (s.page.kind === "worldbooks" ? (s.page.open ?? null) : null));
  const go = useSession((s) => s.go);
  const setEditing = (id: string | null) => go({ kind: "worldbooks", open: id ?? undefined });

  /**
   * **每个动作都包在这里面。** 出错就显示出来，不是让它静默消失。
   *
   * 这是批 0 那条教训的第二遍（S73）：那次 `json()` 加了 content-type 校验，
   * 让失败**抛得更早更响**——但**没有人接**，于是同一个症状又来了一次。
   * 「新建无效」的真相是：新建成功了，紧接着的列表刷新 500 了，异常在
   * `try/finally` 里悄悄没了，界面看起来像什么都没发生。
   *
   * **前端的失败要出声，光会抛不算出声。**
   */
  async function guard(work: () => Promise<void>): Promise<void> {
    setBusy(true);
    setProblem("");
    try {
      await work();
    } catch (err) {
      setProblem((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function reload(): Promise<void> {
    setCards(await api.listWorldbooks());
  }

  useEffect(() => {
    void guard(reload);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 条目编辑复用同一个面板：从故事里打开和从这里打开是同一件事，只是入口不同。
  if (editing) {
    return (
      <WorldbookPanel
        worldbookId={editing}
        onClose={() => void reload().then(() => setEditing(null))}
      />
    );
  }

  /** 导入一份文件 → **一份新设定**（R17）。拿到别人分享的文件时要的就是这个。 */
  async function importAsNew(picked: File): Promise<void> {
    await guard(async () => {
      const bundle = JSON.parse(await picked.text()) as WorldbookBundle;
      const book = await api.importAsNewWorldbook(bundle);
      await reload();
      setEditing(book.id ?? null);
    });
    if (file.current) file.current.value = "";
  }

  async function create(): Promise<void> {
    await guard(async () => {
      const book = await api.createWorldbook({ name: "新的设定" });
      await reload();
      setEditing(book.id ?? null);
    });
  }

  async function start(book: Worldbook): Promise<void> {
    await guard(() => newSession(book.id ?? undefined));
  }

  /** **点标题就地改**（S134 的同一条判据）：不再是「改名按钮 + 浏览器弹窗」。 */
  async function rename(card: WorldbookCard, name: string): Promise<void> {
    await guard(async () => {
      await api.patchWorldbook(card.worldbook.id ?? "", name);
      await reload();
    });
  }

  async function remove(card: WorldbookCard): Promise<void> {
    // **有故事在用就不让删**：服务端也会拦（409），这里先说清是为什么。
    if (card.sessions.length) {
      setProblem(`还有 ${card.sessions.length} 局故事开在这份设定上。先删掉那些故事。`);
      return;
    }
    if (!confirm(`删除「${card.worldbook.name || "未命名"}」？`)) return;
    await guard(async () => {
      await api.deleteWorldbook(card.worldbook.id ?? "");
      await reload();
    });
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">设定库</span>
        <span className="text-[11px] text-dim">{cards.length} 份 · 一份可以开好几局故事</span>
        <div className="flex-1" />
        <button className="btn" onClick={() => file.current?.click()} disabled={busy}>
          导入
        </button>
        <input
          ref={file}
          type="file"
          accept=".json"
          className="hidden"
          onChange={(e) => {
            const picked = e.target.files?.[0];
            if (picked) void importAsNew(picked);
          }}
        />
        <button className="btn" onClick={() => void create()} disabled={busy}>
          + 新建
        </button>
      </header>

      {/* **失败要出声**（S73）：静默消失的异常正是「新建无效」那次的真相。 */}
      {problem && (
        <p className="border-b border-line bg-ink-800 px-5 py-2 text-[11px] leading-snug text-accent">
          {problem}
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto max-w-2xl space-y-2">
          {cards.length === 0 && (
            <p className="text-sm text-dim">
              还没有设定。新建一份，或者直接开一局故事——系统会给它配一份空的，边写边补。
            </p>
          )}

          {cards.map((card) => (
            <article
              key={card.worldbook.id}
              className="rounded border border-line px-4 py-3 transition-colors hover:border-dim"
            >
              <div className="flex items-baseline gap-2">
                <h3 className="flex min-w-0 flex-1">
                  <EditableTitle
                    value={card.worldbook.name ?? ""}
                    onCommit={(name) => void rename(card, name)}
                    className="min-w-0 flex-1 truncate text-sm text-fg"
                  />
                </h3>
                <span className="shrink-0 font-mono text-[11px] text-dim">{card.entries} 条</span>
              </div>

              {/* **读不出来的条目要点出来**（S73）：悄悄少一条比报错更糟——
                  模型据此写出矛盾的剧情，而用户根本不知道自己丢了什么。 */}
              {card.broken?.length > 0 && (
                <p className="mt-1 text-[11px] text-accent">
                  有 {card.broken.length} 条读不出来（{card.broken.join("、")}）——
                  多半是旧格式，服务启动时会自动搬迁；还在的话就是那个文件坏了。
                </p>
              )}

              {/* **谁在用**：改设定对它们全都生效，删之前更得看得见（S63）。 */}
              <p className="mt-1 text-[11px] text-dim">
                {card.sessions.length === 0 ? (
                  "还没有故事开在上面"
                ) : (
                  <span className={card.sessions.length > 1 ? "text-accent" : ""}>
                    {card.sessions.length} 局故事在用
                    {card.sessions.length > 1 && "——改这份设定对它们全都生效"}
                  </span>
                )}
              </p>

              <div className="mt-2.5 flex flex-wrap gap-1.5">
                <button className="btn" onClick={() => void start(card.worldbook)}>
                  在这上面开新故事
                </button>
                <button className="btn" onClick={() => setEditing(card.worldbook.id ?? null)}>
                  编辑条目
                </button>
                <button className="btn" onClick={() => void remove(card)}>
                  删除
                </button>
              </div>
            </article>
          ))}

          {cards.length > 0 && (
            <p className="pt-2 text-[10px] leading-snug text-dim/70">
              改设定<b className="font-medium text-fg">立刻对所有故事生效</b>
              ——它们是「这个世界是什么」，改它等于改角色卡。已经写出来的剧情不会被改动：
              每局故事的<b className="font-medium text-fg">初始状态</b>在它自己的第一轮就冻结了。
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
