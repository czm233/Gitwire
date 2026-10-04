"""issue-radar v2 测试：强信号被占 / 口认领 / 机会池动态 / 关闭检测 / 追踪 / 游标。"""

import pytest
import yaml
from sqlmodel import Session, select

from app.engine.recipes import run_issue_radar, run_watchlist, scan_radar_pool
from app.github import GithubClient
from app.models import Alert
from app.vault import GitwireConfig, OpportunityConfig, ProjectMeta, RepoTarget
from tests.conftest import FakeBark, FakeGithub, RecipeFakeLLM, make_settings

pytestmark = pytest.mark.anyio


async def _make_svc(tmp_path):
    from app.db import init_db, make_engine
    from app.services import Services
    from app.vault import Vault

    settings = make_settings(tmp_path)
    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    vault = await Vault.open(settings)
    return Services(settings, engine, vault, FakeGithub(), RecipeFakeLLM(), FakeBark())


def _issue(n, **kw):
    return {
        "number": n,
        "title": f"issue {n}",
        "author": "u",
        "assignee": None,
        "labels": [],
        "comments": 0,
        "created_at": "2026-09-25T01:00:00Z",
        "body": "body",
        **kw,
    }


def _pr(n, refs_body=""):
    return {
        "number": n, "title": f"pr {n}", "author": "x",
        "created_at": "2026-09-25T02:00:00Z", "body": refs_body,
        "url": f"http://pr/{n}", "state": "open", "merged": False, "head": f"b{n}",
    }


def test_parse_issue_refs():
    """v4：只认强信号；裸 #N 不算被占。"""
    refs = GithubClient.parse_issue_refs({"title": "fix", "body": "closes #12 and fixes #34"})
    assert refs == [12, 34]
    assert GithubClient.parse_issue_refs({"title": "t", "body": "提到 #12 但没说要修"}) == []
    assert GithubClient.parse_issue_refs({"title": "Resolves: #56", "body": ""}) == [56]


async def _sweep(svc, issues, pulls):
    """跑一轮雷达并把状态落盘（模拟 publish）。"""
    r = await run_issue_radar(svc, "a/b", "a-b", issues, pulls)
    svc.vault.write_file("a-b/radar.yml", r.files["radar.yml"])
    return r


async def test_radar_full_sweep(tmp_path):
    """首扫：全部开放 issue 都有难度与状态；被 PR 占的不算机会；报告是机会榜格式。"""
    svc = await _make_svc(tmp_path)
    issues = [_issue(7), _issue(8), _issue(9, assignee="someone")]

    r = await _sweep(svc, issues, [_pr(31, "closes #7")])

    assert "issues.md" in r.files and "radar.yml" in r.files
    report = r.files["issues.md"]
    assert "机会榜" in report and "#8" in report and "#7" in report and "#9" in report
    assert "🔒PR占" in report            # #7 被 PR#31 强引用占
    assert "🔒认领" in report            # #9 有 assignee
    assert "🟢机会" in report            # #8 开放+简单 → 机会
    assert "<details>" in report          # 全量总表折叠

    kinds = [(a["kind"], a["title"]) for a in r.alerts]
    assert any(k == "opportunity" and "#8" in t for k, t in kinds)  # 只有 #8 是新机会
    assert not any("#7" in t or "#9" in t for _, t in kinds)        # 被占/困难不推

    state = yaml.safe_load(r.files["radar.yml"])
    assert state["issues"]["7"]["difficulty"] == "困难"
    assert state["issues"]["7"]["last_status"] == "taken-pr"
    assert state["issues"]["8"]["last_status"] == "open"


async def test_radar_bare_ref_not_taken(tmp_path):
    """v4：PR 正文裸写 #8（无 fixes/closes）不再判定被占。"""
    svc = await _make_svc(tmp_path)
    r = await _sweep(svc, [_issue(8)], [_pr(40, "背景见 #8")])
    assert "🔒PR占" not in r.files["issues.md"]
    assert "🟢机会" in r.files["issues.md"]


