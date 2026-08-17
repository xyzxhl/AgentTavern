// 界面外观：色彩模式、正文字体与字号。
//
// **它不进 session，也不进服务端**（D178）：这是「这台机器上的这个人怎么看得
// 舒服」，换个人来读答案就变了——而 session 里存的是**这局故事是什么**。
// 所以它落在 localStorage，和后端一个字节都不来往。
//
// 这也是全局设置里「界面」那一组的全部内容。另一组是「服务」，那一组只读：
// 流式、trace、开发者模式都在 `.env` 里，启动服务时就定了。

export type Mode = "system" | "dark" | "light";
export type ProseFont = "serif" | "sans";

export interface Appearance {
  mode: Mode;
  font: ProseFont;
  size: number;
}

export const DEFAULT: Appearance = { mode: "system", font: "serif", size: 15 };

const KEY = "agenttavern.appearance";

const FAMILY: Record<ProseFont, string> = {
  serif: 'Georgia, "Songti SC", SimSun, serif',
  sans: 'Inter, system-ui, "PingFang SC", "Microsoft YaHei", sans-serif',
};

export function load(): Appearance {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? { ...DEFAULT, ...(JSON.parse(raw) as Partial<Appearance>) } : DEFAULT;
  } catch {
    // 坏掉的存档不该让界面起不来——它只是外观。
    return DEFAULT;
  }
}

/** 把设置贴到 `<html>` 上。**颜色和字体都是 CSS 变量**，所以这里只换两三个属性。 */
export function apply(look: Appearance): void {
  const root = document.documentElement;
  // `system` 不写 `data-theme`，让 `prefers-color-scheme` 那条规则接管。
  const dark = look.mode === "system" ? matchMedia("(prefers-color-scheme: dark)").matches : look.mode === "dark";
  root.dataset.theme = dark ? "dark" : "light";
  root.style.setProperty("--prose-family", FAMILY[look.font]);
  root.style.setProperty("--prose-size", `${look.size}px`);
  try {
    localStorage.setItem(KEY, JSON.stringify(look));
  } catch {
    // 存不下就只在这一次生效。比整个界面崩掉好。
  }
}

/** 跟随系统时，系统换了要跟着换。返回取消订阅的函数。 */
export function watchSystem(look: () => Appearance): () => void {
  const media = matchMedia("(prefers-color-scheme: dark)");
  const onChange = (): void => {
    if (look().mode === "system") apply(look());
  };
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}
