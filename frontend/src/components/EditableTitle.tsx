import { useEffect, useRef, useState } from "react";

/**
 * 就地改名。**界面上所有"给这个东西起个名字"都走这里**——同 [S134](../../../docs/spec/UI.md)
 * 那条判据的第二次应用：一件事只有一种交互形态。
 *
 * 形态定为**点名字，名字当场变成输入框**，而不是「一个改名按钮 + 一个弹窗」。
 * 弹窗那一版有两处不好：`prompt()` 是浏览器的东西，长得和整个界面不是一路；
 * 而且它把"叫什么"从它所在的位置搬走了——改完要回到列表里才看得见结果。
 *
 * 提交时机：**回车或失焦提交，Esc 取消。** 空白或没改动一律当取消——
 * 一个不小心点中标题又点开的操作不该把名字清成空的。
 *
 * ## 两种进入方式
 *
 * 默认是**点文字自己进**（设定库的卡片、设定页的标题）。
 *
 * 会话列表是例外：那一行的文字本身就是"打开这局故事"的按钮，点它改名就把
 * 主要动作顶掉了。所以那里由外部的 ✎ 触发（传 `editing`），此时文字**不可点**
 * ——避免同一处文字在两个页面上点出两种结果。
 */
export function EditableTitle({
  value,
  onCommit,
  className = "",
  placeholder = "未命名",
  editing: controlled,
  onEditingChange,
}: {
  value: string;
  /** 只在真的改了之后调用。失败由调用方显示——这里不接管报错。 */
  onCommit: (next: string) => void | Promise<void>;
  /** 同时作用在文字和输入框上，两种形态才不会互相跳动。 */
  className?: string;
  placeholder?: string;
  /** 传了就是受控：由外部决定什么时候进编辑，文字本身不再可点。 */
  editing?: boolean;
  onEditingChange?: (editing: boolean) => void;
}): JSX.Element {
  const [own, setOwn] = useState(false);
  const editing = controlled ?? own;
  const [draft, setDraft] = useState(value);
  const input = useRef<HTMLInputElement>(null);

  function setEditing(next: boolean): void {
    setOwn(next);
    onEditingChange?.(next);
  }

  useEffect(() => {
    if (!editing) return;
    setDraft(value);
    // 全选：改名十次有九次是整个换掉，而不是在原名字上补两个字。
    //
    // **必须等这一次点击结束**。进编辑是 click 触发的，在同一拍里 `select()`，
    // 浏览器随后的 mouseup 会把选区**收成一个光标**——于是打出来的字接在原名字
    // 后面。在浏览器里点出来的：「新的设定」改成「旧城区」，落盘的是
    // 「新的设定旧城区」，而且一路上没有任何地方报错。往后挪一拍就躲开了。
    const timer = window.setTimeout(() => {
      input.current?.focus();
      input.current?.select();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [editing, value]);

  function commit(): void {
    setEditing(false);
    const next = draft.trim();
    if (next && next !== value) void onCommit(next);
  }

  if (editing) {
    return (
      <input
        ref={input}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") setEditing(false);
        }}
        // **停在这里**：设定库的卡片、会话列表的行都套在可点的东西里面，
        // 不停的话敲一个空格就等于点了外面那一层。
        onClick={(e) => e.stopPropagation()}
        className={`min-w-0 rounded border border-dim bg-ink-800 px-1 py-0 outline-none ${className}`}
      />
    );
  }

  if (controlled !== undefined) {
    return <span className={className}>{value || placeholder}</span>;
  }

  return (
    <span
      onClick={(e) => {
        e.stopPropagation();
        setEditing(true);
      }}
      title="点一下改名"
      className={`cursor-text rounded px-1 hover:bg-ink-700 ${className}`}
    >
      {value || placeholder}
    </span>
  );
}