async def test_radar_state_persistence_and_taken_alert(tmp_path):
    """二扫不重复分析；上期机会被新 PR 占据 → issue-taken 警报；三扫零警报。"""
    svc = await _make_svc(tmp_path)

    await _sweep(svc, [_issue(7), _issue(8)], [])
    n_calls = len(svc.llm.calls)

    # 二扫：无新 issue，且有人给 #8 提了 PR（强引用）
    r2 = await _sweep(svc, [_issue(7), _issue(8)], [_pr(40, "fixes #8")])
    assert len(svc.llm.calls) == n_calls  # 难度缓存命中，零难度分析调用
    kinds = [a["kind"] for a in r2.alerts]
    assert "issue-taken" in kinds         # #8 上期机会 → 被占
    assert "opportunity" not in kinds     # 没有新机会
    assert "🔒PR占" in r2.files["issues.md"]

    # 三扫：状态不变 → 零警报
    r3 = await _sweep(svc, [_issue(7), _issue(8)], [_pr(40, "fixes #8")])
    assert r3.alerts == []


async def test_radar_claim_detection(tmp_path):
    """口认领：机会池 issue 新评论出现「I'd like to work on this」→ taken-claim + 警报。"""
    svc = await _make_svc(tmp_path)

    await _sweep(svc, [_issue(8, comments=2)], [])
    assert "issue-radar-claim" not in svc.llm.recipe_calls  # 首扫不做口认领

    # 二扫：评论 +1 且内容是认领声明
    svc.gh.comments[8] = [
        {"author": "dave", "created_at": "2026-09-25T03:00:00Z", "body": "I'd like to work on this"},
    ]
    r2 = await _sweep(svc, [_issue(8, comments=3)], [])
    assert "issue-radar-claim" in svc.llm.recipe_calls
    assert any(a["kind"] == "issue-taken" and "dave" in a["title"] for a in r2.alerts)
    state = yaml.safe_load(r2.files["radar.yml"])
    assert state["issues"]["8"]["last_status"] == "taken-claim"
    assert state["issues"]["8"]["claimed_by"] == "dave"
    assert "🙋口认领" in r2.files["issues.md"]


async def test_radar_taken_evidence_and_release(tmp_path):
    """被占依据落库（判定透明化）：PR 占存 PR 元数据、指派存人名、口认领存评论链接；
    被占解除后证据一并清除，不留过期依据。"""
    svc = await _make_svc(tmp_path)

    # 首扫：#8 被 PR#40 强引用占，#9 指派占，#10 开放待认领
    r1 = await _sweep(
        svc, [_issue(8), _issue(9, assignee="alice"), _issue(10, comments=2)],
        [_pr(40, "fixes #8")],
    )
    state = yaml.safe_load(r1.files["radar.yml"])
    assert state["issues"]["8"]["pr_claims"] == [40]
    assert state["issues"]["8"]["pr_evidence"][0] == {
        "number": 40, "url": "http://pr/40", "title": "pr 40",
        "author": "x", "created_at": "2026-09-25T02:00:00Z",
    }
    assert state["issues"]["9"]["assignee"] == "alice"

    # 二扫：#10 出现认领评论（带链接）→ 认领人 + 评论链接落库
    svc.gh.comments[10] = [
        {"author": "dave", "created_at": "2026-09-25T03:00:00Z",
         "html_url": "http://c/1", "body": "I'd like to work on this"},
    ]
    r2 = await _sweep(
        svc, [_issue(8), _issue(9, assignee="alice"), _issue(10, comments=3)],
        [_pr(40, "fixes #8")],
    )
    state = yaml.safe_load(r2.files["radar.yml"])
    assert state["issues"]["10"]["claimed_by"] == "dave"
    assert state["issues"]["10"]["claim_url"] == "http://c/1"

    # 三扫：PR 关闭、指派取消 → 回流机会池，证据清除
    r3 = await _sweep(svc, [_issue(8), _issue(9), _issue(10, comments=3)], [])
    state = yaml.safe_load(r3.files["radar.yml"])
    assert state["issues"]["8"]["last_status"] == "open"
    assert "pr_claims" not in state["issues"]["8"]
    assert "pr_evidence" not in state["issues"]["8"]
    assert state["issues"]["9"]["last_status"] == "open"
    assert "assignee" not in state["issues"]["9"]


