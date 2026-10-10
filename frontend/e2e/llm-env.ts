import { fileURLToPath } from "node:url";

/**
 * e2e 模型配置探测。
 *
 * 原则:测试框架的模型关切 ≠ 应用的模型关切——agent 需要「工具调用 + 图片输入」
 * (多模态),应用的分析模型可能是纯文本(如 DeepSeek)。因此:
 *   1. 显式 E2E_LLM_* 是正确配置方式,独立于应用的 LLM_*;
 *   2. 未显式配置时允许「借用」应用的 LLM_*(省事),但借用要经过能力探测;
 *   3. 探测不合格(比如借到了纯文本模型),agent 测试运行时明确跳过并提示
 *      后果与配置方法,而不是让模型调用在半路报出难懂的错。
 */
try {
  process.loadEnvFile(fileURLToPath(new URL("../../.env", import.meta.url)));
} catch {
  // .env 缺失(如 CI):agent 测试按未配置处理,自动跳过
}

export interface LlmConfig {
  key: string;
  base: string;
  model: string;
  protocol: "anthropic" | "openai";
  /** true = 借用应用配置;false = 显式 E2E_LLM_* */
  borrowed: boolean;
}

const explicit = process.env.E2E_LLM_API_KEY
  ? {
      key: process.env.E2E_LLM_API_KEY,
      base: process.env.E2E_LLM_BASE_URL || "https://open.bigmodel.cn/api/paas/v4",
      model: process.env.E2E_LLM_MODEL || "glm-4.6v",
      protocol: (process.env.E2E_LLM_PROTOCOL || "openai").toLowerCase() as "anthropic" | "openai",
      borrowed: false,
    }
  : null;

const borrowed = process.env.LLM_API_KEY && process.env.LLM_BASE_URL && process.env.LLM_MODEL
  ? {
      key: process.env.LLM_API_KEY,
      base: process.env.LLM_BASE_URL,
      model: process.env.LLM_MODEL,
      protocol: (process.env.LLM_PROTOCOL || "openai").toLowerCase() as "anthropic" | "openai",
      borrowed: true,
    }
  : null;

export const llm: LlmConfig | null = explicit ?? borrowed;

// @ai-sdk/anthropic 以 baseURL + '/messages' 拼接,而应用配置的 LLM_BASE_URL
// (如 https://open.bigmodel.cn/api/anthropic)不含 /v1;直接补齐
if (llm && llm.protocol === "anthropic" && !/\/v1\/?$/.test(llm.base)) {
  llm.base += "/v1";
}

export const llmDescription = llm
  ? `${llm.model} @ ${llm.base}${llm.borrowed ? "(借用应用 LLM_* 配置)" : ""}`
  : "未配置";

/** 1x1 红色 PNG(已对 GLM 口实测可解析),探测图片输入用 */
const PIXEL_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVQI12P4z8AAAAMBAQAY3Y2wAAAAAElFTkSuQmCC";

const HOW_TO_CONFIG = [
  "配置方法(与应用的 LLM_* 相互独立,在仓库 .env 添加):",
  "  E2E_LLM_API_KEY=你的key",
  "  E2E_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 或其他 OpenAI 兼容口",
  "  E2E_LLM_MODEL=glm-4.6v                                   # 必须同时支持工具调用与图像输入",
  "Anthropic 协议口也可:E2E_LLM_PROTOCOL=anthropic + 对应 BASE_URL(自动补 /v1)。",
].join("\n");

async function probeOnce(cfg: LlmConfig): Promise<{ ok: true } | { ok: false; reason: string }> {
  const anthropic = cfg.protocol === "anthropic";
  const url = anthropic ? `${cfg.base}/messages` : `${cfg.base.replace(/\/$/, "")}/chat/completions`;
  const tool = { name: "click", description: "click a button", input_schema: { type: "object", properties: { label: { type: "string" } }, required: ["label"] } };
  const body = anthropic
    ? {
        model: cfg.model, max_tokens: 128, tools: [tool],
        messages: [{
          role: "user",
          content: [
            { type: "image", source: { type: "base64", media_type: "image/png", data: PIXEL_PNG } },
            { type: "text", text: "这是一张红色图片。必须调用 click 工具,参数 label 填 提交。不要直接回答。" },
          ],
        }],
      }
    : {
        model: cfg.model, max_tokens: 128,
        tools: [{ type: "function", function: tool }],
        messages: [{
          role: "user",
          content: [
            { type: "image_url", image_url: { url: `data:image/png;base64,${PIXEL_PNG}` } },
            { type: "text", text: "这是一张红色图片。必须调用 click 函数,参数 label 填 提交。不要直接回答。" },
          ],
        }],
      };
  const headers = anthropic
    ? { "x-api-key": cfg.key, "anthropic-version": "2023-06-01", "content-type": "application/json" }
    : { authorization: `Bearer ${cfg.key}`, "content-type": "application/json" };
  const fail = (detail: string) => ({
    ok: false as const,
    reason: [
      `e2e agent 的模型不可用:${cfg.model} @ ${cfg.base}${cfg.borrowed ? "(自动借用了应用的 LLM_* 配置)" : "(E2E_LLM_* 显式配置)"}`,
      `探测结果:${detail}`,
      "后果:agent 测试本轮跳过;其余确定性测试(不依赖模型)不受影响。",
      HOW_TO_CONFIG,
    ].join("\n"),
  });
  try {
    const response = await fetch(url, { method: "POST", headers, body: JSON.stringify(body) });
    const text = await response.text();
    if (!response.ok) return fail(`HTTP ${response.status} ${text.slice(0, 180)}`);
    const data = JSON.parse(text);
    if (data.error) return fail(`接口报错:${JSON.stringify(data.error).slice(0, 180)}`);
    const called = anthropic
      ? (data.content ?? []).some((block: { type?: string }) => block.type === "tool_use")
      : (data.choices ?? []).some((choice: { message?: { tool_calls?: unknown[] } }) => (choice.message?.tool_calls?.length ?? 0) > 0);
    if (!called) return fail("模型未按要求调用工具(可能不支持工具调用或未看到图片)。");
    return { ok: true };
  } catch (error) {
    return fail(`请求异常:${String(error).slice(0, 180)}`);
  }
}

let probePromise: Promise<{ ok: true } | { ok: false; reason: string }> | null = null;

/** 能力探测(每轮运行至多一次,结果缓存):图片输入 + 工具调用双达标才放行 agent。 */
export function ensureAgentModel() {
  if (!llm) {
    return Promise.resolve({
      ok: false as const,
      reason: [
        "未配置 e2e agent 的模型(应用的 LLM_* 也未检测到,或未同时设置)。",
        "后果:agent 测试跳过;其余确定性测试不受影响。",
        HOW_TO_CONFIG,
      ].join("\n"),
    });
  }
  probePromise ??= probeOnce(llm);
  return probePromise;
}
