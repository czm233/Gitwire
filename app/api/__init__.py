"""REST API 路由聚合。"""

from fastapi import APIRouter

from app.api import alerts, board, daily, files, issues, repos, runs, timeline

api_router = APIRouter()
api_router.include_router(board.router, prefix="/board", tags=["board"])
api_router.include_router(repos.router, prefix="/repos", tags=["repos"])
api_router.include_router(runs.router, prefix="/runs", tags=["runs"])
api_router.include_router(files.router, prefix="/repos", tags=["files"])
api_router.include_router(timeline.router, prefix="/repos", tags=["timeline"])
api_router.include_router(daily.router, prefix="/daily", tags=["daily"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(issues.router, prefix="/issues", tags=["issues"])
