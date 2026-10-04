"""API 全链路测试：TestClient + 假 gh/llm，走真实的 vault git。"""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import DocsSyncFakeLLM, FakeGithub, make_settings

async def _wait_run_done(client, repo, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get("/api/runs", params={"repo": repo})
        runs = resp.json()["runs"]
        if runs and runs[0]["status"] != "running":
            return runs[0]
        time.sleep(0.2)
    raise TimeoutError("run 未在期限内完成")


async def test_api_full_chain(tmp_path):
    settings = make_settings(tmp_path)
    gh = FakeGithub(sha="c" * 40)
    llm = DocsSyncFakeLLM()
    app = create_app(settings, gh=gh, llm=llm)

    with TestClient(app) as client:
        # 1. 健康 + 空态势板
        assert client.get("/api/health").json()["ok"] is True
        board = client.get("/api/board").json()
        assert board["stats"]["targets"] == 0

        # 2. 添加监控项目（自动排队建档）
        resp = client.post("/api/repos", json={"repo": "a/b"})
        assert resp.status_code == 200, resp.text
        import yaml

        yml = yaml.safe_load(open(f"{settings.data_path}/gitwire.yml", encoding="utf-8"))
        assert yml["repos"] == ["a/b"]

        # 重复添加 → 409
        assert client.post("/api/repos", json={"repo": "a/b"}).status_code == 409
        # 不存在格式 → 400
        assert client.post("/api/repos", json={"repo": "不是仓库"}).status_code == 400

        # 3. 等 worker 完成建档
        run = await _wait_run_done(client, "a/b")
        assert run["status"] == "published", run
        assert run["mode"] == "init"

        board = client.get("/api/board").json()
        assert board["stats"]["published"] == 1
        assert board["repos"][0]["sha7"] == "c" * 7
        assert board["repos"][0]["summary"]

        # 4. 档案文件
        files = client.get("/api/repos/a-b/files").json()["files"]
        paths = [f["path"] for f in files]
        assert "README.md" in paths and "tech-stack.md" in paths
        assert any(p.startswith("changelog/") for p in paths)
        content = client.get("/api/repos/a-b/file", params={"path": "README.md"}).json()
        assert content["content"].startswith("#")

        # 5. 模拟新提交：sha 前进 → 手动触发 → 增量
        gh.sha = "e" * 40
        client.post("/api/runs/sync", json={"repo": "a/b"})
        run2 = await _wait_run_done(client, "a/b")
        assert run2["status"] == "published"
        assert run2["mode"] == "incremental"

        # 6. 时间线：两个版本
        timeline = client.get("/api/repos/a-b/timeline").json()["entries"]
        assert len(timeline) == 2
        sha2 = timeline[0]["sha"]
        detail = client.get(f"/api/repos/a-b/timeline/{sha2}").json()["files"]
        assert detail and detail[0]["patch"]

        # 7. 晨报：daily 由 gen_daily 生成，API 只读；写一个晨报验证读取接口
        vault_dir = settings.gitwire_vault
        with open(f"{vault_dir}/daily/2026-09-24.md", "w", encoding="utf-8") as f:
            f.write("# 2026-09-24 晨报\n\n> 测试\n")
        dates = client.get("/api/daily").json()["dates"]
        assert "2026-09-24" in dates
        daily = client.get("/api/daily/2026-09-24").json()
        assert daily["content"].startswith("# 2026-09-24")

        # 8. 运行历史
        runs = client.get("/api/runs").json()["runs"]
        assert len(runs) >= 2
        detail_run = client.get(f"/api/runs/{runs[0]['id']}").json()
        assert "logs" in detail_run and detail_run["logs"]

        # 9. 移除监控（保留档案目录）
        resp = client.delete("/api/repos/a-b")
        assert resp.status_code == 200
        import yaml as _y

        yml = _y.safe_load(open(f"{settings.data_path}/gitwire.yml", encoding="utf-8"))
        assert yml["repos"] == []
        assert client.get("/api/repos/a-b/files").status_code == 200  # 档案还在


async def test_add_repo_accepts_url_forms(tmp_path):
    """输入层宽松：URL / ssh 形态自动规整成 owner/name。"""
    import yaml

    from app.github import normalize_repo

    assert normalize_repo("a/b") == "a/b"
    assert normalize_repo("https://github.com/czm233/VoiceTutor.git") == "czm233/VoiceTutor"
    assert normalize_repo("github.com/czm233/VoiceTutor") == "czm233/VoiceTutor"
    assert normalize_repo("git@github.com:czm233/VoiceTutor.git") == "czm233/VoiceTutor"
    assert normalize_repo("https://github.com/czm233/VoiceTutor/") == "czm233/VoiceTutor"
    assert normalize_repo("not a repo") is None

    settings = make_settings(tmp_path)
    app = create_app(settings, gh=FakeGithub(), llm=DocsSyncFakeLLM())
    with TestClient(app) as client:
        resp = client.post(
            "/api/repos", json={"repo": "https://github.com/a/b.git"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["repo"] == "a/b"
        yml = yaml.safe_load(
            open(f"{settings.data_path}/gitwire.yml", encoding="utf-8")
        )
        assert yml["repos"] == ["a/b"]


async def test_auth_enabled(tmp_path):
    settings = make_settings(tmp_path)
    settings.secret_key = "hunter2"
    app = create_app(settings, gh=FakeGithub(), llm=DocsSyncFakeLLM())
    with TestClient(app) as client:
        assert client.get("/api/board").status_code == 401
        assert client.post("/api/login", json={"key": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"key": "hunter2"}).status_code == 200
        assert client.get("/api/board").status_code == 200  # cookie 已带上
        assert client.get("/api/health").status_code == 200  # 豁免路径
