import type { E2EConfig, Reporter } from "e2e";
import { web } from "@e2e-dev/web";
import { createOpenAI } from "@ai-sdk/openai";
import { createAnthropic } from "@ai-sdk/anthropic";
import { llm } from "./e2e/llm-env";
import { pacedCdpEndpoint, closePacedBrowsers } from "./demo-pace";

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

const base = process.env.E2E_APP_URL ?? "http://127.0.0.1:10249";
const url = base;

/**
 * 回放演示光标:每个页面加载前注入的 DOM 光标(跟随移动 + 点击涟漪)。
 * pointer-events:none 且无文本无角色,不参与任何语义断言;
 * 会被 Playwright 录进回放视频,让 AI 走查的操作肉眼可见(类似云厂商 Agent 演示)。
 *
 * 自动化的 click 只在落点派发一次 mousemove(两点间无轨迹事件),脚本对这类
 * "跳跃"自行补 180~500ms 的 ease-in-out 补间,回放里光标是划过去的,不是瞬移;
 * E2E_DEMO_PACE=1 时另有 demo-pace.ts 的输入节流代理,把"移动"与"按下"真实隔开,
 * 补间上限(500ms)小于节流间隔(600ms),保证光标先滑到、点击后落下。
 *
 * 仅在「给人看」的运行注入(E2E_DEMO_CURSOR=1):
 * 驾驶舱勾选了显示浏览器窗口/录制回放视频,或 AI 试用官走查(录像常开)时设置;
 * 普通 CI 静默回归不注入,保持被测 DOM 与生产一致。
 */
const demoCursor = `
(() => {
  const style = document.createElement("style");
  style.textContent = [
    ".demo-cursor{position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;width:40px;height:40px;",
    "margin:-5px 0 0 -8.3px;will-change:transform;opacity:0;transition:opacity .15s}",
    ".demo-cursor.on{opacity:1}",
    ".demo-cursor img{display:block;transform-origin:8.3px 5px;transition:transform .05s linear}",
    ".demo-cursor.down img{transform:scale(.85)}",
    ".demo-ripple{position:fixed;z-index:2147483646;pointer-events:none;width:44px;height:44px;",
    "margin:-22px 0 0 -22px;border-radius:50%;border:2px solid #6fb1ff;opacity:.9;",
    "animation:demo-ripple .45s ease-out forwards}",
    "@keyframes demo-ripple{to{transform:scale(2.2);opacity:0}}",
  ].join("");
  // 演示光标(Flaticon 图标 17953718,白箭头黑描边,512 原图缩至 128 内嵌;
  // 尖端在原图 (106,64)/512,40px 显示即 (8.3,5),对准真实坐标)
  const ARROW = '<img src="data:image/png;base64,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" width="40" height="40" alt="">';
  // 位移由外层 dot 的 transform:translate 承担(合成器层,录像里更顺);
  // 按下的缩放效果放在内层 img 上,两层 transform 互不干扰。
  // mousemove 分两类处理:事件密集的是真人轨迹(headed 联调),逐事件直接跟随;
  // 隔了 >120ms 才出现且距离 >20px 的是自动化落点,补一条 ease-in-out 曲线滑过去
  const mount = () => {
    if (document.getElementById("demo-cursor")) return;
    const dot = document.createElement("div");
    dot.id = "demo-cursor"; dot.className = "demo-cursor";
    dot.innerHTML = ARROW;
    document.documentElement.append(style, dot);
    let cur = null;         // 当前渲染点(补间途中即起点与终点之间的中间位置)
    let glide = null;       // 进行中的补间 {from,to,start,dur},null = 空闲
    let raf = 0;            // requestAnimationFrame 句柄,0 = 循环未启动
    let lastMoveAt = 0;     // 上一次 mousemove 时刻,用于区分真人轨迹与自动化跳跃
    const place = (x, y) => { dot.style.transform = "translate(" + x + "px," + y + "px)"; };
    const tick = () => {
      raf = 0;
      if (!glide) return;
      const t = Math.min(1, (performance.now() - glide.start) / glide.dur);
      const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;  // ease-in-out:起步加速、落点减速,贴近真人手部动作
      cur = { x: glide.from.x + (glide.to.x - glide.from.x) * e,
              y: glide.from.y + (glide.to.y - glide.from.y) * e };
      place(cur.x, cur.y);
      if (t < 1) raf = requestAnimationFrame(tick);
      else glide = null;
    };
    const glideTo = (x, y) => {
      // 距离越长滑得越久,180~500ms 封顶;上限必须小于 demo-pace.ts 的节流间隔
      // (默认 600ms),节流代理隔开"移动"与"按下"的空档里光标恰好滑完
      glide = { from: { x: cur.x, y: cur.y }, to: { x: x, y: y }, start: performance.now(),
                dur: Math.min(500, 180 + Math.hypot(x - cur.x, y - cur.y) * 0.5) };
      if (!raf) raf = requestAnimationFrame(tick);
    };
    addEventListener("mousemove", (e) => {
      const now = performance.now();
      const jump = cur && now - lastMoveAt > 120 &&
                   Math.hypot(e.clientX - cur.x, e.clientY - cur.y) > 20;
      lastMoveAt = now;
      if (!cur) {  // 首个落点:直接就位并淡入,避免页面刚加载时从默认位置滑入
        cur = { x: e.clientX, y: e.clientY };
        place(cur.x, cur.y); dot.classList.add("on");
        return;
      }
      if (jump || glide) glideTo(e.clientX, e.clientY);  // 跳跃,或滑行途中来了新落点
      else { cur = { x: e.clientX, y: e.clientY }; place(cur.x, cur.y); }  // 真人轨迹:实时跟随
    }, { passive: true });
    addEventListener("mousedown", (e) => {
      dot.classList.add("down");
      const ripple = document.createElement("div");
      ripple.className = "demo-ripple";
      ripple.style.left = e.clientX + "px"; ripple.style.top = e.clientY + "px";
      document.documentElement.append(ripple);
      setTimeout(() => ripple.remove(), 500);
    }, { passive: true });
    addEventListener("mouseup", () => dot.classList.remove("down"), { passive: true });
  };
  if (document.readyState === "loading") addEventListener("DOMContentLoaded", mount, { once: true });
  else mount();
})();`;

/** 拟人化节奏:E2E_DEMO_PACE=1 时演示运行经 demo-pace.ts 的节流代理驱动浏览器。 */
const pace = process.env.E2E_DEMO_PACE === "1";

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
    if (target) {
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
    }
    if (pace) {
      // 演示浏览器收尾:等引擎侧 teardown 先收完(5s 宽限),再优雅回收;
      // 兜底还有 demo-pace.ts 的进程退出钩子
      await new Promise((resolve) => setTimeout(resolve, 5000));
      await closePacedBrowsers();
    }
  },
};

const config: E2EConfig = {
  // E2E_WORKERS=1 时串行执行(可视化观察用,避免多窗口并行没法盯)
  ...(process.env.E2E_WORKERS ? { workers: Math.max(1, +process.env.E2E_WORKERS) } : {}),
  targets: [
    {
      name: "web",
      engine: web({
        initScripts: process.env.E2E_DEMO_CURSOR === "1" ? [demoCursor] : [],
        // 演示运行接到 demo-pace.ts 的节流代理:输入事件强制串行间隔,"移动"与
        // "按下"真实隔开,点击落在演示光标滑到之后;静默回归不带 connect,浏览器路径与 CI 一致
        ...(pace ? { connect: { cdpEndpoint: pacedCdpEndpoint } } : {}),
      }),
      app: { url, readyUrl: `${base}/api/health` },
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