async def test_radar_closed_detection(tmp_path):
    """机会 issue 被关闭：从开放列表消失 → 查详情 + 关闭 PR 强引用 → issue-closed + 记录谁解决。"""
    svc = await _make_svc(tmp_path)

    await _sweep(svc, [_issue(8)], [])

    # 二扫：#8 已关闭（被 PR#40 解决）
    svc.gh.issues = []
    svc.gh.issue_details[8] = {
        "number": 8, "title": "issue 8", "state": "closed", "state_reason": "completed",
        "author": "u", "assignee": None, "labels": [], "comments": 3,
        "created_at": "2026-09-25T01:00:00Z", "closed_at": "2026-09-26T01:00:00Z",
        "body": "", "html_url": "https://github.com/a/b/issues/8",
    }
    svc.gh.pulls = [{**_pr(40, "closes #8"), "state": "closed", "merged": True}]
    r2 = await _sweep(svc, [], [])

    kinds = [a["kind"] for a in r2.alerts]
    assert "issue-closed" in kinds
    assert any("PR#40" in a["title"] for a in r2.alerts)
    report = r2.files["issues.md"]
    assert "近期关闭" in report and "PR#40" in report
    state = yaml.safe_load(r2.files["radar.yml"])
    assert state["issues"]["8"]["last_status"] == "closed"
    assert state["issues"]["8"]["resolved_by"] == "PR#40"


async def test_radar_label_boost_and_surge(tmp_path):
    """池内动态：挂上 help wanted 标签 → 机会升温警报；评论激增 → 池内动态记录。"""
    svc = await _make_svc(tmp_path)

    await _sweep(svc, [_issue(8, comments=2)], [])

    # 二扫：新增 help wanted 标签 + 评论涨 5 条
    r2 = await _sweep(svc, [_issue(8, comments=7, labels=["help wanted"])], [])
    kinds = [a["kind"] for a in r2.alerts]
    assert "opportunity" in kinds           # 标签升温 → 机会警报
    report = r2.files["issues.md"]
    assert "池内动态" in report
    assert "help wanted" in report and "评论 +5" in report


async def test_scan_pool_dynamics_and_watch_trigger(tmp_path):
    """scan_radar_pool 检测池内动态；watch 轮询据此入队（不挂雷达的仓库 issue 不触发）。"""
    from app.engine.watch import watch_round
    from app.services import start_worker, stop_worker

    svc = await _make_svc(tmp_path)
    svc.vault.write_config(
        GitwireConfig(
            repos=[
                RepoTarget(name="a/b", recipes=["docs-sync", "issue-radar"]),
                RepoTarget(name="c/d"),
            ]
        )
    )
    svc.vault.save_meta(
        "a-b", ProjectMeta(repo="a/b", last_synced="a" * 40, last_issue_number=9, last_pr_number=40)
    )
    svc.vault.save_meta(
        "c-d", ProjectMeta(repo="c/d", last_synced="a" * 40, last_issue_number=9, last_pr_number=40)
    )
    svc.gh.sha = "a" * 40
    svc.gh.issues = [_issue(8, comments=2)]
    svc.gh.pulls = []

    # 无雷达快照 → 无动态 → 不入队
    scan = await scan_radar_pool(svc, "a/b", "a-b")
    assert scan.dynamics == []

    # 建立雷达快照（#8 简单、开放、2 条评论）
    await _sweep(svc, svc.gh.issues, [])
    scan = await scan_radar_pool(svc, "a/b", "a-b")
    assert scan.dynamics == []

    # 评论激增 → 动态检测命中 → watch 入队
    svc.gh.issues = [_issue(8, comments=9)]
    scan = await scan_radar_pool(svc, "a/b", "a-b")
    assert any("评论激增" in d for d in scan.dynamics)

    await start_worker(svc)
    try:
        svc.gh.issues = [_issue(8, comments=9)]
        assert await watch_round(svc) == ["a/b"]
        # c/d 没挂雷达：同样新评论数变化也不触发
    finally:
        await stop_worker(svc)


