---
repo: farion1231/cc-switch
sha: f8788719a19be6cdef39151b1c0607b4608fa10a
date: 2026-09-24
---

# tech-stack — cc-switch

**结论**：cc-switch 是一个 Tauri 2 桌面应用（Rust 后端 + React 18 前端），核心是一个本地 SQLite 配置库加上一个内建 axum 本地代理服务器，用于在多个 AI CLI 工具（Claude Code / Codex / Gemini CLI 等 10 个 AppType）之间切换与接管供应商配置。版本 v3.20.4（`src-tauri/Cargo.toml:3`、`package.json:3`）。

## 编程语言

| 语言 | 位置 | 规模（建档时实测） |
|---|---|---|
| Rust（edition 2021，MSRV 1.85.0） | `src-tauri/src/`（230 个 .rs 文件） | ~212,679 行 |
| TypeScript / TSX | `src/`（350 个 .ts/.tsx 文件） | 前端主体 |
| CSS（Tailwind） | `src/index.css`、`tailwind.config.cjs` | 样式层 |

Rust 工具链由 `rust-toolchain.toml` 固定；Node 侧由 `.node-version` 与 `packageManager: pnpm@10.12.3` 固定（`package.json:21`）。

## 后端框架与关键依赖（Rust，`src-tauri/Cargo.toml`）

- **桌面壳**：`tauri 2.8.2`（features: tray-icon / protocol-asset / image-png），`tauri-build 2.4.0`。
- **Tauri 插件**：log / opener / process / updater / dialog / store / deep-link / window-state（均 2.x），桌面端另有 `tauri-plugin-single-instance 2`（`Cargo.toml:86-87`）。
- **本地代理服务器**：`axum 0.7` + `hyper 1.0` + `hyper-util 0.1` + `tower 0.4` + `tower-http 0.5`（CORS）；TLS 栈为 `rustls 0.23` / `tokio-rustls 0.26` / `hyper-rustls 0.27` / `webpki-roots`，启动时安装 ring crypto provider（`src-tauri/src/lib.rs:463`）。
- **HTTP 客户端**：`reqwest 0.12`（rustls-tls / json / stream / socks）。
- **异步运行时**：`tokio 1`（macros / rt-multi-thread / time / sync）+ `futures 0.3` + `async-stream 0.3`。
- **数据库**：`rusqlite 0.31`（bundled SQLite，含 backup / hooks features）。
- **脚本引擎**：`rquickjs 0.8`（用于用户自定义 usage 统计脚本，见 `src-tauri/src/usage_script.rs`）。
- **配置解析**：`serde` / `serde_json`（preserve_order）/ `toml 0.8` + `toml_edit 0.22` / `serde_yaml 0.9` / `json5` + `json-five`（带注释 JSON 的读写保真）。
- **压缩**：`flate2` / `brotli 7` / `zstd 0.13`（代理转发的内容编码处理）。
- **其他**：`arboard 3.6`（剪贴板）、`auto-launch 0.5`（开机自启）、`indexmap 2`（保序 provider 表）、`rust_decimal 1.33`（费用计算）、`uuid`、`sha2`/`hmac`、`regex`、`sys-locale`。
- **平台特定**：Windows `winreg`/`windows-sys`；macOS `objc2`/`objc2-app-kit`（NSColor）；Linux `webkit2gtk`。
- **测试**：`serial_test 3` + `tempfile`。

## 前端框架与关键依赖（`package.json`）

- **UI 核心**：`react 18.2` + `react-dom` + `typescript ^5.3`，构建 `vite ^7.3`（`vite.config.ts`，dev 端口 3000）。
- **样式**：`tailwindcss ^3.4.17` + `postcss`/`autoprefixer` + `tailwind-merge`、`class-variance-authority`（shadcn 风格，`components.json` 存在）。
- **组件库**：Radix UI（dialog / dropdown-menu / tabs / select 等 13 个 `@radix-ui/*` 包）+ `lucide-react` 图标 + `sonner`（toast）+ `cmdk`。
- **数据层**：`@tanstack/react-query ^5.90`（服务端状态）、`@tanstack/react-virtual`（虚拟列表）。
- **表单与校验**：`react-hook-form ^7.65` + `@hookform/resolvers` + `zod ^4.1`。
- **编辑器**：CodeMirror 6 全套（`codemirror` + lang-json / lang-javascript / lang-markdown / lint / theme-one-dark）。
- **其他**：`framer-motion ^12`（动画）、`recharts ^3.5`（用量图表）、`flexsearch`（会话搜索）、`smol-toml`、`i18next ^25` + `react-i18next ^16`（zh/en/ja/zh-TW 四语言，`src/i18n/locales/`）、`@dnd-kit/*`（拖拽排序）。
- **Tauri API**：`@tauri-apps/api ^2.8` + plugin-dialog / log / process / updater。
- **测试**：`vitest ^2.0.5` + Testing Library + `msw ^2.11` + `jsdom`（`tests/` 目录）。

