"""v1/v2 功能测试：多配方 / 警报+Bark / SSE / path filter / release+issue+CVE 触发 / PR 模式。"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import Settings
from app.main import create_app
from app.models import Alert
from app.vault import GitwireConfig, ProjectMeta, RepoTarget, slugify
from tests.conftest import (
    AUTHOR,
    FakeBark,
    FakeGithub,
    RecipeFakeLLM,
    make_settings,
)

pytestmark = pytest.mark.anyio


async def _make_svc(tmp_path, gh=None, llm=None, bark=None, settings=None):
    from app.db import init_db, make_engine
    from app.services import Services
    from app.vault import Vault

    settings = settings or make_settings(tmp_path)
    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    vault = await Vault.open(settings)
    return Services(
        settings, engine, vault,
        gh or FakeGithub(), llm or RecipeFakeLLM(), bark or FakeBark(),
    )


# ---------- 配方链 + 警报 ----------


async def test_recipe_chain_and_alerts(tmp_path):
    """增量触发 docs-sync + tripwire + bug-watch，警报入账并推 Bark。"""
    from app.engine.runner import sync_repo

    svc = await _make_svc(tmp_path)
    svc.vault.write_config(
        GitwireConfig(
            repos=[RepoTarget(name="a/b", recipes=["docs-sync", "feature-tripwire", "bug-watch"])]
        )
    )
    svc.gh.sha = "c" * 40
    await sync_repo(svc, "a/b")  # init

    svc.gh.sha = "e" * 40
    run = await sync_repo(svc, "a/b")
    assert run.status == "published"
    assert set(svc.llm.recipe_calls) >= {"feature-tripwire", "bug-watch"}
    assert svc.vault.read_file("a-b/tripwires.md")
    assert svc.vault.read_file("a-b/bugwatch.md")

    with Session(svc.engine) as s:
        alerts = s.exec(select(Alert).order_by(Alert.id)).all()
    kinds = {a.kind for a in alerts}
    assert "tripwire" in kinds and "breaking" in kinds
    assert all(a.pushed for a in alerts)  # FakeBark 全送达
    titles = [t for t, _, _ in svc.bark.sent]
    assert any("配置格式迁移" in t for t in titles)


async def test_release_trigger(tmp_path):
    from app.engine.runner import sync_repo

    svc = await _make_svc(tmp_path)
    svc.vault.write_config(GitwireConfig(repos=[RepoTarget(name="a/b")]))
    svc.vault.save_meta(
        "a-b",
        ProjectMeta(repo="a/b", last_synced="a" * 40, last_issue_number=99),
    )
    svc.gh.sha = "a" * 40  # 游标不动
    svc.gh.release = {
        "tag": "v1.2.0", "name": "rel", "published_at": "2026-09-24T00:00:00Z", "body": "notes",
    }

    run = await sync_repo(svc, "a/b")
    assert run.status == "published"
    assert svc.vault.read_file("a-b/releases/v1.2.0.md")
    assert svc.vault.meta("a-b").last_release_tag == "v1.2.0"
    assert svc.vault.meta("a-b").last_synced == "a" * 40  # 游标不动
    with Session(svc.engine) as s:
        kinds = [a.kind for a in s.exec(select(Alert)).all()]
    assert "release" in kinds

    # 同一 release 再跑：不再触发
    svc.llm.recipe_calls.clear()
    run2 = await sync_repo(svc, "a/b")
    assert run2.mode == "noop"
    assert "release-brief" not in svc.llm.recipe_calls


async def test_issue_trigger(tmp_path):
    """v4：issue 情报统一归雷达——须挂 issue-radar 才触发；不挂雷达的仓库 noop。"""
    from app.engine.runner import sync_repo

    svc = await _make_svc(tmp_path)
    svc.gh.sha = "a" * 40
    svc.gh.release = {"tag": "v1", "name": "", "published_at": "", "body": ""}
    svc.gh.issues = [
        {"number": 7, "title": "X 太慢", "author": "u", "labels": ["perf"], "created_at": "2026-09-24T01:00:00Z", "body": "如题"},
        {"number": 8, "title": "文档缺失", "author": "v", "labels": [], "created_at": "2026-09-24T02:00:00Z", "body": ""},
    ]

    # 不挂雷达：新 issue 不再触发（issue-watch 已退役）
    svc.vault.write_config(GitwireConfig(repos=[RepoTarget(name="a/b")]))
    svc.vault.save_meta(
        "a-b",
        ProjectMeta(repo="a/b", last_synced="a" * 40, last_release_tag="v1", last_issue_number=5),
    )
    run = await sync_repo(svc, "a/b")
    assert run.mode == "noop"

    # 挂雷达：新 issue 触发雷达 sweep
    svc.vault.write_config(
        GitwireConfig(repos=[RepoTarget(name="a/b", recipes=["docs-sync", "issue-radar"])])
    )
    svc.vault.save_meta(
        "a-b",
        ProjectMeta(repo="a/b", last_synced="a" * 40, last_release_tag="v1", last_issue_number=5, last_pr_number=0),
    )
    run = await sync_repo(svc, "a/b")
    assert run.status == "published"
    assert svc.vault.read_file("a-b/issues.md")
    assert svc.vault.meta("a-b").last_issue_number == 8

    # 无新 issue / 动态再跑（scheduled）→ noop；手动触发会强制重扫雷达
    run2 = await sync_repo(svc, "a/b", "scheduled")
    assert run2.mode == "noop"


async def test_cve_scan(monkeypatch, tmp_path):
    """cve-scan 确定性产出 security.md，新漏洞发警报，重复扫描不重复报。"""
    from app.engine.recipes import run_cve_scan

    svc = await _make_svc(tmp_path)

    class FakeFileGithub(FakeGithub):
        async def file_content(self, repo, path):
            if path == "requirements.txt":
                return "django==3.2.0\nrequests==2.20.0\n"
            return None

    svc.gh = FakeFileGithub()

    class FakeOsv:
        async def query_many(self, deps, concurrency=5):
            return {("PyPI", "django"): [
                {"id": "GHSA-xxxx", "summary": "Django SQL 注入", "severity": "high", "fixed": ["3.2.11"], "url": "https://osv.dev/vulnerability/GHSA-xxxx"}
            ]}

        async def aclose(self):
            pass

    import app.engine.recipes as recipes_mod

    monkeypatch.setattr(recipes_mod, "OsvClient", FakeOsv)

    r = await run_cve_scan(svc, "a/b", "a-b", ["requirements.txt"])
    assert "security.md" in r.files
    assert "GHSA-xxxx" in r.files["security.md"]
    assert [a["kind"] for a in r.alerts] == ["cve"]

    # 第二轮：security.md 已含该 id → 不再警报
    svc.vault.write_file("a-b/security.md", r.files["security.md"])
    r2 = await run_cve_scan(svc, "a/b", "a-b", ["requirements.txt"])
    assert r2.files and not r2.alerts


# ---------- PR 模式 ----------


async def test_pr_mode_publish(tmp_path):
    """PR 模式：内容走分支 + 开 PR；main 游标不动；有待处理 PR 时跳过。"""
    from app.engine.runner import sync_repo
    from app.vault import Vault, git

    settings = make_settings(tmp_path)
    # 本地 bare origin，目录名伪装成 github.com/owner/repo.git 让 remote_repo_slug() 可解析
    gh_dir = tmp_path / "github.com" / "owner"
    gh_dir.mkdir(parents=True)
    origin = gh_dir / "repo.git"
    _init_bare(origin)

    vault_root = tmp_path / "vault"
    await git(tmp_path, "clone", str(origin), "vault")
    vault = Vault(
        vault_root, remote=str(origin), author=AUTHOR,
        config_path=settings.data_path / "gitwire.yml",
    )
    vault.ensure_skeleton()

    from app.db import init_db, make_engine
    from app.services import Services

    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    svc = Services(settings, engine, vault, FakeGithub(), RecipeFakeLLM(), FakeBark())
    svc.vault.write_config(
        GitwireConfig(repos=[RepoTarget(name="a/b")], publish_mode="pr")
    )
    # bare origin 需要至少一个提交作为 PR 的 main 基线（配置不进仓库，用 README 初始化）
    vault.write_file("README.md", "# vault\n")
    await vault.commit(["README.md"], "init vault")
    await vault.push_branch("main")

    svc.gh.sha = "c" * 40
    run = await sync_repo(svc, "a/b")
    assert run.status == "published"
    assert run.pr_url and run.pr_number
    assert svc.gh.created_prs, "应该创建了 PR"

    # main 上游标不动（还在分支里）
    assert svc.vault.meta("a-b") is None or svc.vault.meta("a-b").last_synced is None

    # 再跑一轮：有待处理 PR → pr-pending
    svc.gh.pulls = [
        {"number": run.pr_number, "url": run.pr_url, "state": "open", "merged": False,
         "head": "gitwire/a-b-x", "title": "t"}
    ]
    run2 = await sync_repo(svc, "a/b")
    assert run2.mode == "pr-pending"


def _init_bare(path):
    import subprocess

    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", "--bare", str(path)], check=True)


# ---------- API：警报 / SSE / board ----------


async def test_alerts_api_and_sse(tmp_path):
    settings = make_settings(tmp_path)
    gh = FakeGithub(sha="c" * 40)
    llm = RecipeFakeLLM()
    app = create_app(settings, gh=gh, llm=llm)
    with TestClient(app) as client:
        client.post("/api/repos", json={"repo": "a/b"})
        import time

        for _ in range(80):
            runs = client.get("/api/runs", params={"repo": "a/b"}).json()["runs"]
            if runs and runs[0]["status"] != "running":
                break
            time.sleep(0.2)
        assert runs[0]["status"] == "published"

        # board 带警报字段
        board = client.get("/api/board").json()
        assert "alerts" in board

        # alerts 端点
        alerts = client.get("/api/alerts").json()["alerts"]
        assert isinstance(alerts, list)

        # SSE：已完成 run → 先收历史日志再收 done
        with client.stream("GET", f"/api/runs/{runs[0]['id']}/stream") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            got_done = False
            got_log = False
            for line in resp.iter_lines():
                if line.startswith("event: done"):
                    got_done = True
                    break
                if line.startswith("data:"):
                    payload = json.loads(line[5:])
                    if "message" in payload:
                        got_log = True
            assert got_done and got_log


async def test_obsidian_frontmatter(tmp_path):
    """OBSIDIAN_FRONTMATTER=true：产出 md 注入 frontmatter。"""
    from app.engine.publish import publish

    settings = make_settings(tmp_path)
    settings.obsidian_frontmatter = True
    svc = await _make_svc(tmp_path, settings=settings)
    await publish(
        svc, "a/b", {"README.md": "# 档案\n"}, "c" * 40, "incremental", summary="s"
    )
    content = svc.vault.read_file("a-b/README.md")
    assert content.startswith("---\n")
    assert "repo: a/b" in content and "tags: [gitwire]" in content
    assert "# 档案" in content


async def test_migration_adds_columns(tmp_path):
    """旧库（无 pr 列）打开后自动补列。"""
    import sqlite3

    from app.db import init_db, make_engine

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE run (id INTEGER PRIMARY KEY, repo TEXT, status TEXT, started_at TIMESTAMP)"
    )
    conn.execute("INSERT INTO run (repo, status) VALUES ('a/b', 'published')")
    conn.commit()
    conn.close()

    engine = make_engine(db)
    init_db(engine)  # create_all + migrate
    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(run)")}
    assert {"pr_number", "pr_url"} <= cols
