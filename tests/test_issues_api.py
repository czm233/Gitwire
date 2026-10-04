"""Issue 专区 API 测试：radar.yml 聚合 / 筛选分页 / 追踪切换。"""

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app
from app.vault import GitwireConfig, ProjectMeta, RepoTarget
from tests.conftest import DocsSyncFakeLLM, FakeGithub, make_settings

pytestmark = pytest.mark.anyio


def _radar(entries: dict) -> str:
    return yaml.safe_dump(
        {"last_sweep": "2026-09-25T01:00:00+08:00", "issues": entries},
        allow_unicode=True,
        sort_keys=False,
    )


async def _make_client(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings, gh=FakeGithub(), llm=DocsSyncFakeLLM())
    client = TestClient(app)
    vault = settings.gitwire_vault

    import pathlib

    from app.vault import Vault

    v = await Vault.open(settings)
    v.write_config(
        GitwireConfig(
            repos=[
                RepoTarget(name="a/b", recipes=["docs-sync", "issue-radar"], watch_issues=[12]),
                RepoTarget(name="c/d", recipes=["docs-sync", "issue-radar"]),
            ]
        )
    )
    for slug, entries in {
        "a-b": {
            "12": {
                "title": "文档缺失", "difficulty": "简单", "summary": "补文档",
                "problem": "p", "plan": "pl", "analyzed_at": "2026-09-25",
                "last_status": "open", "labels_seen": ["help wanted"],
                "comments_seen": 2, "created_at": "2026-09-20T00:00:00Z",
            },
            "15": {
                "title": "重构内核", "difficulty": "困难", "summary": "大工程",
                "analyzed_at": "2026-09-25", "last_status": "open",
                "labels_seen": [], "comments_seen": 0, "created_at": "2026-09-21T00:00:00Z",
            },
            "18": {
                "title": "登录修复", "difficulty": "中等", "summary": "被 PR 占",
                "analyzed_at": "2026-09-25", "last_status": "taken-pr",
                "pr_claims": [30], "created_at": "2026-09-22T00:00:00Z",
            },
        },
        "c-d": {
            "3": {
                "title": "秒关问题", "difficulty": "简单", "summary": "已解决",
                "analyzed_at": "2026-09-25", "last_status": "closed",
                "resolved_by": "PR#9", "closed_at": "2026-09-23T00:00:00Z",
                "created_at": "2026-09-19T00:00:00Z",
            },
            "5": {
                "title": "新机会", "difficulty": "简单", "summary": "没人认领",
                "analyzed_at": "2026-09-25", "last_status": "open",
                "labels_seen": [], "comments_seen": 0, "created_at": "2026-09-24T00:00:00Z",
            },
        },
    }.items():
        pathlib.Path(vault, slug).mkdir(parents=True, exist_ok=True)
        pathlib.Path(vault, slug, "radar.yml").write_text(_radar(entries), encoding="utf-8")
        pathlib.Path(vault, slug, "meta.yml").write_text(
            yaml.safe_dump({"repo": slug.replace("-", "/", 1)}, allow_unicode=True), encoding="utf-8"
        )
    return client, vault


async def test_issues_aggregate_filter_paging(tmp_path):
    client, _ = await _make_client(tmp_path)

    with client:
        # 默认视图 = 机会组（open 且难度 ≤ 中等）：a/b#12、c/d#5；新的在前
        data = client.get("/api/issues").json()
        assert data["total"] == 2
        nums = [(it["repo"], it["number"]) for it in data["items"]]
        assert ("c/d", 5) == nums[0]  # created 09-24 比 #12 新
        assert data["stats"] == {"opportunity": 2, "hard": 1, "taken": 1, "closed": 1, "watched": 1}
        assert data["repos"] == ["a/b", "c/d"]

        # 分组筛选：被占 / 关闭
        taken = client.get("/api/issues", params={"group": "taken"}).json()
        assert [it["number"] for it in taken["items"]] == [18]
        closed = client.get("/api/issues", params={"group": "closed"}).json()
        assert closed["items"][0]["resolved_by"] == "PR#9"

        # 追踪中分组：只看点了「追踪」的（夹具里 a/b 追踪 #12）
        watched = client.get("/api/issues", params={"group": "watched"}).json()
        assert [(it["repo"], it["number"]) for it in watched["items"]] == [("a/b", 12)]
        assert all(it["watched"] for it in watched["items"])

        # 仓库 + 难度 + 关键词组合
        both = client.get("/api/issues", params={"group": "all", "repo": "a/b"}).json()
        assert both["total"] == 3
        easy = client.get("/api/issues", params={"group": "all", "difficulty": "简单"}).json()
        assert easy["total"] == 3
        hit = client.get("/api/issues", params={"group": "all", "q": "登录"}).json()
        assert [it["number"] for it in hit["items"]] == [18]

        # 分页
        paged = client.get("/api/issues", params={"group": "all", "page_size": 2, "page": 2}).json()
        assert paged["total"] == 5 and paged["pages"] == 3 and len(paged["items"]) == 2


