"""vault 单元测试：配置往返、态势板锚点重写、commit/时间线、路径安全。"""

import pytest

from app.vault import (
    BOARD_END,
    BOARD_HEADING,
    BOARD_START,
    GitwireConfig,
    ProjectMeta,
    RepoTarget,
    VaultError,
)

async def test_config_roundtrip(vault):
    cfg = GitwireConfig(
        repos=[
            RepoTarget(name="a/b"),
            RepoTarget(name="c/d", recipes=["docs-sync", "feature-tripwire"]),
        ],
        recipes=["docs-sync"],
        publish_mode="pr",
        unwatch_closed=True,
    )
    vault.write_config(cfg)
    loaded = vault.read_config()
    assert [t.name for t in loaded.repos] == ["a/b", "c/d"]
    assert loaded.recipes_for("a/b") == ["docs-sync"]
    assert loaded.recipes_for("c/d") == ["docs-sync", "feature-tripwire"]
    assert loaded.publish_mode == "pr"
    assert loaded.unwatch_closed is True
    # 字符串简写往返：无附加配置的 target 写回应是纯字符串
    dumped = loaded.to_dict()
    assert dumped["repos"][0] == "a/b"
    assert isinstance(dumped["repos"][1], dict)
    assert dumped["unwatch_closed"] is True


async def test_meta_roundtrip(vault):
    meta = ProjectMeta(repo="a/b", last_synced="abc1234", last_mode="init", summary="s")
    vault.save_meta("a-b", meta)
    loaded = vault.meta("a-b")
    assert loaded.last_synced == "abc1234"
    assert loaded.summary == "s"
    assert vault.has_project("a-b")
    assert vault.projects() == ["a-b"]


async def test_board_rewrite_preserves_manual_content(vault):
    readme = vault.root / "README.md"
    readme.write_text(
        "# 我的情报库\n\n手工写的说明，不许动。\n\n---\n\n"
        f"# Gitwire 态势板\n\n旧内容\n\n{BOARD_START}\n旧的表格\n{BOARD_END}\n尾部注释\n",
        encoding="utf-8",
    )
    vault.rewrite_board(
        "- 最近一轮同步：2026-09-24；1 个监控目标，1 个已发布",
        [{"slug": "a-b", "mode": "incremental", "status": "已发布", "sha7": "abc1234", "summary": "修了个 bug"}],
    )
    text = readme.read_text(encoding="utf-8")
    assert "手工写的说明，不许动。" in text
    assert "尾部注释" in text
    assert "旧的表格" not in text
    assert "[a-b](./a-b)" in text
    assert "abc1234" in text
    assert "修了个 bug" in text
    assert BOARD_START in text and BOARD_END in text


async def test_board_rewrite_legacy_heading(vault):
    """真实 vault 的 README：有态势板标题但没有锚点 → 接管标题到文件尾。"""
    (vault.root / "README.md").write_text(
        "# Gitwire Vault\n\n> 介绍\n\n---\n\n# Gitwire 态势板\n\n- 旧的统计\n\n| 旧表 |\n",
        encoding="utf-8",
    )
    vault.rewrite_board("- 新统计", [{"slug": "x-y", "mode": "init", "status": "已发布", "sha7": "fff0000", "summary": "建档"}])
    text = (vault.root / "README.md").read_text(encoding="utf-8")
    assert "> 介绍" in text
    assert "旧表" not in text
    assert BOARD_START in text
    assert text.index(BOARD_START) > text.index(BOARD_HEADING)


async def test_board_rewrite_append(vault):
    (vault.root / "README.md").write_text("# 只有介绍\n", encoding="utf-8")
    vault.rewrite_board("- s", [])
    text = (vault.root / "README.md").read_text(encoding="utf-8")
    assert "# 只有介绍" in text and BOARD_START in text


async def test_commit_and_timeline(vault):
    vault.write_file("a-b/README.md", "# v1\n")
    vault.save_meta("a-b", ProjectMeta(repo="a/b", last_synced="a" * 40))
    sha1 = await vault.commit(["a-b/README.md", "a-b/meta.yml"], "[a/b] docs-sync: 建档")
    vault.write_file("a-b/README.md", "# v2\n")
    sha2 = await vault.commit(["a-b/README.md"], "[a/b] docs-sync: 增量")

    entries = await vault.timeline("a-b")
    assert len(entries) == 2
    assert entries[0]["sha"].startswith(sha2[:7]) or entries[0]["sha"] == sha2
    assert any("README.md" in f["path"] for f in entries[0]["files"])

    diffs = await vault.version_diff("a-b", sha2)
    assert diffs and "README.md" in diffs[0]["path"]
    assert "+# v2" in diffs[0]["patch"]
    assert "-# v1" in diffs[0]["patch"]


async def test_safe_path_rejects_escape(vault):
    with pytest.raises(VaultError):
        vault.write_file("../outside.md", "evil")
    with pytest.raises(VaultError):
        vault.read_file("a-b/../../outside.md")


async def test_daily_dir(vault):
    vault.write_file("daily/2026-09-24.md", "# 晨报\n")
    assert vault.daily_list() == ["2026-09-24"]
    assert vault.daily_read("2026-09-24") == "# 晨报\n"
    assert vault.daily_read(" ../../etc") is None
