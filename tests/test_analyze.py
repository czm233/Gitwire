"""分析引擎测试：输出解析、校验、分层上下文、重试。"""

import pytest

from app.engine.analyze import (
    AnalyzeError,
    changelog_name,
    parse_output,
    validate_output,
)
from tests.conftest import DocsSyncFakeLLM, FakeGithub, make_settings

GOOD = """<<<FILE:README.md>>>
# 档案
<<<FILE:changelog/2026-09-24-abc1234.md>>>
# 变更
<<<META:summary>>>一句话摘要
"""


def test_parse_output():
    files, summary = parse_output(GOOD)
    assert files["README.md"] == "# 档案"
    assert files["changelog/2026-09-24-abc1234.md"] == "# 变更"
    assert summary == "一句话摘要"


def test_parse_garbage():
    files, summary = parse_output("模型自由发挥没有块")
    assert files == {}
    assert summary == ""


def test_validate_incremental():
    files, _ = parse_output(GOOD)
    assert validate_output("incremental", files, "changelog/2026-09-24-abc1234.md") == []
    errs = validate_output("incremental", files, "changelog/2026-09-24-other.md")
    assert any("other.md" in e for e in errs)


def test_validate_init_requires_dossier():
    files, _ = parse_output(GOOD)
    errs = validate_output("init", files, "changelog/2026-09-24-abc1234.md")
    assert any("tech-stack.md" in e for e in errs)
    assert any("architecture.md" in e for e in errs)


def test_validate_unbalanced_fences():
    files = {"README.md": "```mermaid\ngraph TD\nA-->B"}
    errs = validate_output("incremental", files, "README.md")
    assert any("围栏" in e for e in errs)


def test_validate_illegal_path():
    files = {"../evil.md": "x", "changelog/ok.md": "y"}
    errs = validate_output("incremental", files, "changelog/ok.md")
    assert any("非法" in e for e in errs)


async def _make_svc(tmp_path, llm):
    from app.db import init_db, make_engine
    from app.services import Services

    settings = make_settings(tmp_path)
    engine = make_engine(settings.data_path / "gitwire.db")
    init_db(engine, settings.gitwire_vault)
    from app.vault import Vault

    vault = await Vault.open(settings)
    return Services(settings, engine, vault, FakeGithub(), llm), settings


async def test_analyze_incremental_ok(tmp_path):
    from app.engine.analyze import analyze_repo
    from app.vault import ProjectMeta

    svc, settings = await _make_svc(tmp_path, DocsSyncFakeLLM())
    svc.vault.save_meta("a-b", ProjectMeta(repo="a/b", last_synced="a" * 40))
    svc.vault.write_file("a-b/tech-stack.md", "| 旧 |\n|---|\n")

    new_sha = "c" * 40
    result = await analyze_repo(svc, "a/b", "a" * 40, new_sha)
    assert result.mode == "incremental"
    clg = changelog_name(settings, new_sha)
    assert clg in result.files
    assert result.summary


async def test_analyze_retry_recovers(tmp_path):
    from app.engine.analyze import analyze_repo
    from app.vault import ProjectMeta

    llm = DocsSyncFakeLLM(fail_first=True)
    svc, _ = await _make_svc(tmp_path, llm)
    svc.vault.save_meta("a-b", ProjectMeta(repo="a/b", last_synced="a" * 40))
    result = await analyze_repo(svc, "a/b", "a" * 40, "c" * 40)
    assert result.mode == "incremental"
    assert len(llm.calls) == 2  # 第一次违规被驳回，第二次通过


async def test_analyze_init_mode(tmp_path):
    """无档案 → init 模式；clone 失败自动走 API 兜底。"""
    from app.engine.analyze import analyze_repo

    svc, settings = await _make_svc(tmp_path, DocsSyncFakeLLM())
    result = await analyze_repo(svc, "a/b", None, "c" * 40)
    assert result.mode == "init"
    for req in ("README.md", "tech-stack.md", "architecture.md", "business-logic.md"):
        assert req in result.files


