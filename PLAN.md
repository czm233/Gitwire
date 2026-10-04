# Gitwire v0 开发计划

> 一个人的开源情报站：盯住你关注的 GitHub 仓库，有新提交就让 AI 更新情报档案，自动发布到 vault 情报仓库。
> 本文档是 v0 的开发契约，架构与字段约定以这里为准。

## 0. 需求澄清结论（2026-09-24 与用户逐条确认）

| 决策点 | 结论 |
|---|---|
| 产品定位 | **单用户起步，多用户预留**：v0 一套实例一个用户；DB 第一天就有 users / vault_bindings 表，业务表全带 user_id，但不做注册、登录页、OAuth |
| 部署范围 | **v0 只做本地**：Docker 优先；云端部署文档后补，不做平台适配 |
| GitHub 授权 | 无 OAuth。用户自己的 PAT 贴 `.env`；操作的是自己的仓库 |
| vault 模型 | **git 就是版本管理本体，GitHub 只是远程备份**。vault 是一个 git 仓库：指向本地路径 = 纯本地模式（历史完整），指向 GitHub = 自动 push 发布。两种模式代码完全一致 |
| 分析深度 | **分层**：首次建档 clone 仓库让 agent 通读源码（只读、不执行任何代码）；增量更新走 GitHub compare API，快而便宜 |
| 前端 | **React SPA**（React + Vite + TypeScript），前后端分离；构建产物由 FastAPI 托管，生产仍是单容器 |
| v0 功能 | 基础四件套（态势板 / 监控管理 / 运行历史 / 档案浏览）**+ 档案变更时间线 + 每日晨报**；Bark 推送、SSE 进度流留给 v1 |
| 技术栈 | 后端 Python 3.12 + FastAPI + APScheduler + SQLite（SQLModel）；前端 React 18 + Vite + TS + TanStack Query + mermaid |

## 1. 架构

```
React SPA（Vite/TS）──REST──▶ FastAPI 单进程
  态势板 · 监控管理            ├─ web/api/        全量 REST（OpenAPI 自带）
  运行历史 · 档案浏览          ├─ engine/
  变更时间线 · 每日晨报         │   ├─ watch.py     查 main SHA、比游标、建 run
                              │   ├─ analyze.py    分层：建档=clone 深读 / 增量=compare API
                              │   ├─ publish.py    写 vault + 游标 + 态势板 + commit/push
                              │   └─ daily.py      晨报：汇总当日 changelog → LLM 提炼一页
                              ├─ scheduler.py     APScheduler：整点 Watch + 每日 08:00 晨报
                              └─ vault.py         vault git 操作封装（含时间线查询 git log/diff）

存储分工：
  SQLite —— users(预留) / vault_bindings(预留) / runs / run_logs：机器自己的事
  vault   —— 各项目档案 + meta.yml 游标 + daily/：情报产出（系统配置 gitwire.yml 在本地 data/，不入库）
```

## 2. 配置设计

### 2.1 `.env`（Gitwire 自己的，不进任何仓库）

```bash
GITWIRE_VAULT=/path/to/gitwire-vault      # 本地路径或 git URL，二选一
GITHUB_TOKEN=ghp_xxx                      # 查 SHA/diff + push vault 用
LLM_PROTOCOL=anthropic                    # openai（默认）| anthropic（GLM Coding Plan / Gemini 反代）
LLM_BASE_URL=...                          # anthropic 例：open.bigmodel.cn/api/anthropic
LLM_API_KEY=...
LLM_MODEL=...
LLM_THINKING=disabled                     # 可选：anthropic 协议关思考块，批处理更省时
SECRET_KEY=                               # 预留：非空时启用访问密码，v0 本地留空免登录
WATCH_CRON=0 * * * *                      # 整点扫描
DAILY_CRON=0 8 * * *                      # 每日晨报
DATA_DIR=./data                           # SQLite + vault 本地 clone + 监控仓库缓存
```

### 2.2 vault 目录约定（在现有基础上新增两处）

```
gitwire-vault/
├── gitwire.yml                # 监控清单（本地 data/ 下，网页增删项目 = 改此文件；不入情报仓库，见 11.9）
│      repos: [czm233/CC-Balancer, farion1231/cc-switch]
│      recipes: [docs-sync]
├── README.md                  # 态势板，锚点区块 <!-- board:start/end --> 内机器重写
├── daily/2026-09-25.md        # 新增：每日晨报，无变化日不产生文件
└── owner-repo/                # 既有结构不变：档案 + changelog/ + meta.yml(游标) + tripwires.md
```

### 2.3 SQLite

- `users`（多用户预留）：id、name、password_hash(可空)、created_at —— v0 永远只有一行种子用户
- `vault_bindings`（多用户预留）：user_id、vault_url —— v0 一行，值来自 `.env`
- `runs`：id、user_id、repo、trigger、old_sha、new_sha、status(running/published/failed)、error、started_at、finished_at
- `run_logs`：run_id、ts、level、message

## 3. 核心流程

### 3.1 一次 run（watch → analyze → publish）

```
watch：GitHub API 查 main 最新 SHA
  == 游标 → 结束；该 repo 已有未完成 run → 跳过（防重入）
  != 游标 → 建 run

analyze（docs-sync 配方，分层）：
  无档案（首次）→ git clone --depth=50 到 DATA_DIR 缓存，agent 通读源码产出全套档案
  有档案（增量）→ GitHub compare API 拿 old...new diff
  上下文 = 现有档案 + diff + 提交列表 → LLM 按配方模板出结构化稿件
  校验：文件非空、必填章节齐全、mermaid 可解析；失败重试 1 次，再失败 run=failed

publish（成功游标才前进——宁可重跑，不写坏档案）：
  写档案与 changelog/<日期>-<sha7>.md → meta.yml 游标前进 → 重写态势板锚点区块
  commit -m "[owner/repo] docs-sync: 增量更新 (sha7)"；vault 配了 remote 则 push
  push 失败：保留本地 commit，下一轮 watch 前先补 push
```

