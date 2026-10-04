"""Issue 专区：聚合各项目 radar.yml 成跨项目机会榜（工作台视图）。

数据来自 vault（radar.yml 缓存）与本地配置（gitwire.yml 追踪清单），零额外 GitHub API。
筛选：仓库 / 状态 / 难度 / 关键词；分页在后端做。
"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.vault import slugify  # noqa: F401（各处 slug 命名保持一致）

router = APIRouter()

# 状态分组：opportunity=开放且难度≤中等；hard=开放但困难；taken=被占；closed=关闭
GROUP_FOR_STATUS = {
    "open": "hard",  # 开放但难度未到机会线的在 hard 组细分处理，见下方 difficulty 判定
    "taken-pr": "taken",
    "taken-assignee": "taken",
    "taken-claim": "taken",
    "closed": "closed",
}
DIFFICULTY_ORDER = {"简单": 0, "中等": 1, "困难": 2}
GROUP_ORDER = {"opportunity": 0, "hard": 1, "taken": 2, "closed": 3}


def _group(entry: dict) -> str:
    status = entry.get("last_status") or "open"
    if status == "closed":
        return "closed"
    if status != "open":
        return "taken"
    return "hard" if entry.get("excluded") or entry.get("difficulty") == "困难" else "opportunity"


def _collect(svc) -> list[dict]:
    cfg = svc.vault.read_config()
    watch_map = {t.name: t.watch_issues for t in cfg.repos}
    items: list[dict] = []
    for slug in svc.vault.projects():
        meta = svc.vault.meta(slug)
        repo = meta.repo if meta else slug
        raw = svc.vault.read_file(f"{slug}/radar.yml")
        if not raw:
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except Exception:  # noqa: BLE001
            continue
        watched = watch_map.get(repo) or []
        for n, e in (data.get("issues") or {}).items():
            if not isinstance(e, dict) or not e.get("difficulty"):
                continue
            try:
                number = int(n)
            except (TypeError, ValueError):
                continue
            group = _group(e)
            items.append(
                {
                    "repo": repo,
                    "slug": slug,
                    "number": number,
                    "title": e.get("title") or f"#{number}",
                    "url": f"https://github.com/{repo}/issues/{number}",
                    "difficulty": e.get("difficulty", "未分析"),
                    "excluded": e.get("excluded") or "",
                    "status": e.get("last_status") or "open",
                    "group": group,
                    "summary": e.get("summary", ""),
                    "problem": e.get("problem", ""),
                    "plan": e.get("plan", ""),
                    "labels": e.get("labels_seen") or [],
                    "comments": e.get("comments_seen") or 0,
                    "created_at": e.get("created_at") or "",
                    "claimed_by": e.get("claimed_by"),
                    "claimed_at": e.get("claimed_at"),
                    "claim_url": e.get("claim_url"),
                    "assignee": e.get("assignee"),
                    "pr_claims": e.get("pr_claims") or [],
                    "pr_evidence": e.get("pr_evidence") or [],
                    "resolved_by": e.get("resolved_by"),
                    "closed_at": e.get("closed_at") or "",
                    "analyzed_at": e.get("analyzed_at") or "",
                    "watched": number in watched,
                }
            )
    return items


class WatchToggleIn(BaseModel):
    repo: str
    number: int


@router.post("/watch")
async def toggle_watch(body: WatchToggleIn, request: Request):
    """Issue 专区快捷操作：把 issue 加入/移出所在仓库的追踪清单。

    只写本地 gitwire.yml（data 目录，毫秒级生效）——配置不入情报仓库，
    不产生任何 git 提交。新增追踪的基线由 watch_moved 自然触发（首查建基线，不出警报）。"""
    svc = request.app.state.svc
    cfg = svc.vault.read_config()
    target = cfg.target(body.repo)
    if not target:
        raise HTTPException(404, f"不在监控清单：{body.repo}")
    current = list(target.watch_issues)
    if body.number in current:
        current = [n for n in current if n != body.number]
        watched = False
    else:
        current = sorted(current + [body.number])
        watched = True
    target.watch_issues = current
    svc.vault.write_config(cfg)
    return {
        "ok": True,
        "repo": target.name,
        "number": body.number,
        "watched": watched,
        "watch_issues": current,
    }


@router.get("")
async def list_issues(
    request: Request,
    repo: str | None = None,
    group: str = "opportunity",  # opportunity | hard | taken | closed | watched | all
    difficulty: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    svc = request.app.state.svc
    items = _collect(svc)

    stats = {"opportunity": 0, "hard": 0, "taken": 0, "closed": 0, "watched": 0}
    for it in items:
        stats[it["group"]] += 1
        if it["watched"]:
            stats["watched"] += 1

    repos = sorted({it["repo"] for it in items})
    q_lower = (q or "").lower()

    def match(it: dict) -> bool:
        if repo and it["repo"] != repo:
            return False
        if group == "watched":
            if not it["watched"]:
                return False
        elif group != "all" and it["group"] != group:
            return False
        if difficulty and it["difficulty"] != difficulty:
            return False
        if q_lower and q_lower not in f"{it['title']} {it['summary']}".lower():
            return False
        return True

    filtered = [it for it in items if match(it)]
    # Python sort 稳定：先按新旧倒序，再按 组→难度 升序覆盖，组内即保持 新→旧
    filtered.sort(key=lambda it: (it.get("created_at") or "", it["number"]), reverse=True)
    filtered.sort(
        key=lambda it: (GROUP_ORDER.get(it["group"], 9), DIFFICULTY_ORDER.get(it["difficulty"], 9))
    )

    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    total = len(filtered)
    pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": page_size,
        "repos": repos,
        "stats": stats,
        "items": filtered[start : start + page_size],
    }