async def test_watchlist_recipe(tmp_path):
    """追踪：首查建基线不出警报；有新评论 → watch 警报 + watched.md；无动态零产出。"""
    svc = await _make_svc(tmp_path)
    svc.gh.issues = [_issue(8, comments=2, title="追踪我")]

    # 首查：只建基线
    r1 = await run_watchlist(svc, "a/b", "a-b", [8])
    assert r1.files and "watched.md" in r1.files and r1.alerts == []
    svc.vault.write_file("a-b/watch.yml", r1.files["watch.yml"])

    # 无动态：零产出（不产生空提交）
    r2 = await run_watchlist(svc, "a/b", "a-b", [8])
    assert r2.files == {} and r2.alerts == []

    # 新评论 1 条 → watch 警报 + 原文入档
    svc.gh.issues = [_issue(8, comments=3, title="追踪我")]
    svc.gh.comments[8] = [
        {"author": "eve", "created_at": "2026-09-25T04:00:00Z", "body": "这个问题还在吗？"},
    ]
    r3 = await run_watchlist(svc, "a/b", "a-b", [8])
    assert any(a["kind"] == "watch" and "eve" in a["title"] for a in r3.alerts)
    assert "这个问题还在吗？" in r3.files["watched.md"]
    svc.vault.write_file("a-b/watch.yml", r3.files["watch.yml"])

    # 状态翻转 → watch 警报
    svc.gh.issues = []
    svc.gh.issue_details[8] = {
        "number": 8, "title": "追踪我", "state": "closed", "state_reason": "completed",
        "author": "u", "assignee": None, "labels": [], "comments": 3,
        "created_at": "2026-09-25T01:00:00Z", "closed_at": "2026-09-26T01:00:00Z",
        "body": "", "html_url": "https://github.com/a/b/issues/8",
    }
    r4 = await run_watchlist(svc, "a/b", "a-b", [8])
    assert any(a["kind"] == "watch" and "closed" in a["title"] for a in r4.alerts)


def _closed_detail(n: int, title: str = "追踪我") -> dict:
    return {
        "number": n, "title": title, "state": "closed", "state_reason": "completed",
        "author": "u", "assignee": None, "labels": [], "comments": 2,
        "created_at": "2026-09-25T01:00:00Z", "closed_at": "2026-09-26T01:00:00Z",
        "body": "", "html_url": f"https://github.com/a/b/issues/{n}",
    }


async def test_watchlist_auto_unwatch_closed(tmp_path):
    """已关闭不追踪：开启后，关闭的追踪就地撤销（配置移除 + watched.md 留痕）；默认关。"""
    svc = await _make_svc(tmp_path)

    # 开启开关：开放 issue 建基线 → 关闭 → 警报标注自动撤销 + 配置移除
    svc.vault.write_config(
        GitwireConfig(repos=[RepoTarget(name="a/b", watch_issues=[8])], unwatch_closed=True)
    )
    svc.gh.issues = [_issue(8, comments=2, title="追踪我")]
    r1 = await run_watchlist(svc, "a/b", "a-b", [8])
    assert r1.alerts == []
    svc.vault.write_file("a-b/watch.yml", r1.files["watch.yml"])

    svc.gh.issues = []
    svc.gh.issue_details[8] = _closed_detail(8)
    r2 = await run_watchlist(svc, "a/b", "a-b", [8])
    assert any(a["kind"] == "watch" and "自动撤销" in a["title"] for a in r2.alerts)
    assert "自动撤销" in r2.files["watched.md"]
    assert svc.vault.read_config().watch_issues_for("a/b") == []

    # 存量：基线已是 closed 的追踪（如开关后开才生效），下轮同样撤销（首查不出警报）
    svc.vault.write_config(
        GitwireConfig(repos=[RepoTarget(name="a/b", watch_issues=[9])], unwatch_closed=True)
    )
    svc.vault.write_file("a-b/watch.yml", yaml.safe_dump({"issues": {}}, allow_unicode=True))
    svc.gh.issue_details[9] = _closed_detail(9)
    r3 = await run_watchlist(svc, "a/b", "a-b", [9])
    assert r3.alerts == []
    assert "自动撤销" in r3.files["watched.md"]
    assert svc.vault.read_config().watch_issues_for("a/b") == []

    # 默认（开关关）：关闭的 issue 继续追踪
    svc.vault.write_config(GitwireConfig(repos=[RepoTarget(name="a/b", watch_issues=[10])]))
    svc.vault.write_file("a-b/watch.yml", yaml.safe_dump({"issues": {}}, allow_unicode=True))
    svc.gh.issue_details[10] = _closed_detail(10)
    r4 = await run_watchlist(svc, "a/b", "a-b", [10])
    assert svc.vault.read_config().watch_issues_for("a/b") == [10]