### 3.2 档案变更时间线（数据全部来自 vault 的 git 历史，零额外存储）

- 时间线 = `git log --follow -- <owner-repo>/`：每次同步一个版本节点（含手工编辑）
- 版本 diff = `git diff <sha>^ <sha> -- <path>`：前端并排渲染
- API：`GET /api/repos/{slug}/timeline`、`GET /api/repos/{slug}/timeline/{sha}`

### 3.3 每日晨报

```
每日 08:00：收集昨日各项目 changelog → 无变化则跳过
→ LLM 汇总成一页（BLUF：今天谁动了什么、哪条最值得关注）
→ 写 vault daily/YYYY-MM-DD.md → commit（可选 push）→ 态势板顶部加最近晨报入口
```

## 4. 目录结构

```
Gitwire/
├─ app/                          # FastAPI 后端
│  ├─ main.py  config.py  db.py  models.py  services.py
│  ├─ github.py  llm.py  scheduler.py
│  ├─ vault.py                   # git 操作封装（clone/pull/commit/push/log/diff）
│  ├─ engine/{watch,analyze,publish,daily,runner}.py
│  ├─ api/                       # REST 路由，按资源分文件
│  └─ recipes/                   # 配方 prompt 模板（随包分发，docs-sync / daily）
├─ frontend/                     # React SPA
│  ├─ src/{pages,components,api,types}/
│  └─ vite.config.ts             # dev 代理 → localhost:10240（号段 10240-10249 已入端口登记表）
├─ scripts/mock_llm.py           # 开发用 mock LLM（不耗 token 的端到端验证）
├─ tests/                        # 25 项：解析/校验/态势板锚点/发布/晨报/watch/API 全链路
├─ Dockerfile                    # 多阶段：node 构建前端 → python 镜像托管静态产物
├─ docker-compose.yml
├─ docs/deploy-cloud.md          # 云端部署（v0 只写不验）
├─ pyproject.toml / uv.lock      # uv 管理
└─ .env.example
```

## 5. 页面清单（React）

| 路由 | 内容 |
|---|---|
| `/` 态势板 | 项目行：状态、游标 SHA、最近 run、最新 changelog 摘要；「立即同步」 |
| `/repos` | 监控清单增删（= 改本地 data/gitwire.yml，不产生 git 提交） |
| `/runs` `/runs/:id` | 运行历史与日志 |
| `/repos/:slug` | 档案浏览：markdown + mermaid 渲染 |
| `/repos/:slug/timeline` | 变更时间线：版本列表 + 任意一版的文件 diff |
| `/daily` `/daily/:date` | 晨报列表与阅读 |

## 6. 里程碑

- **M1 引擎（CLI，无 web）**：`gitwire sync --repo <owner/repo>` 跑通分层分析 + publish + 游标。验收：新仓库全量建档；把现有项目游标人为回退一个提交重跑，产出增量 changelog、游标前进、态势板刷新、push 成功。
- **M2 API + 前端基座**：REST 全量接口；React 四件套页面（态势板/管理/历史/档案浏览，mermaid 渲染）。
- **M3 情报体验**：变更时间线（版本 diff）；每日晨报生成与页面。
- **M4 打包**：多阶段 Docker 镜像、compose 一键起、重启无人工恢复验证；云端部署文档（只写不验）。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| LLM 输出格式不稳 | 输出契约 + 硬校验 + 重试 1 次；失败不动游标 |
| 建档 clone 体积 | `--depth=50` 起步；超大仓库（>200MB）降级为 API 按需拉文件 |
| diff 超模型上下文 | 按文件分块摘要再汇总；超大 diff 降级逐 commit 摘要 |
| GitHub API 限流 | 引导配 token；429 指数退避 |
| 手工编辑过的档案被覆盖 | analyze 上下文含现有档案全文，LLM 指令明确保留人工内容；冲突时以人工内容为准 |
| 本地机器休眠漏扫描 | 调度器醒来先补一轮 Watch（错过即追），晨报同理 |
| 同 repo 并发 | per-repo 防重入；v0 全局并发 = 1 |

## 8. v0 验收标准

1. `docker compose up` 一键起，浏览器打开 React 态势板，看到现有两个项目
2. 页面添加新仓库 → 本地 gitwire.yml 多一行（不入情报仓库）
3. 模拟新提交（回退游标）→ 一轮调度内自动产出 changelog、游标前进、态势板更新
4. 档案时间线页能看到该项目的每一次同步版本与单版 diff
5. 次日 08:00 后 daily/ 出现昨日晨报；无变化日不产生文件
6. 运行历史可查每次 run 与日志；进程重启后无需人工恢复

## 9. 多用户升级路径（v0 只做预留，不实现）

- 已预留：users / vault_bindings 表、业务表 user_id、vault 访问按 VaultContext 封装（v0 恒为 default 用户）
- 升级时要补：注册/登录会话、GitHub OAuth（替用户操作其 vault）、按用户隔离的调度与 LLM key、vault 凭证加密存储
- 明确不做：不引入多租户中间件、不拆微服务——单进程单体直到真的有用户

## 10. v1 / v2（已实现）

**v1**

- 多配方：`feature-tripwire`（哨兵警戒，判定翻转即警报）、`bug-watch`（缺陷修复 + 破坏性变更警报）；
  配方在 gitwire.yml 按仓库挂载（`repos[].recipes`）
