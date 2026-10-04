"""单次 run 编排 v4：多触发源（sha / release / 雷达 / 追踪）→ 配方链 → 发布 → 警报。

- 触发源：游标前进（含路径过滤）、新 release、雷达（新 issue / 新 PR / 机会池动态 /
  首挂载全量盘点）、追踪 issue 动态——任一命中即入 run
- 配方链：docs-sync 打底（可关），feature-tripwire / bug-watch / cve-scan 按配置挂载，
  release 触发源自动对应 release-brief，issue 情报统一归 issue-radar v2（issue-watch 已退役）
- 发布：direct 直推 main；PR 模式走分支 + Pull Request
- 警报：tripwire 翻转 / 破坏性变更 / 新 release / 新 CVE / 机会进出与关闭 / 追踪动态 /
  运行失败 → 台账 + Bark
"""

from __future__ import annotations

from sqlmodel import Session

from app.engine.analyze import AnalyzeError, analyze_repo, cleanup_repo_cache
from app.engine.publish import publish
from app.engine.recipes import (
    RecipeResult,
    deps_touched,
    run_bug_watch,
    run_cve_scan,
    run_feature_tripwire,
    run_issue_radar,
    run_release_brief,
    run_watchlist,
    scan_radar_pool,
)
from app.models import Alert, Run, RunLog, utcnow
from app.vault import ProjectMeta, slugify


async def record_alert(svc, repo: str, kind: str, title: str, body: str = "") -> None:
    """警报入库 + Bark 推送（容错，不影响主流程）。"""
    pushed = False
    if svc.bark and svc.bark.enabled:
        hot = ("tripwire", "breaking", "cve", "opportunity")
        level = "timeSensitive" if kind in hot else "active"
        pushed = await svc.bark.send(f"[{kind}] {title}", body, level=level)
    with Session(svc.engine) as session:
        session.add(Alert(repo=repo, kind=kind, title=title, body=body, pushed=pushed))
        session.commit()


async def sync_repo(svc, repo: str, trigger: str = "manual") -> Run:
    with Session(svc.engine) as session:
        run = Run(repo=repo, trigger=trigger, status="running")
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id

    def log(message: str, level: str = "info") -> None:
        print(f"[{repo}] {message}")
        with Session(svc.engine) as session:
            session.add(RunLog(run_id=run_id, level=level, message=message))
            session.commit()

    try:
        await _sync(svc, repo, run_id, log, trigger)
        with Session(svc.engine) as session:
            db_run = session.get(Run, run_id)
            db_run.finished_at = utcnow()
            session.add(db_run)
            session.commit()
            session.refresh(db_run)
            return db_run
    except Exception as e:  # noqa: BLE001
        message = f'公共配方执行失败（{type(e).__name__}）' if svc.settings.public_source_only else str(e)
        log(f"运行失败: {message}", "error")
        await record_alert(svc, repo, "error", f"同步失败：{message}"[:120])
        with Session(svc.engine) as session:
            db_run = session.get(Run, run_id)
            db_run.status = "failed"
            db_run.error = message[:2000]
            db_run.finished_at = utcnow()
            session.add(db_run)
            session.commit()
            session.refresh(db_run)
            return db_run


