---
repo: czm233/CC-Balancer
sha: dfecb1fe47d7770f7bbe3b4b69f682323ec2f3e9
date: 2026-09-24
---

# 技术栈

**结论：这是一个零后端、零运行时第三方依赖（除 React 外）的纯前端单页应用。** TypeScript + React 19 + Vite 构建，Tailwind CSS 4 与手写 CSS 混用，数据仅存浏览器 localStorage，无网络请求、无数据库。所有版本号均取自 `package.json`（gitwire-sources/czm233-CC-Balancer/package.json:12-31）。

## 编程语言

| 语言 | 位置 | 说明 |
|---|---|---|
| TypeScript ~5.9.3 | `src/**/*.ts(x)` | 全部源码；构建时 `tsc -b` 类型检查（package.json:8） |
| CSS | `src/index.css`（Tailwind 入口）、`src/planner.css`（手写主样式） | 主界面样式为手写 CSS，组件内混用 Tailwind 原子类 |

核心源码规模极小：`src/` 下 7 个文件共 845 行（含空行注释）。

## 前端框架

- **React 19.2.0**（`react` / `react-dom`，package.json:13-14）——唯一的运行时依赖。无路由库、无状态管理库、无 UI 组件库；状态就是一个 `useState<Settings>`（src/Planner.tsx:14）。
- 入口 `src/main.tsx:7-11`：`createRoot` + `StrictMode` 渲染单个根组件 `Planner`。
- 可视化核心 `src/components/ClockFace.tsx` 为纯 SVG 手绘双时钟（AM/PM），不依赖图表库。

## 后端

**无。** 仓库中不存在任何服务端代码；README.md:41 明确「纯前端，无需数据库、Docker 或密钥」，README.md:23「不连接账号、不发送 API 请求」。唯一的"后端交互"是生成一段让用户自己的 AI 去配置定时任务的 Prompt 文本（src/utils/scriptPrompt.ts）。

## 数据库与存储

**无数据库。** 唯一持久化是浏览器 `localStorage`，key 为 `cc-balancer-planner-v1`（src/Planner.tsx:8），读写逻辑在 `read()`（src/Planner.tsx:9）与 `persist()`（src/Planner.tsx:25），存储用户设置（工作时间/午休/忙时偏好/额度支撑时长/首次触发时间）。

## 通信协议

- 应用自身**不发起任何网络请求**。
- 部署侧：GitHub Pages 静态托管（https://czm233.github.io/CC-Balancer/ ，README.md:5）。
- 生成的 Prompt 指导用户侧脚本向 Coding Plan API 发送内容固定为 `say OK` 的最小请求（src/utils/scriptPrompt.ts:23），属于工具产出物而非本工具行为。

## 构建工具

| 工具 | 版本 | 用途 |
|---|---|---|
| Vite | ^7.3.1 | dev server（127.0.0.1:10220，strictPort，base `/CC-Balancer/`，vite.config.ts）与生产构建 |
| @vitejs/plugin-react | ^5.1.1 | React Fast Refresh |
| @tailwindcss/vite + tailwindcss | ^4.2.1 | Tailwind v4 Vite 插件模式（无 tailwind.config，v4 CSS-first） |
| tsc | ~5.9.3 | `build` 脚本先 `tsc -b` 再 `vite build`（package.json:8） |
| ESLint | ^9.39.1 + typescript-eslint ^8.48.1 + react-hooks ^7.0.1 + react-refresh ^0.4.24 | `pnpm lint` |
| pnpm | lockfile 为 pnpm-lock.yaml；CI 固定 pnpm 10 + Node 22（.github/workflows/deploy-pages.yml:29-37） | 包管理 |

## 测试

无测试框架。`tests/planner.test.mjs` 用 `node:assert` + TypeScript 编译器 API 现场把 `src/utils/planner.ts` 转译成 JS 再动态 import 断言（tests/planner.test.mjs:1-5），覆盖解析基线、守恒、搜索最优性、窗口压力、避用区间等。执行方式 `node tests/planner.test.mjs`（README.md:46）。本轮建档时实际运行通过（含 lint 与 build，详见 changelog/2026-09-24-dfecb1f.md）。

## 关键依赖清单（运行时）

```
react        ^19.2.0
react-dom    ^19.2.0
```

其余全部为 devDependencies（package.json:16-31）。`pnpm.onlyBuiltDependencies` 仅放行 esbuild（package.json:32-36）。

## CI/CD

`.github/workflows/deploy-pages.yml`：main 分支 push 或手动触发 → pnpm 10 / Node 22 → `pnpm install --frozen-lockfile` → `pnpm build` → `actions/upload-pages-artifact` → `actions/deploy-pages@v4` 发布 GitHub Pages。无测试/CI 检查步骤。