- Bark 推送：`BARK_URL` 配好即推手机；未配置时警报仍入台账（`alert` 表 + `/api/alerts` + 态势板/警报页）
- SSE 实时进度：`GET /api/runs/{id}/stream`，运行页实时滚动 agent 日志
- Obsidian：`GITWIRE_VAULT` 指向 Obsidian 库文件夹 + `OBSIDIAN_FRONTMATTER=true` 注入属性

**v2**

- 触发源扩展：新 release（release-brief 简报 + 警报）、新 issue（issue-watch 摘要）、
  CVE（OSV 依赖扫描，零 LLM，仅新增漏洞报警）；游标记在 meta.yml（`last_release_tag` / `last_issue_number`）
- ~~path filter~~（v4.5 移除：要关注就关注整个仓库，不再按路径过滤变更）
- PR 模式：gitwire.yml `publish_mode: pr` → 同步内容走分支 + Pull Request，人工审核合入；
  有待处理 PR 的仓库自动跳过新同步，游标随 PR 分支前进，main 不受污染

**多用户 SaaS** 仍为预留（users / vault_bindings / user_id 已就位），按需再启用。

## 11. v4：issue 监控重做（2026-09-25 需求澄清）

### 11.1 需求澄清结论

| 决策点 | 结论 |
|---|---|
| 核心目标 | 三者并存：**找贡献机会**（雷达重做）+ **追踪特定 issue**（新增 watchlist）+ **项目风向**（进晨报） |
| 分析深度 | 维持现状：只看标题+正文，不喂代码档案；评论**不用于**难度分析 |
| 被占判定 | 强信号（fixes/closes/resolves + assignee）+ LLM 读评论识别「我在做」口认领；**裸 #N 不再算被占** |
| 追踪广度 | 机会池内动态：label 变化、评论激增、占坑 PR 关闭、issue 被关闭（记录被谁解决） |
| issue-watch | 退役：并入 issue-radar，issues.md 只由雷达一个配方产出；不挂雷达的仓库不做 issue 情报 |

### 11.2 雷达 v2（issue-radar）

- 触发源：新 issue / 新开放 PR / **机会池动态** / 首挂载全量盘点
- 机会池动态检测零额外成本：labels、评论数、PR 引用本来就在每轮 API 里，与 `radar.yml` 快照对比（watch 轮询与 sweep 共用 `scan_radar_pool`，不调 LLM）
- 口认领检测：仅对机会池 issue 拉**新增**评论（comments API `since` 游标），一批 LLM 判定「谁在声称做」；维护者的 "PR welcome" 不算认领
- 状态机：`open → taken-assignee / taken-pr / taken-claim / closed`，迁移即警报（机会/被占/已关闭）；占坑 PR 关闭则回流机会池
- 关闭检测：活跃 issue 从开放列表消失 → 查详情（`state_reason` 区分完成/未计划）+ 近期关闭 PR 强引用 → 记录「被谁解决」
- 难度分析沿用缓存一次；报告 `issues.md` 从全量大表改为**机会榜**（确定性打分排序：难度 × 新鲜度 × help 标签 × 讨论热度，top 10 + 池内动态 + 折叠总表）

### 11.3 issue 追踪（watchlist）

- 配置：gitwire.yml `repos[].watch_issues: [123, 456]`（仅限已监控仓库，编号列表）
- 任何动态（新评论、状态翻转、label 变化）→ `watch` 警报 + `watched.md`（零 LLM，评论原文摘录）
- 状态存 `watch.yml`，与 radar.yml 同为引擎状态文件随 vault 提交

### 11.4 项目风向（晨报）

- 晨报材料新增 issue 小节：按 `radar.yml` 的 `analyzed_at` / `status_changed_at` 聚合当日新分析、机会进出、关闭事件
- 无 changelog 但有 issue 动态的日子也产出晨报

### 11.5 成本边界

- 不拉全量评论：口认领只拉机会池 issue 的增量评论；完整评论只拉 watchlist issue
- 单轮 watch 的 API 增量：雷达仓库改为全量开放 issue 列表（分页上限 7 页）+ watchlist 逐个详情（数量级为个位数）

### 11.6 v4.1 UI 重排（同日反馈）

- **Issue 专区**（导航 02，`/issues`）：跨项目聚合 radar.yml 的机会榜工作台——按状态分组（机会/困难/被占/关闭）+ 仓库/难度/关键词筛选 + 后端分页 + 展开分析详情 + 一键「追踪/取消追踪」（`POST /api/issues/watch` 切换 watch_issues）；工作流：看机会 → 看难度 → 点项目进档案建立认知 → 决定做不做
- **监控清单**：配方/追踪改为只读 tag 展示；编辑统一收进行内「设置」弹窗（modal：配方勾选 + 追踪编号）；列表带搜索/配方筛选/分页
- **全列表规范化**：运行历史（仓库/状态/触发筛选 + 分页）、警报（类型/仓库筛选 + 分页）——后端 `/api/runs`、`/api/alerts` 增加 offset 分页与筛选参数，`limit` 参数保留兼容 Dashboard

### 11.7 机会硬条件（v4.2，2026-09-25 反馈）

- 动机：原判定只有「没人占 + 难度≤中等」，缺「提了有人收」的仓库侧证据；未达条件者混在机会组冒充机会
- 硬条件（gitwire.yml `opportunity`，repos[] 可覆盖，0=关闭）：难度入榜白名单 / 超龄且无动静 / 评论过热 / 仓库 N 天无提交 / 近 N 天无外部 PR 合并（author_association 非 OWNER/MEMBER/COLLABORATOR）
- 软信号入排序：近 14 天有外部合并 +10（收外部PR）；reactions≥5 -8（想要的人多）
- 透明化：radar.yml 每条记 `excluded` 原因；报告新增「未入榜」小节；Issue 专区行内展示原因徽标，排除项归「未入榜」组（原「困难」组）

