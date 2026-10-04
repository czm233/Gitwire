"""Watch v4：定时扫描。触发源：sha / release / 雷达（新 issue、新 PR、机会池动态、首挂载）/
追踪 issue 动态。雷达仓库的 issue 检测是全量快照对比（与 sweep 共用 scan_radar_pool，
不调 LLM）；不挂雷达的仓库不做 issue 情报。"""

from __future__ import annotations

from sqlmodel import Session, select

from app.engine.recipes import scan_radar_pool, watchlist_changed
from app.engine.runner import sync_repo
from app.github import GithubError
from app.models import Run
from app.vault import slugify


async def watch_round(svc) -> list[str]:
    """一轮扫描：任一触发源前进的仓库入队。返回本轮入队的 repo 列表。"""
    from app.services import enqueue

    await svc.vault.push_pending()  # 先补上一轮没推上去的
    await svc.vault.pull()

    queued: list[str] = []
    cfg = svc.vault.read_config()
    for target in cfg.repos:
        repo = target.name
        slug = slugify(repo)
        with Session(svc.engine) as session:
            running = session.exec(
                select(Run).where(Run.repo == repo, Run.status == "running")
            ).first()
        if running:
            continue
        meta = svc.vault.meta(slug)
        try:
            head = await svc.gh.head_sha(repo)
            sha_moved = head != (meta.last_synced if meta else None)
            release = await svc.gh.latest_release(repo)
            release_moved = bool(
                release and meta and release.get("tag") != meta.last_release_tag
            )
            # v4：issue 情报统一归雷达——新 issue / 新开放 PR / 机会池动态 / 首挂载全量盘点
            radar_moved = False
            if meta and "issue-radar" in cfg.recipes_for(repo):
                if meta.last_pr_number is None:
                    radar_moved = True
                elif meta.last_synced:
                    scan = await scan_radar_pool(svc, repo, slug)
                    radar_moved = bool(scan.dynamics) or any(
                        i["number"] > (meta.last_issue_number or 0) for i in scan.issues
                    ) or any(
                        p["number"] > (meta.last_pr_number or 0) for p in scan.pulls
                    )
            # v4：追踪 issue 动态
            watch_numbers = cfg.watch_issues_for(repo)
            watch_moved = bool(
                watch_numbers
                and meta
                and meta.last_synced
                and await watchlist_changed(svc, repo, slug, watch_numbers)
            )
        except GithubError as e:
            print(f"[watch] {repo} 查询失败: {e}")
            continue
        if sha_moved or release_moved or radar_moved or watch_moved:
            enqueue(svc, repo, "scheduled")
            queued.append(repo)
    return queued


async def run_queued_now(svc) -> None:
    """CLI 场景：把队列里现有的任务就地跑完。"""
    from app.services import enqueue

    enqueue(svc, "__drain__", "manual")
    if svc.queue is None:
        return
    while True:
        repo, trigger = await svc.queue.get()
        try:
            if repo == "__drain__":
                break
            await sync_repo(svc, repo, trigger)
        finally:
            svc.queue.task_done()
