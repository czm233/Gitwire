"""Public-only repository ingestion and user-scoped candidate discovery."""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from app.hosted import github
from app.hosted.jobs import enqueue
from app.hosted.models import Account, Candidate, Repository, Subscription, Issue, Event, Notification, Job, now


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def resolve_repository(rt, name: str) -> Repository:
    # This is an authorization boundary, not an existence check with a privileged token.
    info = await github.public_repo(rt, name)
    with Session(rt.engine) as s:
        row = s.exec(select(Repository).where(Repository.github_id == str(info['id']))).first()
        if not row:
            row = Repository(github_id=str(info['id']), full_name=info['full_name'])
        row.full_name, row.description = info['full_name'], info.get('description') or ''
        row.public, row.unavailable_reason = True, ''
        row.default_branch = info.get('default_branch', 'main')
        row.stars, row.checked_at = info.get('stargazers_count', 0), now()
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            row = s.exec(select(Repository).where(Repository.github_id == str(info['id']))).one()
        s.refresh(row)
        return row


async def sync_candidates(rt, job):
    source = job.payload.get('source', 'starred')
    with Session(rt.engine) as s:
        account = s.get(Account, job.user_id)
        if not account:
            return {'count': 0}
    profile = await github.get(rt, '/user', user_id=account.id)
    if str(profile['id']) != account.github_id:
        raise RuntimeError('GitHub identity mismatch')
    with Session(rt.engine) as s:
        user = s.get(Account, account.id)
        user.login, user.avatar_url, user.updated_at = profile['login'], profile.get('avatar_url', ''), now()
        s.add(user)
        s.commit()
    page = int(job.payload.get('page', 1))
    generation = job.id
    started = int(job.payload.get('started', now()))
    path = '/user/starred' if source == 'starred' else f'/users/{profile["login"]}/repos'
    total = 0
    while True:
        params = {'per_page': 100, 'page': page, 'sort': 'created', 'direction': 'desc'}
        if source == 'owned':
            params['type'] = 'owner'
        entries = await github.get(rt, path, params, user_id=account.id,
            accept='application/vnd.github.star+json' if source == 'starred' else 'application/vnd.github+json')
        with Session(rt.engine) as s:
            for entry in entries:
                repo = entry.get('repo', entry)
                if repo.get('private') is not False or repo.get('visibility', 'public') != 'public':
                    continue
                gid = str(repo['id'])
                row = s.exec(select(Candidate).where(Candidate.user_id == account.id,
                    Candidate.github_id == gid, Candidate.source == source)).first()
                if row is None:
                    row = Candidate(user_id=account.id, github_id=gid, full_name=repo['full_name'], source=source,
                                    discovered_at=started)
                row.starred_at = entry.get('starred_at', '')
                row.full_name, row.description, row.present = repo['full_name'], repo.get('description') or '', True
                row.seen_generation = generation
                # Ignored choices survive refresh, unstar and a later re-star.
                s.add(row)
                total += 1
            durable = s.get(Job, job.id)
            if len(entries) < 100:
                s.execute(update(Candidate).where(Candidate.user_id == account.id, Candidate.source == source,
                    Candidate.seen_generation != generation).values(present=False))
                if source == 'starred':
                    user = s.get(Account, account.id)
                    user.stars_synced_at = now()
                    s.add(user)
            elif durable:
                durable.payload = {**job.payload, 'page': page + 1, 'started': started}
                s.add(durable)
            s.commit()
        if len(entries) < 100:
            return {'pages': page, 'count': total}
        page += 1


def status_of(data: dict, pr_evidence: list[dict]) -> str:
    if data.get('state') == 'closed':
        return 'resolved' if data.get('state_reason') == 'completed' else 'closed'
    if pr_evidence:
        return 'taken-pr'
    if data.get('assignees'):
        return 'taken-assignee'
    return 'open'


def create_event(s, issue: Issue, kind: str, previous: str, title: str, body: str, before=None):
    key = fingerprint([issue.repo_id, issue.number, kind, issue.analysis_hash or issue.content_hash]) if kind == 'opportunity' else fingerprint([issue.repo_id, issue.number, issue.updated_at, previous, issue.status, kind])
    event = s.exec(select(Event).where(Event.key == key)).first()
    if not event:
        event = Event(key=key, repo_id=issue.repo_id, issue_number=issue.number, kind=kind,
                      title=title, body=body, url=issue.url, difficulty=issue.difficulty, observation=before or {})
        s.add(event)
        s.flush()
    from app.hosted.opportunities import exclusion, change_matches, keyword_match
    repo = s.get(Repository, issue.repo_id)
    subscriptions = s.exec(select(Subscription).where(Subscription.repo_id == issue.repo_id, Subscription.paused == False)).all()
    for sub in subscriptions:
        user = s.get(Account, sub.user_id)
        if not user or kind not in user.notify_kinds:
            continue
        # Explicit watchlists receive all selected changes; opportunity-pool
        # subscribers also receive status/label/comment changes relevant to them.
        if kind != 'opportunity' and not change_matches(issue, repo, sub, event.observation):
            continue
        if kind == 'opportunity' and ('issue-radar' not in sub.recipes or issue.status != 'open' or exclusion(issue, repo, sub)):
            continue
        if kind == 'opportunity':
            try:
                created = datetime.fromisoformat(issue.created_at.replace('Z', '+00:00')).timestamp()
            except ValueError:
                continue
            if created < sub.created_at:
                continue  # Initial inventory remains visible without flooding new subscribers.
        if kind == 'opportunity' and not keyword_match(issue, sub):
            continue
        if not s.exec(select(Notification).where(Notification.user_id == sub.user_id, Notification.event_id == event.id)).first():
            s.add(Notification(user_id=sub.user_id, event_id=event.id))


