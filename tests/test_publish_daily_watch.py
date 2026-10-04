"""发布 / 晨报 / watch 测试。"""

import pytest
from sqlmodel import Session, select

from app.engine.analyze import AnalyzeResult
from app.models import Run
from tests.conftest import (
    AUTHOR,
    DocsSyncFakeLLM,
    FakeGithub,
    PlainFakeLLM,
    make_settings,
)

async def _make_svc(tmp_path, gh=None, llm=None):
    from app.db import init_db, make_engine
    from app.services import Services
    from app.vault import Vault

    settings = make_settings(tmp_path)
    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    vault = await Vault.open(settings)
    return Services(settings, engine, vault, gh or FakeGithub(), llm or DocsSyncFakeLLM())


async def test_publish_advances_cursor_and_board(tmp_path):
    from app.engine.publish import publish

    svc = await _make_svc(tmp_path)
    files = {
        "README.md": "# 档案\n",
        "changelog/2026-09-24-ccccccc.md": "# 变更\n",
    }
    outcome = await publish(
        svc, "a/b", files, "c" * 40, "incremental",
        meta_updates={"last_release_tag": "v1.0", "last_issue_number": 7},
        summary="发布了新版本",
    )
    assert outcome.commit_sha and outcome.pushed and outcome.pr_url is None

    meta = svc.vault.meta("a-b")
    assert meta.last_synced == "c" * 40
    assert meta.summary == "发布了新版本"
    assert meta.last_release_tag == "v1.0"
    assert meta.last_issue_number == 7

    board = (svc.vault.root / "README.md").read_text(encoding="utf-8")
    assert "[a-b](./a-b)" in board and "ccccccc"[:7] in board

    entries = await svc.vault.timeline("a-b")
    assert len(entries) == 1


async def test_runner_sync_full_chain(tmp_path):
    """runner 全链路：init 建档 → sha 前进 → 增量。"""
    from app.engine.runner import sync_repo

    svc = await _make_svc(tmp_path)
    svc.gh.sha = "c" * 40

    run = await sync_repo(svc, "a/b", "manual")
    assert run.status == "published" and run.mode == "init"
    assert svc.vault.meta("a-b").last_synced == "c" * 40

    svc.gh.sha = "e" * 40
    run2 = await sync_repo(svc, "a/b", "scheduled")
    assert run2.mode == "incremental"
    assert run2.new_sha == "e" * 40
    assert run2.commit_sha

    run3 = await sync_repo(svc, "a/b", "scheduled")  # 游标一致
    assert run3.mode == "noop"


async def test_runner_failure_keeps_cursor(tmp_path):
    """分析失败 → run failed，游标不动。"""

    class BadLLM:
        async def chat(self, *a, **k):
            return "不按契约输出"

    svc = await _make_svc(tmp_path, llm=BadLLM())
    svc.gh.sha = "c" * 40
    from app.engine.runner import sync_repo

    run = await sync_repo(svc, "a/b", "manual")
    assert run.status == "failed"
    assert run.error
    assert svc.vault.meta("a-b") is None  # 从未建档成功


async def test_daily_skips_when_empty(tmp_path):
    from app.engine.daily import gen_daily

    svc = await _make_svc(tmp_path)
    assert await gen_daily(svc, "2026-09-24") is None
    assert svc.vault.daily_list() == []


async def test_daily_generates(tmp_path):
    from app.engine.daily import gen_daily
    from app.vault import ProjectMeta

    svc = await _make_svc(tmp_path, llm=PlainFakeLLM())
    svc.vault.write_file("a-b/changelog/2026-09-24-ccccccc.md", "# 修复了登录\n")
    svc.vault.save_meta("a-b", ProjectMeta(repo="a/b"))
    result = await gen_daily(svc, "2026-09-24")
    assert result == "2026-09-24"
    assert svc.vault.daily_read("2026-09-24").startswith("# 晨报")


async def test_watch_round(tmp_path):
    from app.engine.watch import watch_round
    from app.services import start_worker, stop_worker
    from app.vault import GitwireConfig, ProjectMeta, RepoTarget

    svc = await _make_svc(tmp_path)
    svc.vault.write_config(GitwireConfig(repos=[RepoTarget(name="a/b")]))
    svc.vault.save_meta("a-b", ProjectMeta(repo="a/b", last_synced="a" * 40))
    await start_worker(svc)

    try:
        svc.gh.sha = "a" * 40
        assert await watch_round(svc) == []  # 游标一致，不入队

        svc.gh.sha = "e" * 40
        assert await watch_round(svc) == ["a/b"]  # 游标前进，入队

        # 已有 running run 的仓库跳过（防重入）
        with Session(svc.engine) as s:
            s.add(Run(repo="a/b", status="running"))
            s.commit()
        assert await watch_round(svc) == []

        # 收掉 running run，恢复可入队
        with Session(svc.engine) as s:
            for r in s.exec(select(Run).where(Run.repo == "a/b")).all():
                r.status = "failed"
                s.add(r)
            s.commit()

        # 新 release 同样触发入队
        svc.gh.sha = "a" * 40
        svc.gh.release = {"tag": "v2", "name": "x", "published_at": "2026-09-24", "body": "n"}
        assert await watch_round(svc) == ["a/b"]
        svc.gh.release = None
        await svc.queue.join()  # 等 worker 跑完本轮（游标落定），避免调度竞态

        # v4：不挂雷达的仓库，新 issue 不再触发（issue-watch 已退役）
        svc.gh.issues = [{"number": 3, "title": "t", "author": "u", "labels": [], "created_at": "2026-09-24", "body": "b"}]
        assert await watch_round(svc) == []

        # v4：挂雷达的仓库，新 issue 触发
        svc.vault.write_config(
            GitwireConfig(repos=[RepoTarget(name="a/b", recipes=["docs-sync", "issue-radar"])])
        )
        svc.vault.save_meta(
            "a-b", ProjectMeta(repo="a/b", last_synced="a" * 40, last_issue_number=1, last_pr_number=0)
        )
        assert await watch_round(svc) == ["a/b"]
    finally:
        await stop_worker(svc)


async def test_hosted_recipe_failures_do_not_store_provider_credentials(tmp_path, monkeypatch, capsys):
    from app.engine import runner
    from app.models import Alert, RunLog
    svc=await _make_svc(tmp_path)
    svc.settings.public_source_only=True
    async def fail(*args,**kwargs):
        raise RuntimeError('request failed Authorization: Bearer TEST_PROVIDER_SECRET')
    monkeypatch.setattr(runner,'_sync',fail)
    result=await runner.sync_repo(svc,'public/project','hosted')
    assert result.status=='failed' and 'TEST_PROVIDER_SECRET' not in result.error
    with Session(svc.engine) as s:
        assert all('TEST_PROVIDER_SECRET' not in row.message for row in s.exec(select(RunLog)))
        assert all('TEST_PROVIDER_SECRET' not in row.title for row in s.exec(select(Alert)))
    assert 'TEST_PROVIDER_SECRET' not in capsys.readouterr().out