## 数据库与存储

- **SQLite**（rusqlite bundled）：单文件 `~/.cc-switch/cc-switch.db`（`src-tauri/src/lib.rs:533`、`database/mod.rs:99`）。schema 由 `database/schema.rs` 管理，含版本化迁移（v1→v17+），建档时基础建表段共 17 张持久表：`providers`、`provider_endpoints`、`mcp_servers`、`prompts`、`skills`、`skill_repos`、`settings`、`proxy_config`、`provider_health`、`proxy_request_logs`、`model_pricing`、`stream_check_logs`、`proxy_live_backup`、`usage_daily_rollups`、`session_log_sync`、`session_usage_dedup`、`profiles`（`schema.rs:27-351`；迁移段出现的 `proxy_config_new`/`proxy_config_v14` 为临时中间表，`session_manager/providers/` 下的建表属外部 CLI 自有数据库，均不计入）。
- **各 CLI 的 live 配置文件**（真正的"生效配置"落点）：`~/.claude/settings.json`、`~/.claude.json`（MCP）、`~/.codex/config.toml` + auth.json、`~/.gemini/settings.json` 等，由 `config.rs` 与各 `*_config.rs` 模块读写。
- **旧版兼容**：v3.x 之前用 `~/.cc-switch/config.json`（MultiAppConfig），启动时若只有 JSON 则执行一次性 JSON→SQLite 迁移（`lib.rs:536-560`）。
- **云端同步**：WebDAV（`services/webdav_sync.rs`）与 S3（`services/s3_sync.rs`）两种，支持自动同步触发。

## 通信协议

- **IPC**：Tauri 2 invoke 机制——后端在 `lib.rs:1388` 通过 `generate_handler!` 注册约 309 个 command（`commands/` 下 35+ 模块），前端经 `src/lib/api/*` 封装调用；另有 Tauri event（如 `configLoadError`、`usage-log-recorded`）。
- **本地代理**：axum HTTP 服务器监听 `listen_address:listen_port`（默认本地，`proxy/server.rs:101-112`），支持 SSE 流式转发、内容编码（gzip/brotli/zstd）、多供应商协议转换（Anthropic↔OpenAI↔Gemini 等，`proxy/providers/transform_*.rs`）。
- **Deep link**：`ccswitch://v1/import?resource=provider|mcp|prompt|skill&...`（`deeplink/parser.rs:14`，tauri.conf.json 注册 scheme）。
- **对外 HTTP(S)**：reqwest + rustls 访问供应商 API、models.dev 定价同步、更新检查（`dl.ccswitch.io` / GitHub Releases，`tauri.conf.json` updater 段）。

## 构建工具与发布

- **构建**：`pnpm tauri build`（`package.json:8`）驱动 Vite 前端产物 + cargo release 编译；release profile 做了体积优化（codegen-units=1、thin LTO、opt-level="s"、strip symbols，`Cargo.toml:111-117`）。
- **CI/CD**：`.github/` 工作流（含多平台打包）；bundle targets "all"（dmg/msi/AppImage 等），Windows 用 WiX per-user 模板，macOS 最低 12.0（`tauri.conf.json` bundle 段）。
- **自动更新**：tauri-plugin-updater + 签名 pubkey，endpoint 指向 `https://dl.ccswitch.io/latest.json` 与 GitHub Releases。
- **代码质量**：`typecheck`（tsc --noEmit）、prettier、vitest 单测；Rust 侧内嵌 `#[cfg(test)]` 测试（如 `database/tests.rs`）。
