"""测试公共设施：临时 settings / vault / 假 GitHub / 假 LLM。"""

from __future__ import annotations

import re

import pytest

from app.config import Settings
from app.vault import Vault

AUTHOR = "Gitwire Test <test@gitwire.local>"


def make_settings(tmp_path, vault_dir=None) -> Settings:
    return Settings(
        _env_file=None,
        hosted_enabled=False,
        gitwire_vault=str(vault_dir or (tmp_path / "vault")),
        data_dir=str(tmp_path / "data"),
        llm_model="fake-model",
        git_author=AUTHOR,
    )


@pytest.fixture
async def vault(tmp_path):
    return await Vault.open(make_settings(tmp_path))


class FakeGithub:
    """假 GitHub：sha 可变，compare 返回固定 diff，release/issue 可注入。"""

    def __init__(self, sha: str = "a" * 40):
        self.sha = sha
        self.default = "main"
        self.release: dict | None = None  # 注入 {"tag": "v1", ...} 模拟 release
        self.issues: list[dict] = []  # 注入新 issue
        self.pulls: list[dict] = []
        self.created_prs: list[dict] = []
        self.issue_details: dict[int, dict] = {}  # 覆盖详情（模拟已关闭等）
        self.comments: dict[int, list[dict]] = {}  # {编号: [{author, created_at, body}]}
        self.pushed_at = "2026-09-25T00:00:00Z"  # 仓库健康（近期有 push）
        # 近期已合并的外部 PR（机会硬条件：仓库收不收外人贡献）；置 [] 模拟不收
        self.closed_pulls = [
            {
                "number": 99, "title": "community fix", "author": "ext",
                "created_at": "2026-09-20T00:00:00Z", "url": "http://pr/99",
                "state": "closed", "merged": True, "head": "ext", "body": "",
                "merged_at": "2026-09-21T00:00:00Z", "author_association": "CONTRIBUTOR",
            }
        ]

    async def exists(self, repo: str) -> bool:
        return True

    async def repo_info(self, repo: str) -> dict:
        return {"default_branch": "main", "stargazers_count": 352, "pushed_at": self.pushed_at}

    async def default_branch(self, repo: str) -> str:
        return self.default

    async def head_sha(self, repo: str) -> str:
        return self.sha

    async def issue_detail(self, repo: str, number: int) -> dict | None:
        if number in self.issue_details:
            return self.issue_details[number]
        for it in self.issues:
            if it["number"] == number:
                return {
                    "number": number,
                    "title": it.get("title", ""),
                    "state": "open",
                    "state_reason": None,
                    "author": it.get("author", ""),
                    "assignee": it.get("assignee"),
                    "labels": it.get("labels", []),
                    "comments": it.get("comments", 0),
                    "created_at": it.get("created_at", ""),
                    "closed_at": "",
                    "body": it.get("body", ""),
                    "html_url": f"https://github.com/{repo}/issues/{number}",
                }
        return None

    async def issue_comments(
        self, repo: str, number: int, since: str = "", limit: int = 100
    ) -> list[dict]:
        return list(self.comments.get(number, []))

    async def compare(self, repo: str, base: str, head: str) -> dict:
        return {
            "commits": [
                {
                    "sha": "b" * 40,
                    "date": "2026-09-24T10:00:00Z",
                    "message": "fix: 修复登录竞态",
                }
            ],
            "files": [
                {
                    "filename": "src/auth.py",
                    "status": "modified",
                    "additions": 3,
                    "deletions": 1,
                    "patch": "@@ -10,7 +10,8 @@ def login():\n-    old\n+    new\n+    guard",
                }
            ],
        }

    async def latest_release(self, repo: str) -> dict | None:
        return self.release

    async def list_issues(self, repo: str, since_number: int = 0, limit: int = 20) -> list[dict]:
        return [i for i in self.issues if i["number"] > since_number]

    async def list_pulls(self, repo: str, state: str = "open", head_prefix: str = "") -> list[dict]:
        src = self.pulls + self.closed_pulls if state == "closed" else self.pulls
        return [
            p
            for p in src
            if p["state"] == state and (not head_prefix or p.get("head", "").startswith(head_prefix))
        ]

    async def create_pull(self, repo: str, title: str, head: str, base: str, body: str = "") -> dict:
        pr = {"number": 100 + len(self.created_prs), "url": f"http://pr/{len(self.created_prs)}", "state": "open"}
        self.created_prs.append({"title": title, "head": head, "base": base, **pr})
        return pr

    async def tree_paths(self, repo: str, ref: str = 'HEAD') -> list[str]:
        return ["README.md", "pyproject.toml", "src/main.py"]

    async def file_content(self, repo: str, path: str, ref: str = '') -> str | None:
        return "# hello\n"


CLG_RE = re.compile(r"changelog 文件名（必须精确）：`([^`]+)`")


