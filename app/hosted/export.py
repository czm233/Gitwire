"""Account-filtered, credential-free portable exports."""
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import zipfile
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlmodel import Session, select
from app.hosted.models import Artifact, Digest, Issue, LegacyRecord, Repository, Subscription, now
from app.hosted.security import current_user
from app.hosted.limits import consume

router = APIRouter()
MAX_EXPORT_BYTES = 50 * 1024 * 1024


def safe_path(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and '\\' not in path and ':' not in path and all(
        part not in {'.', '..', '.git'} and not part.startswith('.') for part in p.parts)


@router.get('/export')
async def export(request: Request, repo: str = ''):
    user = current_user(request)
    rt = request.app.state.runtime
    consume(rt.engine, 'export:' + user.id, 6)
    files, size = {}, 0
    def add(path, content):
        nonlocal size
        if not safe_path(path):
            raise HTTPException(500, '归档文件路径无效')
        payload = content.encode('utf-8')
        size += len(payload)
        if size > MAX_EXPORT_BYTES:
            raise HTTPException(413, '导出内容超过 50 MB，请按仓库分别导出')
        files[path] = payload
    def dump(value):
        return json.dumps(value, ensure_ascii=False, indent=2)

    with Session(rt.engine) as s:
        stmt = select(Subscription, Repository).join(Repository).where(
            Subscription.user_id == user.id, Repository.public == True)
        if repo:
            stmt = stmt.where(Repository.full_name == repo)
        rows = s.exec(stmt.order_by(Subscription.created_at.desc())).all()
        if repo and not rows:
            raise HTTPException(404, '该仓库不在你的公开监控清单中')
        subscriptions = []
        for sub, repository in rows:
            subscriptions.append({'repo': repository.full_name, 'paused': sub.paused,
                'recipes': sub.recipes, 'watched_issues': sub.watched_issues,
                'difficulty': sub.difficulty, 'keywords': sub.keywords, 'opportunity': sub.opportunity})
            prefix = 'repositories/' + repository.full_name
            issues = s.exec(select(Issue).where(Issue.repo_id == repository.id).order_by(Issue.number.desc()))
            # One file per issue allows local Git diffs without rewriting a giant JSON file.
            for issue in issues:
                add(f'{prefix}/issues/{issue.number}.json', dump(issue.model_dump(exclude={'id', 'repo_id'})))
            latest = set()
            for artifact in s.exec(select(Artifact).where(Artifact.repo_id == repository.id)
                                  .order_by(Artifact.created_at.desc(), Artifact.id.desc())):
                if not safe_path(artifact.path):
                    continue
                revision = hashlib.sha256(artifact.content.encode()).hexdigest()
                add(f'{prefix}/history/{revision}/{artifact.path}', artifact.content)
                if artifact.path not in latest:
                    add(f'{prefix}/latest/{artifact.path}', artifact.content)
                    latest.add(artifact.path)
        add('subscriptions.json', dump(subscriptions))
        if not repo:
            for daily in s.exec(select(Digest).where(Digest.user_id == user.id).order_by(Digest.date.desc())):
                add(f'daily/{daily.date}.md', daily.content)
            for record in s.exec(select(LegacyRecord).where(LegacyRecord.user_id == user.id)):
                add(f'legacy/{record.kind}/{record.id}.json', dump(record.model_dump(exclude={'user_id', 'source_key'})))
        add('preferences.json', dump({k: getattr(user, k) for k in
            ('timezone', 'digest_hour', 'email_mode', 'quiet_start', 'quiet_end', 'notify_kinds')}))
    manifest = {'format': 'gitwire-export-v1', 'account': user.github_id, 'created_at': now(),
        'repositories': [sub['repo'] for sub in subscriptions],
        'files': {path: hashlib.sha256(payload).hexdigest() for path, payload in files.items()}}
    files['manifest.json'] = dump(manifest).encode()
    output = BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, payload in files.items():
            archive.writestr(path, payload)
    return Response(output.getvalue(), media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="gitwire-{now()}.zip"', 'Cache-Control': 'no-store'})
