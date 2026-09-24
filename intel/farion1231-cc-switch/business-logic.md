---
repo: farion1231/cc-switch
sha: f8788719a19be6cdef39151b1c0607b4608fa10a
date: 2026-09-24
---

# business-logic — cc-switch

**结论**：cc-switch 的业务核心是"配置搬运工"——把 SQLite 里的供应商/MCP/skill/prompt 配置物化到各 CLI 的 live 文件；第二大业务是内建代理的接管与故障转移；其余为项目快照（Profile）、deep link 导入两大入口流程。以下 5 条为核心流程，其余见文末概述。

## 1. 供应商切换（最核心）

入口：前端 ProviderCard / `useProviderActions` → `commands/provider.rs` → `ProviderService::switch`（`src-tauri/src/services/provider/mod.rs:5712`）。

```mermaid
flowchart TD
    A["switch(app_type, id)"] --> B{"目标供应商存在?"}
    B -->|否| E1["报错: 供应商不存在"]
    B -->|是| C{"Pi / OpenCode-OMO /<br/>ClaudeDesktop 特例?"}
    C -->|是| N["走 switch_normal 专属路径"]
    C -->|否| D["获取 per-app switch lock<br/>防止与接管开关竞态"]
    D --> F{"live 文件被代理接管?<br/>live_backup 存在 或<br/>live 内容指向代理占位符"}
    F -->|是| G{"切换目标是官方供应商?"}
    G -->|是| E2["报错: 接管模式下禁切官方<br/>(防封号)"]
    G -->|否| H["热切换 hot_switch:<br/>只改代理路由目标 is_current<br/>刷新代理安全的 live 标签<br/>跳过 MCP 同步"]
    F -->|否| N
    N --> I["① backfill: 当前 live 配置<br/>回填到旧当前供应商"]
    I --> J["② 更新 settings 表<br/>current_provider_xxx"]
    J --> K["③ 更新 providers.is_current"]
    K --> L["④ 写目标供应商配置到 live 文件<br/>~/.claude/settings.json 等"]
    L --> M["⑤ 同步 MCP 配置到目标 CLI"]
    H --> OK["返回 SwitchResult"]
    M --> OK
```

依据：`services/provider/mod.rs:5700-5802`（switch 注释明确 5 步流程）、`5743-5749`（switch guard）、`5763-5798`（热切换分支与官方禁切）。

## 2. 代理接管（takeover）与故障转移

入口：`ProxyToggle` → `commands/proxy.rs` → `ProxyService::set_takeover_for_app`（`src-tauri/src/services/proxy.rs:1150`）。接管 = 备份 live 配置、改写为指向本地 axum 代理；CLI 的所有请求改由代理转发，可跨供应商故障转移。

```mermaid
sequenceDiagram
    participant U as 用户(前端)
    participant PS as ProxyService
    participant DB as SQLite
    participant AX as axum 代理(proxy/server.rs)
    participant CLI as Claude Code 等 CLI
    participant UP as 第三方供应商

    U->>PS: set_takeover_for_app(app, true)
    PS->>AX: 未运行则 start()
    PS->>DB: 读 live 配置 → 存入 proxy_live_backup
    PS->>CLI: live 文件改写为 127.0.0.1:port 占位符
    CLI->>AX: 之后所有 API 请求打本地代理
    AX->>DB: select_providers(故障转移队列+熔断器)
    AX->>UP: 转发请求(协议转换 transform_*)
    UP-->>AX: 响应(SSE 流式回传)
    AX-->>CLI: 透传响应 + 记 proxy_request_logs(用量)
    Note over AX,UP: 上游连续失败→CircuitBreaker 熔断<br/>→按队列切换下一供应商
    U->>PS: set_takeover_for_app(app, false)
    PS->>DB: 取回 proxy_live_backup
    PS->>CLI: 恢复原 live 配置, 无接管则 stop()
```

依据：`services/proxy.rs:1150-1210`（接管幂等与重建判定）、`proxy/provider_router.rs:44-52`（故障转移队列选择）、`proxy/circuit_breaker.rs`（熔断）、`proxy/server.rs:101-145`（监听与 accept 循环）。

## 3. Profile 项目快照切换

