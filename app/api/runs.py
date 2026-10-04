"""运行历史、手动触发、SSE 实时日志。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.models import Run, RunLog
from app.services import enqueue


class SyncIn(BaseModel):
    repo: str | None = None  # None = 全部


router = APIRouter()


def _run_dict(r: Run, with_logs: bool = False, logs: list[RunLog] | None = None) -> dict:
    d = {
        "id": r.id,
        "repo": r.repo,
        "trigger": r.trigger,
        "mode": r.mode,
        "old_sha": r.old_sha,
        "new_sha": r.new_sha,
        "status": r.status,
        "summary": r.summary,
        "error": r.error,
        "commit_sha": r.commit_sha,
        "pushed": r.pushed,
        "pr_number": r.pr_number,
        "pr_url": r.pr_url,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
    if with_logs:
        d["logs"] = [
            {"ts": lg.ts.isoformat(), "level": lg.level, "message": lg.message}
            for lg in (logs or [])
        ]
    return d


@router.get("")
async def list_runs(
    request: Request,
    repo: str | None = None,
    status: str | None = None,  # running | published | failed
    trigger: str | None = None,  # scheduled | manual
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    svc = request.app.state.svc
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    with Session(svc.engine) as session:
        stmt = select(Run).order_by(Run.id.desc())  # type: ignore[union-attr]
        if repo:
            stmt = stmt.where(Run.repo == repo)
        if status:
            stmt = stmt.where(Run.status == status)
        if trigger:
            stmt = stmt.where(Run.trigger == trigger)
        if q:
            stmt = stmt.where(Run.repo.contains(q))  # type: ignore[union-attr]
        total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
        runs = session.exec(stmt.offset((page - 1) * page_size).limit(page_size)).all()
        return {
            "runs": [_run_dict(r) for r in runs],
            "total": total,
            "page": page,
            "page_size": page_size,
        }


@router.get("/{run_id}")
async def run_detail(run_id: int, request: Request):
    svc = request.app.state.svc
    with Session(svc.engine) as session:
        run = session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run 不存在")
        logs = session.exec(
            select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id)  # type: ignore[union-attr]
        ).all()
        return _run_dict(run, with_logs=True, logs=logs)


@router.get("/{run_id}/stream")
async def run_stream(run_id: int, request: Request):
    """SSE：增量推送 run 日志，结束时发 done 事件。"""
    svc = request.app.state.svc

    async def gen():
        last_id = 0
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            with Session(svc.engine) as session:
                run = session.get(Run, run_id)
                if run is None:
                    yield 'event: error\ndata: {"message": "run 不存在"}\n\n'
                    return
                logs = session.exec(
                    select(RunLog)
                    .where(RunLog.run_id == run_id, RunLog.id > last_id)  # type: ignore[union-attr]
                    .order_by(RunLog.id)
                ).all()
                for lg in logs:
                    last_id = lg.id  # type: ignore[assignment]
                    data = json.dumps(
                        {"id": lg.id, "ts": lg.ts.isoformat(), "level": lg.level, "message": lg.message},
                        ensure_ascii=False,
                    )
                    yield f"data: {data}\n\n"
                if run.status != "running":
                    yield 'event: done\ndata: {}\n\n'
                    return
            idle += 1
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/sync")
async def trigger_sync(body: SyncIn, request: Request):
    svc = request.app.state.svc
    names = svc.vault.repo_names()
    targets = [body.repo] if body.repo else list(names)
    for t in targets:
        if t not in names:
            raise HTTPException(404, f"不在监控清单：{t}")
    for t in targets:
        enqueue(svc, t, "manual")
    return {"queued": targets}
