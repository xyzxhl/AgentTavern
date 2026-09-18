import { useCallback, useEffect, useState } from "react";

import { api, type CorpusCard, type CorpusLibrary as Library, type PendingChange } from "../api";
import type { Corpus } from "../protocol";
import { useSession } from "../store";

/**
 * 资料库：**文件夹是暂存区，知识库是快照**（S149）。
 *
 * 用户那一侧的动作只有一个——**把文件丢进那个文件夹**。这一页负责把
 * 「文件夹和知识库对不上的地方」摆出来，然后等他按。
 *
 * **系统自己不动手**（S151）。处理一份大资料要花时间也要花钱，而编辑器的
 * 每一次保存都会改文件——自动同步等于让保存动作触发重建。
 *
 * 三样必须看得见，缺一样就退回"静默"：
 *
 * | | 不显示会怎样 |
 * |---|---|
 * | **进度** | 没有进度的等待和卡死分不出来（S144） |
 * | **编码** | 猜了就要说出来；猜错的症状是"这份资料怎么查都查不到"（S153） |
 */
export function CorpusLibrary(): JSX.Element {
  const [library, setLibrary] = useState<Library | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [notice, setNotice] = useState("");
  const sessionId = useSession((s) => s.sessionId);

  /**
   * **每个动作都包在这里面**（S135）：出错就显示出来，不是让它静默消失。
   *
   * 「一个只会抛不会显示的错误，和不报错没有区别」——这条在设定库那边
   * 是踩出来的，不重复第二遍。
   */
  const guard = useCallback(async (work: () => Promise<void>): Promise<void> => {
    setBusy(true);
    setProblem("");
    try {
      await work();
    } catch (err) {
      setProblem((err as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  const reload = useCallback(async () => setLibrary(await api.listCorpora()), []);

  useEffect(() => {
    void guard(reload);
  }, [guard, reload]);

  // **回到这个窗口时重新对一次账。** 用户的动作是「切到文件管理器、丢个文件、
  // 切回来」——回来那一刻正是他期待看见变化的时刻，而这比定时轮询省得多：
  // 每次对账都要把源文件读一遍算 hash（S150）。
  useEffect(() => {
    const again = () => void reload().catch(() => undefined);
    window.addEventListener("focus", again);
    return () => window.removeEventListener("focus", again);
  }, [reload]);

  // 建索引期间才轮询。**它是唯一会自己变的状态**，别的都要用户动手。
  const building = (library?.corpora ?? []).some(
    (c) => c.corpus.build?.state === "pending" || c.corpus.build?.state === "building",
  );
  useEffect(() => {
    if (!building) return;
    const timer = setInterval(() => void reload().catch(() => undefined), 1000);
    return () => clearInterval(timer);
  }, [building, reload]);

  async function act(work: () => Promise<unknown>, said = ""): Promise<void> {
    await guard(async () => {
      await work();
      await reload();
      setNotice(said);
    });
  }

  // **通知会过期，所以它得自己消失。**
  //
  // 「收下了 X，正在建索引」在几秒之后就是**一句假话**——那时卡片上已经写着
  // 「可用」。留着它比不说更糟：屏幕上同时有两个互相矛盾的说法。
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  async function remove(id: string, name: string, users: string[]): Promise<void> {
    // **有故事挂着也照删，但按下之前要看到后果**（S136）。
    // 拦不住的事就更得说清楚——资料是可选的增援不是地基，删了只少一条检索路。
    const warning = users.length ? `\n\n有 ${users.length} 局故事挂着它，会一并摘掉。` : "";
    if (!confirm(`从知识库里删掉「${name}」？${warning}`)) return;
    await act(
      () => api.deleteCorpus(id),
      users.length ? `删掉了「${name}」，${users.length} 局故事已摘掉它。` : `删掉了「${name}」。`,
    );
  }

  const corpora = library?.corpora ?? [];
  const pending = library?.pending ?? [];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">资料库</span>
        <span className="text-[11px] text-dim">
          {corpora.length} 份 · 一份可以给好几局故事用
        </span>
        <div className="flex-1" />
        <button className="btn" onClick={() => void guard(reload)} disabled={busy}>
          重新对账
        </button>
      </header>

      {problem && (
        <p className="border-b border-line bg-ink-800 px-5 py-2 text-[11px] leading-snug text-accent">
          {problem}
        </p>
      )}
      {notice && !problem && (
        <p className="border-b border-line px-5 py-1.5 text-[11px] text-dim">{notice}</p>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto max-w-2xl space-y-4">
          <Folder path={library?.folder ?? ""} />

          {pending.length > 0 && (
            <section>
              <h3 className="panel-title px-0">待办 · {pending.length}</h3>
              <p className="mb-2 text-[11px] leading-snug text-dim">
                文件夹和知识库对不上的地方。
                <b className="font-medium text-fg">按了才会动</b>
                ——处理一份大资料要花时间也要花钱，而你可能只是在文件夹里临时改了点什么。
              </p>
              <div className="space-y-1.5">
                {ordered(pending).map((change) => (
                  <Change
                    key={`${change.kind}:${change.source_name}`}
                    change={change}
                    busy={busy}
                    onImport={() =>
                      void act(
                        () => api.importCorpus(change.source_name),
                        `收下了「${change.source_name}」，正在建索引。`,
                      )
                    }
                    onRebuild={() =>
                      void act(
                        () => api.rebuildCorpus(change.corpus_id),
                        `正在重新处理「${change.source_name}」。`,
                      )
                    }
                    onRename={() =>
                      void act(
                        () => api.renameCorpusSource(change.corpus_id, change.source_name),
                        `记下了新名字「${change.source_name}」，索引没动。`,
                      )
                    }
                    onDelete={() =>
                      void remove(change.corpus_id, change.source_name, change.sessions)
                    }
                  />
                ))}
              </div>
            </section>
          )}

          {library?.broken?.length ? (
            <p className="text-[11px] leading-snug text-accent">
              有 {library.broken.length} 份读不出来（{library.broken.join("、")}）——
              那几个目录里的 `corpus.json` 坏了。**这里点出来而不是跳过**：
              悄悄少一份，你根本不会知道自己丢了什么。
            </p>
          ) : null}

          <section>
            <h3 className="panel-title px-0">知识库 · {corpora.length}</h3>
            {corpora.length === 0 && pending.length === 0 && (
              <p className="text-sm text-dim">
                还没有资料。把 txt / md / docx / pdf 丢进上面那个文件夹，这一页就会出现一条待办。
              </p>
            )}
            <div className="space-y-2">
              {corpora.map((card) => (
                <Card
                  key={card.corpus.corpus_id}
                  card={card}
                  busy={busy}
                  sessionId={sessionId}
                  onAttach={(on) =>
                    void act(
                      () =>
                        on
                          ? api.attachCorpus(sessionId ?? "", card.corpus.corpus_id ?? "")
                          : api.detachCorpus(sessionId ?? "", card.corpus.corpus_id ?? ""),
                      on ? "挂上了，下一轮就能查。" : "已从这局故事上摘掉。",
                    )
                  }
                  onRebuild={() =>
                    void act(
                      () => api.rebuildCorpus(card.corpus.corpus_id ?? ""),
                      "正在重新处理。",
                    )
                  }
                  onDelete={() =>
                    void remove(
                      card.corpus.corpus_id ?? "",
                      card.corpus.name ?? "",
                      card.sessions,
                    )
                  }
                />
              ))}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

/** 那个文件夹在哪。**位置固定不给配**（S149），所以这里只负责让人找得到它。 */
function Folder({ path }: { path: string }): JSX.Element {
  return (
    <section className="rounded border border-line px-4 py-3">
      <p className="text-[11px] leading-snug text-dim">
        把文件丢进这个文件夹，这一页就会看见：
      </p>
      <p className="mt-1 break-all font-mono text-[11px] text-accent">{path || "（读取中）"}</p>
      <p className="mt-1.5 text-[10px] leading-snug text-dim/70">
        纯文本（txt / md / json / yaml / csv）直接读；Word 与 PDF 先解析成文字。
        <b className="font-medium text-fg">扫描件读不出字</b>
        ——那种会当场说清，不会变成一份查不到任何东西的资料。
      </p>
    </section>
  );
}

/**
 * **能动手的排前面，不能动手的沉到底。**
 *
 * `unsupported` 是**从这一页永远解决不了**的一条（要去文件夹里把那个文件删掉
 * 或换个格式），而它按文件名排序时常常压在真正要处理的东西上面——
 * 于是每次打开这一页，第一眼看到的都是那条帮不上忙的。
 */
function ordered(pending: PendingChange[]): PendingChange[] {
  const rank = (kind: PendingChange["kind"]) => (kind === "unsupported" ? 1 : 0);
  return [...pending].sort((a, b) => rank(a.kind) - rank(b.kind));
}

const KIND: Record<PendingChange["kind"], { label: string; hint: string }> = {
  new: { label: "新文件", hint: "还没处理过" },
  modified: { label: "已修改", hint: "处理过，文件后来改了——旧索引仍然能查" },
  renamed: { label: "改名了", hint: "内容没变，只要更新记录" },
  deleted: { label: "源文件没了", hint: "知识库里还有它" },
  unsupported: { label: "不支持", hint: "" },
};

function Change({
  change,
  busy,
  onImport,
  onRebuild,
  onRename,
  onDelete,
}: {
  change: PendingChange;
  busy: boolean;
  onImport: () => void;
  onRebuild: () => void;
  onRename: () => void;
  onDelete: () => void;
}): JSX.Element {
  const { label, hint } = KIND[change.kind];
  return (
    <article className="rounded border border-line px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <span
          className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${
            change.kind === "unsupported" ? "bg-ink-700 text-dim" : "bg-ink-600 text-fg"
          }`}
        >
          {label}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg">
          {change.source_name}
        </span>

        {change.kind === "new" && (
          <button className="btn" onClick={onImport} disabled={busy}>
            收进来
          </button>
        )}
        {change.kind === "modified" && (
          <button className="btn" onClick={onRebuild} disabled={busy}>
            重新处理
          </button>
        )}
        {change.kind === "renamed" && (
          <button className="btn" onClick={onRename} disabled={busy}>
            更新记录
          </button>
        )}
        {change.kind === "deleted" && (
          <button className="btn" onClick={onDelete} disabled={busy}>
            从知识库删掉
          </button>
        )}
      </div>

      {(hint || change.reason) && (
        <p className="mt-1 text-[11px] leading-snug text-dim">{change.reason || hint}</p>
      )}

      {/* **按下之前就要看到后果**（S136）：删除不拦，那就更得说清楚。 */}
      {change.kind === "deleted" && change.sessions.length > 0 && (
        <p className="mt-1 text-[11px] text-accent">
          {change.sessions.length} 局故事挂着它——删掉会一并摘掉，那几局就少一条检索路。
        </p>
      )}
    </article>
  );
}

function Card({
  card,
  busy,
  sessionId,
  onAttach,
  onRebuild,
  onDelete,
}: {
  card: CorpusCard;
  busy: boolean;
  sessionId: string | null;
  onAttach: (on: boolean) => void;
  onRebuild: () => void;
  onDelete: () => void;
}): JSX.Element {
  const corpus = card.corpus;
  const attached = sessionId !== null && card.sessions.includes(sessionId);

  return (
    <article className="rounded border border-line px-4 py-3 transition-colors hover:border-dim">
      <div className="flex items-baseline gap-2">
        <h3 className="min-w-0 flex-1 truncate text-sm text-fg">{corpus.name || "未命名"}</h3>
        <span className="shrink-0 font-mono text-[11px] text-dim">
          {(corpus.chars ?? 0).toLocaleString()} 字
        </span>
      </div>

      <Build corpus={corpus} />

      <p className="mt-1 text-[11px] text-dim">
        {card.sessions.length === 0
          ? "还没有故事挂着它"
          : `${card.sessions.length} 局故事在用${
              card.sessions.length > 1 ? "——改这份资料对它们全都生效" : ""
            }`}
        {corpus.source_name ? ` · 来自 ${corpus.source_name}` : " · 手工导入（没有源文件）"}
      </p>

      {/* **没有会话时说清为什么挂不了。** 按钮直接消失的话，用户会以为
          这一页坏了，或者以为资料本来就不能挂到故事上。 */}
      {!sessionId && (
        <p className="mt-1 text-[11px] text-dim/70">先在左侧开一局故事，才能把它挂上去。</p>
      )}

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {sessionId && (
          <button className="btn" onClick={() => onAttach(!attached)} disabled={busy}>
            {attached ? "从当前故事摘掉" : "挂到当前故事"}
          </button>
        )}
        <button className="btn" onClick={onRebuild} disabled={busy}>
          重新处理
        </button>
        <button className="btn" onClick={onDelete} disabled={busy}>
          删除
        </button>
      </div>
    </article>
  );
}

/** 建索引到哪一步了（S144）。内部检索降级只进诊断 trace（S140）。 */
function Build({ corpus }: { corpus: Corpus }): JSX.Element {
  const build = corpus.build ?? {};
  const state = build.state ?? "pending";
  const done = build.chunks_done ?? 0;
  const total = build.chunks_total ?? 0;

  if (state === "pending" || state === "building") {
    const percent = total ? Math.round((done / total) * 100) : 0;
    return (
      <div className="mt-1.5">
        <div className="flex items-baseline gap-2 text-[11px] text-dim">
          <span>建索引中</span>
          <span className="font-mono">
            {total ? `${done} / ${total} 块 · ${percent}%` : "切块中"}
          </span>
        </div>
        <div className="mt-1 h-1 overflow-hidden rounded bg-ink-700">
          <div className="h-full bg-accent transition-all" style={{ width: `${percent}%` }} />
        </div>
      </div>
    );
  }

  if (state === "failed") {
    return (
      <p className="mt-1.5 text-[11px] leading-snug text-accent">
        建索引失败：{build.error || "没说原因"} —— 按「重新处理」再来一次。
      </p>
    );
  }

  return (
    <p className="mt-1.5 text-[11px] text-dim">
      <span className="text-fg">可用</span> · {total} 块 · 编码 {corpus.encoding || "—"}
    </p>
  );
}