async def test_source_snapshot_is_exact_bare_and_removed_after_read(tmp_path, monkeypatch):
    from app.engine import analyze
    from app.vault import git as real_git
    from types import SimpleNamespace
    from conftest import make_settings
    origin=tmp_path/'origin';origin.mkdir()
    await real_git(origin,'init','-b','main')
    await real_git(origin,'config','user.name','Test')
    await real_git(origin,'config','user.email','test@example.test')
    (origin/'README.md').write_text('OLD SNAPSHOT')
    await real_git(origin,'add','README.md');await real_git(origin,'commit','-m','old')
    sha=(await real_git(origin,'rev-parse','HEAD')).strip()
    (origin/'README.md').write_text('NEWER CONTENT MUST NOT APPEAR')
    await real_git(origin,'add','README.md');await real_git(origin,'commit','-m','new')
    settings=make_settings(tmp_path)
    settings.public_source_only=True;settings.github_token='TEST_SECRET_MARKER'
    calls=[]
    async def local_git(cwd,*args,**opts):
        calls.append((args,opts))
        if 'fetch' in args:
            assert '--depth=1' in args and '--no-tags' in args
            assert 'GIT_CONFIG_VALUE_0' not in opts['env']
            pos=args.index('fetch')
            args=(*args[:pos],'-c','protocol.file.allow=always',*args[pos:])
            args=tuple(str(origin) if x=='https://github.com/public/project.git' else x for x in args)
        return await real_git(cwd,*args,**opts)
    monkeypatch.setattr(analyze,'git',local_git)
    result=await analyze.gather_first_context(SimpleNamespace(settings=settings,gh=None),'public/project',sha)
    assert 'OLD SNAPSHOT' in result and 'NEWER CONTENT' not in result
    assert not list((settings.data_path/'repos').iterdir())
    assert all('checkout' not in args and 'TEST_SECRET_MARKER' not in repr(args) for args,_ in calls)


async def test_source_limits_and_cancellation_cleanup_only_owned_temp(tmp_path, monkeypatch):
    import asyncio
    from app.engine import analyze
    from conftest import make_settings
    settings=make_settings(tmp_path);settings.source_clone_max_mb=1;settings.source_clone_timeout_seconds=1
    parent=settings.data_path/'repos';parent.mkdir(parents=True)
    sentinel=parent/'other-task';sentinel.mkdir();(sentinel/'keep').write_text('keep')
    cancelled=[];started=asyncio.Event(); mode='size'
    async def fake_git(cwd,*args,**kwargs):
        if 'fetch' in args:
            started.set()
            try:
                if mode=='size': (cwd/'pack').write_bytes(b'x'*(2*1024*1024))
                await asyncio.sleep(60)
            finally: cancelled.append(True)
        return ''
    monkeypatch.setattr(analyze,'git',fake_git)
    for current in ('size','timeout','cancel'):
        mode=current;started.clear()
        task=asyncio.create_task(analyze._clone_target_repo(settings,'public/project','public-project','a'*40))
        await started.wait()
        if mode=='cancel':
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            assert await task is None
        assert list(parent.iterdir())==[sentinel]
    assert len(cancelled)==3 and (sentinel/'keep').read_text()=='keep'


async def test_api_fallback_pins_revision_and_caps_combined_context(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.engine import analyze
    from conftest import make_settings
    async def no_clone(*args): return None
    sha='b'*40
    class API:
        async def tree_paths(self, repo, ref):
            assert ref==sha
            return [f'src/file{i}.py' for i in range(20)]
        async def file_content(self, repo, path, ref):
            assert ref==sha
            return 'CONTENT_MARKER'+('x'*30000)
    monkeypatch.setattr(analyze,'_clone_target_repo',no_clone)
    result=await analyze.gather_first_context(SimpleNamespace(settings=make_settings(tmp_path),gh=API()),'public/project',sha)
    assert result.count('CONTENT_MARKER')==5
    assert '不代表完整源码' in result and len(result)<102000
