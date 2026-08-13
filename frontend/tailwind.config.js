/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 单一深色主题。codex 桌面版的底色偏中性冷灰，不是纯黑。
        ink: { 900: "#0d0f12", 800: "#14171c", 700: "#1c2027", 600: "#262b34" },
        line: "#2c323c",
        dim: "#8b93a1",
        fg: "#dfe3ea",
        accent: "#7aa2f7",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
        // 正文是要读的东西，衬线体在长段落上更省力。
        prose: ["Georgia", "Songti SC", "SimSun", "serif"],
      },
    },
  },
  plugins: [],
};
