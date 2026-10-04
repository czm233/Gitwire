"""GitHub API 封装：查 SHA、compare diff、文件内容。带限流退避。"""

from __future__ import annotations

import asyncio
import re

import httpx

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo(raw: str) -> str | None:
    """把任意常见形态规整成内部规范格式 owner/name：
    - owner/name
    - https://github.com/owner/name（带或不带 .git、带或不带协议）
    - git@github.com:owner/name.git
    认不出返回 None。"""
    s = raw.strip().rstrip("/")
    if not s:
        return None
    if REPO_RE.match(s):
        return s
    if s.startswith("git@"):
        s = s.split("@", 1)[1].replace(":", "/")
    m = re.match(
        r"^(?:https?://)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$",
        s,
    )
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


class GithubError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"GitHub API {status}: {message}")


class GithubClient:
    def __init__(self, token: str = "", transport=None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30, transport=transport
        )
        self._default_branch: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None):
        delay = 1.0
        for attempt in range(4):
            resp = await self._client.get(url, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (403, 429) and attempt < 3:
                # 限流：指数退避
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 500 and attempt < 3:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            try:
                msg = resp.json().get("message", resp.text[:200])
            except Exception:
                msg = resp.text[:200]
            raise GithubError(resp.status_code, str(msg))
        raise GithubError(429, "rate limited after retries")

    async def repo_info(self, repo: str) -> dict:
        return await self._get(f"/repos/{repo}")

    async def exists(self, repo: str) -> bool:
        try:
            await self.repo_info(repo)
            return True
        except GithubError as e:
            if e.status == 404:
                return False
            raise

    async def default_branch(self, repo: str) -> str:
        if repo not in self._default_branch:
            info = await self.repo_info(repo)
            self._default_branch[repo] = info.get("default_branch") or "main"
        return self._default_branch[repo]

    async def head_sha(self, repo: str) -> str:
        branch = await self.default_branch(repo)
        data = await self._get(f"/repos/{repo}/commits/{branch}")
        return data["sha"]

    async def compare(self, repo: str, base: str, head: str) -> dict:
        """返回 {commits: [...], files: [...]}；identical 时两者为空。"""
        data = await self._get(f"/repos/{repo}/compare/{base}...{head}")
        commits = [
            {
                "sha": c["sha"],
                "date": (c.get("commit", {}).get("author", {}) or {}).get("date", ""),
                "message": (c.get("commit", {}).get("message", "") or "").splitlines()[0][:120],
            }
            for c in data.get("commits", [])
        ]
        files = [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": f.get("patch") or "",
            }
            for f in data.get("files", [])
        ]
        return {"commits": commits, "files": files}

    async def tree_paths(self, repo: str, ref: str = 'HEAD') -> list[str]:
        data = await self._get(
            f"/repos/{repo}/git/trees/{ref}", params={"recursive": "1"}
        )
        return [t["path"] for t in data.get("tree", []) if t.get("type") == "blob"]

    async def file_content(self, repo: str, path: str, ref: str = '') -> str | None:
        resp = await self._client.get(f"/repos/{repo}/contents/{path}", params={'ref': ref} if ref else None)
        if resp.status_code != 200:
            return None
        import base64

        data = resp.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
        return data.get("content")

    # ---------- v2：release / issue / PR ----------

    async def latest_release(self, repo: str) -> dict | None:
        """最新 release：{tag, name, published_at, body}；无 release 返回 None。"""
        resp = await self._client.get(f"/repos/{repo}/releases/latest")
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "tag": data.get("tag_name") or "",
            "name": data.get("name") or "",
            "published_at": data.get("published_at") or "",
            "body": (data.get("body") or "")[:8000],
        }

    async def list_issues(
        self, repo: str, since_number: int = 0, limit: int = 20
    ) -> list[dict]:
        """编号大于 since_number 的开放 issue（不含 PR），按编号升序返回。
        limit=0 表示全量拉取（翻页至尽，硬上限 300）。"""
        hard_cap = 300 if limit == 0 else limit
        max_pages = max(3, hard_cap // 50 + 1)
        issues: list[dict] = []
        page = 1
        while len(issues) < hard_cap and page <= max_pages:
            data = await self._get(
                f"/repos/{repo}/issues",
                params={
                    "state": "open",
                    "sort": "created",
                    "direction": "desc",
                    "per_page": 50,
                    "page": page,
                },
            )
            if not data:
                break
            hit_old = False
            for it in data:
                if "pull_request" in it:  # issues API 会混入 PR
                    continue
                n = it.get("number") or 0
                if n <= since_number:
                    hit_old = True
                    continue
                issues.append(
                    {
                        "number": n,
                        "title": it.get("title") or "",
                        "author": (it.get("user") or {}).get("login", ""),
                        "assignee": (it.get("assignee") or {}).get("login")
                        if it.get("assignee")
                        else None,
                        "labels": [lb.get("name") for lb in it.get("labels", [])],
                        "comments": it.get("comments") or 0,
                        "created_at": it.get("created_at") or "",
                        "updated_at": it.get("updated_at") or "",
                        "reactions": (it.get("reactions") or {}).get("total_count") or 0,
                        "body": (it.get("body") or "")[:800],
                    }
                )
            if hit_old:
                break
            page += 1
        return list(reversed(issues[:hard_cap]))

    async def issue_detail(self, repo: str, number: int) -> dict | None:
        """单个 issue 当前状态（关闭检测 / 追踪用）；不存在返回 None。"""
        resp = await self._client.get(f"/repos/{repo}/issues/{number}")
        if resp.status_code != 200:
            return None
        it = resp.json()
        return {
            "number": it.get("number") or number,
            "title": it.get("title") or "",
            "state": it.get("state") or "open",
            "state_reason": it.get("state_reason"),
            "author": (it.get("user") or {}).get("login", ""),
            "assignee": (it.get("assignee") or {}).get("login")
            if it.get("assignee")
            else None,
            "labels": [lb.get("name") for lb in it.get("labels", [])],
            "comments": it.get("comments") or 0,
            "created_at": it.get("created_at") or "",
            "closed_at": it.get("closed_at") or "",
            "body": (it.get("body") or "")[:400],
            "html_url": it.get("html_url") or f"https://github.com/{repo}/issues/{number}",
        }

    async def issue_comments(
        self, repo: str, number: int, since: str = "", limit: int = 100
    ) -> list[dict]:
        """issue 评论（升序）；since 为 ISO 时间时只取之后的（口认领/追踪增量拉取）。"""
        params: dict = {"per_page": min(limit, 100)}
        if since:
            params["since"] = since
        data = await self._get(f"/repos/{repo}/issues/{number}/comments", params=params)
        return [
            {
                "author": (c.get("user") or {}).get("login", ""),
                "created_at": c.get("created_at") or "",
                "html_url": c.get("html_url") or "",
                "body": (c.get("body") or "")[:1200],
            }
            for c in data
        ]

    async def create_pull(
        self, repo: str, title: str, head: str, base: str, body: str = ""
    ) -> dict:
        """开 PR；已存在同分支 PR 时直接返回它（幂等）。"""
        resp = await self._client.post(
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "number": data["number"],
                "url": data["html_url"],
                "state": data["state"],
            }
        if resp.status_code == 422 and "pull request already exists" in resp.text.lower():
            existing = await self.list_pulls(repo, state="open", head_prefix=head)
            if existing:
                return existing[0]
        raise GithubError(resp.status_code, resp.text[:200])

    async def list_pulls(
        self, repo: str, state: str = "open", head_prefix: str = ""
    ) -> list[dict]:
        data = await self._get(
            f"/repos/{repo}/pulls", params={"state": state, "per_page": 50}
        )
        out = []
        for p in data:
            head = p.get("head", {}).get("ref", "")
            if head_prefix and not head.startswith(head_prefix):
                continue
            out.append(
                {
                    "number": p["number"],
                    "url": p["html_url"],
                    "state": p["state"],
                    "merged": bool(p.get("merged_at")),
                    "merged_at": p.get("merged_at") or "",
                    "author_association": p.get("author_association") or "",
                    "head": head,
                    "title": p.get("title") or "",
                    "author": (p.get("user") or {}).get("login", ""),
                    "created_at": p.get("created_at") or "",
                    "body": (p.get("body") or "")[:800],
                }
            )
        return out

    @staticmethod
    def parse_issue_refs(pr: dict) -> list[int]:
        """从 PR 标题/正文解析它声称要解决的 issue 编号（强引用）。

        v4：只认 fixes/closes/resolves 关键词（含 "closes: #12 and #34" 连写形态）；
        裸 #N 不再算被占信号——PR 正文顺口提到编号的误报太多。
        """
        import re as _re

        text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        groups = _re.findall(
            r"(?:fix|fixes|fixed|close|closes|closed|resolve|resolves|resolved)"
            r"\s*:?\s*((?:#\d+(?:\s*(?:,|and|&|/)\s*#?\d+)*))",
            text,
            _re.I,
        )
        refs: set[int] = set()
        for g in groups:
            refs.update(int(n) for n in _re.findall(r"#(\d+)", g))
        return sorted(refs)