class DocsSyncFakeLLM:
    """按 docs-sync 契约回话的假 LLM；fail_first 模拟第一次输出违规。"""

    def __init__(self, fail_first: bool = False):
        self.calls: list[str] = []
        self.fail_first = fail_first

    async def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        self.calls.append(user)
        m = CLG_RE.search(user)
        clg = m.group(1) if m else "changelog/unknown.md"
        if self.fail_first and len(self.calls) == 1:
            return "<<<META:summary>>>第一次故意不合规"
        if "首次全量建档" in user:
            return f"""<<<FILE:README.md>>>
# a/b 档案

测试项目档案索引。

<<<FILE:tech-stack.md>>>
| 项 | 值 | 用途 |
|---|---|---|
| 语言 | Python | 全部逻辑 |

<<<FILE:architecture.md>>>
```mermaid
graph TD
    A[入口] --> B{{路由}}
    B{{路由}} --> C[引擎]
```

<<<FILE:business-logic.md>>>
核心流程：入口 → 路由 → 引擎。

<<<FILE:{clg}>>>
# 初始建档

- 全量建立档案（aaaaaaa）

<<<META:summary>>>完成初始建档，核心链路已梳理
"""
        return f"""<<<FILE:{clg}>>>
# 增量更新

## 修复
- 修复登录竞态（bbbbbbb）

<<<META:summary>>>修复登录竞态问题
"""


class RecipeFakeLLM(DocsSyncFakeLLM):
    """全配方假 LLM：按 prompt 标记分发 tripwire / bug-watch / issue / release。"""

    def __init__(self, fail_first: bool = False):
        super().__init__(fail_first=fail_first)
        self.recipe_calls: list[str] = []

    async def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        if "哨兵警戒（feature-tripwire）" in user:
            self.recipe_calls.append("feature-tripwire")
            return (
                "<<<FILE:tripwires.md>>>\n"
                "# 哨兵清单\n\n"
                "- T1 配置格式迁移：**已翻转**——v2 将全面改用新配置格式（依据 bbbbbbb）\n\n"
                "<<<ALERT:tripwire|配置格式迁移翻转|v2 将改用新配置格式，升级需迁移>>>\n"
                "<<<META:summary>>>哨兵翻转 1 条\n"
            )
        if "缺陷观察（bug-watch）" in user:
            self.recipe_calls.append("bug-watch")
            return (
                "<<<FILE:bugwatch.md>>>\n"
                "# 缺陷观察\n\n## 2026-09-24\n- 修复登录竞态（bbbbbbb）\n\n"
                "<<<ALERT:breaking|login() 被移除|破坏性变更，调用方需迁移>>>\n"
                "<<<META:summary>>>1 修复 1 破坏性变更\n"
            )
        if "Issue 难度分析（issue-radar" in user:
            self.recipe_calls.append("issue-radar")
            nums = re.findall(r"### #(\d+)", user)
            blocks = []
            for n in nums:
                if n == "7":
                    blocks.append(
                        f"<<<ISSUE:#7|困难|登录问题跨模块>>>\n- 问题：复杂跨层\n- 方案：重构（周级）"
                    )
                else:
                    blocks.append(
                        f"<<<ISSUE:#{n}|简单|示例结论>>>\n- 问题：明确\n- 方案：一行修复（小时级）"
                    )
            return "\n".join(blocks) + "\n"
        if "口认领检测（issue-radar·claim）" in user:
            self.recipe_calls.append("issue-radar-claim")
            claims = []
            for block in user.split("### #")[1:]:
                n = block.split(" ", 1)[0]
                if "work on this" in block or "我来做" in block or "认领" in block:
                    m = re.search(r"\[(\S+)", block)
                    claims.append(f"<<<CLAIM:#{n}|{m.group(1) if m else 'someone'}>>>")
            return "\n".join(claims) if claims else "<<<CLAIM:NONE>>>"
        if "Release 简报（release-brief）" in user:
            self.recipe_calls.append("release-brief")
            m = re.search(r"版本：`?([^（`\n]+)", user)
            tag = m.group(1).strip() if m else "v0"
            return (
                f"<<<FILE:releases/{tag}.md>>>\n# {tag} 简报\n\n> 要点\n\n"
                f"<<<ALERT:release|{tag} 发布|版本要点一句话>>>\n"
                f"<<<META:summary>>>{tag} 已发布\n"
            )
        return await super().chat(system, user, temperature)


class FakeBark:
    """假 Bark：记录推送，永真。"""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.enabled = True

    async def send(self, title: str, body: str = "", level: str = "") -> bool:
        self.sent.append((title, body, level))
        return True


class PlainFakeLLM:
    """晨报用：直接回一段 markdown。"""

    def __init__(self, text: str = "# 晨报\n\n> 测试摘要\n"):
        self.text = text

    async def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        return self.text
