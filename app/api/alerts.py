"""警报台账。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlmodel import Session, func, select

from app.models import Alert

router = APIRouter()


@router.get("")
async def list_alerts(
    request: Request,
    repo: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = 50,
    limit: int | None = None,  # 兼容旧调用（Dashboard 最近警报）：给 limit 时截断不分页
):
    svc = request.app.state.svc
    with Session(svc.engine) as session:
        stmt = select(Alert).order_by(Alert.id.desc())  # type: ignore[union-attr]
        if repo:
            stmt = stmt.where(Alert.repo == repo)
        if kind:
            stmt = stmt.where(Alert.kind == kind)
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        if limit is not None:
            alerts = session.exec(stmt.limit(min(limit, 500))).all()  # type: ignore[union-attr]
            pg, pg_size = 1, limit
        else:
            pg = max(1, page)
            pg_size = max(1, min(page_size, 200))
            alerts = session.exec(
                stmt.offset((pg - 1) * pg_size).limit(pg_size)
            ).all()
        return {
            "alerts": [
                {
                    "id": a.id,
                    "ts": a.ts.isoformat() if a.ts else None,
                    "repo": a.repo,
                    "kind": a.kind,
                    "title": a.title,
                    "body": a.body,
                    "pushed": a.pushed,
                }
                for a in alerts
            ],
            "total": total,
            "page": pg,
            "page_size": pg_size,
        }
