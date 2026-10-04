"""监控清单管理：网页增删项目 = 改本地 gitwire.yml（data 目录，不入情报仓库）。"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app.github import normalize_repo
from app.models import Run
from app.services import enqueue
from app.vault import GitwireConfig, RepoTarget, slugify

router = APIRouter()


class RepoIn(BaseModel):
    repo: str
    recipes: list[str] | None = None  # 可选配方覆盖
    watch_issues: list[int] | None = None  # v4：issue 追踪编号


class RepoUpdateIn(BaseModel):
    """监控项行内编辑：追踪清单与配方挂载（至少传一个字段）。"""

    watch_issues: list[int] | None = None
    recipes: list[str] | None = None


# 可挂载配方白名单（release-brief / issue 情报由触发源自动对应，不在此列）
MOUNTABLE_RECIPES = ("docs-sync", "feature-tripwire", "bug-watch", "cve-scan", "issue-radar")


def _config_rows(svc) -> list[dict]:
    cfg = svc.vault.read_config()
    rows = []
    with Session(svc.engine) as session:
        for target in cfg.repos:
            repo = target.name
            slug = slugify(repo)
            meta = svc.vault.meta(slug)
            last = session.exec(
                select(Run).where(Run.repo == repo).order_by(Run.id.desc())  # type: ignore[union-attr]
            ).first()
            first_pub = session.exec(
                select(Run)
                .where(Run.repo == repo, Run.status == "published")  # type: ignore[union-attr]
                .order_by(Run.id)  # type: ignore[union-attr]
            ).first()
            rows.append(
                {
                    "slug": slug,
                    "repo": repo,
                    "published": bool(meta and meta.last_synced),
                    "sha7": (meta.last_synced[:7] if meta and meta.last_synced else None),
                    "last_status": last.status if last else None,
                    "archived_at": first_pub.finished_at if first_pub else None,
                    "recipes": cfg.recipes_for(repo),
                    "watch_issues": cfg.watch_issues_for(repo),
                }
            )
    # 配置顺序 = 添加顺序（旧→新）；界面要求最新添加的在最上面
    rows.reverse()
    return rows


@router.get("")
async def list_repos(request: Request):
    svc = request.app.state.svc
    cfg = svc.vault.read_config()
    return {
        "repos": _config_rows(svc),
        "publish_mode": cfg.publish_mode,
        "unwatch_closed": cfg.unwatch_closed,
        "vault_remote": bool(svc.vault.remote),
    }


class PublishModeIn(BaseModel):
    publish_mode: str  # direct | pr


@router.post("/config/publish-mode")
async def set_publish_mode(body: PublishModeIn, request: Request):
    """报告入库方式：direct = 分析完自动推送保存；pr = 每次先开 PR 人工审核。"""
    svc = request.app.state.svc
    if body.publish_mode not in ("direct", "pr"):
        raise HTTPException(400, "publish_mode 只支持 direct / pr")
    if body.publish_mode == "pr" and not svc.vault.remote:
        raise HTTPException(400, "先审核模式需要配置 GitHub 远端情报仓库")
    cfg = svc.vault.read_config()
    if cfg.publish_mode == body.publish_mode:
        return {"ok": True, "publish_mode": cfg.publish_mode, "changed": False}
    cfg.publish_mode = body.publish_mode
    svc.vault.write_config(cfg)
    return {"ok": True, "publish_mode": body.publish_mode, "changed": True}


class UnwatchClosedIn(BaseModel):
    unwatch_closed: bool


def _prune_closed_watched(svc, cfg: GitwireConfig) -> list[str]:
    """按 radar.yml 缓存把已关闭的追踪 issue 移出清单（零 GitHub API）。
    只改 cfg 内存对象，写盘由调用方负责；radar 覆盖不到的留给 run_watchlist 下轮兜底。"""
    pruned: list[str] = []
    for target in cfg.repos:
        if not target.watch_issues:
            continue
        raw = svc.vault.read_file(f"{slugify(target.name)}/radar.yml")
        if not raw:
            continue
        try:
            entries = (yaml.safe_load(raw) or {}).get("issues") or {}
        except Exception:  # noqa: BLE001
            continue
        closed: set[int] = set()
        for n, e in entries.items():
            if not isinstance(e, dict) or e.get("last_status") != "closed":
                continue
            try:
                closed.add(int(n))
            except (TypeError, ValueError):
                continue
        gone = [n for n in target.watch_issues if n in closed]
        if gone:
            target.watch_issues = [n for n in target.watch_issues if n not in closed]
            pruned.append(f"{target.name} " + " ".join(f"#{n}" for n in gone))
    return pruned


@router.post("/config/unwatch-closed")
async def set_unwatch_closed(body: UnwatchClosedIn, request: Request):
    """追踪策略：issue 关闭后继续追踪（默认），还是自动撤销追踪。
    开启时立即按 radar 缓存清掉现存已关闭的追踪。"""
    svc = request.app.state.svc
    cfg = svc.vault.read_config()
    if cfg.unwatch_closed == body.unwatch_closed:
        return {"ok": True, "unwatch_closed": cfg.unwatch_closed, "changed": False, "pruned": []}
    cfg.unwatch_closed = body.unwatch_closed
    pruned = _prune_closed_watched(svc, cfg) if body.unwatch_closed else []
    svc.vault.write_config(cfg)
    return {"ok": True, "unwatch_closed": body.unwatch_closed, "changed": True, "pruned": pruned}


@router.post("")
async def add_repo(body: RepoIn, request: Request):
    svc = request.app.state.svc
    # 输入层宽松：owner/name、GitHub URL、git@ ssh 形态都收，规整成 owner/name
    repo = normalize_repo(body.repo)
    if not repo:
        raise HTTPException(
            400,
            f"无法识别仓库标识：{body.repo.strip()[:100]}"
            "（支持 owner/name 或完整 GitHub URL）",
        )
    if not await svc.gh.exists(repo):
        raise HTTPException(404, f"GitHub 上不存在或不可访问：{repo}")
    cfg = svc.vault.read_config()
    if any(t.name == repo for t in cfg.repos):
        raise HTTPException(409, f"已在监控清单：{repo}")
    cfg.repos.append(
        RepoTarget(
            name=repo,
            recipes=body.recipes,
            watch_issues=sorted({n for n in (body.watch_issues or []) if n > 0}),
        )
    )
    svc.vault.write_config(cfg)
    enqueue(svc, repo, "manual")  # 添加即排队首次建档
    return {"ok": True, "repo": repo}


@router.patch("/{slug}")
async def update_repo(slug: str, body: RepoUpdateIn, request: Request):
    """v4：监控项行内编辑——追踪清单与配方挂载；只写本地 gitwire.yml（data 目录），
    配置不入情报仓库，任何配置操作都不产生 git 提交。"""
    svc = request.app.state.svc
    if body.watch_issues is None and body.recipes is None:
        raise HTTPException(400, "至少提供 watch_issues 或 recipes 之一")
    cfg = svc.vault.read_config()
    target = next((t for t in cfg.repos if slugify(t.name) == slug), None)
    if not target:
        raise HTTPException(404, f"监控清单中没有 {slug}")
    had_radar = "issue-radar" in cfg.recipes_for(target.name)

    changed: list[str] = []
    if body.recipes is not None:
        recipes = list(dict.fromkeys(body.recipes))  # 去重保序
        unknown = [r for r in recipes if r not in MOUNTABLE_RECIPES]
        if unknown:
            raise HTTPException(
                400, f"未知配方：{unknown}（可选：{', '.join(MOUNTABLE_RECIPES)}）"
            )
        if recipes != cfg.recipes_for(target.name):
            target.recipes = recipes
            changed.append(f"配方 → {recipes or '空'}")
    if body.watch_issues is not None:
        numbers = sorted({int(n) for n in body.watch_issues if n > 0})
        if len(numbers) > 50:
            raise HTTPException(400, "追踪清单最多 50 个 issue")
        if numbers != target.watch_issues:
            target.watch_issues = numbers
            changed.append(f"追踪 → {numbers or '清空'}")

    result: dict = {"ok": True, "repo": target.name}
    if changed:
        svc.vault.write_config(cfg)  # 本地生效；提交随下一轮同步收口
        # 新挂雷达：立即入队做首次全量盘点（追踪基线由 watch_moved 自然触发）
        if not had_radar and "issue-radar" in cfg.recipes_for(target.name):
            enqueue(svc, target.name, "manual")
    if body.watch_issues is not None:
        result["watch_issues"] = target.watch_issues
    if body.recipes is not None:
        result["recipes"] = cfg.recipes_for(target.name)
    return result


@router.delete("/{slug}")
async def remove_repo(slug: str, request: Request):
    svc = request.app.state.svc
    cfg = svc.vault.read_config()
    target = next((t for t in cfg.repos if slugify(t.name) == slug), None)
    if not target:
        raise HTTPException(404, f"监控清单中没有 {slug}")
    cfg.repos = [t for t in cfg.repos if t.name != target.name]
    svc.vault.write_config(cfg)
    return {"ok": True, "removed": target.name}
