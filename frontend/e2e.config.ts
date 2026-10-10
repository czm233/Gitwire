import type { E2EConfig, Reporter } from "e2e";
import { web } from "@e2e-dev/web";
import { createOpenAI } from "@ai-sdk/openai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { llm } from "./e2e/llm-env";

/**
 * Gitwire 浏览器端到端测试。
 *
 * 目标地址来自 E2E_APP_URL(默认指向 scripts/e2e_stack.py 拉起的隔离栈:
 * sqlite 后端 10248 + 构建版前端 preview 10249,种子数据,不碰真实数据)。
 * 全镜像验收栈(10242)冒烟:E2E_APP_URL=http://127.0.0.1:10242。
 *
 * 模型(agent 步骤,可选):自动复用应用 .env 的 LLM_* 或 E2E_LLM_* 覆盖,
 * 详见 e2e/llm-env.ts。未配置时 agent 测试自动跳过,其余全部确定性断言零模型调用。
 */

const url = process.env.E2E_APP_URL ?? "http://127.0.0.1:10249";

/** 回归驾驶舱(scripts/e2e_dashboard.py):设置 E2E_DASHBOARD 时把运行事件实时推给它。 */
const DASHBOARD_EVENTS = new Set([
  "plan", "test-started", "test-finished",
  "run-finished", "run-stopped", "run-interrupted", "run-error",
  "step", "output",
]);
const dashboard: Reporter = {
  name: "dashboard",
  onEvent(event) {
    const target = process.env.E2E_DASHBOARD;
    if (!target || !DASHBOARD_EVENTS.has(event.type)) return;
    fetch(target, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(event),
    }).catch(() => {});
  },
  // run-finished 事件会在 CLI 退出途中丢失(发后不管的 POST 被截断);
  // onRunFinished 有完成宽限,traces 映射从这里可靠送达驾驶舱
  async onRunFinished(run, signal) {
    const target = process.env.E2E_DASHBOARD;
    if (!target) return;
    // run.traces 以 results[].id 为键;借 report 把 id 翻译成标题,驾驶舱按标题落位
    const results = run.report?.run?.results ?? [];
    const titleById = new Map(results.map((r) => [r.id, (r.titlePath ?? []).join(" ")]));
    const byTitle: Record<string, string> = {};
    for (const [id, path] of run.traces ?? []) {
      const title = titleById.get(id);
      if (title) byTitle[title] = path;
    }
    await fetch(target, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ type: "traces", traces: byTitle }),
      signal,
    }).catch(() => {});
  },
};

const config: E2EConfig = {
  // E2E_WORKERS=1 时串行执行(可视化观察用,避免多窗口并行没法盯)
  ...(process.env.E2E_WORKERS ? { workers: Math.max(1, +process.env.E2E_WORKERS) } : {}),
  targets: [
    {
      name: "web",
      engine: web(),
      app: { url, readyUrl: `${url}/api/health` },
    },
  ],
  tests: ["e2e/**/*.e2e.ts"],
  // read-write:agent.act 冷跑录制,之后回放零模型调用
  cache: { mode: "read-write", dir: ".e2e/cache" },
  reporters: ["list", "markdown", "junit", dashboard],
};

if (llm) {
  config.agents = {
    default: {
      model: llm.protocol === "anthropic"
        ? createAnthropic({ baseURL: llm.base, apiKey: llm.key })(llm.model)
        : createOpenAI({ baseURL: llm.base, apiKey: llm.key })(llm.model),
    },
  };
}

export default config satisfies E2EConfig;