### 11.9 配置出仓库（v4.4，2026-09-25 反馈：架构修正，取代 11.8）

- 根因：旧设计「配置即仓库」把 gitwire.yml 存进 vault——配置（用户意图）与产出（情报文档）混淆，
  UI 配置操作被绑上 commit/push；按钮慢、vault 历史刷屏、PR 分支带配置皆此症状
- 修正：gitwire.yml 移到本地 `data/gitwire.yml`（.gitignore 已忽略 data/）；任何配置操作
  （增删监控/追踪切换/配方/发布模式）只写本地文件，零 git 操作，毫秒级生效
- 迁移：`Vault.open` 时 `migrate_config_out_of_repo()`——仓库里若还有 gitwire.yml，先搬到 data
  再在仓库删除（一条删除提交，随常规节奏推送）；11.8 的 commit_config_if_dirty 收口机制移除
- meta.yml 游标仍随产出提交：「发布成功游标才前进」的原子性设计，与配置无关

### 11.8 配置变更收口（v4.3，2026-09-25 反馈）

- 动机：追踪按钮每次点击都同步 commit+push——网络往返阻塞按钮（2-10s）、vault 历史被逐次操作刷屏、新增追踪还触发一整轮 run 再 push 一次
- 改为：追踪切换/设置编辑只写本地 gitwire.yml；`watch_round` 与 `publish` 开头 `commit_config_if_dirty()` 把攒下的配置改动收口为一条 `[gitwire] 配置更新` 提交，随既有 push 节奏（push_pending / publish push）推送；追踪基线由 watch_moved 自然触发（首查建基线零警报）；新挂雷达仍立即入队首扫
- 前端：追踪按钮按行禁用（原先一次操作全表按钮灰）

## 12. 托管多用户升级（2026-10-04，当前执行计划）

本节覆盖旧版“全局 PAT + 单用户 + 自动推送”的产品假设。目标是完整交付一期至三期，完成前持续验证，不以局部通过测试代替全量验收。

### 已确定约束

- 访客可查询公开仓库和阅读共享情报；监控、追踪、日报和邮件须 GitHub 登录。
- 不提供独立注册或密码登录；用户无需生成 PAT。GitHub App OAuth 只读。
- 只读取公开仓库，不修改源仓库、Star、Issue、PR 或 GitHub 通知。
- Star 每日或手动同步进入候选，必须由用户选择加入监控，忽略状态持久保存。
- 多用户的数据、规则、邮箱、Token、任务可见性隔离；公开快照和通用分析共享。
- PostgreSQL 保存业务、持久任务与邮件 outbox；Redis 缓存可重建，不能作为唯一任务存储。
- 服务器持续监控；本地客户端用本机 Git 凭证进行可选情报备份，服务端不获取本机密钥。
- 真实 OAuth、Star 导入用用户本人的账号验收，登录由用户完成。不得用后台 PAT 伪装 OAuth 成功。
- 远期推荐、对比和社区不在本次前三期内；本次不做实际贡献操作。

### 一期至三期验收清单（2026-10-04 本地交付审计）

- [x] PostgreSQL 正式迁移、备份与旧 SQLite/vault 数据迁移；不自动把旧数据交给第一个登录者。
- [x] GitHub 真实登录、刷新、断连、跨账户隔离、过期状态处理。
- [x] 我的公开仓库与 Star 完整分页、每日/手动同步、搜索、多选、忽略、去重导入。
- [x] 访客查询、公开快照共享、Redis 热缓存、条件请求、并发任务去重、分页和配额。
- [x] 仓库监控/暂停/移除、配方、关键词和难度规则、Issue 追踪、动态证据。
- [x] Worker/调度器独立运行；重启恢复、租约、重试、限流、积压观测。
- [x] 模型分析版本缓存、预算、解释证据、仓库档案、历史对比。
- [x] 邮箱验证、提醒方式、免打扰、邮件 outbox、去重、退订、失败和退信处理。
- [x] 按时区生成日报；Star 候选与关注动态汇总。
- [x] 本地导出、客户端版本管理、可选本机 Git 备份；确认远端初始化方式。
- [x] 自动化覆盖真实 PostgreSQL 隔离/并发/恢复，保留原配方回归。
- [x] 浏览器覆盖访客及真实用户关键链路；本地 Mailpit 验收；明确真实外发邮件的配置限制。

### 初始实施记录（历史状态，最终结果见本节末尾审计）

已新增 app/hosted 模块、独立 PostgreSQL/Redis/Mailpit、用户/公开语料/任务/outbox 数据模型、OAuth/候选/查询/订阅 API 与前端初版。
旧程序源文件和 data/ 原始数据保留，尚未迁移个人归属。真实 GitHub App 凭证由用户配置 .env；没有测试登录后门。
目前使用平台现有模型配置，匿名查询不自动调用模型；用户手动分析和平台模型调用有可配置额度。此默认可按后续反馈修改。
尚未通过以上完整验收，不得宣称三期已完成。

### 2026-10-04 验收进展（持续进行）

