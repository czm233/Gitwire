"""档案浏览：vault 项目目录的文件清单与内容。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.vault import VaultError

router = APIRouter()


@router.get("/{slug}/files")
async def list_files(slug: str, request: Request):
    svc = request.app.state.svc
    if not svc.vault.has_project(slug):
        raise HTTPException(404, "项目尚未建档")
    return {"slug": slug, "files": svc.vault.list_files(slug)}


@router.get("/{slug}/file")
async def read_file(slug: str, request: Request, path: str):
    svc = request.app.state.svc
    try:
        content = svc.vault.read_file(f"{slug}/{path}")
    except VaultError as e:
        raise HTTPException(400, str(e))
    if content is None:
        raise HTTPException(404, f"文件不存在：{path}")
    return {"slug": slug, "path": path, "content": content}
