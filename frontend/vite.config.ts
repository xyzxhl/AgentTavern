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
    // **加一条 REST 前缀、或给一条改名，这里都要跟着改。**
    //
    // 漏了不会报错：Vite 会把请求当成前端路由、回一份 `index.html`
    // **且状态码 200**，于是 `res.ok` 为真，异常拖到 `res.json()` 才发生。
    //
    // 这里栽过两次：`/worldbooks` 整个失灵过（R17，于是 `api.ts` 的 `json()`
    // 加了 content-type 校验）；后来 `/agents` 改名成 `/prompts`（S147）而
    // 这张表没动，**提示词管理页在开发环境坏了好几个提交**——生产构建里是好的，
    // 所以没人发现。**加前缀会被想起来，改名不会。**
    //
    // 所以现在由 `tests/test_dev_proxy.py` 双向对账，不再靠这段注释。
    proxy: {
      "/sessions": { target: "http://127.0.0.1:8000", ws: true, changeOrigin: true },
      "/worldbooks": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/corpora": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/prompts": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/meta": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
