"""每日晨报。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("")
async def list_daily(request: Request):
    svc = request.app.state.svc
    return {"dates": svc.vault.daily_list()}


@router.get("/{date}")
async def read_daily(date: str, request: Request):
    svc = request.app.state.svc
    content = svc.vault.daily_read(date)
    if content is None:
        raise HTTPException(404, "该日无晨报")
    return {"date": date, "content": content}
