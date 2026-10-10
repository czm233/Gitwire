# Gitwire 工程化试点:开发 → 测试 → CI → 验收 → 发布

目标:降低需求修改造成的回归风险,让开发、测试、排错和交付成为可重复的工作流。
本文是这套约定的唯一入口;稳定后的规则同步进 AGENTS.md。

## 流程图

```
新需求 / 改动(AI 或人)
        │
        ▼
  uv run python scripts/check.py        ← 本机与 CI 同一套命令
  ├─ backend    pytest(业务规则 + 接口/集成,假 GitHub/LLM,全隔离)
  ├─ frontend   node --test(纯函数单测)
  ├─ build      tsc 类型检查 + vite 构建
  └─ e2e        浏览器回归(tester-army/e2e,隔离栈,确定性断言零模型)
        │
   全绿 ┤ 红 ▶ 先分类,不许盲目重跑或改断言变绿:
        │     · 实现缺陷      → 修实现
        │     · 已批准的需求变更 → 同步改断言并说明差异(人工批准)
        │     · 测试代码问题   → 只修定位器等细节,业务断言含义不变
        │     · 环境/数据/时序  → 修环境(见"环境")
        ▼
  git commit(门禁通过) ──▶ git push
        │
        ▼
  GitHub Actions CI(.github/workflows/ci.yml,必需检查)
        │ 通过(未推送前该文件未在 GitHub 实跑过)
        ▼
  测试环境验收:scripts/acceptance_container.py up(10242 一次性全镜像栈,
  与真实数据/OAuth 隔离)→ 健康检查 + 冒烟(访客面)
        │
        ▼
  正式发布(人工批准):同一份已验证镜像 + alembic 迁移
  回退:应用回滚 ≠ 数据回滚,迁移兼容性单独评估(见"发布")
```

## 命令清单

