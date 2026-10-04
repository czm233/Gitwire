"""分析引擎：组装上下文 → LLM 按 docs-sync 配方出稿 → 解析与校验。

分层策略：
- init（首次建档）：获取指定提交（--depth=1，不检出、不执行），agent 通读源码
- incremental（增量）：GitHub compare API 拿 old...new 的 diff，快而便宜
"""

from __future__ import annotations

import asyncio
import fnmatch
import contextlib
import os
import shutil
import tempfile
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.vault import Vault, git, git_raw, slugify

SYSTEM_PROMPT = (
    "你是 Gitwire 的开源情报分析员，为开源项目维护长期情报档案。"
    "你的产出会被程序解析并原样写入情报仓库，必须严格遵守输出契约。"
)

FILE_BLOCK = re.compile(r"<<<FILE:(.+?)>>>\s*\n(.*?)(?=\n<<<|\Z)", re.S)
META_SUMMARY = re.compile(r"<<<META:summary>>>\s*(.+)")

# 上下文预算（字符）
TREE_CAP = 12_000
FILE_CHAR_CAP = 20_000
FIRST_TOTAL_CAP = 100_000
PATCH_PER_FILE_CAP = 6_000
INCREMENTAL_TOTAL_CAP = 60_000
DOCS_PER_FILE_CAP = 10_000
DOCS_TOTAL_CAP = 40_000

MANIFEST_PATTERNS = [
    "pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt",
    "package.json", "tsconfig.json", "vite.config.*", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle*", "Makefile", "Dockerfile", "docker-compose*.yml",
    "LICENSE*", ".env.example",
]
CODE_DIR_PREFIXES = (
    "src/", "lib/", "app/", "internal/", "cmd/", "core/", "pkg/",
    "server/", "backend/", "components/", "pages/", "api/",
)


class AnalyzeError(Exception):
    pass


@dataclass
class AnalyzeResult:
    files: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    mode: str = "incremental"


def parse_output(text: str) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    for m in FILE_BLOCK.finditer(text):
        path = m.group(1).strip()
        content = m.group(2).strip("\n")
        if path:
            files[path] = content
    summary = ""
    ms = META_SUMMARY.search(text)
    if ms:
        summary = ms.group(1).strip()
    return files, summary


def validate_output(mode: str, files: dict[str, str], changelog_path: str) -> list[str]:
    errors: list[str] = []
    if not files:
        errors.append("没有解析到任何 <<<FILE:path>>> 块")
        return errors
    if changelog_path not in files:
        errors.append(f"缺少必须输出的文件 {changelog_path}")
    if mode == "init":
        for req in ("README.md", "tech-stack.md", "architecture.md", "business-logic.md"):
            if req not in files:
                errors.append(f"首次建档缺少 {req}")
    for path, content in files.items():
        if path.startswith("/") or ".." in path.split("/") or path.startswith("\\"):
            errors.append(f"非法文件路径: {path}")
        if not content.strip():
            errors.append(f"{path} 内容为空")
        elif content.count("```") % 2 != 0:
            errors.append(f"{path} 中代码围栏不配对（``` 数量为奇数）")
    return errors


# ---------- 上下文采集 ----------


def _score_path(path: str) -> int:
    name = path.rsplit("/", 1)[-1]
    if name.startswith("README"):
        return 100
    for pat in MANIFEST_PATTERNS:
        if fnmatch.fnmatch(name, pat) and "/" not in path:
            return 90
    if path.startswith(CODE_DIR_PREFIXES):
        return 50
    if name.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".kt")):
        return 40
    return 10


def cleanup_repo_cache(settings, slug: str) -> None:
    """建档成功后删除仓库 clone 缓存：增量分析只走 diff API，用不到本地代码。
    监控 20 个仓库也不会在磁盘上堆积代码镜像。"""
    import shutil

    workdir = settings.data_path / "repos" / slug
    shutil.rmtree(workdir, ignore_errors=True)


def _cache_size(directory: Path, limit: int) -> int:
    total = 0
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                total += (Path(root) / name).lstat().st_size
                if total > limit:
                    return total
    return total


