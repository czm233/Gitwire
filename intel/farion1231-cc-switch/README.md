---
repo: farion1231/cc-switch
sha: f8788719a19be6cdef39151b1c0607b4608fa10a
date: 2026-09-24
---

# cc-switch 情报档案索引

> 源码仓库：https://github.com/farion1231/cc-switch ｜ 当前 SHA：[`f878871`](https://github.com/farion1231/cc-switch/commit/f8788719a19be6cdef39151b1c0607b4608fa10a)

| 文档 | 一句话导读 |
|---|---|
| [tech-stack.md](tech-stack.md) | Tauri 2 + Rust(axum/rusqlite) + React 18 的完整技术栈与关键依赖版本清单。 |
| [architecture.md](architecture.md) | 入口/服务/数据三层架构图：React UI 与托盘经 ~309 个 Tauri command 驱动 Rust 服务层，落 SQLite 与各 CLI live 配置文件，附内建代理子系统。 |
| [business-logic.md](business-logic.md) | 5 条核心流程图：供应商切换、代理接管与故障转移、Profile 项目快照、MCP 物化同步、deep link 导入。 |
| [changelog/](changelog/) | 按建档/同步日的版本快照与档案变更说明。 |
| [tripwires.md](tripwires.md) | 哨兵问题档案：每次同步回答一个能力判定问题，留档证据。 |