async def _sync(svc, repo: str, run_id: int, log, trigger: str = "manual") -> None:
    await svc.vault.pull()
    cfg = svc.vault.read_config()
    slug = slugify(repo)
    meta = svc.vault.meta(slug) or ProjectMeta(repo=repo)
    pr_mode = cfg.publish_mode == "pr" and bool(svc.vault.remote)
    recipes = cfg.recipes_for(repo)

    # PR 模式：已有待处理 PR 的仓库本轮跳过（PR 是游标，合入即续跑）
    if pr_mode:
        vault_slug = svc.vault.remote_repo_slug()
        open_prs = await svc.gh.list_pulls(vault_slug, "open", head_prefix=f"gitwire/{slug}")
        if open_prs:
            log(f"PR #{open_prs[0]['number']} 待人工处理，本轮跳过")
            await _finish(svc, run_id, "pr-pending", summary=open_prs[0]["url"])
            return

    # ---- 配方产出累积（追踪可能在触发源检测前就有产出）----
    files: dict[str, str] = {}
    alerts: list[dict] = []
    summaries: list[str] = []
    meta_updates: dict = {}

    # ---- 追踪 issue（零 LLM，先跑：动态即触发源之一）----
    watch_numbers = cfg.watch_issues_for(repo)
    watch_r: RecipeResult | None = None
    if watch_numbers:
        watch_r = await run_watchlist(svc, repo, slug, watch_numbers)
        if watch_r.files or watch_r.alerts:
            log(f"issue 追踪：{watch_r.summary}")
            _merge(files, alerts, summaries, watch_r)

    # ---- 触发源检测 ----
    head = await svc.gh.head_sha(repo)
    first = meta.last_synced is None or not svc.vault.has_project(slug)
    sha_advanced = head != meta.last_synced

    release = await svc.gh.latest_release(repo)
    new_release = bool(release and release.get("tag") != meta.last_release_tag)

    # issue 情报统一归雷达 v2：全量快照 + 机会池动态检测
    has_radar = "issue-radar" in recipes
    radar_first = has_radar and meta.last_pr_number is None  # 首挂载：先做一次全量盘点
    scan = await scan_radar_pool(svc, repo, slug) if has_radar else None
    new_issues: list[dict] = []
    new_prs: list[dict] = []
    if scan is not None and radar_first:
        new_issues = scan.issues  # 首挂载全量盘点（含建档时）
    elif scan is not None and not first and meta.last_synced:
        new_issues = [i for i in scan.issues if i["number"] > (meta.last_issue_number or 0)]
        new_prs = [p for p in scan.pulls if p["number"] > (meta.last_pr_number or 0)]
    pool_moved = bool(scan and scan.dynamics)
    watch_moved = bool(watch_r and (watch_r.files or watch_r.alerts))
    # 手动同步 = 用户要刷新报告：已盘点过的雷达仓库强制重扫（零 LLM，难度缓存命中）
    manual_radar = trigger == "manual" and has_radar and scan is not None and bool(meta.last_synced)

    if not (
        sha_advanced or new_release or new_issues or new_prs
        or radar_first or pool_moved or watch_moved or manual_radar
    ):
        log(f"游标一致（{head[:7]}），无新提交/release/issue 动态")
        await _finish(svc, run_id, "noop", new_sha=head, old_sha=meta.last_synced)
        return

    # ---- 配方链 ----
    mode = "incremental"
    changed_files: list[str] = []

    if sha_advanced:
        log(f"游标前进：{(meta.last_synced or '∅')[:7]} → {head[:7]}")
        if not first:
            compare = await svc.gh.compare(repo, meta.last_synced, head)  # type: ignore[arg-type]
            changed_files = [f["filename"] for f in compare["files"]]
        # docs-sync 打底
        if "docs-sync" in recipes:
            result = await analyze_repo(svc, repo, meta.last_synced, head)
            files.update(result.files)
            summaries.append(result.summary)
            mode = result.mode
            log(f"docs-sync 完成（{result.mode}）：{len(result.files)} 个文件")
        else:
            mode = "incremental"
        # 挂载配方（增量时有 diff 语义）
        if mode == "incremental" and meta.last_synced:
            for name, fn in (
                ("feature-tripwire", run_feature_tripwire),
                ("bug-watch", run_bug_watch),
            ):
                if name in recipes:
                    r: RecipeResult = await fn(svc, repo, meta.last_synced, head, slug)
                    _merge(files, alerts, summaries, r)
                    log(f"{name} 完成：{len(r.files)} 文件，{len(r.alerts)} 警报")
        if "cve-scan" in recipes and (first or deps_touched(changed_files)):
            r = await run_cve_scan(svc, repo, slug, changed_files)
            if r.files:
                _merge(files, alerts, summaries, r)
                log(f"cve-scan 完成：{r.summary}")

    if new_release and release:
        log(f"新 release：{release['tag']}")
        r = await run_release_brief(svc, repo, release)
        _merge(files, alerts, summaries, r)
        meta_updates["last_release_tag"] = release["tag"]

    if has_radar and scan is not None and (new_issues or new_prs or pool_moved or radar_first or manual_radar):
        what = []
        if new_issues:
            what.append(f"{len(new_issues)} 个新 issue")
        if new_prs:
            what.append(f"{len(new_prs)} 个新 PR")
        if pool_moved:
            what.append(f"{len(scan.dynamics)} 项池内动态")
        if radar_first and not what:
            what.append("首次全量盘点")
        if manual_radar and not what:
            what.append("手动刷新")
        log(f"issue 雷达：{'、'.join(what)}")
        r = await run_issue_radar(svc, repo, slug, scan.issues, scan.pulls)
        _merge(files, alerts, summaries, r)
        if scan.issues:
            mx = max(i["number"] for i in scan.issues)
            if mx > (meta.last_issue_number or 0):  # 游标只前进（最新 issue 可能已关闭）
                meta_updates["last_issue_number"] = mx
        if scan.pulls:
            meta_updates["last_pr_number"] = max(p["number"] for p in scan.pulls)
        elif radar_first:
            meta_updates["last_pr_number"] = 0  # 首扫基线（0 = 已盘点过）

    if not files:
        # 触发源只有 issue 游标基线之类：只推游标
        log("无文件产出，仅更新游标")
        outcome = await publish(svc, repo, {}, head if sha_advanced else meta.last_synced or "", "incremental", meta_updates, pr_mode=False)
        _apply_outcome(svc, run_id, outcome, mode=mode, new_sha=head, summary="；".join(summaries))
        return

    outcome = await publish(
        svc,
        repo,
        files,
        head if sha_advanced else (meta.last_synced or head),
        mode,
        meta_updates,
        pr_mode=pr_mode,
        summary=summaries[0] if summaries else "",
    )
    msg = f"已提交 {outcome.commit_sha[:7]}" if outcome.commit_sha else "已发布"
    if outcome.pr_url:
        msg += f"，PR：{outcome.pr_url}"
    if not outcome.pushed:
        msg += f"；push 失败待补推：{outcome.push_err}"
    log(msg, "info" if outcome.pushed else "warn")

    _apply_outcome(
        svc, run_id, outcome, mode=mode, new_sha=head, summary="；".join(summaries)[:500]
    )

    # 警报统一出账（发布成功才发，避免重复轰炸）
    for a in alerts:
        await record_alert(svc, repo, a["kind"], a["title"], a.get("body", ""))

    # 建档完成即清理仓库 clone 缓存（增量走 diff API，不再需要本地代码）
    if mode == "init":
        cleanup_repo_cache(svc.settings, slug)
        log("已清理仓库 clone 缓存")


