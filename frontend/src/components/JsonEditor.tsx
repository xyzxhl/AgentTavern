import { useRef } from "react";

/**
 * 写 JSON 的小编辑器。**Tab 缩进，不跳焦点**。
 *
 * 浏览器里 Tab 是"下一个控件"，而写嵌套 JSON 时它是最常按的键——不接管的话
 * 每按一次就掉出输入框。选中多行时 Tab / Shift+Tab 整体缩进与反缩进。
 *
 * 顺带把括号和引号补齐、回车沿用缩进：写嵌套结构时漏一个右括号是最常见的错，
 * 而错了要等到保存才知道。
 */
export function JsonEditor({
  value,
  onChange,
  error,
  rows = 10,
}: {
  value: string;
  onChange: (next: string) => void;
  error?: string | null;
  rows?: number;
}): JSX.Element {
  const box = useRef<HTMLTextAreaElement>(null);

  function put(next: string, start: number, end = start): void {
    onChange(next);
    requestAnimationFrame(() => box.current?.setSelectionRange(start, end));
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>): void {
    const el = event.currentTarget;
    const { selectionStart: from, selectionEnd: to, value: text } = el;

    if (event.key === "Tab") {
      event.preventDefault();
      const multiline = text.slice(from, to).includes("\n");
      if (!multiline && !event.shiftKey) {
        put(text.slice(0, from) + "  " + text.slice(to), from + 2);
        return;
      }
      // 整块缩进：连头一行也算上，哪怕光标停在行中间。
      const head = text.lastIndexOf("\n", from - 1) + 1;
      const block = text.slice(head, to);
      const shifted = event.shiftKey ? block.replace(/^ {1,2}/gm, "") : block.replace(/^/gm, "  ");
      put(text.slice(0, head) + shifted + text.slice(to), head, head + shifted.length);
      return;
    }

    // 回车沿用当前行的缩进，并在 `{` / `[` 之后多缩一级。
    if (event.key === "Enter") {
      const head = text.lastIndexOf("\n", from - 1) + 1;
      const indent = /^ */.exec(text.slice(head, from))?.[0] ?? "";
      const deeper = /[{[]\s*$/.test(text.slice(head, from)) ? "  " : "";
      const closing = /^\s*[}\]]/.test(text.slice(to));
      event.preventDefault();
      const inserted = closing ? `\n${indent}${deeper}\n${indent}` : `\n${indent}${deeper}`;
      put(text.slice(0, from) + inserted + text.slice(to), from + 1 + indent.length + deeper.length);
      return;
    }

    const pairs: Record<string, string> = { "{": "}", "[": "]", '"': '"' };
    const close = pairs[event.key];
    if (close && from === to) {
      event.preventDefault();
      put(text.slice(0, from) + event.key + close + text.slice(to), from + 1);
    }
  }

  return (
    <div>
      <textarea
        ref={box}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        rows={rows}
        spellCheck={false}
        className="w-full resize-y rounded border border-line bg-ink-800 px-3 py-2 font-mono text-xs leading-relaxed text-fg focus:border-dim focus:outline-none"
      />
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  );
}