- 已用用户真实 GitHub App 完成 OAuth 登录和 Star 读取：202 个 Star 候选；随后读取 23 个自有公开仓库。没有自动添加监控，也没有向 GitHub 写入。
- 真实登录曾因 10 分钟 state 过期失败；重新发起登录成功。仍需继续覆盖刷新令牌/断连的完整浏览器体验。
- 初始 Alembic 迁移已冻结；0002 增加同步检查点及查询索引。测试从空 PostgreSQL schema 执行全部迁移，并与当前模型对比通过。
- Worker 分为控制、采集、模型三个通道；额度/免打扰延期不消耗失败次数，邮件终态失败不再无限重新排队。真实 PostgreSQL 并发验证了唯一任务合并、唯一领取和原子额度。
- 候选和 Issue 同步已改成同一持久任务内的逐页检查点；整轮完成后才提交候选缺失状态/增量水位。取消 Star 不修改已有监控，忽略选择保留。
- 列表改为 SQL 统计与分页，公开响应加入 Redis 热缓存和 ETag 回退，缓存前验证公开状态，用户响应不共享缓存。
- 个人 ZIP 导出和本地 archive-import / archive-diff / archive-push 已实现。自动化验证了跨用户过滤、不导出凭证、路径穿越拒绝、快照幂等、本地版本对比、不覆盖人工改动、源仓库不可作为备份目标。没有实际执行远端 push。
- PostgreSQL 已做本地 pg_dump 和独立临时数据库恢复演练，备份位于被忽略的 data/backups，权限 0600；未覆盖业务数据库。
- 浏览器已验证：真实账号候选分页、23 个自有公开仓库、个人 ZIP 下载、Mailpit 实际收件和邮箱验证。测试邮箱设置验收后恢复为空，未启用实际邮件提醒。
- 本轮完整检查：98 passed / 1 skipped（SQLite 下跳过仅适用 PostgreSQL 的并发测试，PostgreSQL 对应测试已运行通过）；前端生产构建通过，Mermaid 大分包提示仍在。
- 仍未完成：旧数据归属迁移、真实模型产出到档案历史的完整链路、现有配方事件完整接入、Issue 动态与解释链路补齐、退订/退信和真实外发条件、部署流程整理，以及全部关键路径验收。不能据以上局部进展宣称三期全部完成。

- 项目档案页面已接入导航、监控清单和 Issue 仓库链接；支持文档筛选、历史分页、正文阅读和上一版差异。明确标注的本地测试数据已在真实浏览器验证正文与差异弹窗、筛选默认值和布局，验收后测试仓库与版本已清除。
- 修复版本内容回退时漏记历史的问题：A → B → A 会保留三个有顺序的版本。修复托管界面输入框未复用现有 input 样式和候选复选框列过宽的问题。
- 实际采集 pallets/itsdangerous 的 125 条 Issue，整轮完成并保存增量游标。匿名 HTTP 查询可复用结果，个人监控 API 拒绝匿名访问；本次公开查询未创建模型任务、未添加监控。
- 第二次恢复演练已覆盖 0002_sync_checkpoints 结构及 225 条候选（202 Star + 23 自有公开仓库）。

### 2026-10-04 后续验收：邮件与任务可见性

- 0003 新增私人旧资料和邮件回执表；已对升级前 0002、升级后 0003 分别执行备份及临时数据库恢复演练，通过后保持本地服务运行。
- 退订确认页面、设置中的邮件投递记录/旧版私人资料入口已实现。浏览器完成本地 Mailpit 实际投递 → 邮件链接 → 确认退订；数据库确认打开链接不退订，确认后才关闭测试邮箱提醒。测试账户/资料已清理，真实账号的空邮箱与关闭提醒状态不变。
- 邮件回执支持时间戳与 HMAC 校验、重复回执幂等及编号冲突拒绝；SMTP 成功晚于退信回执返回时，不覆盖退信状态。生产仍需配置真实 SMTP、DKIM 及供应商回执转接，不能把 Mailpit 验收当成真实外发验收。
- 登录请求超时/浏览器不匹配时，浏览器返回设置页显示重新登录入口；API 保持拒绝无效 state。真实浏览器已验证，不再停在 JSON 错误页。
- 自动任务可见性正在完善：公开采集/分析任务只分享给关联的有效监控者；候选、邮件、晨报始终归本人；运行页增加状态/任务类型筛选、进度、详情和重新排队。具体检查结果以最新测试和浏览器验收为准。
- 旧资料迁移命令已具备显式 GitHub 数字 ID、只读预览、个人归属、幂等和源文件不变验证。尚未对真实旧资料执行迁移，需先补齐原机会硬条件的等价配置，避免迁移时丢失规则。
- 本轮最终自动化：`GITWIRE_TEST_POSTGRES=1 uv run pytest tests/ -q --tb=short` 为 112 passed / 1 skipped；前端生产构建通过（仍有 Mermaid 分包体积提示）。运行历史的共享/私有任务边界、失败重试时当前订阅和公开性复核、原失败历史保留均有 SQLite + PostgreSQL 覆盖。
- 浏览器真实账号再次手动同步 Star，单个任务完成 3 页、202 个候选；运行页看到逐页进度和完成结果，详情弹窗与页面布局正常。前后端、Worker、调度器及专属 PostgreSQL/Redis/Mailpit 均保持运行。


### 2026-10-04 旧规则及真实资料迁移

