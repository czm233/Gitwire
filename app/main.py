"""FastAPI 入口：装配服务、调度器、worker、鉴权、静态托管。"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import api_router
from app.config import Settings
from app.engine.watch import watch_round
from app.github import GithubClient
from app.llm import LLMClient
from app.scheduler import build_scheduler
from app.services import build_services, start_worker, stop_worker

DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


def create_app(
    settings: Settings | None = None,
    gh: GithubClient | None = None,
    llm: LLMClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if settings.hosted_enabled:
        from app.hosted.application import create_hosted_app

        return create_hosted_app(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = await build_services(settings, gh=gh, llm=llm)
        from app.db import recover_orphan_runs

        orphans = recover_orphan_runs(svc.engine)
        if orphans:
            print(f"[startup] 已恢复 {orphans} 个被中断的 run（标记 failed，待 watch 重跑）")
        app.state.svc = svc
        await start_worker(svc)
        scheduler = build_scheduler(svc)
        scheduler.start()
        # 启动补跑：错过的 watch 一轮追上
        asyncio.get_running_loop().create_task(_startup_catchup(svc))
        yield
        scheduler.shutdown(wait=False)
        await stop_worker(svc)

    app = FastAPI(title="Gitwire", version=__version__, lifespan=lifespan)
    app.include_router(api_router, prefix="/api")

    @app.get("/api/health")
    async def health(request: Request):
        svc = request.app.state.svc
        cfg = svc.vault.read_config()
        return {
            "ok": True,
            "version": __version__,
            "vault": str(svc.vault.root),
            "remote": svc.vault.remote or None,
            "targets": len(cfg.repos),
            "pending_push": await svc.vault.ahead_count(),
        }

    # ---- 鉴权（SECRET_KEY 非空时启用） ----
    if settings.secret_key:

        @app.post("/api/login")
        async def login(request: Request):
            body = await request.json()
            if not secrets.compare_digest(str(body.get("key", "")), settings.secret_key):
                return JSONResponse({"detail": "密码不对"}, status_code=401)
            resp = JSONResponse({"ok": True})
            resp.set_cookie("gitwire_key", settings.secret_key, httponly=True)
            return resp

        @app.middleware("http")
        async def auth_mw(request: Request, call_next):
            if request.url.path.startswith("/api"):
                exempt = request.url.path in ("/api/login", "/api/health")
                ok_cookie = request.cookies.get("gitwire_key", "") == settings.secret_key
                ok_header = request.headers.get("x-access-key", "") == settings.secret_key
                if not exempt and not (ok_cookie or ok_header):
                    return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)

    # ---- 前端静态托管（SPA 回退） ----
    if (DIST / "index.html").exists():
        if (DIST / "assets").exists():
            app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            f = DIST / full_path
            if full_path and f.is_file() and f.resolve().is_relative_to(DIST.resolve()):
                return FileResponse(f)
            return FileResponse(DIST / "index.html")
    else:

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_dev(full_path: str):
            if full_path.startswith("api"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            return JSONResponse(
                {
                    "hint": "前端未构建：cd frontend && npm install && npm run build，"
                    "或开发模式 npm run dev 后访问 5173 端口"
                }
            )

    return app


async def _startup_catchup(svc) -> None:
    await asyncio.sleep(5)
    try:
        await watch_round(svc)
    except Exception as e:  # noqa: BLE001
        print(f"[startup] 补跑 watch 失败: {e}")


_app = None


def __getattr__(name: str):
    """uvicorn app.main:app 惰性创建（读 .env 在导入期之后）。"""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
