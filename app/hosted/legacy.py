"""Explicit-account legacy import. Source files and SQLite stay untouched."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from sqlmodel import Session, select
from app.hosted import github
from app.hosted.corpus import resolve_repository
from app.hosted.models import Account, LegacyRecord, Subscription, now
from app.vault import Vault


def scrub(content: str, settings) -> str:
    for key in ('github_token', 'github_client_secret', 'llm_api_key', 'encryption_key',
                'smtp_password', 'mail_feedback_secret', 'secret_key', 'bark_url', 'database_url'):
        value = getattr(settings, key, '')
        if value and len(value) >= 8:
            content = content.replace(value, '[已移除凭证]')
    return re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})', '[已移除凭证]', content)


def epoch(value, fallback):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return int(date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp())
    except (TypeError, ValueError):
        return fallback


def read_records(settings, config_path: Path, vault_path: Path, db_path: Path):
    records = []
    size = 0
    def add(kind, key, title, content, repo='', created_at=None):
        nonlocal size
        clean = scrub(content, settings)
        size += len(clean.encode())
        if size > 100 * 1024 * 1024:
            raise ValueError('旧资料超过 100 MB，请分批迁移')
        source_key = hashlib.sha256((key + '\n' + clean).encode()).hexdigest()
        records.append(dict(kind=kind, source_key=source_key, title=scrub(title, settings), content=clean,
                            repo=scrub(repo, settings), created_at=created_at or now()))
    if config_path.exists():
        add('configuration', 'configuration', '旧版监控配置（只读备份）', config_path.read_text())
    if vault_path.exists():
        root = vault_path.resolve()
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if '.git' in relative.parts or path.is_symlink() or not path.is_file() or path.suffix not in {'.md', '.yml', '.yaml'}:
                continue
            if not path.resolve().is_relative_to(root):
                continue
            if path.stat().st_size > 5 * 1024 * 1024:
                raise ValueError('单份旧资料超过 5 MB，请单独处理：' + str(relative))
            kind = 'daily' if relative.parts[0] == 'daily' else 'document' if path.suffix == '.md' else 'state'
            add(kind, 'vault:' + relative.as_posix(), relative.as_posix(), path.read_text(errors='replace'), created_at=int(path.stat().st_mtime))
    if db_path.exists():
        with sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, keys in [('run', ['id','repo','trigger','mode','status','summary','old_sha','new_sha','started_at','finished_at']),
                                ('alert', ['id','repo','kind','title','body','ts'])]:
                if table not in tables:
                    continue
                columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                selected = [k for k in keys if k in columns]
                for raw in db.execute(f'SELECT {",".join(selected)} FROM "{table}" ORDER BY id'):
                    data = dict(raw)
                    # Raw provider errors/logs may contain credentials; keep them only in the original SQLite.
                    title = data.get('title') or f'旧版运行 #{data["id"]} · {data.get("repo", "")}'
                    add(table, f'sqlite:{table}:{data["id"]}', title, json.dumps(data, ensure_ascii=False, indent=2),
                        repo=data.get('repo', ''), created_at=epoch(data.get('started_at') or data.get('ts'), now()))
    return records


async def import_legacy(rt, github_id: str, config_path: Path, vault_path: Path, db_path: Path, apply=False):
    # Resolve an explicitly supplied immutable GitHub ID, never the first login.
    with Session(rt.engine) as s:
        user = s.exec(select(Account).where(Account.github_id == github_id)).first()
        if not user:
            raise ValueError('目标 GitHub 账号尚未登录过当前应用')
    cfg = Vault(vault_path, config_path=config_path).read_config()
    records = read_records(rt.settings, config_path, vault_path, db_path)
    eligible, unavailable = [], []
    for target in cfg.repos:
        try:
            info = await github.public_repo(rt, target.name)
        except github.GitHubFailure as exc:
            if exc.status == 404:
                unavailable.append(target.name)
                continue
            raise
        eligible.append((target, info['full_name']))
    report = {'account': user.login, 'public_repositories': [name for _, name in eligible],
        'unavailable_repositories': unavailable, 'private_archive_records': len(records), 'applied': False}
    if not apply:
        return report
    from app.hosted.api import RECIPES
    repos = [(target, await resolve_repository(rt, name)) for target, name in eligible]
    with Session(rt.engine) as s:
        s.exec(select(Account).where(Account.id == user.id).with_for_update()).one()
        existing = {sub.repo_id for sub in s.exec(select(Subscription).where(Subscription.user_id == user.id))}
        new = [(target, repo) for target, repo in repos if repo.id not in existing]
        if len(existing) + len(new) > rt.settings.max_subscriptions:
            raise ValueError('迁移将超过监控上限，请先调整上限或清单')
        for target, repo in new:
            s.add(Subscription(user_id=user.id, repo_id=repo.id,
                recipes=[r for r in cfg.recipes_for(target.name) if r in RECIPES] or ['docs-sync'],
                watched_issues=target.watch_issues,
                difficulty=cfg.opportunity_for(target.name).difficulties,
                opportunity={k: v for k, v in cfg.opportunity_for(target.name).to_dict().items() if k != 'difficulties'}))
        existing_keys = set(s.exec(select(LegacyRecord.source_key).where(LegacyRecord.user_id == user.id)))
        imported = 0
        for record in records:
            if record['source_key'] not in existing_keys:
                s.add(LegacyRecord(user_id=user.id, **record))
                imported += 1
        s.commit()
    return {**report, 'applied': True, 'added_subscriptions': len(new), 'added_archive_records': imported}
