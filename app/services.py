"""服务容器：把 settings / db / vault / gh / llm / 队列捏在一起。

编排归代码、语义归 agent——确定性组件在此装配，分析与写作交给 LLM 配方。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.config import Settings
from app.db import init_db, make_engine
from app.engine.runner import sync_repo
from app.github import GithubClient
from app.llm import LLMClient
from app.vault import Vault


@dataclass
class Services:
    settings: Settings
    engine: object  # sqlmodel engine
    vault: Vault
    gh: GithubClient
    llm: LLMClient
    bark: "BarkClient | None" = None
    queue: asyncio.Queue | None = None
    worker: asyncio.Task | None = None


async def build_services(
    settings: Settings, gh: GithubClient | None = None, llm: LLMClient | None = None
) -> Services:
    from app.bark import BarkClient

    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    vault = await Vault.open(settings)
    return Services(
        settings=settings,
        engine=engine,
        vault=vault,
        gh=gh or GithubClient(settings.github_token),
        llm=llm
        or LLMClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            protocol=settings.llm_protocol,
            max_tokens=settings.llm_max_tokens,
            thinking=settings.llm_thinking,
        ),
        bark=BarkClient(settings.bark_url, settings.bark_group),
    )


def enqueue(svc: Services, repo: str, trigger: str = "manual") -> None:
    if svc.queue is not None:
        svc.queue.put_nowait((repo, trigger))


async def start_worker(svc: Services) -> None:
    """单 worker 串行消费：v0 全局并发 = 1，同 repo 天然防重入。"""
    svc.queue = asyncio.Queue()

    async def loop() -> None:
        while True:
            repo, trigger = await svc.queue.get()
            try:
                if repo == "__drain__":
                    continue
                await sync_repo(svc, repo, trigger)
            except Exception as e:  # noqa: BLE001
                print(f"[worker] {repo} 异常: {e}")
            finally:
                svc.queue.task_done()

    svc.worker = asyncio.create_task(loop(), name="gitwire-worker")


async def stop_worker(svc: Services) -> None:
    if svc.worker:
        svc.worker.cancel()
        try:
            await svc.worker
        except asyncio.CancelledError:
            pass
        svc.worker = None
