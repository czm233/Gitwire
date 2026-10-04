from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from app.config import Settings
from app.hosted.auth import router as auth_router
from app.hosted.github import GitHubFailure
from app.hosted.runtime import Runtime

DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'


def create_hosted_app(settings: Settings, runtime: Runtime | None = None):
    @asynccontextmanager
    async def lifespan(app):
        rt = runtime or Runtime.create(settings)
        app.state.runtime = rt
        # Only the migration command changes schema. Fail clearly on an unmigrated database.
        with rt.engine.connect() as conn:
            conn.execute(text('SELECT id FROM h_account LIMIT 1'))
        yield
        if runtime is None:
            await rt.close()

    app = FastAPI(title='Gitwire', lifespan=lifespan)
    if runtime:
        app.state.runtime = runtime
    app.include_router(auth_router, prefix='/api')

    @app.exception_handler(GitHubFailure)
    async def github_error(request, exc):
        return JSONResponse({'detail': exc.safe_message}, status_code=429 if exc.status in (403, 429) else exc.status)

    @app.middleware('http')
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/health')
    async def health(request: Request):
        rt = request.app.state.runtime
        with rt.engine.connect() as c:
            c.execute(text('SELECT 1'))
        redis_ok = False
        if rt.redis:
            try:
                redis_ok = bool(await rt.redis.ping())
            except Exception:
                pass
        return {'ok': True, 'mode': 'hosted', 'database': True, 'redis': redis_ok,
                'github_configured': bool(settings.github_client_id and settings.github_client_secret)}

    from app.hosted.api import router
    app.include_router(router, prefix='/api')
    from app.hosted.mail import router as mail_router
    app.include_router(mail_router, prefix='/api')
    from app.hosted.export import router as export_router
    app.include_router(export_router, prefix='/api')
    if (DIST / 'assets').exists():
        app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')

    @app.get('/{path:path}', include_in_schema=False)
    async def spa(path: str):
        if path.startswith('api/'):
            return JSONResponse({'detail': '接口不存在'}, status_code=404)
        if (DIST / 'index.html').exists():
            return FileResponse(DIST / 'index.html')
        return JSONResponse({'detail': '请访问 Vite 开发页面或先构建前端'}, status_code=404)

    return app
