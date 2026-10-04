"""档案变更时间线：数据全部来自 vault 自己的 git 历史。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.vault import VaultError

router = APIRouter()


@router.get("/{slug}/timeline")
async def timeline(slug: str, request: Request):
    svc = request.app.state.svc
    entries = await svc.vault.timeline(slug)
    return {"slug": slug, "entries": entries}


@router.get("/{slug}/timeline/{sha}")
async def version(slug: str, sha: str, request: Request):
    svc = request.app.state.svc
    try:
        files = await svc.vault.version_diff(slug, sha)
    except VaultError as e:
        raise HTTPException(400, str(e))
    return {"slug": slug, "sha": sha, "files": files}
