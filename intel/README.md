# Gitwire 态势板

- 最近一轮同步：2026-09-24；2 个监控目标，2 个已发布

| 项目 | 本轮 | 状态 | 同步到 | 摘要 |
| --- | --- | --- | --- | --- |
| czm233/CC-Balancer | 建档 | 已发布 | dfecb1f | CC-Balancer 是纯前端「额度规划实验室」单页应用（TypeScript + React 19 + Vite 7 + Tailwind v4，仅 845 行核心源码，零后端零数据库，GitHub Pages 托管），模拟 Coding Plan 的 5 小时额度窗口并生成交给外部 AI 配置定时调用的 Prompt。本轮 bootstrap @ dfecb1f（浅克隆，src/tests/CI 全量通读）：产出技术栈、三层架构图、5 条核心业务流程图与首版 changelog；实测 node tests/planner.test.mjs、pnpm lint、pnpm build 全部通过，并核实 HEAD 重构遗留——time.ts 多个导出与 ClockFace 拖拽创建忙时代码已无调用方/未接线。 |
| farion1231/cc-switch | 建档 | 已发布 | f878871 | cc-switch（v3.20.4）是 Tauri 2 桌面应用，统一管理 10 个 AI CLI/桌面应用（Claude Code、Codex、Gemini CLI 等）的供应商/MCP/skills/prompts 配置，核心是 SQLite 配置库物化写入各 CLI live 文件，并内建带熔断与故障转移的本地 axum 代理。本轮重点：按任务要求选择性分析了 src-tauri/src（Rust 后端 230 文件/约 21.3 万行）与前端 src/（350 个 TS 文件），忽略资源与构建产物；档案厘清'切换=写 live 文件'与'接管=live 指向本地代理'两条主线，产出 5 条核心流程图；哨兵确认 Profile 为手动命名快照、不按目录自动应用。 |
