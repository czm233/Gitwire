"""调度：APScheduler 常驻进程内。休眠唤醒后补跑（misfire 宽限 1 小时）。"""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.engine.daily import gen_daily
from app.engine.watch import watch_round


def build_scheduler(svc) -> AsyncIOScheduler:
    settings = svc.settings
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    # 必须是 async 函数：AsyncIOExecutor 会在事件循环上 await 它，
    # sync 函数会被扔进线程池（没有事件循环，一切异步调用都会炸）
    async def watch_job() -> None:
        try:
            await watch_round(svc)
        except Exception as e:  # noqa: BLE001
            print(f"[watch] 调度失败: {e}")

    async def daily_job() -> None:
        try:
            await gen_daily(svc)
        except Exception as e:  # noqa: BLE001
            print(f"[daily] 调度失败: {e}")

    scheduler.add_job(
        watch_job,
        CronTrigger.from_crontab(settings.watch_cron, timezone=settings.tz),
        id="watch",
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.add_job(
        daily_job,
        CronTrigger.from_crontab(settings.daily_cron, timezone=settings.tz),
        id="daily",
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    return scheduler