async def test_issue_watch_toggle(tmp_path):
    client, vault = await _make_client(tmp_path)

    with client:
        # a/b 已追踪 #12 → 切换 = 取消
        resp = client.post("/api/issues/watch", json={"repo": "a/b", "number": 12}).json()
        assert resp["watched"] is False and resp["watch_issues"] == []
        # 再追踪 c/d#5（新增 → watched=True）
        resp = client.post("/api/issues/watch", json={"repo": "c/d", "number": 5}).json()
        assert resp["watched"] is True and resp["watch_issues"] == [5]
        cfg2 = client.app.state.svc.vault.read_config()
        cd = next(r for r in cfg2.repos if r.name == "c/d")
        assert cd.watch_issues == [5]

        # watched 统计与追踪标记
        data = client.get("/api/issues").json()
        assert data["stats"]["watched"] == 1
        assert next(it for it in data["items"] if it["number"] == 5)["watched"] is True

        # v4.4：配置存本地 data 目录，不入情报仓库——切换后仓库里没有 gitwire.yml
        import pathlib

        svc = client.app.state.svc
        assert svc.vault.config_path.exists()
        assert not (pathlib.Path(vault) / "gitwire.yml").exists()


async def test_unwatch_closed_setting_and_prune(tmp_path):
    """已关闭不追踪：默认关；开启时按 radar 缓存清掉现存已关闭的追踪，开放的保留。"""
    client, _ = await _make_client(tmp_path)

    with client:
        # 默认：继续追踪
        assert client.get("/api/repos").json()["unwatch_closed"] is False

        # c/d#3 在 radar.yml 里是 closed → 追踪后 watched 有两个（a/b#12 开放、c/d#3 已关闭）
        assert client.post("/api/issues/watch", json={"repo": "c/d", "number": 3}).json()["watched"]
        watched = client.get("/api/issues", params={"group": "watched"}).json()
        assert len(watched["items"]) == 2

        # 开启：立即清掉 c/d#3；a/b#12 仍开放不受影响
        resp = client.post(
            "/api/repos/config/unwatch-closed", json={"unwatch_closed": True}
        ).json()
        assert resp["changed"] is True and resp["pruned"] == ["c/d #3"]
        assert client.get("/api/repos").json()["unwatch_closed"] is True
        cfg = client.app.state.svc.vault.read_config()
        assert cfg.unwatch_closed is True
        assert cfg.watch_issues_for("c/d") == []
        assert cfg.watch_issues_for("a/b") == [12]
        watched = client.get("/api/issues", params={"group": "watched"}).json()
        assert [(it["repo"], it["number"]) for it in watched["items"]] == [("a/b", 12)]

        # 重复开启：幂等
        resp2 = client.post(
            "/api/repos/config/unwatch-closed", json={"unwatch_closed": True}
        ).json()
        assert resp2["changed"] is False and resp2["pruned"] == []

        # 关闭开关：恢复默认继续追踪，不动清单
        resp3 = client.post(
            "/api/repos/config/unwatch-closed", json={"unwatch_closed": False}
        ).json()
        assert resp3["changed"] is True and resp3["pruned"] == []
        assert client.app.state.svc.vault.read_config().unwatch_closed is False
