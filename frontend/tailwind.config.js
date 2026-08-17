/** @type {import('tailwindcss').Config} */

// **颜色走 CSS 变量，不写死十六进制**（D178）：色彩模式是全局设置里的一项，
// 而切换主题如果要重新构建，那就不是一个设置项，是一次部署。
//
// 通道值分开写（`13 15 18` 而不是 `#0d0f12`），是为了让 Tailwind 的透明度修饰符
// 继续有效——`bg-accent/10`、`text-dim/70` 这类到处都在用，换成整串 `var(--x)`
// 它们会静默失效（**颜色照样出得来，只是不透明了**，最难发现的那种坏法）。
const themed = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          900: themed("ink-900"),
          800: themed("ink-800"),
          700: themed("ink-700"),
          600: themed("ink-600"),
        },
        line: themed("line"),
        dim: themed("dim"),
        fg: themed("fg"),
        accent: themed("accent"),
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
        // 正文是要读的东西。**字体族也走变量**——衬线在长段落上更省力，
        // 但那是偏好不是事实，所以它是设置里的一项。
        prose: "var(--prose-family)",
      },
    },
  },
  plugins: [],
};
