"""态势板数据。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlmodel import Session, select

from app.models import Alert, Run
from app.vault import slugify

router = APIRouter()


@router.get("")
async def board(request: Request):
    svc = request.app.state.svc
    cfg = svc.vault.read_config()
    ahead = await svc.vault.ahead_count()

    repos = []
    published = 0
    with Session(svc.engine) as session:
        for target in cfg.repos:
            repo = target.name
            slug = slugify(repo)
            meta = svc.vault.meta(slug)
            if meta and meta.last_synced:
                published += 1
            last = session.exec(
                select(Run).where(Run.repo == repo).order_by(Run.id.desc())  # type: ignore[union-attr]
            ).first()
            repos.append(
                {
                    "slug": slug,
                    "repo": repo,
                    "published": bool(meta and meta.last_synced),
                    "mode": meta.last_mode if meta else None,
                    "sha7": (meta.last_synced[:7] if meta and meta.last_synced else None),
                    "summary": meta.summary if meta else None,
                    "updated_at": meta.updated_at if meta else None,
                    "recipes": cfg.recipes_for(repo),
                    "last_run": (
                        {
                            "id": last.id,
                            "status": last.status,
                            "trigger": last.trigger,
                            "mode": last.mode,
                            "error": last.error,
                            "pr_url": last.pr_url,
                            "started_at": last.started_at.isoformat() if last.started_at else None,
                            "finished_at": last.finished_at.isoformat() if last.finished_at else None,
                        }
                        if last
                        else None
                    ),
                }
            )
        recent_alerts = session.exec(
            select(Alert).order_by(Alert.id.desc())  # type: ignore[union-attr]
        ).all()
    dailies = svc.vault.daily_list()
    return {
        "stats": {
            "targets": len(cfg.repos),
            "published": published,
            "pending_push": ahead,
            "now": svc.settings.local_now().isoformat(timespec="seconds"),
        },
        "repos": repos,
        "daily_latest": dailies[0] if dailies else None,
        "alerts": [_alert_dict(a) for a in recent_alerts[:8]],
    }


def _alert_dict(a: Alert) -> dict:
    return {
        "id": a.id,
        "ts": a.ts.isoformat() if a.ts else None,
        "repo": a.repo,
        "kind": a.kind,
        "title": a.title,
        "body": a.body,
        "pushed": a.pushed,
    }
