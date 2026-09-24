# Gitwire

> Your personal open-source intelligence desk · 一个人的开源情报站

Gitwire 监控你关注的 GitHub 开源项目，用 AI 持续为你产出情报：定时扫描仓库的 main 分支，一旦有新提交，就按可自定义的「配方」（recipe）交给 agent 分析，自动维护架构图、时序图、业务流程文档与逐版本 changelog，并把结果版本化地发布到专属的情报仓库——可以推回 GitHub、可以留在本地 Obsidian、也可以通过 Bark 推送警报。

## 情报产出

本仓库是 Gitwire **软件本体**；情报档案发布在独立仓库 [czm233/gitwire-intel](https://github.com/czm233/gitwire-intel)——每个被监控项目一个文件夹，内含技术栈、架构图、业务逻辑、changelog、哨兵记录与同步游标（`meta.yml`）。

## 它解决什么问题

- 每次想了解一个开源项目，都得临时问 AI，又慢又没有积累
- 代码一提交，手上那份架构 / 流程文档就过时了
- 自己在意的功能悄悄上线了、依赖的库出 CVE 了，总是后知后觉

## 核心循环

```
Watch（扫描游标） → Analyze（配方分析） → Publish（发布分发）
```

- **Watch**：定时扫描（如每小时），只记录各项目 main 分支最新 SHA，便宜且无状态
- **Analyze**：游标前进才触发分析；首次全量建档，此后按累计 diff 增量更新
- **Publish**：产出写入独立情报仓库（配置与游标也存放于此），提交前缀 `[owner/repo]`

## 情报产品

| 类型 | 产出 |
|---|---|
| 档案 dossier | 技术栈清单 / 架构图 / 业务逻辑（mermaid 时序图 + 流程图） |
| 简报 brief | 每次同步一份 changelog：新增 / 修复 / 重构 / 破坏性变更 |
| 晨报 daily digest | 每日跨项目变化汇总，一页读完 |
| 警报 alert | 哨兵（tripwire）判决翻转、破坏性变更、CVE → Bark 推送 |

## 设计原则

- **编排归代码，语义归 agent**：扫描 / 队列 / 游标 / 发布是确定性代码，分析与写作交给 agent 配方
- **配置即仓库**：`gitwire.yml` 与各项目 `meta.yml` 游标存放在情报仓库里，系统本身无状态，clone 即续跑
- **情报写作规范**：结论先行（BLUF）、分层抽象（摘要 → 详情 → 原始 diff）、标注信源与时效（repo + SHA + 时间）

## 路线图

- [ ] v0：扫描器 + 游标 + 队列 + `docs-sync` 配方 + 独立情报仓库发布 + 态势板 README
- [ ] v1：多配方（`feature-tripwire` / `bug-watch`）+ Bark 推送 + 每日晨报 + Obsidian vault 集成
- [ ] v2：release / issue / CVE 触发、path filter、推回自有仓库的 PR 模式

---

🚧 构想已收敛，v0 开发中。
