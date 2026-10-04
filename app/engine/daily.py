"""每日晨报：汇总当日各项目 changelog，LLM 提炼一页，写入 vault daily/。"""

from __future__ import annotations

from pathlib import Path


def load_daily_recipe() -> str:
    path = Path(__file__).resolve().parents[1] / "recipes" / "daily" / "prompt.md"
    if not path.exists():
        raise FileNotFoundError(f"晨报配方不存在: {path}")
    return path.read_text(encoding="utf-8")


def issue_digests(vault, date: str) -> list[str]:
    """从各项目 radar.yml 聚合当日 issue 风向材料：新分析 / 机会进出 / 关闭。"""
    import yaml

    sections: list[str] = []
    for slug in vault.projects():
        raw = vault.read_file(f"{slug}/radar.yml")
        if not raw:
            continue
        try:
            data = yaml.safe_load(raw) or {}
        except Exception:  # noqa: BLE001
            continue
        events: list[str] = []
        for n, e in (data.get("issues") or {}).items():
            if not isinstance(e, dict):
                continue
            if e.get("analyzed_at") == date:
                events.append(
                    f"- 新分析 #{n} [{e.get('difficulty', '?')}] {e.get('title', '')}"
                    f" —— {e.get('summary', '')}"
                )
            if (e.get("status_changed_at") or "")[:10] == date:
                status = e.get("last_status")
                if status == "closed":
                    events.append(f"- #{n} 已关闭（{e.get('resolved_by', '来源未识别')}）")
                elif status == "taken-claim":
                    events.append(f"- #{n} 被 {e.get('claimed_by', '?')} 口认领")
                elif status == "taken-pr":
                    prs = e.get("pr_claims") or ["?"]
                    events.append(f"- #{n} 被 PR#{prs[0]} 占据")
                elif status == "taken-assignee":
                    events.append(f"- #{n} 被认领（assignee）")
                elif status == "open":
                    events.append(f"- #{n} 重回机会池")
        if events:
            meta = vault.meta(slug)
            repo_name = meta.repo if meta else slug
            sections.append(f"### {repo_name}\n" + "\n".join(events))
    return sections


async def gen_daily(svc, date_str: str | None = None) -> str | None:
    """生成某日晨报；当日无 changelog 且无 issue 动态则跳过。返回日期或 None。"""
    settings = svc.settings
    date = date_str or settings.local_yesterday_str()
    vault = svc.vault

    items: list[str] = []
    for slug in vault.projects():
        meta = vault.meta(slug)
        repo_name = meta.repo if meta else slug
        for f in vault.list_files(slug):
            path = f["path"]
            if path.startswith(f"changelog/{date}-") and path.endswith(".md"):
                content = vault.read_file(f"{slug}/{path}") or ""
                items.append(
                    f"### {repo_name}\n来源：`{path}`\n\n{content}"
                )
    issue_sections = issue_digests(vault, date)
    if not items and not issue_sections:
        print(f"[daily] {date} 无 changelog 与 issue 动态，跳过")
        return None

    template = load_daily_recipe()
    prompt = (
        template.replace("{{DATE}}", date)
        .replace("{{ITEMS}}", "\n\n".join(items) or "（今日无代码变更）")
        .replace("{{ISSUES}}", "\n\n".join(issue_sections) or "（今日无 issue 动态）")
    )
    system = "你是 Gitwire 的情报编辑，负责撰写每日开源情报晨报。"
    out = await svc.llm.chat(system, prompt)
    out = out.strip()
    # 模型偶尔整篇裹一层代码围栏，去掉
    if out.startswith("```"):
        out = out.strip("`").lstrip("markdown\n").strip()

    vault.write_file(f"daily/{date}.md", out + "\n")
    await vault.commit([f"daily/{date}.md"], f"[daily] {date} 晨报")
    ok, err = await vault.push()
    if not ok:
        print(f"[daily] push 失败待补推: {err}")
    print(f"[daily] {date} 晨报已生成")
    return date