async def _clone_target_repo(settings, repo: str, slug: str, sha: str) -> Path | None:
    """Download just the requested snapshot into a task-owned bare directory.

    No checkout, hooks, submodules, LFS smudge, ambient credentials or 50-commit
    history. On limits/errors, delete only this task's directory and use the API.
    """
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or any(p in {'.','..'} for p in repo.split('/')):
        return None
    if not re.fullmatch(r'[0-9a-fA-F]{40,64}', sha):
        return None
    parent = settings.data_path / 'repos'
    parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=slugify(repo)[:80] + '-', dir=parent))
    env = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_LFS_SKIP_SMUDGE': '1'}
    if settings.github_token and not settings.public_source_only:
        import base64
        basic = base64.b64encode(f'x-access-token:{settings.github_token}'.encode()).decode()
        # Legacy private CLI only; never expose Authorization in argv or logs.
        env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                    'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + basic})
    args = ('-c', 'core.hooksPath=' + os.devnull, '-c', 'init.templateDir=',
            '-c', 'credential.helper=', '-c', 'protocol.file.allow=never', '-c', 'gc.auto=0')
    limit = settings.source_clone_max_mb * 1024 * 1024
    async def check_size():
        if await asyncio.to_thread(_cache_size, workdir, limit) > limit:
            from app.processes import ProcessLimitError
            raise ProcessLimitError('源码缓存超过采集上限')
    async def download():
        await git(workdir, *args, 'init', '--bare', env=env)
        await git(workdir, *args, 'fetch', '--depth=1', '--no-tags',
                  f'https://github.com/{repo}.git', sha, env=env,
                  timeout=settings.source_clone_timeout_seconds)
        await git(workdir, *args, 'update-ref', 'HEAD', sha, env=env)
        actual = await git(workdir, *args, 'rev-parse', 'HEAD', env=env)
        if actual.strip().lower() != sha.lower():
            raise AnalyzeError('源码提交与请求不一致')
        await check_size()
    async def watch_size():
        while True:
            await check_size()
            await asyncio.sleep(.25)
    task, guard = asyncio.create_task(download()), asyncio.create_task(watch_size())
    success = False
    try:
        async with asyncio.timeout(settings.source_clone_timeout_seconds):
            done, _ = await asyncio.wait([task, guard], return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                await guard
            await task
        success = True
        return workdir
    except Exception:
        return None
    finally:
        for pending in (task, guard):
            pending.cancel()
        await asyncio.gather(task, guard, return_exceptions=True)
        if not success:
            await asyncio.to_thread(shutil.rmtree, workdir, True)


def _build_first_context(tree_text: str, file_contents: dict[str, str]) -> str:
    parts = [f"# 仓库快照\n\n## 文件树（路径  字节）\n\n```\n{tree_text}\n```\n"]
    parts.append("## 关键文件内容\n")
    for path, content in file_contents.items():
        parts.append(f"### {path}\n\n```\n{content}\n```\n")
    return "\n".join(parts)


def _select_key_files(entries: list[tuple[str, int]]) -> list[str]:
    """按价值排序选文件：README/清单文件优先，其次核心代码目录。"""
    scored = sorted(
        entries, key=lambda e: (-_score_path(e[0]), len(e[0]))
    )
    picked: list[str] = []
    total = 0
    for path, size in scored:
        if len(picked) >= 40 or total >= FIRST_TOTAL_CAP:
            break
        if size > 300_000:  # 大文件（锁文件/数据）跳过
            continue
        if size > FILE_CHAR_CAP:
            picked.append(path)
            total += FILE_CHAR_CAP
        else:
            picked.append(path)
            total += max(size, 200)
    return picked


async def gather_first_context(svc, repo: str, sha: str) -> str:
    settings, gh = svc.settings, svc.gh
    slug = slugify(repo)
    workdir = await _clone_target_repo(settings, repo, slug, sha)

    if workdir is not None:
        try:
            ls = await git(workdir, "ls-tree", "-r", "--long", "HEAD", check=False)
            entries: list[tuple[str, int]] = []
            tree_lines: list[str] = []
            for line in ls.splitlines():
                m = re.match(r"^\d+ \w+ [0-9a-f]+\s+(\d+)\t(.+)$", line)
                if m:
                    size, path = int(m.group(1)), m.group(2)
                    entries.append((path, size))
                    tree_lines.append(f"{path}  {size}")
            tree_text = "\n".join(tree_lines)[:TREE_CAP]
            key_files = _select_key_files(entries)

            async def read_one(path: str) -> tuple[str, str]:
                content = await git(workdir, "show", f"HEAD:{path}", check=False)
                return path, content[:FILE_CHAR_CAP]

            async with asyncio.TaskGroup() as group:
                reads = [group.create_task(read_one(p)) for p in key_files]
            contents = dict(task.result() for task in reads)
            # 二次总量裁剪
            total = 0
            for p in list(contents):
                total += len(contents[p])
                if total > FIRST_TOTAL_CAP:
                    contents[p] = contents[p][: max(0, FIRST_TOTAL_CAP - (total - len(contents[p])))]
            return _build_first_context(tree_text, contents)
        except Exception:
            pass  # Bounded Git reads can also fail; fall back to the same SHA.
        finally:
            await asyncio.to_thread(shutil.rmtree, workdir, True)

    # API 兜底：文件树 + 清单/README
    paths = await gh.tree_paths(repo, ref=sha)
    entries = [(p, 0) for p in paths]
    tree_text = "\n".join(paths)[:TREE_CAP]
    contents: dict[str, str] = {}
    remaining = FIRST_TOTAL_CAP
    for p in _select_key_files(entries)[:12]:
        if remaining <= 0:
            break
        c = await gh.file_content(repo, p, ref=sha)
        if c:
            contents[p] = c[:min(FILE_CHAR_CAP, remaining)]
            remaining -= len(contents[p])
    if not contents:
        raise AnalyzeError('无法读取指定提交的源码，未生成推测性档案')
    return '采集说明：Git 快照获取未完成，以下仅为同一提交的 API 关键文件样本，不代表完整源码。\n\n' + _build_first_context(tree_text, contents)


def _build_incremental_context(compare: dict) -> str:
    commits = compare.get("commits", [])
    files = compare.get("files", [])
    lines = [f"# 本次变更\n\n## 提交列表（{len(commits)} 个）\n"]
    for c in commits:
        lines.append(f"- `{c['sha'][:7]}` {c['date'][:10]} {c['message']}")
    lines.append(f"\n## 文件变更（{len(files)} 个）\n")
    total = 0
    for f in files:
        header = (
            f"### {f['filename']} (+{f['additions']}/-{f['deletions']}, {f['status']})"
        )
        lines.append(header)
        patch = (f.get("patch") or "").strip()
        if not patch:
            lines.append("（无文本 patch，可能为二进制/大文件）\n")
            continue
        budget = INCREMENTAL_TOTAL_CAP - total
        if budget <= 0:
            lines.append("（后续 patch 因长度预算省略，参考提交列表）\n")
            continue
        patch = patch[: min(PATCH_PER_FILE_CAP, budget)]
        total += len(patch)
        lines.append(f"```diff\n{patch}\n```\n")
    return "\n".join(lines)


def _build_existing_docs(vault: Vault, slug: str) -> str:
    docs = ["# 现有档案（更新以此为基底，仍然正确的人工内容必须保留）\n"]
    total = 0
    for name in ("README.md", "tech-stack.md", "architecture.md", "business-logic.md", "tripwires.md"):
        content = vault.read_file(f"{slug}/{name}")
        if content:
            content = content[:DOCS_PER_FILE_CAP]
            total += len(content)
            docs.append(f"### 档案 {name}\n\n```\n{content}\n```\n")
            if total > DOCS_TOTAL_CAP:
                break
    if len(docs) == 1:
        return ""
    return "\n".join(docs)


def load_recipe(name: str = "docs-sync", filename: str = "prompt.md") -> str:
    path = Path(__file__).resolve().parents[1] / "recipes" / name / filename
    if not path.exists():
        raise AnalyzeError(f"配方文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def changelog_name(settings, new_sha: str) -> str:
    return f"changelog/{settings.local_date_str()}-{new_sha[:7]}.md"


def render(template: str, **vars: str) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", v)
    return out


async def analyze_repo(svc, repo: str, old_sha: str | None, new_sha: str) -> AnalyzeResult:
    settings = svc.settings
    vault = svc.vault
    slug = slugify(repo)
    mode = "incremental" if (old_sha and vault.has_project(slug)) else "init"
    clg = changelog_name(settings, new_sha)

    if mode == "init":
        context = await gather_first_context(svc, repo, new_sha)
        existing = ""
        mode_desc = "init（首次全量建档：仓库还没有档案，请通读源码快照建立全套档案）"
        required = (
            "必须输出：README.md、tech-stack.md、architecture.md、business-logic.md、"
            f"{clg}；可选：tripwires.md"
        )
    else:
        compare = await svc.gh.compare(repo, old_sha, new_sha)
        context = _build_incremental_context(compare)
        existing = _build_existing_docs(vault, slug)
        mode_desc = "incremental（增量更新：档案已存在，依据 diff 更新档案并撰写本次 changelog）"
        required = (
            f"必须输出：{clg}；其他档案文件仅当内容有实质变化时才输出（必须输出完整文件）"
        )

    template = load_recipe("docs-sync")
    old_desc = old_sha[:7] if old_sha else "∅（无游标）"

    prompt = render(
        template,
        REPO=repo,
        MODE_DESC=mode_desc,
        OLD_SHA=old_desc,
        NEW_SHA=new_sha[:7],
        DATE=settings.local_date_str(),
        CHANGELOG_PATH=clg,
        REQUIRED_FILES=required,
        CONTEXT=context,
        EXISTING_SECTION=existing,
    )

    last_errors: list[str] = []
    for attempt in range(2):
        final_prompt = prompt
        if last_errors:
            final_prompt += (
                "\n\n# 上一次输出被驳回，原因：\n"
                + "\n".join(f"- {e}" for e in last_errors)
                + "\n请严格按输出契约重新输出全部文件。"
            )
        out = await svc.llm.chat(SYSTEM_PROMPT, final_prompt)
        files, summary = parse_output(out)
        last_errors = validate_output(mode, files, clg)
        if not last_errors:
            return AnalyzeResult(files=files, summary=summary, mode=mode)
    raise AnalyzeError("LLM 输出两次未通过校验: " + "; ".join(last_errors[:5]))