async def scan_repository(rt, job):
    with Session(rt.engine) as s:
        repo = s.get(Repository, job.payload['repo_id'])
        if not repo:
            return {'count': 0}
    page = int(job.payload.get('page', 1))
    started = int(job.payload.get('started', now()))
    cursor = int(job.payload.get('cursor', repo.issues_cursor))
    since = datetime.fromtimestamp(max(0, cursor - 120), timezone.utc).isoformat() if cursor else ''
    total = 0
    while True:
        try:
            info = await github.public_repo(rt, repo.full_name)
        except github.GitHubFailure as exc:
            if exc.status == 404:
                with Session(rt.engine) as s:
                    row = s.get(Repository, repo.id)
                    row.public, row.unavailable_reason = False, '仓库不存在或不再公开，已停止展示与监控'
                    s.add(row)
                    s.commit()
            raise
        params = {'state': 'all', 'sort': 'updated', 'direction': 'desc', 'per_page': 100, 'page': page}
        if since:
            params['since'] = since
        entries = await github.get(rt, f'/repos/{info["full_name"]}/issues', params, cached=True)
        changed_ids = []
        with Session(rt.engine) as s:
            for data in entries:
                if 'pull_request' in data:
                    continue
                row = s.exec(select(Issue).where(Issue.repo_id == repo.id, Issue.number == data['number'])).first()
                existed = row is not None
                if row is None:
                    row = Issue(repo_id=repo.id, number=data['number'], title=data['title'], url=data['html_url'])
                previous = row.status
                from app.hosted.opportunities import observation
                before = (row.watch_snapshot.get('eligibility') or observation(row, repo)) if existed else {}
                if existed and not row.watch_snapshot and row.difficulty != '未分析':
                    row.watch_snapshot = {'status': row.status, 'labels': row.labels,
                                          'comments': row.comments, 'eligibility': before}
                row.title, row.body = data['title'], data.get('body') or ''
                row.url = data['html_url']
                row.created_at, row.updated_at = data.get('created_at', ''), data.get('updated_at', '')
                row.state, row.state_reason = data.get('state', 'open'), data.get('state_reason') or ''
                row.labels = [x['name'] for x in data.get('labels', [])]
                row.assignees = [x['login'] for x in data.get('assignees', [])]
                row.comments, row.checked_at = data.get('comments', 0), now()
                row.status = status_of(data, [e for e in row.evidence if e.get('open')])
                if row.status == 'open' and row.claim.get('until', 0) > now():
                    row.status = 'taken-claim'
                row.content_hash = fingerprint([row.title, row.body, row.labels])
                from app.hosted.analysis import PROMPT_VERSION
                expected = fingerprint([row.repo_id, row.number, row.content_hash, rt.settings.llm_model, PROMPT_VERSION])
                if row.state == 'open' and row.analysis_hash != expected:
                    changed_ids.append(row.id)
                s.add(row)
                if existed and previous != row.status:
                    kind = 'reopened' if row.status == 'open' else 'taken' if row.status.startswith('taken') else row.status
                    label = {'reopened': '机会重新开放', 'taken': 'Issue 已被认领', 'resolved': 'Issue 已标记完成', 'closed': 'Issue 已关闭，未确认完成'}[kind]
                    create_event(s, row, kind, previous, f'#{row.number} {label}', row.title, before)
            dbrepo = s.get(Repository, repo.id)
            dbrepo.checked_at, dbrepo.public, dbrepo.full_name = now(), True, info['full_name']
            s.add(dbrepo)
            s.commit()
            active = s.exec(select(Subscription).where(Subscription.repo_id == repo.id, Subscription.paused == False)).all()
        if any('issue-radar' in sub.recipes for sub in active):
            for issue_id in changed_ids:
                enqueue(rt.engine, 'analyze_issue', {'issue_id': issue_id}, 'analyze_issue:' + issue_id)
        # Advance only after durable analysis tasks exist. A crash before this
        # checkpoint repeats the page and coalesces jobs instead of losing work.
        with Session(rt.engine) as s:
            durable = s.get(Job, job.id)
            if len(entries) < 100:
                row = s.get(Repository, repo.id)
                row.issues_cursor = started
                row.next_scan = now() + rt.settings.repo_scan_minutes * 60
                s.add(row)
            elif durable:
                durable.payload = {**job.payload, 'page': page + 1, 'started': started, 'cursor': cursor}
                s.add(durable)
            s.commit()
        total += len(entries)
        if len(entries) < 100:
            return {'pages': page, 'count': total}
        page += 1