| 命令 | 作用 |
|---|---|
| `uv run python scripts/check.py` | 完整门禁 = backend + frontend + build + e2e,提交前必跑 |
| `uv run python scripts/check.py backend` | pytest(改 `app/` 后) |
| `uv run python scripts/check.py e2e` | 浏览器回归(改 `frontend/src` 后),含隔离栈自动起停 |
| `uv run python scripts/e2e_stack.py up / status / down / seed` | 单独管理 e2e 隔离栈(10248 api / 10249 web) |
| `uv run python scripts/e2e_dashboard.py` | 回归驾驶舱(http://127.0.0.1:10247):案例表实时进度(标记 确定性/AI·模型,每轮自动纳入新测试)、勾选任意条目单独运行、停止/只跑失败项/全量回归;**关闭会话或 Ctrl-C 自动收摊**(SIGTERM/SIGHUP 均优雅退出并拆除隔离栈) |
| `uv run python scripts/acceptance.py up / status / down / restart` | 本机验收环境生命周期(10240/10241 + Postgres/Redis/Mailpit) |
| `uv run python scripts/acceptance_container.py up / status / down` | 10242 一次性全镜像验收栈(测试环境) |
| `npm --prefix frontend run e2e -- --headed` | 可视化跑 e2e(需隔离栈已 up) |

## 分层测试:谁管什么

| 层 | 工具 | 覆盖 | 数据 |
|---|---|---|---|
| 业务规则 | pytest(`tests/`) | 计算、权限、状态机、配额、隔离、CSRF、OAuth | 临时 sqlite / Postgres schema,假 GitHub + 假 LLM |
| 前端纯逻辑 | `node --test`(`frontend/tests/`) | 状态映射等纯函数 | 无 |
| 浏览器回归 | tester-army/e2e(`frontend/e2e/`) | 58 条页面规则"答案卡":8 个导航页全部覆盖(雷达统计/分组/筛选/详情标签页/翻译失效、候选排序与 Star 时间、分页条行为、监控暂停恢复与确认弹窗、警报已读、晨报、档案、投递、退订、设置)+ 写操作路径(第二种子用户 writer 隔离) | e2e 隔离栈(sqlite + `scripts/e2e_stack.py` 种子,含 pilot/writer 双用户) |

e2e 断言以确定性 `expect`(定位器 + 文本)为主;另有一条 `agent.act`
演示(操作路径交给 AI,结果仍由 `expect` 裁决)。模型自动复用应用 `.env` 的
`LLM_*`(智谱 Coding Plan 的 Anthropic 兼容口,glm-5.3-flash 已实测支持工具调用
+ 图片输入;`e2e/llm-env.ts` 负责探测与 baseURL 修正),或用 `E2E_LLM_*` 覆盖。
实测(2026-10-10):冷跑 5 次模型调用 / 57.9k tokens(63% 命中提示缓存)/ 33s;
缓存回放 0 调用 / 1s。无 key 的环境(如 CI)agent 测试自动跳过。
**应用模型与测试模型是独立关切**:应用可换纯文本模型(如 DeepSeek),agent 必须多模态。
未显式配置 E2E_LLM_* 时借用应用 LLM_*,但每轮先做能力探测(1像素图+1个工具定义);
不合格(纯文本模型/key 不匹配)则 agent 测试跳过并提示「后果 + 独立配置方法」,
绝不静默失败。

### e2e 种子契约

`scripts/e2e_stack.py` 顶部的 `SEED` 与 `frontend/e2e/*.e2e.ts` 的断言互为契约:
两个种子账户——`pilot`(只读断言:5 条 Issue 含翻译失效/未入榜样本、29 条 Star 候选、
任务/警报/晨报/档案/投递/旧版资料)与 `writer`(写操作断言:独立仓库 pilot/gamma、
独立警报,与 pilot 的数据互不干扰,保证并行测试确定性)。
会话 cookie `e2e-pilot-session` / `e2e-writer-session` 经 `browser.setCookies` 注入,
绕开 GitHub OAuth;前端写操作自动携带 `/auth/me` 下发的 CSRF 令牌。
每次 `up()`(含栈复用)都会重建种子世界。改种子必须同步改断言,反之亦然。

## 测试规则的变更边界(AI 与人都适用)

**机制层(2026-10-10 加固):答案卡门禁。** 受保护路径 = `frontend/e2e/**`(浏览器断言)、
`tests/**`(pytest 断言)、`scripts/e2e_stack.py`(种子契约)。任何对它们的提交必须先由人签发
一次性令牌(15 分钟有效):

```bash
uv run python scripts/answer_card.py approve "默认分组改为可上手:产品决议"
git commit ...   # 钩子放行;建议提交信息附「答案卡变更:...」
```

无令牌时 pre-commit 直接拒绝(`.githooks/pre-commit`,经 `git config core.hooksPath .githooks` 启用)。
自己确认要改也可 `git commit --no-verify` 跳过,慎用——每一次考卷变更都应留下"是谁批准的"。

纪律层:

1. **不得为了变绿**:删用例、跳过检查、放宽断言、修改预期结果。
2. 红灯先分类(实现缺陷 / 已批准需求变更 / 测试代码问题 / 环境数据时序),再动手。
3. 原预期仍正确 → 修实现;需求已批准变更 → 签发令牌后同步改断言,提交说明写清差异;
   预期含义不明确 → 先问产品(仓库主人),不要猜。
4. 修定位器等操作细节时,业务断言含义保持不变。
5. 自动修复重试上限 3 次;反复重跑偶然通过不算修复完成。
6. 修复过的 bug 必须先转成回归断言(pytest 或 e2e)再关闭。
7. 断言行数等"动态量"不做精确断言(如运行历史行数会含自动排入的翻译任务),按语义过滤断言。
8. 演示/故障注入若改的是**未跟踪文件**(尚未提交过的新文件),`git checkout` 无法还原——必须反向编辑还原,并以重跑测试全绿作为还原确认(2026-10-10 门禁演示残留被回归测试当场抓获的教训)。

## 环境

| 环境 | 端口 | 数据 | 用途 |
|---|---|---|---|
| 本机验收 | 10240/10241(+10243-10246 compose) | 真实(Postgres 卷) | 日常开发 |
| e2e 隔离栈 | 10248/10249 | 一次性 sqlite(`data/e2e-stack/`,gitignore) | 浏览器回归,每次 up 重置种子 |
| 全镜像验收栈 | 10242 | 一次性 Docker 卷(`gitwire-acceptance-deploy`) | 测试环境验收 |

- 凭证全部在 gitignore 的本地文件(`.env`、`.env.infra`、`data/e2e-stack/runtime.env`),
  仓库只提交 `*.example`;e2e 栈的 `ENCRYPTION_KEY` 首次生成后落盘复用,密钥不进代码。
- e2e 与 CI 均不依赖 Postgres/Redis/Docker(后端 REDIS_URL 为空走降级路径,有 pytest 覆盖)。

## 覆盖率

`GITWIRE_COV=1 uv run python scripts/check.py backend` 输出语句覆盖率并设防倒退线
(2026-10-11 基线:总覆盖 80%,5740 语句;`--cov-fail-under=79`)。默认不启用,
避免日常门禁被覆盖率噪声干扰;发版前或大改后建议跑一次。低覆盖模块(如 osv.py)
是补 pytest 的优先方向。

## CI(GitHub Actions)

`.github/workflows/ci.yml`:GitHub 托管 ubuntu runner,uv.lock + package-lock 锁定依赖,
跑 `scripts/check.py all`(与本机完全同一套),失败上传 `frontend/.e2e/` 报告
(trace.md 含步骤/截图/应用日志,`summary.md` 汇总,junit.xml 可机读)。

**状态(2026-10-09):workflow 已就绪,但尚未 push,未在 GitHub 实际运行过。**
首次 push 后需在仓库 Settings → Branches 给 `main` 配置分支保护 + 必需检查 `check`,
才会真正阻止未过 CI 的合入(需要仓库管理员权限)。

## 测试环境验收与发布

- **测试环境**:`docker build -t gitwire:hosted .`(栈脚本不构建镜像,要求已存在)→
  `uv run python scripts/acceptance_container.py up` 起全镜像栈(迁移 → api/worker/scheduler),
  `/api/health` 健康后冒烟:
  `E2E_APP_URL=http://127.0.0.1:10242 npx e2e run e2e/guest.e2e.ts`(在 frontend/ 下执行;
  空库、无真实 OAuth,冒烟覆盖访客面:`/` 重定向、公开浏览引导、登录入口)。
  验收后 `down` 会连卷一起销毁,不影响真实数据。
- **发布**:人工批准后用同一份已验证镜像 + `alembic upgrade head`;
  数据库迁移的兼容性与恢复单独评估——**回退应用不等于回退数据库**,
  破坏性迁移必须先备份卷(`scripts/backup_local.py`)并提供前滚 SQL。
- 尚无外部部署目标/凭证;具备后把"发布"一节替换为实际地址与步骤。

## 验证记录(2026-10-09)

- 全量绿:`check.py` 四层全过(pytest 124+4s、前端单测、构建、e2e 12/12,约 40s)。
- 故障注入 1(前端默认分组 全部→可上手):3 条 e2e 断言红,精确命中该规则;恢复后绿。
- 故障注入 2(后端 Issue 排序 倒→正):"最新在前"断言红;恢复后绿。注入未留任何痕迹(`git diff` 干净)。
- 测试环境实跑:`docker build -t gitwire:hosted .` → `acceptance_container.py up`(10242,
  迁移+健康全过)→ 访客面冒烟 4/4 绿。容器运行时为 OrbStack。
- 未验证项:GitHub Actions 实跑(待 push 后首次触发)、分支保护必需检查(需仓库管理员在
  Settings → Branches 配置,把 `check` 设为必需)。
