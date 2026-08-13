// 从后端的 Pydantic 模型生成 TS 协议类型。
//
// **不手写对照物**（TechStack.md「前后端契约」）：协议消息有二十几种，手写一份
// 只能保证写下来那一刻是对的。改了 Pydantic 模型而忘了改前端，应当在这里变成
// 编译错误，而不是运行时某个字段悄悄是 undefined。
//
//     npm run gen:types
//
// 它做两件事：调后端把 schema 打出来，再过 json-schema-to-typescript。
// **生成的文件不要手改**，改了下次生成就没了。

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { compile } from "json-schema-to-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const backend = resolve(here, "../../backend");
const out = resolve(here, "../src/protocol.ts");

// venv 里的解释器优先——后端的依赖装在那儿，系统 python 多半跑不起来。
const venv = join(backend, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const python = existsSync(venv) ? venv : "python";

console.log(`> ${python} -m agenttavern.server.schema`);
const raw = execFileSync(python, ["-m", "agenttavern.server.schema"], {
  cwd: backend,
  encoding: "utf-8",
  maxBuffer: 32 * 1024 * 1024,
});

const schema = JSON.parse(raw);

const banner = `/**
 * 本文件由 \`npm run gen:types\` 生成，**不要手改**。
 * 来源：backend/agenttavern/server/protocol.py
 */`;

const ts = await compile(schema, "Protocol", {
  bannerComment: banner,
  additionalProperties: false,
  declareExternallyReferenced: true,
  enableConstEnums: false,
  style: { singleQuote: false, semi: true },
});

mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, ts, "utf-8");
console.log(`> 写入 ${out}（${ts.length} 字符）`);