def _merge(files: dict, alerts: list, summaries: list, r: RecipeResult) -> None:
    files.update(r.files)
    alerts.extend(r.alerts)
    if r.summary:
        summaries.append(r.summary)


def _apply_outcome(
    svc, run_id: int, outcome, mode: str = "", new_sha: str | None = None, summary: str = ""
) -> None:
    with Session(svc.engine) as session:
        db_run = session.get(Run, run_id)
        db_run.status = "published"
        db_run.mode = mode or db_run.mode
        if new_sha:
            db_run.new_sha = new_sha
        if outcome.commit_sha:
            db_run.commit_sha = outcome.commit_sha
        db_run.pushed = outcome.pushed
        db_run.pr_number = outcome.pr_number
        db_run.pr_url = outcome.pr_url
        if summary:
            db_run.summary = summary
        session.add(db_run)
        session.commit()


async def _finish(
    svc, run_id: int, mode: str, new_sha: str | None = None,
    old_sha: str | None = None, summary: str = "", meta_updates: dict | None = None,
) -> None:
    """noop / pr-pending 收尾（必要时只推游标字段）。"""
    if meta_updates:
        repo_run = None
        with Session(svc.engine) as session:
            repo_run = session.get(Run, run_id)
        if repo_run:
            meta_updates_vault(svc, repo_run.repo, meta_updates)
    with Session(svc.engine) as session:
        db_run = session.get(Run, run_id)
        db_run.mode = mode
        db_run.status = "published"
        db_run.new_sha = new_sha
        db_run.old_sha = old_sha
        if summary:
            db_run.summary = summary
        session.add(db_run)
        session.commit()


def meta_updates_vault(svc, repo: str, updates: dict) -> None:
    """不产生文件时也要推进的游标（如 issue 基线）。"""
    from app.vault import slugify

    slug = slugify(repo)
    meta = svc.vault.meta(slug)
    if not meta:
        return
    if "last_issue_number" in updates:
        meta.last_issue_number = updates["last_issue_number"]
    svc.vault.save_meta(slug, meta)
