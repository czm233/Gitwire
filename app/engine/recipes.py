"""配方框架 v1/v2/v4：docs-sync 之外的五个配方 + 警报解析。

- feature-tripwire：哨兵警戒。增量时判断每个哨兵的判决是否翻转，翻转即警报
- bug-watch：从 diff 中提炼缺陷修复与破坏性变更，破坏性变更即警报
- issue-radar v2：机会雷达。强信号被占 + 口认领 + 机会池动态 + 机会榜排序，
  issue-watch 已并入（issues.md 只由雷达产出）
- watchlist：追踪 issue 动态（零 LLM，评论原文）
- release-brief：新 release 的情报简报
- cve-scan：OSV 依赖漏洞扫描（确定性代码产出，不耗 LLM）

LLM 配方共用输出契约：<<<FILE:path>>> 文件块 + <<<ALERT:kind|标题|说明>>> 警报行
+ <<<META:summary>>> 摘要行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.engine.analyze import AnalyzeError, _build_incremental_context, load_recipe, render
from app.osv import DEP_FILES, OsvClient, parse_deps

ALERT_LINE = re.compile(r"<<<ALERT:([\w-]+)\|([^|>]+)\|([^>]*)>>>")
SYSTEM_PROMPT = (
    "你是 Gitwire 的开源情报分析员。你的产出会被程序解析并原样写入情报仓库，"
    "必须严格遵守输出契约。"
)


@dataclass
class RecipeResult:
    files: dict[str, str] = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)  # {kind,title,body}
    summary: str = ""


def parse_alerts(text: str) -> list[dict]:
    return [
        {"kind": m.group(1).strip(), "title": m.group(2).strip(), "body": m.group(3).strip()}
        for m in ALERT_LINE.finditer(text)
    ]


def parse_recipe_output(text: str) -> tuple[dict[str, str], list[dict], str]:
    from app.engine.analyze import META_SUMMARY, FILE_BLOCK

    files = {
        m.group(1).strip(): m.group(2).strip("\n")
        for m in FILE_BLOCK.finditer(text)
        if m.group(1).strip()
    }
    alerts = parse_alerts(text)
    ms = META_SUMMARY.search(text)
    summary = ms.group(1).strip() if ms else ""
    return files, alerts, summary


def _validate(files: dict[str, str], required: list[str]) -> list[str]:
    errors: list[str] = []
    for req in required:
        if req not in files:
            errors.append(f"缺少必须输出的文件 {req}")
    for path, content in files.items():
        if path.startswith("/") or ".." in path.split("/"):
            errors.append(f"非法路径 {path}")
        if not content.strip():
            errors.append(f"{path} 内容为空")
        elif content.count("```") % 2 != 0:
            errors.append(f"{path} 代码围栏不配对")
    return errors


async def _run_llm_recipe(
    svc,
    recipe: str,
    variables: dict[str, str],
    required_files: list[str],
) -> RecipeResult:
    """通用 LLM 配方执行：渲染 → 调用 → 解析 → 校验 → 重试一次。"""
    template = load_recipe(recipe)
    prompt = render(template, **variables)
    last_errors: list[str] = []
    for attempt in range(2):
        final = prompt
        if last_errors:
            final += (
                "\n\n# 上一次输出被驳回，原因：\n"
                + "\n".join(f"- {e}" for e in last_errors)
                + "\n请严格按输出契约重新输出全部内容。"
            )
        out = await svc.llm.chat(SYSTEM_PROMPT, final)
        files, alerts, summary = parse_recipe_output(out)
        last_errors = _validate(files, required_files)
        if not last_errors:
            return RecipeResult(files=files, alerts=alerts, summary=summary)
    raise AnalyzeError(f"[{recipe}] 输出两次未通过校验: " + "; ".join(last_errors[:4]))


async def build_diff_context(svc, repo: str, old_sha: str, new_sha: str) -> str:
    compare = await svc.gh.compare(repo, old_sha, new_sha)
    return _build_incremental_context(compare)


# ---------- feature-tripwire（哨兵警戒） ----------


async def run_feature_tripwire(
    svc, repo: str, old_sha: str, new_sha: str, slug: str
) -> RecipeResult:
    from app.vault import slugify

    slug = slug or slugify(repo)
    existing = svc.vault.read_file(f"{slug}/tripwires.md") or "（尚无哨兵清单，请依据本次材料建立）"
    context = await build_diff_context(svc, repo, old_sha, new_sha)
    return await _run_llm_recipe(
        svc,
        "feature-tripwire",
        {
            "REPO": repo,
            "OLD_SHA": old_sha[:7],
            "NEW_SHA": new_sha[:7],
            "DATE": svc.settings.local_date_str(),
            "EXISTING": existing,
            "CONTEXT": context,
        },
        required_files=["tripwires.md"],
    )


# ---------- bug-watch（缺陷观察） ----------


async def run_bug_watch(svc, repo: str, old_sha: str, new_sha: str, slug: str) -> RecipeResult:
    existing = svc.vault.read_file(f"{slug}/bugwatch.md")
    context = await build_diff_context(svc, repo, old_sha, new_sha)
    return await _run_llm_recipe(
        svc,
        "bug-watch",
        {
            "REPO": repo,
            "OLD_SHA": old_sha[:7],
            "NEW_SHA": new_sha[:7],
            "DATE": svc.settings.local_date_str(),
            "EXISTING": existing or "（尚无记录，请依据本次材料建立）",
            "CONTEXT": context,
        },
        required_files=["bugwatch.md"],
    )


# ---------- v4 共用：口认领检测 / 机会池扫描 / issue 追踪 ----------

CLAIM_LINE = re.compile(r"<<<CLAIM:#(\d+)\|([^|>]+)>>>")
COMMENT_SURGE = 5  # 评论激增阈值（超过才算池内动态）
CLAIM_TTL_DAYS = 14  # 口认领黏性：没人跟进多久后回流机会池
HELP_LABELS = {
    "good first issue", "good-first-issue", "help wanted", "help-wanted",
    "beginner", "beginner-friendly", "accepting prs", "accepting-prs",
    "difficulty: easy", "easy",
}
STATUS_LABEL = {
    "open": "🟢机会", "taken-pr": "🔒PR占", "taken-assignee": "🔒认领",
    "taken-claim": "🙋口认领", "closed": "✅关闭",
}


def _age_days(iso: str | None, now) -> int:
    from datetime import datetime

    if not iso:
        return 9999
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 9999
    return max(0, (now - t).days)


def _parse_claims(text: str) -> dict[int, str]:
    return {int(m.group(1)): m.group(2).strip() for m in CLAIM_LINE.finditer(text)}


async def _detect_claims(
    svc, repo: str, materials: list[tuple[dict, list[dict]]]
) -> dict[int, dict]:
    """materials: [(issue, 新增评论)]。一批 LLM 判定口认领，返回 {issue 编号: {by, url}}。
    认领评论链接不由 LLM 产出，按认领人回查原始评论取 html_url（防编造）。
    尽力而为：解析不到就当没有，不重试不阻断雷达。"""
    if not materials:
        return {}
    blocks = []
    for it, comments in materials:
        c_lines = [
            f"- [{c.get('author', '')} {(c.get('created_at') or '')[:10]}] "
            f"{(c.get('body') or '（空）')[:600]}"
            for c in comments
        ]
        blocks.append(f"### #{it['number']} {it['title']}\n" + "\n".join(c_lines))
    prompt = render(
        load_recipe("issue-radar", "claim.md"),
        REPO=repo,
        DATE=svc.settings.local_date_str(),
        COMMENTS="\n\n".join(blocks),
    )
    out = await svc.llm.chat(SYSTEM_PROMPT, prompt)
    by_number = _parse_claims(out)
    claims: dict[int, dict] = {}
    for it, comments in materials:
        n = it["number"]
        author = by_number.get(n)
        if not author:
            continue
        c = next((x for x in reversed(comments) if x.get("author") == author), None)
        claims[n] = {"by": author, "url": (c or {}).get("html_url") or ""}
    return claims


@dataclass
class RadarScan:
    issues: list[dict]  # 全量开放 issue
    pulls: list[dict]  # 开放 PR
    dynamics: list[str]  # 检测到的机会池动态（人话描述）


async def scan_radar_pool(svc, repo: str, slug: str) -> RadarScan:
    """机会池状态采集 + 动态检测（不调 LLM）。watch 轮询与雷达 sweep 共用一套数据：
    labels、评论数、PR 引用本来就在列表 API 里，与 radar.yml 快照对比即得。"""
    issues = await svc.gh.list_issues(repo, since_number=0, limit=0)
    pulls = await svc.gh.list_pulls(repo, state="open")
    cache = _load_radar_state(svc, slug)["issues"]
    by_number = {it["number"]: it for it in issues}
    open_pr_numbers = {p["number"] for p in pulls}
    dynamics: list[str] = []
    for n_str, entry in cache.items():
        n = int(n_str)
        if entry.get("last_status") == "closed":
            continue
        it = by_number.get(n)
        if it is None:
            dynamics.append(f"#{n} 从开放列表消失（可能已关闭）")
            continue
        if entry.get("last_status") == "taken-pr":
            claims = {int(x) for x in entry.get("pr_claims") or []}
            if claims and not (claims & open_pr_numbers):
                dynamics.append(f"#{n} 占坑 PR 已关闭，可能重回机会池")
        if it.get("comments", 0) - int(entry.get("comments_seen") or 0) >= COMMENT_SURGE:
            dynamics.append(f"#{n} 评论激增（竞争升温）")
        if sorted(it.get("labels") or []) != sorted(entry.get("labels_seen") or []):
            dynamics.append(f"#{n} 标签变化")
    return RadarScan(issues=issues, pulls=pulls, dynamics=dynamics)


# ---------- issue 追踪（watchlist，零 LLM） ----------


def _load_watch_state(svc, slug: str) -> dict:
    import yaml

    raw = svc.vault.read_file(f"{slug}/watch.yml")
    if not raw:
        return {"issues": {}}
    try:
        data = yaml.safe_load(raw) or {}
        return {"issues": data.get("issues") or {}}
    except Exception:  # noqa: BLE001
        return {"issues": {}}


def _dump_watch_state(state: dict) -> str:
    import yaml

    return yaml.safe_dump(state, allow_unicode=True, sort_keys=False)


def _watch_entry(detail: dict, now_iso: str) -> dict:
    return {
        "title": detail.get("title") or "",
        "state": detail.get("state") or "open",
        "state_reason": detail.get("state_reason"),
        "labels_seen": sorted(detail.get("labels") or []),
        "comments_seen": detail.get("comments", 0),
        "checked_at": now_iso,
    }


async def watchlist_changed(svc, repo: str, slug: str, numbers: list[int]) -> bool:
    """watch 轮询用：追踪 issue 是否有动态（逐个查详情，数量级为个位数）。"""
    state = _load_watch_state(svc, slug)["issues"]
    for n in numbers:
        detail = await svc.gh.issue_detail(repo, n)
        if detail is None:
            continue
        prev = state.get(str(n))
        if (
            prev is None
            or detail.get("state") != prev.get("state")
            or detail.get("comments", 0) > int(prev.get("comments_seen") or 0)
            or sorted(detail.get("labels") or []) != sorted(prev.get("labels_seen") or [])
        ):
            return True
    return False


async def run_watchlist(svc, repo: str, slug: str, numbers: list[int]) -> RecipeResult:
    """issue 追踪：追踪 issue 的任何动态（新评论/状态翻转/标签变化）→ watched.md + watch 警报。
    评论只在这里拉原文；无动态不产出文件（不产生空提交）。
    设置 unwatch_closed 开启时：已关闭的追踪就地撤销（写入本地配置，watched.md 留痕）。"""
    cfg = svc.vault.read_config()
    unwatch_closed = cfg.unwatch_closed
    state = _load_watch_state(svc, slug)
    issues_state: dict = state["issues"]
    now_iso = svc.settings.local_now().isoformat(timespec="seconds")
    date = svc.settings.local_date_str()
    alerts: list[dict] = []
    sections: list[str] = []
    changed = False
    auto_unwatched: list[int] = []

    for n in sorted(numbers):
        detail = await svc.gh.issue_detail(repo, n)
        if detail is None:
            sections.append(f"## #{n}\n\n- ⚠️ 无法获取（issue 可能已随仓库删除）\n")
            continue
        # 已关闭不追踪：撤销仍留在报告与配置里留痕（首查建基线不出警报的规则不变）
        auto = unwatch_closed and detail.get("state") == "closed"
        prev = issues_state.get(str(n))
        url = detail.get("html_url") or f"https://github.com/{repo}/issues/{n}"
        lines = [
            f"## [#{n} {detail['title']}]({url})",
            "",
            f"- 状态：{detail.get('state', 'open')}"
            + (f"（{detail['state_reason']}）" if detail.get("state_reason") else ""),
            f"- 标签：{', '.join(detail.get("labels") or []) or '—'}"
            f" · 评论 {detail.get('comments', 0)} 条 · assignee：{detail.get('assignee') or '无'}",
        ]
        if prev is None:
            changed = True  # 首次追踪：只建基线，不警报
            issues_state[str(n)] = _watch_entry(detail, now_iso)
            events = []
        else:
            events: list[str] = []
            if detail.get("state") != prev.get("state"):
                reason = f"，原因 {detail['state_reason']}" if detail.get("state_reason") else ""
                events.append(f"状态 {prev.get('state')} → {detail.get('state')}{reason}")
                title = f"追踪 #{n}：{prev.get('state')} → {detail.get('state')}"
                if auto:
                    title += "（已自动撤销追踪）"
                alerts.append(
                    {
                        "kind": "watch",
                        "title": title,
                        "body": detail.get("title") or "",
                    }
                )
            new_labels = sorted(
                set(detail.get("labels") or []) - set(prev.get("labels_seen") or [])
            )
            if new_labels:
                events.append(f"新增标签 {', '.join(new_labels)}")
                alerts.append(
                    {
                        "kind": "watch",
                        "title": f"追踪 #{n}：新增标签 {', '.join(new_labels)}",
                        "body": detail.get("title") or "",
                    }
                )
            elif sorted(detail.get("labels") or []) != sorted(prev.get("labels_seen") or []):
                events.append("标签有变化")
            if detail.get("comments", 0) > int(prev.get("comments_seen") or 0):
                comments = await svc.gh.issue_comments(
                    repo, n, since=prev.get("checked_at") or ""
                )
                if comments:
                    events.append(f"新评论 {len(comments)} 条")
                    latest = comments[-1]
                    alerts.append(
                        {
                            "kind": "watch",
                            "title": (
                            f"追踪 #{n}：新评论 {len(comments)} 条"
                            f"（最新 by {latest.get('author', '')}）"
                        ),
                            "body": (latest.get("body") or "")[:400],
                        }
                    )
                    lines.append("")
                    lines.append(f"### 新评论（{date}）")
                    lines.extend(
                        f"- [{c.get('author', '')} {(c.get('created_at') or '')[:10]}] "
                        f"{c.get('body') or '（空）'}"
                        for c in comments[-10:]
                    )
            issues_state[str(n)] = _watch_entry(detail, now_iso)
        if auto:
            auto_unwatched.append(n)
            events.append("已关闭 → 自动撤销追踪（设置：已关闭不追踪）")
        if events:
            changed = True
            lines.append("")
            lines.append(f"### 动态（{date}）")
            lines.extend(f"- {e}" for e in events)
        lines.append("")
        sections.append("\n".join(lines))

    gone = set(auto_unwatched)
    for k in list(issues_state):  # 清掉已取消追踪的（含本轮自动撤销的）
        if int(k) not in numbers or int(k) in gone:
            del issues_state[k]

    if gone:  # 从本地追踪清单移除（只写 data 目录，不入情报仓库）
        target = cfg.target(repo)
        if target:
            target.watch_issues = [x for x in target.watch_issues if x not in gone]
            svc.vault.write_config(cfg)

    if not changed:
        return RecipeResult()  # 无动态：不产出文件（checked_at 基线待下次动态时一起推进）

    report = [
        f"# Issue 追踪 · {repo}",
        "",
        f"> 更新：{date} · 追踪 {len(numbers)} 个 issue（动态推 watch 警报，新评论原文见下）",
    ]
    if auto_unwatched:
        report.append(
            f"> 已关闭自动撤销：{'、'.join(f'#{n}' for n in auto_unwatched)}"
            "（设置：已关闭不追踪）"
        )
    report += [""] + sections
    summary = f"追踪 {len(numbers)} 个 issue" + (f"，{len(alerts)} 条动态" if alerts else "")
    if auto_unwatched:
        summary += f"，自动撤销 {len(auto_unwatched)} 个已关闭"
    return RecipeResult(
        files={
            "watched.md": "\n".join(report).strip() + "\n",
            "watch.yml": _dump_watch_state(state),
        },
        alerts=alerts,
        summary=summary,
    )


# ---------- issue-radar v2（机会雷达：强信号被占 / 口认领 / 机会池动态 / 机会榜） ----------

ISSUE_BLOCK = re.compile(r"<<<ISSUE:#(\d+)\|([^|]+)\|([^>]*)>>>(.*?)(?=\n<<<ISSUE:|\Z)", re.S)
DIFFICULTIES = ("简单", "中等", "困难")
RADAR_BATCH = 15  # 每批送 LLM 分析的 issue 数
OPPORTUNITY_TOP = 10  # 机会榜展示条数

# ---------- 机会硬条件（v4.2）：仓库信号 + 未入榜原因 ----------

# 外部贡献者 = 非 OWNER / MEMBER / COLLABORATOR（GitHub author_association）
EXTERNAL_ASSOC = {"NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER"}


@dataclass
class RepoSignals:
    """仓库级机会信号：维护者还活着吗、收不收外部贡献。None / False = 未知（不据此排除）。"""

    pushed_days_ago: float | None = None
    ext_merge_known: bool = False
    last_ext_merge_days_ago: float | None = None
    ext_merges: int = 0


async def _repo_signals(svc, repo: str, now) -> RepoSignals:
    """repo_info.pushed_at + 近期已合并 PR 的作者身份（每轮雷达各一次）。查询失败字段留未知。"""
    sig = RepoSignals()
    try:
        pushed = (await svc.gh.repo_info(repo)).get("pushed_at") or ""
        if pushed:
            sig.pushed_days_ago = _age_days(pushed, now)
    except Exception:  # noqa: BLE001
        return sig  # 仓库信息拿不到：仓库级条件全部跳过
    try:
        merged_days = [
            _age_days(p.get("merged_at"), now)
            for p in await svc.gh.list_pulls(repo, state="closed")
            if p.get("merged")
            and p.get("merged_at")
            and p.get("author_association") in EXTERNAL_ASSOC
        ]
        sig.ext_merge_known = True
        if merged_days:
            sig.ext_merges = len(merged_days)
            sig.last_ext_merge_days_ago = min(merged_days)
    except Exception:  # noqa: BLE001
        pass
    return sig


def _exclusion_reason(it: dict, opp_cfg, sig: RepoSignals, now) -> str:
    """开放且难度合格的 issue 为什么不进机会榜；空串 = 入榜。阈值为 0 表示关闭该条件。"""
    if sig.pushed_days_ago is not None:
        if 0 < opp_cfg.repo_pushed_within_days < sig.pushed_days_ago:
            return f"仓库 {int(sig.pushed_days_ago)} 天无提交"
        if 0 < opp_cfg.external_merge_within_days and sig.ext_merge_known:
            if sig.last_ext_merge_days_ago is None:
                return "仓库从未合并外部 PR"
            if sig.last_ext_merge_days_ago > opp_cfg.external_merge_within_days:
                return f"仓库 {int(sig.last_ext_merge_days_ago)} 天无外部 PR 合并"
    age = _age_days(it.get("created_at"), now)
    idle = _age_days(it.get("updated_at") or it.get("created_at") or "", now)
    if 0 < opp_cfg.max_age_days < age and idle > 14:
        return f"建于 {age} 天前且近 {int(idle)} 天无动静"
    if 0 < opp_cfg.max_comments < it.get("comments", 0):
        return f"评论 {it['comments']} 条，竞争过热"
    return ""


def _radar_issue_block(issues: list[dict]) -> str:
    lines = []
    for it in issues:
        labels = ",".join(filter(None, it.get("labels", [])))
        assignee = it.get("assignee") or "无"
        lines.append(
            f"### #{it['number']} {it['title']}\n"
            f"- 提交人：{it.get('author', '')} · 日期：{(it.get('created_at') or '')[:10]}"
            f" · assignee：{assignee} · 评论 {it.get('comments', 0)} 条"
            + (f" · 标签：{labels}" if labels else "")
            + f"\n- 正文：{it.get('body') or '（空）'}"
        )
    return "\n\n".join(lines)


def _load_radar_state(svc, slug: str) -> dict:
    import yaml

    raw = svc.vault.read_file(f"{slug}/radar.yml")
    if not raw:
        return {"last_sweep": "", "issues": {}}
    try:
        data = yaml.safe_load(raw) or {}
        return {"last_sweep": data.get("last_sweep") or "", "issues": data.get("issues") or {}}
    except Exception:  # noqa: BLE001
        return {"last_sweep": "", "issues": {}}


def _dump_radar_state(state: dict) -> str:
    import yaml

    return yaml.safe_dump(state, allow_unicode=True, sort_keys=False)


async def _analyze_batch(svc, repo: str, batch: list[dict]) -> dict[int, dict]:
    """一批 issue 交给 LLM 难度分析，返回 {number: {difficulty,summary,problem,plan}}。"""
    template = load_recipe("issue-radar")
    stars = ""
    try:
        info = await svc.gh.repo_info(repo)
        stars = str(info.get("stargazers_count", "?"))
    except Exception:  # noqa: BLE001
        stars = "?"
    prompt = render(
        template,
        REPO=repo,
        STARS=stars,
        DATE=svc.settings.local_date_str(),
        ISSUES=_radar_issue_block(batch),
    )
    wanted = {it["number"] for it in batch}
    last_errors: list[str] = []
    for _attempt in range(2):
        final = prompt + (
            "\n\n# 上一次输出被驳回，原因：\n"
            + "\n".join(f"- {e}" for e in last_errors)
            + "\n请严格按契约重新输出全部 issue 块。"
            if last_errors
            else ""
        )
        out = await svc.llm.chat(SYSTEM_PROMPT, final)
        analyses: dict[int, dict] = {}
        for m in ISSUE_BLOCK.finditer(out):
            n = int(m.group(1))
            difficulty = m.group(2).strip()
            detail = m.group(4).strip("\n")
            problem = plan = ""
            for line in detail.splitlines():
                line = line.strip()
                if line.startswith("- 问题："):
                    problem = line[len("- 问题："):].strip()
                elif line.startswith("- 方案："):
                    plan = line[len("- 方案："):].strip()
            if difficulty not in DIFFICULTIES:
                continue
            analyses[n] = {
                "difficulty": difficulty,
                "summary": m.group(3).strip(),
                "problem": problem,
                "plan": plan,
                "analyzed_at": svc.settings.local_date_str(),
            }
        missing = wanted - set(analyses)
        if not missing:
            return analyses
        last_errors = [f"缺少 issue #{n} 的分析块" for n in sorted(missing)]
    raise AnalyzeError(
        f"[issue-radar] 批量分析两次未通过校验: {last_errors[:3]}"
    )


def _worth(it: dict, entry: dict, now, sig: RepoSignals | None = None) -> tuple[int, str]:
    """确定性打分：难度 × 新鲜度 × help 标签 × 讨论热度 × 仓库信号。返回 (分, 信号描述)。"""
    score = {"简单": 40, "中等": 20}.get(entry.get("difficulty", ""), 0)
    signals: list[str] = []
    age = _age_days(it.get("created_at"), now)
    if age <= 7:
        score += 20
        signals.append("新鲜")
    elif age <= 14:
        score += 12
        signals.append("较新")
    elif age <= 30:
        score += 6
    help_lbls = sorted(
        {lb for lb in (it.get("labels") or []) if lb.strip().lower() in HELP_LABELS}
    )
    if help_lbls:
        score += 15
        signals.append("+".join(help_lbls))
    c = it.get("comments", 0)
    if c >= 20:
        score -= 15
        signals.append(f"讨论多({c})")
    elif c >= 10:
        score -= 8
        signals.append(f"有讨论({c})")
    r = it.get("reactions") or 0
    if r >= 5:
        score -= 8
        signals.append(f"想要的多({r})")
    if sig and sig.last_ext_merge_days_ago is not None and sig.last_ext_merge_days_ago <= 14:
        score += 10
        signals.append("收外部PR")
    return score, "·".join(signals) if signals else "—"


async def run_issue_radar(
    svc, repo: str, slug: str, all_issues: list[dict], open_prs: list[dict]
) -> RecipeResult:
    """issue 雷达 v2（全景扫描 + 机会池动态）：
    - 被占判定只认强信号（assignee / fixes-closes PR）+ LLM 口认领（14 天黏性）
    - 机会池动态：标签变化、评论激增、占坑 PR 关闭回流、issue 关闭（记录被谁解决）
    - 难度分析沿用缓存一次；报告为按值得做排序的机会榜
    """
    from app.github import GithubClient

    now = svc.settings.local_now()
    now_iso = now.isoformat(timespec="seconds")
    date = svc.settings.local_date_str()
    state = _load_radar_state(svc, slug)
    cache: dict = state["issues"]
    opp_cfg = svc.vault.read_config().opportunity_for(repo)
    sig = await _repo_signals(svc, repo, now)
    by_number = {it["number"]: it for it in all_issues}
    alerts: list[dict] = []
    dynamics: list[str] = []

    # ---- 1. 消失的活跃 issue：查详情判关闭 + 谁解决 ----
    vanished = sorted(
        int(n)
        for n, e in cache.items()
        if e.get("last_status") != "closed" and int(n) not in by_number
    )
    if vanished:
        closed_refs: dict[int, list[int]] = {}
        for p in await svc.gh.list_pulls(repo, state="closed"):
            for n in GithubClient.parse_issue_refs(p):
                closed_refs.setdefault(n, []).append(p["number"])
        for n in vanished:
            detail = await svc.gh.issue_detail(repo, n)
            if not detail or detail.get("state") != "closed":
                continue  # 仍开放（>300 截断或瞬态），留待下轮
            entry = cache.get(str(n)) or {}
            entry["title"] = detail.get("title") or entry.get("title", "")
            entry["closed_at"] = detail.get("closed_at") or now_iso
            if detail.get("state_reason") == "not_planned":
                entry["resolved_by"] = "维护者关闭（未计划）"
            elif closed_refs.get(n):
                entry["resolved_by"] = f"PR#{min(closed_refs[n])}"
            else:
                entry["resolved_by"] = "已关闭（来源未识别）"
            prev = entry.get("last_status") or ""
            entry["last_status"] = "closed"
            entry["status_changed_at"] = now_iso
            cache[str(n)] = entry
            dynamics.append(f"#{n} 已关闭（{entry['resolved_by']}）")
            if prev == "open" and entry.get("difficulty") in ("简单", "中等"):
                alerts.append(
                    {
                        "kind": "issue-closed",
                        "title": f"issue#{n} 机会已了结：{entry['resolved_by']}",
                        "body": f"{entry.get('title', '')} —— {entry.get('summary', '')}",
                    }
                )

    # ---- 2. 新 issue 难度分析（缓存命中不花钱）----
    todo = [it for it in all_issues if str(it["number"]) not in cache]
    for i in range(0, len(todo), RADAR_BATCH):
        batch = todo[i : i + RADAR_BATCH]
        analyses = await _analyze_batch(svc, repo, batch)
        for n, a in analyses.items():
            cache[str(n)] = a

    # ---- 3. 口认领检测：机会池 issue 的新增评论 ----
    pr_claims: dict[int, list[int]] = {}
    pr_info = {p["number"]: p for p in open_prs}
    for p in open_prs:
        for n in GithubClient.parse_issue_refs(p):
            pr_claims.setdefault(n, []).append(p["number"])
    claim_materials: list[tuple[dict, list[dict]]] = []
    for it in all_issues:
        n = it["number"]
        if it.get("assignee") or n in pr_claims:
            continue  # 已被强信号占据，无需查口认领
        entry = cache.get(str(n)) or {}
        if entry.get("difficulty") not in DIFFICULTIES:
            continue
        if (
            state.get("last_sweep")
            and it.get("comments", 0) > int(entry.get("comments_seen") or 0)
        ):
            comments = await svc.gh.issue_comments(repo, n, since=state["last_sweep"])
            if comments:
                claim_materials.append((it, comments))
    claims = await _detect_claims(svc, repo, claim_materials)

    # ---- 4. 状态判定 + 迁移警报 + 池内动态 ----
    for it in all_issues:
        n = it["number"]
        key = str(n)
        entry = cache.setdefault(key, {})
        entry.setdefault("title", it["title"])
        entry.setdefault("created_at", it.get("created_at") or "")  # Issue 专区排序用
        difficulty = entry.get("difficulty", "未分析")

        if it.get("assignee"):
            status = "taken-assignee"
            entry["assignee"] = it["assignee"]
        elif n in pr_claims:
            status = "taken-pr"
            entry["pr_claims"] = pr_claims[n]
            # 判定依据落库：PR 链接/标题/作者/开立时间（radar.yml → API → 前端展示）
            entry["pr_evidence"] = [
                {
                    "number": num,
                    "url": (pr_info.get(num) or {}).get("url")
                    or f"https://github.com/{repo}/pull/{num}",
                    "title": (pr_info.get(num) or {}).get("title") or "",
                    "author": (pr_info.get(num) or {}).get("author") or "",
                    "created_at": (pr_info.get(num) or {}).get("created_at") or "",
                }
                for num in pr_claims[n]
                if pr_info.get(num)
            ]
        else:
            status = "open"
            # 被占解除：清除对应证据，避免前端展示过期依据
            entry.pop("assignee", None)
            entry.pop("pr_claims", None)
            entry.pop("pr_evidence", None)
            claimed_by = entry.get("claimed_by")
            if claimed_by:
                if _age_days(entry.get("claimed_at"), now) > CLAIM_TTL_DAYS:
                    entry.pop("claimed_by", None)
                    entry.pop("claimed_at", None)
                    entry.pop("claim_url", None)
                    dynamics.append(f"#{n} 口认领过期（{claimed_by} 未跟进），重回机会池")
                else:
                    status = "taken-claim"
            if status == "open" and n in claims:
                entry["claimed_by"] = claims[n]["by"]
                entry["claim_url"] = claims[n]["url"]
                entry["claimed_at"] = now_iso
                status = "taken-claim"

        # v4.2：开放且难度合格的 issue 逐条判未入榜原因（空串 = 入榜）
        entry["excluded"] = (
            _exclusion_reason(it, opp_cfg, sig, now)
            if status == "open" and difficulty in opp_cfg.difficulties
            else ""
        )

        prev = entry.get("last_status") or ""
        if status != prev:
            entry["last_status"] = status
            entry["status_changed_at"] = now_iso
            if status == "open" and not entry["excluded"] and difficulty in opp_cfg.difficulties:
                if prev:  # 回流：被占/口认领解除后重回机会池
                    alerts.append(
                        {
                            "kind": "opportunity",
                            "title": f"issue#{it['number']} [{difficulty}] 机会回流（{prev} 解除）",
                            "body": f"{entry.get('title', '')} —— {entry.get('summary', '')}",
                        }
                    )
                    dynamics.append(f"#{it['number']} 重回机会池")
                else:  # 新 issue 且可做 → 参与机会
                    alerts.append(
                        {
                            "kind": "opportunity",
                            "title": f"issue#{it['number']} [{difficulty}] {entry.get('summary', '')[:40]}",
                            "body": entry.get("plan") or "无人认领、无 PR 声称解决",
                        }
                    )
            elif prev == "open" and difficulty in opp_cfg.difficulties:
                if status == "taken-assignee":
                    who = f"assignee {entry.get('assignee')}"
                elif status == "taken-pr":
                    ev = (entry.get("pr_evidence") or [{}])[0]
                    who = f"PR#{(entry.get('pr_claims') or ['?'])[0]}（{ev.get('author') or '?'}）"
                else:
                    who = f"{entry.get('claimed_by', '')}（口认领）"
                alerts.append(
                    {
                        "kind": "issue-taken",
                        "title": f"issue#{it['number']} 已被 {who} 占据",
                        "body": "上期机会失效，可停止关注",
                    }
                )

        # 池内动态：评论激增 / help 标签升温
        old_labels = {lb.strip().lower() for lb in entry.get("labels_seen") or []}
        new_labels = {lb.strip().lower() for lb in it.get("labels") or []}
        boosted = sorted((new_labels - old_labels) & HELP_LABELS)
        if it.get("comments", 0) - int(entry.get("comments_seen") or 0) >= COMMENT_SURGE:
            dynamics.append(f"#{n} 评论 +{it['comments'] - int(entry.get('comments_seen') or 0)}，竞争升温")
        if boosted and status == "open" and not entry["excluded"] and difficulty in opp_cfg.difficulties and prev:
            dynamics.append(f"#{n} 挂上 {'、'.join(boosted)} 标签，机会升温")
            alerts.append(
                {
                    "kind": "opportunity",
                    "title": f"issue#{n} 挂上 {boosted[0]} 标签，机会升温",
                    "body": f"{entry.get('title', '')} —— {entry.get('summary', '')}",
                }
            )
        entry["labels_seen"] = sorted(it.get("labels") or [])
        entry["comments_seen"] = it.get("comments", 0)

    # 关闭超过 30 天的条目出缓存（保持 radar.yml 精简）
    for k in list(cache):
        if cache[k].get("last_status") == "closed" and _age_days(cache[k].get("closed_at"), now) > 30:
            del cache[k]

    state["last_sweep"] = now_iso

    # ---- 5. 报告：机会榜 + 池内动态 + 被占 + 近期关闭 + 折叠总表 ----
    opp, taken, hard = [], [], []
    for it in all_issues:
        entry = cache.get(str(it["number"])) or {}
        status = entry.get("last_status") or "open"
        difficulty = entry.get("difficulty", "未分析")
        row = (it, entry)
        if status == "open" and difficulty in opp_cfg.difficulties and not entry.get("excluded"):
            opp.append(row)
        elif status == "open":
            hard.append(row)  # 困难 / 未分析 / 未达机会硬条件
        elif status.startswith("taken"):
            taken.append(row)
    closed_recent = sorted(
        (
            (int(n), e)
            for n, e in cache.items()
            if e.get("last_status") == "closed"
        ),
        key=lambda kv: kv[1].get("closed_at") or "",
        reverse=True,
    )[:10]

    lines = [
        f"# Issue 雷达 · {repo}",
        "",
        f"> 全景扫描：{date} · 开放 issue {len(all_issues)} 个"
        f" · 机会 {len(opp)} · 未入榜 {len(hard)} · 被占 {len(taken)}",
        "",
    ]
    if opp:
        ranked = sorted(
            ((it, entry) + _worth(it, entry, now, sig) for it, entry in opp),
            key=lambda r: -r[2],
        )
        lines.append(f"## 机会榜（按值得做排序，top {min(OPPORTUNITY_TOP, len(ranked))}）")
        lines.append("")
        lines.append("| # | 难度 | 信号 | 标题 → 一句话 |")
        lines.append("| --- | --- | --- | --- |")
        for it, entry, score, sig in ranked[:OPPORTUNITY_TOP]:
            lines.append(
                f"| [#{it['number']}]({it.get('url') or f'https://github.com/{repo}/issues/' + str(it['number'])}) "
                f"| {entry.get('difficulty')} | {sig} "
                f"| {it['title'][:60]} —— {entry.get('summary', '')} |"
            )
        lines.append("")
    if hard:
        lines.append(f"## 未入榜（{len(hard)}）")
        for it, entry in sorted(hard, key=lambda r: -r[0]["number"]):
            reason = entry.get("excluded") or f"难度{entry.get('difficulty', '未分析')}"
            lines.append(f"- #{it['number']} {it['title'][:60]} —— {reason}")
        lines.append("")
    if dynamics:
        lines.append("## 池内动态")
        lines.extend(f"- {d}" for d in dynamics)
        lines.append("")
    if taken:
        lines.append("## 已被占（不必再看）")
        for it, entry in sorted(taken, key=lambda r: -r[0]["number"]):
            status = entry.get("last_status", "")
            if status == "taken-assignee":
                who = f"assignee {entry.get('assignee') or it.get('assignee')}"
            elif status == "taken-pr":
                ev = (entry.get("pr_evidence") or [{}])[0]
                who = f"PR#{(entry.get('pr_claims') or ['?'])[0]}（{ev.get('author') or '?'}）"
            else:
                who = f"{entry.get('claimed_by', '?')}（口认领）"
            lines.append(f"- #{it['number']} {it['title']} —— 被 {who} 占")
        lines.append("")
    if closed_recent:
        lines.append("## 近期关闭")
        for n, e in closed_recent:
            lines.append(
                f"- #{n} {e.get('title', '')} —— {e.get('resolved_by', '已关闭')}"
                f"（{(e.get('closed_at') or '')[:10]}）"
            )
        lines.append("")
    lines.append("## 分析详情（最新分析在前）")
    analyzed = sorted(
        (it for it in all_issues if str(it["number"]) in cache),
        key=lambda it: -it["number"],
    )
    for it in analyzed[:20]:
        entry = cache[str(it["number"])]
        sl = STATUS_LABEL.get(entry.get("last_status", ""), "?")
        lines.append(
            f"### #{it['number']} [{entry.get('difficulty')}|{sl}] {it['title']}\n"
            f"- {entry.get('summary', '')}\n"
            f"- 问题：{entry.get('problem', '—')}\n"
            f"- 方案：{entry.get('plan', '—')}\n"
            f"- 分析于 {entry.get('analyzed_at', '?')}\n"
        )
    lines.append("## 全量总表")
    lines.append("")
    lines.append("<details><summary>展开全部开放 issue</summary>")
    lines.append("")
    lines.append("| # | 难度 | 状态 | 一句话 |")
    lines.append("| --- | --- | --- | --- |")
    for it in all_issues:
        entry = cache.get(str(it["number"])) or {}
        status = entry.get("last_status") or "open"
        if status == "open" and entry.get("difficulty") == "困难":
            sl = "🟡困难"
        else:
            sl = STATUS_LABEL.get(status, "❓")
        lines.append(
            f"| #{it['number']} | {entry.get('difficulty', '未分析')} | {sl}"
            f" | {entry.get('summary', it['title'][:30])} |"
        )
    lines.append("")
    lines.append("</details>")
    lines.append("")

    new_opp = sum(
        1
        for a in alerts
        if a["kind"] == "opportunity" and "回流" not in a["title"] and "升温" not in a["title"]
    )
    summary = (
        f"雷达：机会 {len(opp)}（新 {new_opp}）· 未入榜 {len(hard)} · 被占 {len(taken)}"
        + (f" · 池内动态 {len(dynamics)}" if dynamics else "")
    )
    return RecipeResult(
        files={
            "issues.md": "\n".join(lines).strip() + "\n",
            "radar.yml": _dump_radar_state(state),
        },
        alerts=alerts,
        summary=summary,
    )


# ---------- release-brief（release 简报） ----------


async def run_release_brief(svc, repo: str, release: dict, recent: str = "") -> RecipeResult:
    tag = release.get("tag", "unknown")
    return await _run_llm_recipe(
        svc,
        "release-brief",
        {
            "REPO": repo,
            "TAG": tag,
            "RELEASE_NAME": release.get("name", ""),
            "PUBLISHED_AT": release.get("published_at", "")[:10],
            "BODY": release.get("body") or "（无 release notes）",
            "RECENT": recent or "（未提供）",
            "DATE": svc.settings.local_date_str(),
        },
        required_files=[f"releases/{tag}.md"],
    )


# ---------- cve-scan（OSV 依赖漏洞，确定性产出） ----------


async def run_cve_scan(svc, repo: str, slug: str, changed_files: list[str]) -> RecipeResult:
    """依赖清单变化（或首次建档）时扫描 OSV；security.md 由代码直接生成，不耗 LLM。"""
    # 找仓库里的依赖清单：优先本次 diff 涉及的，否则从 API 拉根目录常见文件
    manifests: dict[str, str] = {}
    for f in changed_files:
        base = f.rsplit("/", 1)[-1]
        if base in DEP_FILES:
            content = await svc.gh.file_content(repo, f)
            if content:
                manifests[f] = content
    if not manifests:
        for path in DEP_FILES:
            content = await svc.gh.file_content(repo, path)
            if content:
                manifests[path] = content
    if not manifests:
        return RecipeResult()  # 无依赖清单，跳过

    deps: list[tuple[str, str, str]] = []
    for path, content in manifests.items():
        deps.extend(parse_deps(path, content))
    deps = deps[:60]  # OSV 并发预算
    if not deps:
        return RecipeResult()

    osv = OsvClient()
    try:
        vulns = await osv.query_many(deps)
    finally:
        await osv.aclose()

    current = svc.vault.read_file(f"{slug}/security.md") or ""
    lines = [
        "# 依赖安全（OSV 扫描）",
        "",
        f"- 扫描时间：{svc.settings.local_date_str()} · 依赖 {len(deps)} 个 · 清单：{', '.join(sorted(manifests))}",
        "",
    ]
    new_alerts: list[dict] = []
    if not vulns:
        lines.append("当前无已知漏洞。")
    else:
        lines.append(f"共 {sum(len(v) for v in vulns.values())} 条已知漏洞：\n")
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
        for (eco, name), vs in sorted(
            vulns.items(), key=lambda kv: min(order.get(v["severity"], 9) for v in kv[1])
        ):
            lines.append(f"## {name}（{eco}）")
            for v in vs:
                fixed = f" → 修复版本 {'、'.join(v['fixed'])}" if v["fixed"] else ""
                lines.append(
                    f"- **[{v['severity']}]** [{v['id']}]({v['url']}) {v['summary']}{fixed}"
                )
                if v["id"] not in current:  # 只对新出现的漏洞发警报
                    new_alerts.append(
                        {
                            "kind": "cve",
                            "title": f"{name} {v['id']}（{v['severity']}）",
                            "body": v["summary"],
                        }
                    )
            lines.append("")
    return RecipeResult(
        files={"security.md": "\n".join(lines).strip() + "\n"},
        alerts=new_alerts,
        summary=f"OSV 扫描：{len(deps)} 个依赖，{sum(len(v) for v in vulns.values())} 条已知漏洞"
        + (f"，{len(new_alerts)} 条新发现" if new_alerts else ""),
    )


def deps_touched(changed_files: list[str]) -> bool:
    return any(f.rsplit("/", 1)[-1] in DEP_FILES for f in changed_files)
