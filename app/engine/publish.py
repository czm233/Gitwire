"""发布引擎 v2：写档案 → meta 游标 → commit → direct 推送 / PR 模式。

原则不变：发布成功游标才前进——宁可重跑，不写坏档案。
PR 模式：内容走分支 + Pull Request（人工审核后合入 main），游标随分支前进；
纯游标推进（如路径过滤跳过）仍直接提交 main，避免每小时空转。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.vault import ProjectMeta, slugify

MODE_LABELS = {"init": "建档", "incremental": "增量"}


@dataclass
class PublishOutcome:
    commit_sha: str = ""
    pushed: bool = True
    push_err: str = ""
    pr_number: int | None = None
    pr_url: str | None = None
    alerts: list[dict] = field(default_factory=list)


def _frontmatter(settings, repo: str, sha: str, rel: str, content: str) -> str:
    """Obsidian 模式：给 markdown 注入 YAML frontmatter（已有 frontmatter 的不动）。"""
    if not settings.obsidian_frontmatter or not rel.endswith(".md"):
        return content
    if content.startswith("---"):
        return content
    fm = (
        "---\n"
        f"repo: {repo}\n"
        f"cursor: {sha[:7] if sha else ''}\n"
        f"synced: {settings.local_date_str()}\n"
        "tags: [gitwire]\n"
        "---\n\n"
    )
    return fm + content


async def publish(
    svc,
    repo: str,
    files: dict[str, str],
    new_sha: str,
    mode: str,
    meta_updates: dict | None = None,
    pr_mode: bool = False,
    summary: str = "",
) -> PublishOutcome:
    """把配方产出写入 vault 并提交。meta_updates 含 last_release_tag / last_issue_number。"""
    vault = svc.vault
    settings = svc.settings
    slug = slugify(repo)
    meta_updates = meta_updates or {}

    paths: list[str] = []
    for rel, content in files.items():
        vault.write_file(f"{slug}/{rel}", _frontmatter(settings, repo, new_sha, rel, content))
        paths.append(f"{slug}/{rel}")

    meta = vault.meta(slug) or ProjectMeta(repo=repo)
    if new_sha:
        meta.last_synced = new_sha
        meta.last_mode = mode
        if summary:
            meta.summary = summary
        meta.updated_at = settings.local_now().isoformat(timespec="seconds")
    if "last_release_tag" in meta_updates:
        meta.last_release_tag = meta_updates["last_release_tag"]
    if "last_issue_number" in meta_updates:
        meta.last_issue_number = meta_updates["last_issue_number"]
    if "last_pr_number" in meta_updates:
        meta.last_pr_number = meta_updates["last_pr_number"]
    vault.save_meta(slug, meta)
    paths.append(f"{slug}/meta.yml")

    label = MODE_LABELS.get(mode, mode)
    commit_msg = f"[{repo}] docs-sync: {label} ({new_sha[:7]})" if new_sha else \
        f"[{repo}] 触发源更新（release/issue 游标）"

    if pr_mode and files:
        return await _publish_via_pr(svc, repo, slug, paths, commit_msg, summary, new_sha)

    # direct 模式：重写态势板锚点区块
    rows = []
    for s in vault.projects():
        pm = vault.meta(s)
        if pm:
            rows.append(
                {
                    "slug": s,
                    "mode": MODE_LABELS.get(pm.last_mode or "", pm.last_mode or "—"),
                    "status": "已发布",
                    "sha7": (pm.last_synced or "—")[:7],
                    "summary": pm.summary,
                }
            )
    stats = (
        f"- 最近一轮同步：{settings.local_date_str()}；"
        f"{len(vault.read_config().repos)} 个监控目标，{len(rows)} 个已发布"
    )
    vault.rewrite_board(stats, rows)
    paths.append("README.md")

    commit_sha = await vault.commit(paths, commit_msg)
    push_ok, push_err = await vault.push()
    return PublishOutcome(commit_sha=commit_sha, pushed=push_ok, push_err=push_err)


async def _publish_via_pr(
    svc,
    repo: str,
    slug: str,
    paths: list[str],
    commit_msg: str,
    summary: str,
    new_sha: str,
) -> PublishOutcome:
    """PR 模式：分支提交 + 开 PR；态势板不在 PR 里动（合入后由 direct 提交刷新）。

    文件与 meta 在调用前已写入工作树；checkout -B 会带着未提交改动切到新分支，
    因此无需重写。
    """
    vault = svc.vault
    vault_slug = vault.remote_repo_slug()
    if not vault_slug:
        return PublishOutcome(pushed=False, push_err="PR 模式需要 GitHub https remote")

    tag = (new_sha or "trigger")[:7]
    branch = f"gitwire/{slug}-{svc.settings.local_date_str()}-{tag}"
    await vault.fetch()
    base_branch = await vault.branch()
    await vault.checkout_branch(branch, f"origin/{base_branch}")
    try:
        commit_sha = await vault.commit(paths, commit_msg)
        ok, err = await vault.push_branch(branch)
        if not ok:
            return PublishOutcome(commit_sha=commit_sha, pushed=False, push_err=err)
        pr = await svc.gh.create_pull(
            vault_slug,
            title=commit_msg,
            head=branch,
            base=base_branch,
            body=(summary or "Gitwire 自动同步，请审核后合入。") + "\n\n_由 Gitwire PR 模式生成_",
        )
        return PublishOutcome(
            commit_sha=commit_sha,
            pushed=True,
            pr_number=pr["number"],
            pr_url=pr["url"],
        )
    finally:
        await vault.checkout_default(base_branch)
        await vault.clean_untracked(paths)
