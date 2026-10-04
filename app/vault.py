"""vault 情报仓库：git 操作封装。

vault 只存情报产出（各项目档案 / meta.yml 游标 / daily 晨报）。
系统配置 gitwire.yml 存本地数据目录（data/，.gitignore 已忽略）——配置与产出分离，
配置操作永不触碰情报仓库的 git 历史。
git 就是版本管理本体；配了 remote 才会 push，本地模式代码完全一致。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

BOARD_START = "<!-- board:start -->"
BOARD_END = "<!-- board:end -->"
BOARD_HEADING = "# Gitwire 态势板"

GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


class VaultError(Exception):
    pass


async def _run(cwd: Path, *args: str, timeout=120, env=None, output_limit=8 * 1024 * 1024) -> tuple[int, str, str]:
    from app.processes import run_bounded
    return await run_bounded('git', *args, cwd=cwd, env={**GIT_ENV, **(env or {})},
                             timeout=timeout, output_limit=output_limit)


async def git(cwd: Path, *args: str, check: bool = True, **options) -> str:
    code, out, err = await _run(cwd, *args, **options)
    if check and code != 0:
        # Arguments/stderr can contain Authorization headers or credential URLs.
        raise VaultError(f"Git 操作失败（退出码 {code}），请检查仓库可访问性与本地配置")
    return out


async def git_raw(cwd: Path, *args: str) -> tuple[int, str, str]:
    """返回 (code, stdout, stderr)，不做失败断言。"""
    return await _run(cwd, *args)


def slugify(repo: str) -> str:
    return repo.replace("/", "-")


@dataclass
class ProjectMeta:
    repo: str
    last_synced: str | None = None
    last_mode: str | None = None
    summary: str | None = None
    updated_at: str | None = None
    # v2 触发源游标
    last_release_tag: str | None = None
    last_issue_number: int | None = None
    last_pr_number: int | None = None

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "last_synced": self.last_synced,
            "last_mode": self.last_mode,
            "summary": self.summary,
            "updated_at": self.updated_at,
            "last_release_tag": self.last_release_tag,
            "last_issue_number": self.last_issue_number,
            "last_pr_number": self.last_pr_number,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProjectMeta":
        def _int(v):
            return int(v) if v is not None and v != "" else None

        return cls(
            repo=d.get("repo", ""),
            last_synced=d.get("last_synced"),
            last_mode=d.get("last_mode"),
            summary=d.get("summary"),
            updated_at=d.get("updated_at"),
            last_release_tag=d.get("last_release_tag"),
            last_issue_number=_int(d.get("last_issue_number")),
            last_pr_number=_int(d.get("last_pr_number")),
        )


@dataclass
class OpportunityConfig:
    """机会榜硬条件（gitwire.yml 顶层 opportunity，repos[] 可覆盖）：
    不满足任一条 → 不入机会榜（归入未入榜组，原因随条目透出）。数值 0 = 关闭该条件。"""

    difficulties: list[str] = field(default_factory=lambda: ["简单", "中等"])
    max_age_days: int = 30  # 建于 N 天前且近 14 天无任何动静 → 未入榜
    max_comments: int = 15  # 评论超过 N 条（竞争过热）→ 未入榜
    repo_pushed_within_days: int = 30  # 仓库最近 N 天无提交 → 未入榜
    external_merge_within_days: int = 90  # 近 N 天无外部 PR 合并 → 未入榜

    @classmethod
    def from_dict(cls, d: dict | None) -> "OpportunityConfig":
        cfg = cls()
        if not isinstance(d, dict):
            return cfg
        if isinstance(d.get("difficulties"), list) and d["difficulties"]:
            cfg.difficulties = [str(x) for x in d["difficulties"]]
        for k in (
            "max_age_days",
            "max_comments",
            "repo_pushed_within_days",
            "external_merge_within_days",
        ):
            v = d.get(k)
            if isinstance(v, int) and v >= 0:
                setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> dict:
        return {
            "difficulties": list(self.difficulties),
            "max_age_days": self.max_age_days,
            "max_comments": self.max_comments,
            "repo_pushed_within_days": self.repo_pushed_within_days,
            "external_merge_within_days": self.external_merge_within_days,
        }


@dataclass
class RepoTarget:
    """监控目标：字符串简写（owner/name）或对象（含路径过滤与配方覆盖）。"""

    name: str
    recipes: list[str] | None = None  # None = 用全局默认
    watch_issues: list[int] = field(default_factory=list)  # v4：issue 追踪编号
    opportunity: OpportunityConfig | None = None  # 机会硬条件覆盖（None = 用全局）

    def to_dict(self) -> str | dict:
        if self.recipes is None and not self.watch_issues and self.opportunity is None:
            return self.name
        d: dict = {"name": self.name}
        if self.recipes is not None:
            d["recipes"] = self.recipes
        if self.watch_issues:
            d["watch_issues"] = self.watch_issues
        if self.opportunity:
            d["opportunity"] = self.opportunity.to_dict()
        return d


@dataclass
class GitwireConfig:
    repos: list[RepoTarget] = field(default_factory=list)
    recipes: list[str] = field(default_factory=lambda: ["docs-sync"])
    publish_mode: str = "direct"  # direct | pr
    unwatch_closed: bool = False  # 追踪的 issue 一旦关闭就自动撤销追踪（默认继续追踪）
    opportunity: OpportunityConfig = field(default_factory=OpportunityConfig)

    def target(self, repo: str) -> RepoTarget | None:
        return next((t for t in self.repos if t.name == repo), None)

    def opportunity_for(self, repo: str) -> OpportunityConfig:
        t = self.target(repo)
        return (t.opportunity if t and t.opportunity else None) or self.opportunity

    def recipes_for(self, repo: str) -> list[str]:
        t = self.target(repo)
        if t and t.recipes is not None:
            return t.recipes
        return self.recipes

    def watch_issues_for(self, repo: str) -> list[int]:
        t = self.target(repo)
        return t.watch_issues if t else []

    def to_dict(self) -> dict:
        return {
            "publish_mode": self.publish_mode,
            "recipes": self.recipes,
            "unwatch_closed": self.unwatch_closed,
            "opportunity": self.opportunity.to_dict(),
            "repos": [t.to_dict() for t in self.repos],
        }


class Vault:
    """vault 本地工作副本。local_path 模式直接在原地工作。"""

    def __init__(
        self,
        root: Path,
        remote: str = "",
        author: str = "",
        github_token: str = "",
        config_path: Path | None = None,
    ):
        self.root = root
        self.remote = remote
        self.author = author  # "Name <email>"，空则用本机 git config
        self.github_token = github_token  # GitHub https 远程的推送凭证（不落盘）
        # 系统配置在本地数据目录（配置与产出分离）；None 仅兼容旧的直接构造
        self.config_path = Path(config_path) if config_path else root / "gitwire.yml"

    def _gh_auth_args(self) -> list[str]:
        """github.com 的 https 远程：用 GITHUB_TOKEN 做请求头认证，
        不改 remote URL、不写 credential store，token 不落盘。"""
        if not (self.github_token and self.remote.startswith("https://github.com/")):
            return []
        import base64

        basic = base64.b64encode(
            f"x-access-token:{self.github_token}".encode()
        ).decode()
        return [
            "-c",
            f"http.https://github.com/.extraheader=Authorization: Basic {basic}",
        ]

    # ---------- 打开 / 同步 ----------

    @classmethod
    async def open(cls, settings) -> "Vault":
        if settings.vault_is_url:
            root = settings.data_path / "vault"
            root.parent.mkdir(parents=True, exist_ok=True)
            vault = cls(
                root, remote=settings.gitwire_vault, author=settings.git_author,
                github_token=settings.github_token,
                config_path=settings.data_path / "gitwire.yml",
            )
            if not (root / ".git").exists():
                await git(
                    root.parent,
                    *vault._gh_auth_args(),
                    "clone",
                    settings.gitwire_vault,
                    root.name,
                )
        else:
            if not settings.gitwire_vault:
                raise VaultError("GITWIRE_VAULT 未配置")
            root = Path(settings.gitwire_vault).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            if not (root / ".git").exists():
                await git(root, "init", "-b", "main")
            remote = ""
            remotes = await git(root, "remote", "-v", check=False)
            for line in remotes.splitlines():
                if line.startswith("origin"):
                    remote = line.split()[1]
                    break
            vault = cls(
                root, remote=remote, author=settings.git_author,
                github_token=settings.github_token,
                config_path=settings.data_path / "gitwire.yml",
            )
        await vault.migrate_config_out_of_repo()
        vault.ensure_skeleton()
        return vault

    def ensure_skeleton(self) -> None:
        """补齐本地配置文件与 vault 骨架目录；已存在则不动。"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.config_path.write_text(
                yaml.safe_dump(GitwireConfig().to_dict(), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        (self.root / "daily").mkdir(exist_ok=True)

    async def migrate_config_out_of_repo(self) -> str:
        """一次性迁移：把误入情报仓库的 gitwire.yml 搬到本地配置路径并从仓库删除。

        旧架构把配置存进 vault（配置与产出混淆，UI 配置操作被绑上 git push）。
        幂等：仓库里没有 gitwire.yml 时直接返回；删除产生一条提交，随常规节奏推送。"""
        old = self.root / "gitwire.yml"
        if not old.exists():
            return ""
        moved = ""
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
            moved = "moved"
        try:
            code, _, _ = await _run(self.root, "rm", "--quiet", "--", "gitwire.yml")
        except Exception:  # noqa: BLE001
            code = -1
        if code != 0:
            old.unlink(missing_ok=True)  # 未跟踪 / 无 git：直接删工作树文件
            return moved
        try:
            args = await self._author_args()
            await git(self.root, *args, "commit", "-m", "[gitwire] 配置移出仓库（gitwire.yml 不再入库）")
        except VaultError:
            pass  # commit 失败容忍：工作树已删，下轮 pull/push 自行收敛
        return moved

    async def pull(self) -> None:
        """发布模式下同步远端；本地模式跳过。失败容忍（下轮再试）。"""
        if not self.remote:
            return
        await git(self.root, *self._gh_auth_args(), "pull", "--ff-only", check=False)

    # ---------- gitwire.yml ----------

    def read_config(self) -> GitwireConfig:
        path = self.config_path
        if not path.exists():
            return GitwireConfig()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        repos: list[RepoTarget] = []
        for entry in data.get("repos") or []:
            if isinstance(entry, str):
                repos.append(RepoTarget(name=entry))
            elif isinstance(entry, dict) and entry.get("name"):
                watch = []
                for v in entry.get("watch_issues") or []:
                    try:
                        n = int(v)
                    except (TypeError, ValueError):
                        continue
                    if n > 0:
                        watch.append(n)
                repos.append(
                    RepoTarget(
                        name=entry["name"],
                        recipes=list(entry.get("recipes")) if entry.get("recipes") is not None else None,
                        watch_issues=sorted(set(watch)),
                        opportunity=OpportunityConfig.from_dict(entry.get("opportunity"))
                        if entry.get("opportunity")
                        else None,
                    )
                )
        return GitwireConfig(
            repos=repos,
            recipes=list(data.get("recipes") or ["docs-sync"]),
            publish_mode=str(data.get("publish_mode") or "direct"),
            unwatch_closed=bool(data.get("unwatch_closed")),
            opportunity=OpportunityConfig.from_dict(data.get("opportunity")),
        )

    def write_config(self, cfg: GitwireConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            yaml.safe_dump(cfg.to_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def repo_names(self) -> list[str]:
        return [t.name for t in self.read_config().repos]

    # ---------- 项目与文件 ----------

    def projects(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and (p / "meta.yml").exists() and p.name != "daily"
        )

    def has_project(self, slug: str) -> bool:
        return (self.root / slug / "meta.yml").exists()

    def meta(self, slug: str) -> ProjectMeta | None:
        path = self.root / slug / "meta.yml"
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ProjectMeta.from_dict(data)

    def save_meta(self, slug: str, meta: ProjectMeta) -> None:
        d = self.root / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.yml").write_text(
            yaml.safe_dump(meta.to_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _safe_path(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()):
            raise VaultError(f"非法路径: {rel}")
        return p

    def read_file(self, rel: str) -> str | None:
        p = self._safe_path(rel)
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8", errors="replace")

    def write_file(self, rel: str, content: str) -> None:
        p = self._safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def list_files(self, slug: str) -> list[dict]:
        """项目目录下的档案文件清单（相对路径 + 大小）。"""
        base = self.root / slug
        if not base.is_dir():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                out.append(
                    {"path": p.relative_to(base).as_posix(), "size": p.stat().st_size}
                )
        return out

    # ---------- commit / push ----------

    async def _author_args(self) -> list[str]:
        if self.author:
            m = re.match(r"^(.*?)\s*<(.+?)>$", self.author)
            if m:
                return [
                    "-c", f"user.name={m.group(1)}",
                    "-c", f"user.email={m.group(2)}",
                ]
        return []

    async def commit(self, paths: list[str], message: str) -> str:
        """只提交本次写过的路径；失败时恢复这些路径，不碰手工内容。"""
        try:
            await git(self.root, "add", "--", *paths)
            args = await self._author_args()
            await git(self.root, *args, "commit", "-m", message)
            return (await git(self.root, "rev-parse", "HEAD")).strip()
        except VaultError:
            await git(self.root, "restore", "--staged", "--", *paths, check=False)
            await git(self.root, "checkout", "--", *paths, check=False)
            raise

    async def branch(self) -> str:
        out = await git(self.root, "symbolic-ref", "--short", "HEAD", check=False)
        return out.strip() or "main"

    async def push(self) -> tuple[bool, str]:
        """容忍失败：push 不成只记日志，commit 已在本地。"""
        if not self.remote:
            return True, ""
        b = await self.branch()
        code, _, err = await _run(
            self.root, *self._gh_auth_args(), "push", "-u", "origin", b
        )
        if code != 0:
            return False, err.strip()[:300] or "push 失败"
        return True, ""

    async def ahead_count(self) -> int:
        if not self.remote:
            return 0
        code, out, _ = await _run(
            self.root, "rev-list", "--count", "@{upstream}..HEAD"
        )
        out = out.strip()
        return int(out) if code == 0 and out.isdigit() else 0

    async def push_pending(self) -> bool:
        """watch 前补 push 上一轮没推上去的提交。"""
        if not self.remote:
            return True
        if await self.ahead_count() == 0:
            return True
        ok, _ = await self.push()
        return ok

    # ---------- PR 模式分支操作（v2） ----------

    def remote_repo_slug(self) -> str:
        """从 remote URL 解析 owner/name；非 GitHub https 返回空。"""
        import re

        m = re.search(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?$", self.remote)
        return m.group(1) if m else ""

    async def fetch(self) -> None:
        if self.remote:
            await git(self.root, *self._gh_auth_args(), "fetch", "origin", check=False)

    async def checkout_branch(self, branch: str, base: str = "origin/main") -> None:
        await git(self.root, "checkout", "-B", branch, base)

    async def checkout_default(self, base: str) -> None:
        """从 PR 分支切回默认分支。base 必须显式传入：branch() 在分支上返回的是分支名。"""
        await git(self.root, "checkout", base, check=False)
        if self.remote:
            await git(self.root, *self._gh_auth_args(), "pull", "--ff-only", check=False)

    async def push_branch(self, branch: str) -> tuple[bool, str]:
        code, _, err = await _run(
            self.root, *self._gh_auth_args(), "push", "-u", "origin", branch
        )
        return (code == 0, err.strip()[:300] if code else "")

    async def clean_untracked(self, paths: list[str]) -> None:
        """PR 分支切回 main 后，清掉分支带过来的残留：
        未跟踪的文件删除；已跟踪但被分支版本覆盖的恢复为 main 版本。"""
        for p in paths:
            code, _, _ = await _run(self.root, "ls-files", "--error-unmatch", p)
            if code != 0:
                target = self._safe_path(p)
                if target.is_file():
                    target.unlink()
            else:
                await git(self.root, "checkout", "--", p, check=False)

    # ---------- 态势板 ----------

    def rewrite_board(self, stats_line: str, rows: list[dict]) -> None:
        """重写 README 中锚点区块；无锚点时接管「# Gitwire 态势板」标题到文件尾；
        连标题都没有则追加。锚点外的手工内容一律不动。
        rows: [{slug, mode, status, sha7, summary}]
        """
        readme = self.root / "README.md"
        text = readme.read_text(encoding="utf-8") if readme.exists() else ""
        table_lines = [
            "| 项目 | 本轮 | 状态 | 同步到 | 摘要 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in rows:
            table_lines.append(
                f"| [{r['slug']}](./{r['slug']}) | {r.get('mode', '—')} "
                f"| {r.get('status', '—')} | {r.get('sha7', '—')} "
                f"| {(r.get('summary') or '—').replace('|', '/')} |"
            )
        block = (
            f"{BOARD_START}\n\n{stats_line}\n\n"
            + "\n".join(table_lines)
            + f"\n\n{BOARD_END}"
        )

        if BOARD_START in text and BOARD_END in text:
            text = re.sub(
                re.escape(BOARD_START) + r".*?" + re.escape(BOARD_END),
                lambda _: block,
                text,
                flags=re.S,
            )
        elif BOARD_HEADING in text:
            head = text.split(BOARD_HEADING, 1)[0].rstrip("\n")
            text = f"{head}\n\n{BOARD_HEADING}\n\n{block}\n"
        else:
            text = text.rstrip("\n") + f"\n\n---\n\n{BOARD_HEADING}\n\n{block}\n"
        readme.write_text(text, encoding="utf-8")

    # ---------- 时间线（全部来自 vault 自己的 git 历史） ----------

    async def timeline(self, slug: str, limit: int = 100) -> list[dict]:
        """项目目录的版本节点：每次同步（或手工编辑）一个。"""
        if not (self.root / slug).is_dir():
            return []
        # %x00 = NUL 字段分隔，避免与内容冲突
        fmt = "%H%x00%ad%x00%s"
        out = await git(
            self.root,
            "log",
            f"--pretty=format:{fmt}",
            "--date=iso-strict",
            "--numstat",
            "-n",
            str(limit),
            "--",
            slug,
            check=False,
        )
        entries: list[dict] = []
        cur: dict | None = None
        for line in out.split("\n"):
            if not line.strip("\x00").strip():
                continue
            if "\x00" in line:
                sha, date, subject = (line.split("\x00") + ["", ""])[:3]
                cur = {"sha": sha, "date": date, "subject": subject, "files": []}
                entries.append(cur)
            elif cur is not None:
                parts = line.split("\t")
                if len(parts) == 3:
                    add, dele, path = parts
                    cur["files"].append({"path": path, "add": add, "del": dele})
        return entries

    async def version_diff(self, slug: str, sha: str) -> list[dict]:
        """单个版本中该项目目录的逐文件 patch。"""
        if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
            raise VaultError("非法 sha")
        code = (await _run(self.root, "rev-parse", "--verify", "--quiet", sha + "^"))[0]
        if code == 0:
            patch = await git(
                self.root, "diff", sha + "^", sha, "--", slug, check=False
            )
        else:  # 根提交
            patch = await git(
                self.root, "show", "--format=", "--unified=3", sha, "--", slug,
                check=False,
            )
        return self._split_patch(patch)

    @staticmethod
    def _split_patch(patch_text: str) -> list[dict]:
        files: list[dict] = []
        cur: dict | None = None
        for line in patch_text.splitlines():
            if line.startswith("diff --git "):
                if cur:
                    files.append(cur)
                m = re.search(r"diff --git a/(.+?) b/(.+)$", line)
                cur = {"path": (m.group(2) if m else line), "patch": ""}
            elif cur is not None:
                cur["patch"] += line + "\n"
        if cur:
            files.append(cur)
        return files

    # ---------- 晨报 ----------

    def daily_list(self) -> list[str]:
        d = self.root / "daily"
        if not d.is_dir():
            return []
        return sorted(
            (p.stem for p in d.glob("*.md") if p.name != "README.md"), reverse=True
        )

    def daily_read(self, date: str) -> str | None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return None
        return self.read_file(f"daily/{date}.md")