- 0004 增加个人机会阈值和共享仓库证据。机会筛选恢复超龄/闲置、评论过热、近期提交与外部 PR 合并窗口；0 可关闭，难度白名单仍由个人设置控制。SQL 分组/总数/分页和通知资格一致，证据不足明确进入“未入榜”。
- 独立 repo_signals 任务分完整分页读取公开 PR，带检查点与时间覆盖范围；不从单页数据推断没有外部贡献。证据完成后重新检查机会通知资格并去重，解决分析先于证据完成时漏提醒的问题。
- 完整自动化 116 passed / 1 skipped，PostgreSQL 迁移与模型一致性通过；前端生产构建通过。新测试验证同一公开 Issue 在不同用户规则下独立分组、统计/分页一致、分页第二页的外部合并证据及延迟通知去重。
- 使用已真实登录的 czm233 的不可变 GitHub ID 完成迁移：公开可读的 shibing624/agentica、RailtownAI/railtracks、browser-use/jev-ultrafast 恢复监控；另外两个旧仓库当前公开 API 不可访问，未启用监控。
- 222 份旧文档/配置/状态/运行与警报记录保存为该账号的私人档案。逐项核对配方、原追踪编号 #28、难度和机会阈值；全部源文件哈希不变。迁移报告保存在 Git 忽略的 data/backups/legacy-migration-20261004.json（0600）。
- 升级前后均已 pg_dump 并恢复到独立临时数据库验证；最新备份为 gitwire-20261004T143655Z-fa460a92.dump，结构 0004。
- 浏览器验证真实监控清单、机会参数保存、追踪编号展示/保存、旧资料入口。明确标注的本地规则测试 Issue 验证“未入榜→评论过热原因→证据详情”后已清除，没有留在用户清单或共享语料中。
- 实际后台首次采集已启动并产生真实 Issue；档案任务 cb1e7f63375443dab76b6a8a7da6e140 正在为 shibing624/agentica 获取源码，执行租约正常。尚未验证真实模型档案产出；不能将任务运行中当作模型链路已完成。
- 尚需继续：Issue 当前 PR/口认领与动态证据、配方事件接入、日程补偿和运维清理、源码采集超时/体积限制、真实模型产出以及完整生产部署说明等原范围工作。

### 2026-10-04 动态观察、真实档案与配方事件

- 0005 已实际迁移：Issue 观察快照、增量评论游标和限时认领。完整时间线分页、关联 PR 当前状态、跨仓库关闭引用、认领原文编号校验、14 天过期、评论和标签变化均已接入；新增 watch 提醒类型。
- 实际三个仓库分析完成：shibing624/agentica 8 个、RailtownAI/railtracks 7 个、browser-use/jev-ultrafast 6 个文档版本。浏览器已阅读真实 architecture.md；旧记录中“首个档案任务仍在执行”的状态已被此结果取代。
- 真实浏览器验证 RailtownAI/railtracks #1156：有仍开放的 PR #1603、作者 CoronRing、指派和原文链接，页面区分 PR 状态和 Issue 状态，提供机会阈值证据与模型分析正文。
- 配方台账接入公共事件和私人通知，覆盖 release/tripwire/breaking/cve。按有效监控、所选配方、关键词、提醒种类和订阅时间分发；不导入错误台账或私人旧资料。通知与事件同事务，重复任务不重复提醒；前次本地发布成功而托管保存中断后，即使新分析返回 noop 仍可恢复事件。
- SQLite + PostgreSQL 集成测试验证上述事件边界、回放幂等、账户隔离、事后偏好变化不重放历史、发送前重新检查配方和暂停状态。新增“功能判断变化”设置入口已在浏览器看到。
- 实际重放三份独立公共配方台账，导入 2 条 release、76 条 cve 事件；第二次重放均为 0。真实账户未选择这些提醒类型，私人通知仍为 0，邮箱提醒仍关闭，没有向用户发送真实邮件。
- 验收脚本加入队列排空保护：停止调度和新任务领取，等待当前任务完成再重启，恢复排队任务原时间；生命周期锁防并发操作。已真实执行两次，均等到运行任务归零后重启，前后端/worker/scheduler/数据库/Redis/Mailpit 全部健康且属于当前工作区。
- 最新完整检查：127 passed / 1 skipped；前端生产构建通过，仍有 Mermaid 分包体积提示。0005 数据库备份及独立临时数据库恢复演练通过，备份 gitwire-20261004T145451Z-746664f8.dump。
- 后续仍需补齐：日报与当次 Star 同步的先后顺序及跨停机补偿、机会池退出时的变化提醒、源码采集边界和子进程取消、运维过期数据清理及积压指标、完整生产部署和剩余浏览器验收。三期目标继续进行，不据此标记全部完成。

### 2026-10-04 日报同步顺序与停机补偿

- 0006 已迁移至本地验收库：日报起止时间、通知和候选的已汇总关联。旧日报按原自然日覆盖回填，测试包含纽约夏令时 23 小时的一天、边界记录和另一账户，避免升级后重新发送已汇总内容。
- 日报先持久化本人的 Star 同步依赖，释放控制通道等待完成；我的仓库同步不替代 Star 同步。等候不消耗失败重试次数；15 分钟后或同步失败/断连时生成带原因说明的日报，半轮候选不冒充完整结果。
- 改为汇总所有尚未入报的通知和已完成同步的当前候选，同日上午新 Star 能入当日报；同步迟到或记录晚到会在后续补入，忽略/取消 Star 的候选不展示。候选仍由用户选择监控。
- 调度恢复会检查最近一次应生成日期，包括当天设置小时之前漏掉的昨日日报；多日停机合成一份补偿报告。按用户锁定生成，同一天仅一份日报和 outbox 邮件；旧任务不会回退日程游标。
- 完整自动化为 133 passed / 2 skipped（两个跳过均为 SQLite 实例中只适用于 PostgreSQL 的并发用例，对应 PostgreSQL 用例已通过）。前端生产构建通过，既有 Mermaid 分包提示仍在。
- 等当前两项任务自然完成后安全重启，运行环境健康。升级前 0005 与升级后 0006 均已备份并恢复到临时库验证；最新备份 gitwire-20261004T150137Z-90391ae7.dump。
- 真实浏览器验证升级后日报页面仍可读取旧日报，新帮助说明解释同步先后与补偿逻辑。旧日报正文未被改写；真实账户的下一次定时日报尚未到点，其新生成逻辑目前由隔离 PostgreSQL 集成测试证明，不能称已真实外发验收。真实账户仍无启用邮箱，202 个 Star 候选等待首次按新机制入报。
- 运维清理后续须保护未汇总候选引用的已完成同步任务，以及活跃日报的 star_job_id。其余原清单仍待逐项完成；本目标保持进行中。