async def test_radar_runner_chain(tmp_path):
    from app.engine.runner import sync_repo

    svc = await _make_svc(tmp_path)
    svc.vault.write_config(
        GitwireConfig(repos=[RepoTarget(name="a/b", recipes=["docs-sync", "issue-radar"])])
    )
    svc.vault.save_meta("a-b", ProjectMeta(repo="a/b", last_synced="a" * 40, last_issue_number=6, last_pr_number=30))
    svc.gh.sha = "a" * 40
    svc.gh.issues = [_issue(8)]
    svc.gh.pulls = []

    run = await sync_repo(svc, "a/b")
    assert run.status == "published"
    assert "issue-radar" in svc.llm.recipe_calls
    meta = svc.vault.meta("a-b")
    assert meta.last_issue_number == 8
    assert meta.last_synced == "a" * 40  # 代码游标不动
    assert svc.vault.read_file("a-b/issues.md")

    with Session(svc.engine) as s:
        kinds = [a.kind for a in s.exec(select(Alert)).all()]
    assert "opportunity" in kinds

    # 无新事件再跑（scheduled）→ noop；手动触发则会强制重扫雷达
    svc.llm.recipe_calls.clear()
    run2 = await sync_repo(svc, "a/b", "scheduled")
    assert run2.mode == "noop"


async def test_watchlist_runner_chain(tmp_path):
    """追踪动态单独即可成 run：无代码/release/issue 变化也发布 watched.md。"""
    from app.engine.runner import sync_repo

    svc = await _make_svc(tmp_path)
    svc.vault.write_config(
        GitwireConfig(repos=[RepoTarget(name="a/b", watch_issues=[8])])
    )
    svc.vault.save_meta("a-b", ProjectMeta(repo="a/b", last_synced="a" * 40))
    svc.gh.sha = "a" * 40
    svc.gh.release = None
    svc.gh.issues = [_issue(8, comments=2)]
    svc.gh.pulls = []

    # 首跑：建追踪基线（watched.md 发布）
    run = await sync_repo(svc, "a/b")
    assert run.status == "published"
    assert svc.vault.read_file("a-b/watched.md")

    # 新评论 → 追踪警报
    svc.gh.issues = [_issue(8, comments=4)]
    svc.gh.comments[8] = [
        {"author": "frank", "created_at": "2026-09-25T05:00:00Z", "body": "补个复现步骤"},
    ]
    run2 = await sync_repo(svc, "a/b")
    assert run2.status == "published" and run2.mode != "noop"
    with Session(svc.engine) as s:
        kinds = [a.kind for a in s.exec(select(Alert)).all()]
    assert "watch" in kinds


async def test_radar_opportunity_hard_filters(tmp_path):
    """v4.2 机会硬条件：超龄无动静/评论过热/仓库不收外部贡献 → 未入榜且不推机会警报。"""
    svc = await _make_svc(tmp_path)
    old = _issue(5, created_at="2026-07-01T00:00:00Z")  # 超龄且无 updated_at（无动静）
    hot = _issue(6, comments=30)  # 讨论过热
    ok = _issue(8)
    r = await _sweep(svc, [old, hot, ok], [])

    state = yaml.safe_load(r.files["radar.yml"])
    assert "无动静" in state["issues"]["5"]["excluded"]
    assert "过热" in state["issues"]["6"]["excluded"]
    assert state["issues"]["8"]["excluded"] == ""
    report = r.files["issues.md"]
    assert "未入榜" in report and "#5" in report and "#6" in report
    assert any(a["kind"] == "opportunity" and "#8" in a["title"] for a in r.alerts)
    assert not any("#5" in a["title"] or "#6" in a["title"] for a in r.alerts)

    # 仓库从不合并外部 PR → 全体未入榜
    svc.gh.closed_pulls = []
    r2 = await _sweep(svc, [_issue(8)], [])
    assert "外部 PR" in yaml.safe_load(r2.files["radar.yml"])["issues"]["8"]["excluded"]

    # 配置关闭该条件（0 = 不检查）→ 重新入榜
    svc.vault.write_config(
        GitwireConfig(
            repos=[RepoTarget(name="a/b")],
            opportunity=OpportunityConfig(external_merge_within_days=0),
        )
    )
    r3 = await _sweep(svc, [_issue(9)], [])
    assert yaml.safe_load(r3.files["radar.yml"])["issues"]["9"]["excluded"] == ""
