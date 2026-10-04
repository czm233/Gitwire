"""Gitwire CLI。

用法：
  gitwire init                          初始化 .env 与数据库
  gitwire sync --repo owner/name        手动同步一个仓库（全链路）
  gitwire sync --repo X --resync-from SHA   把游标拨回 SHA 再同步（模拟新提交/验收用）
  gitwire watch                         跑一轮 Watch 扫描并把队列跑干
  gitwire daily [--date YYYY-MM-DD]     生成晨报
  gitwire serve                         启动 Web 服务
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from app.config import Settings

app = typer.Typer(help="Gitwire —— 一个人的开源情报站", no_args_is_help=True)


async def _sync(repo: str, resync_from: str) -> None:
    from app.engine.runner import sync_repo
    from app.services import build_services
    from app.vault import ProjectMeta, slugify

    settings = Settings()
    svc = await build_services(settings)
    if resync_from:
        slug = slugify(repo)
        meta = svc.vault.meta(slug) or ProjectMeta(repo=repo)
        meta.last_synced = resync_from
        svc.vault.save_meta(slug, meta)
        print(f"游标已拨回 {resync_from[:7]}")
    run = await sync_repo(svc, repo, trigger="manual")
    print(
        f"\nrun #{run.id} {run.status}"
        + (f" · {run.mode}" if run.mode else "")
        + (f" · {run.new_sha[:7] if run.new_sha else ''}")
    )
    if run.summary:
        print(f"摘要：{run.summary}")
    if run.error:
        print(f"错误：{run.error}")


@app.command()
def sync(
    repo: str = typer.Option(..., help="owner/name 或完整 GitHub URL"),
    resync_from: str = typer.Option("", help="先把游标拨回此 SHA 再同步"),
):
    """手动同步一个仓库：watch → analyze → publish 全链路。"""
    from app.github import normalize_repo

    norm = normalize_repo(repo)
    if not norm:
        print(f"无法识别仓库标识：{repo}（支持 owner/name 或完整 GitHub URL）")
        raise typer.Exit(1)
    asyncio.run(_sync(norm, resync_from))


async def _watch() -> None:
    from app.engine.watch import run_queued_now, watch_round
    from app.services import build_services, start_worker, stop_worker

    svc = await build_services(Settings())
    await start_worker(svc)
    queued = await watch_round(svc)
    print(f"本轮游标前进：{queued or '无'}")
    if queued:
        await run_queued_now(svc)
    await stop_worker(svc)


@app.command()
def watch():
    """跑一轮 Watch 扫描，并把入队任务就地跑完。"""
    asyncio.run(_watch())


async def _daily(date: str) -> None:
    from app.engine.daily import gen_daily
    from app.services import build_services

    svc = await build_services(Settings())
    result = await gen_daily(svc, date or None)
    if result is None:
        print("当日无 changelog，未生成晨报")
    else:
        print(f"晨报已生成：daily/{result}.md")


@app.command()
def daily(
    date: str = typer.Option("", help="YYYY-MM-DD，缺省为昨天"),
):
    """生成每日晨报。"""
    asyncio.run(_daily(date))


@app.command()
def init():
    """初始化：生成 .env（若缺）与数据库。"""
    env = Path(".env")
    if not env.exists():
        example = Path(__file__).resolve().parents[1] / ".env.example"
        content = (
            example.read_text(encoding="utf-8") if example.exists() else "# Gitwire 配置\n"
        )
        env.write_text(content, encoding="utf-8")
        print("已生成 .env，请填写后重新执行")
        raise typer.Exit(1)
    from app.db import init_db, make_engine
    from app.config import Settings

    settings = Settings()
    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    print(f"数据库就绪：{settings.data_path / 'gitwire.db'}")
    print(f"vault：{settings.gitwire_vault or '（未配置 GITWIRE_VAULT）'}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(10240),  # 本机端口登记表 10240-10249 号段（~/.config/local-deploy/ports.yaml）
):
    """启动 Web 服务。"""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, access_log=False)


def main() -> None:
    app()




@app.command('worker')
def hosted_worker():
    """运行托管任务执行器（独立于 Web 服务）。"""
    import asyncio
    from app.hosted.runtime import Runtime
    from app.hosted.jobs import worker_loop
    from app.hosted.worker import handle
    async def run():
        import signal
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        runtime = Runtime.create(Settings())
        try:
            await worker_loop(runtime, handle, stop)
        finally:
            await runtime.close()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)
    asyncio.run(run())


@app.command('ops-status')
def hosted_ops_status():
    """输出运维聚合状态；不展示用户资料、凭证或任务正文。"""
    import json
    from app.hosted.operations import snapshot
    from app.hosted.store import engine_for
    engine = engine_for(Settings())
    try:
        print(json.dumps(snapshot(engine), ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


@app.command('scheduler')
def hosted_scheduler():
    """运行托管调度器；仅投递任务，不在 Web 进程中调度。"""
    import asyncio
    from app.hosted.runtime import Runtime
    from app.hosted.scheduler import loop
    async def run():
        import signal
        stop = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            event_loop.add_signal_handler(signum, stop.set)
        runtime = Runtime.create(Settings())
        try:
            await loop(runtime, stop)
        finally:
            await runtime.close()
            for signum in (signal.SIGTERM, signal.SIGINT):
                event_loop.remove_signal_handler(signum)
    asyncio.run(run())


@app.command('archive-import')
def archive_import(source: Path, directory: Path, version: bool = True):
    """导入下载的情报 ZIP；默认仅做本地 Git 提交，不推送。"""
    from app.local_archive import import_archive
    try:
        result = import_archive(source, directory, version)
    except (ValueError, OSError) as exc:
        print(str(exc))
        raise typer.Exit(1)
    print('本地快照已保存' if result['changed'] else '内容相同，无需重复保存')


@app.command('migrate-legacy')
def migrate_legacy(github_id: str, apply: bool = False,
                   config: Path = Path('data/gitwire.yml'), vault: Path = Path('data/vault'),
                   database: Path = Path('data/gitwire.db')):
    """预览旧数据迁移；显式指定 GitHub 数字 ID，--apply 才写入新数据库。"""
    import json
    from app.hosted.runtime import Runtime
    from app.hosted.legacy import import_legacy
    async def run():
        rt = Runtime.create(Settings())
        try:
            return await import_legacy(rt, github_id, config, vault, database, apply)
        finally:
            await rt.close()
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
    except ValueError as exc:
        print(str(exc))
        raise typer.Exit(1)


@app.command('archive-push')
def archive_push(directory: Path, remote: str):
    """显式推送本地情报到指定备份仓库，使用本机 Git/SSH 凭证。"""
    from app.local_archive import push_archive
    try:
        push_archive(directory, remote)
    except (ValueError, OSError) as exc:
        print(str(exc))
        raise typer.Exit(1)
    print('情报备份已推送')


@app.command('archive-diff')
def archive_diff(directory: Path, before: str = '', after: str = ''):
    """对比本地情报快照正文；默认比较最近两次导入。"""
    from app.local_archive import compare_archives
    try:
        print(compare_archives(directory, before, after))
    except (ValueError, OSError) as exc:
        print(str(exc))
        raise typer.Exit(1)


if __name__ == "__main__":
    main()