### 2026-10-04 源码采集边界与容器部署实测

- 首次建档改为获取指定提交的单层 bare Git 快照，不检出、不执行项目代码，不拉 50 层历史；默认 120 秒、200 MiB 目录轮询检查，Git 输出另有 8 MiB 上限。临时目录按任务独占，成功读取、失败、超时或取消均清理；API 兜底固定同一提交并注明仅为关键文件样本，正文总量受限。
- Git 子进程单独成组，取消/超时/超量时先终止再清理整个进程组，测试包含忽略 SIGTERM 的后代进程。公共源码获取不使用平台或用户凭证；旧 CLI 私有获取凭证移到环境，不进入 argv。Git 错误及托管配方异常不记录请求凭证。
- 旧配方 HTTP 调用统一经 PublicRecipeGithub 进入托管公共性检查和公共缓存，拒绝写方法和非仓库接口。测试验证仓库变私有后不继续返回缓存正文，create_pull 被拦在网络发送之前。
- 真实只读采集 pallets/itsdangerous 的提交 672971d6 成功，生成 102653 字符上下文（含文件树/标题），未走 API 兜底，读取后临时源码目录为空；没有新建监控或调用模型。
- Worker / 调度器处理 SIGTERM、SIGINT。Worker 停止领取新任务，等待当前工作结束；Compose 为 Worker 设置 31 分钟退出宽限，任务上限 30 分钟。SQLite + PostgreSQL 测试确认停止信号后当前任务完成、后续任务保留等待。
- Dockerfile 包含六次迁移和前端构建，固定 uv 0.9.26，以 UID 10001 运行；.dockerignore 改为源码白名单，真实 .env/data/依赖不进入镜像上下文。根 Compose 切换为 API/worker/scheduler/migrate/PostgreSQL/Redis 独立角色，无数据库或 Redis 宿主端口。
- 新增生产配置示例和只生成一次密钥的初始化脚本（0600、已有文件拒绝覆盖）；部署指南替换旧密码登录、单 SQLite、服务端推送等失效假设。未向公网部署，也没有申请 GitHub 写权限。
- 镜像 gitwire:hosted 构建成功；临时 gitwire-acceptance-deploy 资源组在已登记的 10242 端口从空库迁移至 0006，全部服务启动。镜像内验证 UID 10001、前端产物和六次迁移存在，.env 不存在。
- 真实浏览器验证容器生产页面：访客监控入口要求登录；匿名搜索 pallets/itsdangerous，经持久队列完成采集 125 条 Issue。数据库确认 0 账号、0 订阅、0 模型分析、5 条公共 HTTP 缓存。调度器生成并完成 tick，采集 job 完成。
- Docker SIGTERM 实测 worker 和 scheduler 均退出 0。临时组容器、网络、卷已清除，10242 端口释放；真实开发环境 10240/10241 和原专属基础设施始终保留，真实账号仍能看到 932 条 Issue。
- 最新完整自动化 144 passed / 2 skipped（SQLite 中的 PostgreSQL 专用并发场景）；Docker 中前端生产构建通过。源码、边界与退出测试均已覆盖，未执行远端 push。
- 原目标仍需继续：机会池退出时的动态提醒、过期记录清理与积压/心跳观测、剩余 OAuth 刷新/断连浏览器回归、真实日报到点验证、个人配置导出完整性和全部验收清单审计。生产域名/HTTPS 和真实 SMTP 的外部配置限制仍需明确，不把本地镜像验证等同公网生产验收。

### 2026-10-04 机会退出提醒、配置备份与运维

- 0007 保存事件变化前的公开证据；观察快照包含公开 Issue 和仓库活跃证据，不含用户 ID、邮箱或个人规则。机会退出后保留最后一次动态观察，覆盖常规采集先更新评论数/关闭状态的竞态，处理后不会无限追踪已退出的 Issue。
- 通知按各用户当前规则判断变化前或当前是否相关，邮件发送前仍复核暂停、关键词、机会阈值和显式追踪。SQLite + PostgreSQL 测试覆盖评论 14→16、关闭、另一用户阈值 10、事后规则收紧/追踪/暂停、重复观察不重复提醒。
- 个人 subscriptions.json 导出补齐 opportunity 阈值；既有账户过滤与不导出密钥/邮箱的测试增加实际非默认规则断言。
- 新增服务器专用 `gitwire ops-status`：Worker/调度器心跳、可执行积压及等待年龄、延期数、过期租约、任务/邮件聚合状态；无公开 HTTP 入口，不输出用户资料或任务正文。
- 每小时清理任务按类最多处理 500 条过期临时记录，保留业务历史、日报依赖、已完成同步任务和排空恢复标记。自动化验证批次上限、活跃凭证保留、历史/恢复标记保留以及聚合输出不带敏感内容。
- 完整自动化 150 passed / 2 skipped（SQLite 中不适用的 PostgreSQL 专用用例）。已确认无活动分析再重启，0007 迁移成功。升级前后均完成 pg_dump 和独立临时数据库恢复；最新备份 gitwire-20261004T152453Z-fec7d5fe.dump。
- 真实环境心跳正常，无可执行积压或过期租约；51 个模型任务延期等待额度，未强制取消预算。首个 cleanup 已完成，清理 2 条过期登录尝试，未删除活跃会话。前后端、Worker、调度器和专属基础设施保持运行。
- 浏览器确认真实账户登录保留，升级后警报页正常读取、目前无真实警报；机会退出内容由隔离数据库集成测试验证，未伪造真实用户警报。尚须继续 OAuth 刷新/断连回归、剩余浏览器关键路径及整个清单证据审计。真实日报尚未到点，真实外发 SMTP 未配置；仍不能宣称前三期全部完成。

