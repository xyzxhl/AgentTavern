import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 后端跑在 8000，前端 5173。**开发期走代理而不是 CORS**：WebSocket 的跨源
// 处理各浏览器不一致，代理之后前后端同源，`/ws` 和 REST 都不用特判。
export default defineConfig({
  plugins: [react()],
  server: {
    // **写死 127.0.0.1**：不指定时 Vite 只监听 IPv6 的 `[::1]`，而后端和多数
    // 工具默认走 IPv4，于是 `127.0.0.1:5173` 连不上、`localhost` 时通时不通。
    host: "127.0.0.1",
    port: 5173,
    // **加一条 REST 前缀时这里也要加**——漏了不会报错，Vite 会把请求当成前端
    // 路由、回一份 `index.html` **且状态码 200**，于是 `res.ok` 为真，异常拖到
    // `res.json()` 才发生。`/worldbooks` 就这么整个失灵过（D172）。
    // `api.ts` 的 `json()` 因此加了 content-type 校验，让它更早、更响地炸。
    proxy: {
      "/sessions": { target: "http://127.0.0.1:8000", ws: true, changeOrigin: true },
      "/worldbooks": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/meta": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