入口：`ProfileSwitcher`（`src/components/profiles/ProfileSwitcher.tsx:90` 手动触发）→ `commands/profile.rs:166` `apply_profile(id, scope)` → `ProfileService::apply`。Profile 是命名的"项目"实体，快照按 app 分槽保存供应商/MCP/skills/prompt 状态。

```mermaid
flowchart TD
    A["apply_profile(id, scope)"] --> B["autosave: 把当前状态<br/>回填到离开的项目(同 scope)"]
    B --> C["逐 app 关闭代理接管<br/>(takeover teardown)"]
    C --> D["按快照槽位逐项应用"]
    D --> D1["供应商: ProviderService::switch"]
    D --> D2["MCP: 最小 diff toggle_app"]
    D --> D3["Skills: 最小 diff toggle_app"]
    D --> D4["Prompt: enable_prompt 互斥激活"]
    D1 & D2 & D3 & D4 --> E{"单项失败?"}
    E -->|是| F["收集为 warning 继续<br/>(best-effort 不回滚)"]
    E -->|否| G["更新 current_profile_id_scope<br/>返回 warnings 列表"]
    F --> G
```

依据：`services/profile.rs:1-14`（模块头注释描述 apply 原语与 best-effort 语义）、`commands/profile.rs:165-171`。注意：Profile 与文件系统目录**无绑定**，见 tripwires.md。

## 4. MCP 统一管理与物化同步

入口：`UnifiedMcpPanel` → `commands/mcp.rs` → `McpService` / `mcp/mod.rs`。统一库（`mcp_servers` 表）+ 每服务器按应用启用标志，切换供应商或 toggle 时把启用集投影写入各 CLI 的 MCP 配置文件。

```mermaid
flowchart LR
    A["统一 MCP 库<br/>mcp_servers 表<br/>(spec + per-app enabled)"] --> B["sync_enabled_to_claude<br/>写 ~/.claude.json"]
    A --> C["sync_enabled_to_codex<br/>写 ~/.codex/config.toml"]
    A --> D["sync_enabled_to_gemini<br/>写 ~/.gemini/settings.json"]
    E["import_from_claude/codex/gemini<br/>反向导入已有配置"] --> A
    F["deep link / 用户手动添加"] --> A
```

依据：`mcp/claude.rs:41-47`（投影写入）、`mcp/claude.rs:51-108`（反向导入，单项失败不中止）、`lib.rs:55-61`（导出的 sync/remove 函数族覆盖 4 个 CLI）。

## 5. Deep link 一键导入

入口：操作系统 `ccswitch://` URL → `deeplink/parser.rs:14` 解析 → 按 resource 类型分发。

```mermaid
flowchart TD
    A["ccswitch://v1/import?resource=…"] --> B["parse_deeplink_url"]
    B --> C{"resource 类型"}
    C -->|provider| D["import_provider_from_deeplink<br/>endpoint/api_key/模型 等"]
    C -->|mcp| E["import_mcp_from_deeplink"]
    C -->|prompt| F["import_prompt_from_deeplink"]
    C -->|skill| G["import_skill_from_deeplink"]
    D & E & F & G --> H["落库 + 可选 enabled<br/>前端确认对话框"]
```

依据：`deeplink/mod.rs:1-9`（支持的四种资源）、`deeplink/mod.rs:29-70`（DeepLinkImportRequest 字段）。

## 非核心逻辑（概述）

- **用量统计**：双通道——代理模式的 `proxy_request_logs` 直接记账；无代理模式由 `services/session_usage_*.rs` 增量解析各 CLI 会话 JSONL（`~/.claude/projects/` 等），SHA256 去重后入库，`usage_stats.rs` 聚合成日/月费用（models.dev 定价同步 + `model_pricing` 表）。
- **云同步**：WebDAV / S3 上传下载整个配置库快照，含自动同步触发（profiles 表变更也触发，CHANGELOG #6147）。
- **会话管理器**：`session_manager/` 按供应商注入环境变量在终端拉起各 CLI 会话。
- **杂项**：speedtest（多端点测速）、订阅/余额查询（subscription/balance）、Claude Desktop 路由切换、环境变量冲突检测（env_checker）、自动更新、托盘、开机自启、i18n（zh/en/ja/zh-TW）。