### 2026-10-04 最终本地交付审计

本次范围为上方一期至三期功能在本地可操作环境中的实现与验收。公网部署、真实 SMTP 供应商接入和用户可选的远端 Git 推送仍按原约定由部署方/用户配置，不将这些未执行操作描述为成功。本节之前的“尚未完成”段落记录当时状态，以下证据取代已完成项目的旧状态。

| 验收项 | 当前证据 | 边界 |
|---|---|---|
| 数据库迁移与旧资料 | 0001–0007 冻结迁移与模型结构对比；多次 pg_dump / 独立临时库恢复；真实 222 份私人旧资料、3 个监控已迁移，源文件哈希未变 | 无自动认领旧资料 |
| OAuth 与隔离 | 真实 czm233 登录；提前触发实际 refresh 交换后 `/user` 身份一致；浏览器断开清空本地授权、保留资料，重新登录恢复；SQLite / PostgreSQL 两账户 CSRF、访问隔离及单次 state 测试 | 并发刷新只有一次交换；429/暂时故障不误断连；迟到的旧 Token 401 不覆盖新授权 |
| Star / 自有仓库候选 | 真实 202 Star（三页）及 23 自有公开仓库；重连后再次手动同步 202 成功；浏览器搜索、忽略、恢复、多选入口；完整分页、断点、忽略持久化及别名去重集成测试 | 多选验收后清空勾选；没有自动添加真实监控 |
| 访客与公共缓存 | 最新镜像从空库启动至 0007，浏览器匿名查询 pallets/itsdangerous 得到 125 Issue；0 账户、0 订阅、0 模型分析、5 条公共缓存；Redis/ETag/可见性与故障回退测试 | 访客只查询，不触发新模型分析 |
| 监控与动态 | 跨账户监控 CRUD、暂停/配方/关键词/难度/机会阈值测试；真实监控编辑、追踪编号与证据详情浏览器验证；当前 PR、引用、认领过期、评论/标签和机会退出竞态测试 | GitHub 始终只读；临时验收选择已恢复 |
| 后台任务与运维 | 本地独立四进程和 Docker 独立角色；实际排空重启；PostgreSQL 并发领取/合并/额度、租约恢复、延期重试、优雅停止测试；真实心跳、cleanup 和积压命令 | 正常预算延期单独计数，不冒充失败或清空预算 |
| 模型与档案 | 三个真实项目共产出 21 个文档版本；浏览器真实文档/PR 证据及隔离测试版本差异；A→B→A 历史、分析缓存与模型预算测试；原配方回归均保留 | 尚待额度的 Issue 按队列继续处理，不代表全部 Issue 都已有分析 |
| 提醒和邮件 | 本地 Mailpit 实际邮箱验证、退订页面；outbox、免打扰、当前订阅复核、失败、签名退信与竞态测试；真实账户邮箱仍为空且提醒关闭 | 不声称公网投递、收件箱到达或供应商回执已验收 |
| 日报 | `scripts/acceptance_digest.py` 在独立 schema 用真实 OAuth 读取 202 Star，实际调度/Worker 顺序执行同步→日报→SMTP；精确 Message-ID 收件核对；一份报告、一份 outbox、最新 Star 在前；时区、DST、补偿、迟到、并发重复测试 | 临时账户触发日程；真实账户原定 08:00 未改动。最终记录 data/acceptance-digest/verification-c3d22456.json；临时 schema 已清除 |
| 本地归档 | 真实 ZIP 下载；账户过滤、不含凭证/邮箱、完整个人阈值测试；本地 Git 提交、幂等、差异、手工改动保护、路径穿越与源仓库目标拒绝测试 | 用户先创建独立空远端；仅显式 archive-push 使用本机凭证，无服务器推送、无本次远端 push |
| 自动化与运行环境 | 最终完整命令 `GITWIRE_TEST_POSTGRES=1 uv run pytest tests/ -q --tb=short`：177 passed / 3 skipped；三个跳过为 SQLite 不适用的 PostgreSQL 专用场景，对应 PostgreSQL 用例通过；生产前端构建产物和最新镜像成功 | 当前镜像 c9f721576b5a；没有提交密钥，没有远端推送 |

最新授权修复统一校验 token 响应再替换密文；坏 refresh token 才要求重连，客户端配置错误/临时服务错误不会错误清除用户连接。GitHub 的刷新契约参考官方 [Refreshing user access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)。浏览器重连后数据库仍为 1 个真实账户、225 个候选、3 个监控、21 个档案版本、222 份旧资料。

剩余上线配置：真实域名和 HTTPS、GitHub App 正式回调、真实 SMTP/SPF/DKIM 和供应商回执转接；这些不是本地验收已完成的远端操作。推荐/对比/社区和源仓库贡献操作不在本次前三期范围内。

最终补充测试确认同一 Issue 的相同内容只调用一次模型，内容变化后保留第二个分析版本且只增加一次预算；首次观察早于模型结果时，公开基线补齐难度，不丢失后续机会退出依据。最新备份 gitwire-20261004T153834Z-dcfc2143.dump 已恢复验证至 0007。临时容器/网络/卷及日报测试 schema 已清除，10242 释放；当前工作区的 10240/10241、Worker/调度器、PostgreSQL/Redis/Mailpit 保持健康。真实账号邮箱仍未配置或启用；51 个 Issue 模型任务因预算延期，业务队列继续运行。
