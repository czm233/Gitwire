"""Public corpus reads and strictly account-scoped subscriptions/settings."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import or_, and_, case, false, func
from sqlmodel import Session, select
from app.github import normalize_repo
from app.hosted.corpus import resolve_repository
from app.hosted.jobs import enqueue
from app.hosted.limits import consume, guest_limit
from app.hosted.models import Account, Candidate, Repository, Subscription, Issue, Notification, Event, Job, JobAccess, Digest, Artifact, Delivery, LegacyRecord, now
from app.hosted.security import current_user, require_csrf, user_view
from app.hosted.opportunities import exclusion_sql

router = APIRouter()
RECIPES = {'docs-sync', 'issue-radar', 'feature-tripwire', 'bug-watch', 'cve-scan'}


def paged(items, page, page_size, key='items'):
    page_size = max(1, min(page_size, 100))
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    return {key: items[(page-1)*page_size:page*page_size], 'total': total, 'pages': pages, 'page': page, 'page_size': page_size}


def db_page(session, stmt, page, page_size):
    """Count and paginate in SQL; never load an entire public corpus per request."""
    page_size = max(1, min(page_size, 100))
    total = session.exec(select(func.count()).select_from(stmt.order_by(None).subquery())).one()
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    rows = session.exec(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return rows, {'total': total, 'pages': pages, 'page': page, 'page_size': page_size}


def sub_view(sub, repo):
    return {'id': sub.id, 'repo_id': repo.id, 'repo': repo.full_name, 'description': repo.description,
            'created_at': sub.created_at, 'paused': sub.paused, 'recipes': sub.recipes,
            'watched_issues': sub.watched_issues, 'difficulty': sub.difficulty, 'keywords': sub.keywords, 'opportunity': sub.opportunity,
            'checked_at': repo.checked_at, 'public': repo.public, 'unavailable_reason': repo.unavailable_reason}


@router.get('/explore')
async def explore(request: Request, repo: str):
    rt = request.app.state.runtime
    guest_limit(request)
    name = normalize_repo(repo)
    if not name:
        raise HTTPException(400, '请输入 owner/repo 或 GitHub 仓库地址')
    with Session(rt.engine) as s:
        row = s.exec(select(Repository).where(Repository.full_name == name)).first()
    if not row or now() - row.checked_at > 300 or not row.public:
        row = await resolve_repository(rt, name)
    user = current_user(request, required=False)
    with Session(rt.engine) as s:
        first = s.exec(select(Issue).where(Issue.repo_id == row.id)).first()
    job = None
    if not first or not row.next_scan or row.next_scan < now():
        job = enqueue(rt.engine, 'scan', {'repo_id': row.id}, f'scan:{row.id}:1', requester=user.id if user else None)
    return {'repo': row.model_dump(), 'refreshing': bool(job)}


@router.get('/repos')
async def subscriptions(request: Request):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        rows = s.exec(select(Subscription, Repository).join(Repository).where(Subscription.user_id == user.id).order_by(Subscription.created_at.desc(), Subscription.id.desc())).all()
        return {'repos': [sub_view(sub, repo) for sub, repo in rows]}


class SubscribeIn(BaseModel):
    repos: list[str] = Field(min_length=1, max_length=50)
    recipes: list[str] = Field(default_factory=lambda: ['docs-sync', 'issue-radar'])


@router.post('/repos')
async def subscribe(body: SubscribeIn, request: Request):
    user = require_csrf(request)
    rt = request.app.state.runtime
    if not body.recipes or set(body.recipes) - RECIPES:
        raise HTTPException(400, '请选择有效的监控配方')
    consume(rt.engine, 'import:' + user.id, 30)
    names = list(dict.fromkeys(normalize_repo(v) for v in body.repos))
    if None in names:
        raise HTTPException(400, '仓库地址格式不正确')
    # Validate every repository before changing this user's subscriptions.
    resolved = [await resolve_repository(rt, name) for name in names]
    # Old and new names can redirect to the same immutable GitHub repository.
    repos = list({repo.id: repo for repo in resolved}.values())
    with Session(rt.engine) as s:
        s.exec(select(Account).where(Account.id == user.id).with_for_update()).one()
        old = s.exec(select(Subscription).where(Subscription.user_id == user.id)).all()
        existing = {x.repo_id for x in old}
        new = [repo for repo in repos if repo.id not in existing]
        if len(old) + len(new) > rt.settings.max_subscriptions:
            raise HTTPException(400, f'当前最多监控 {rt.settings.max_subscriptions} 个仓库')
        for repo in new:
            s.add(Subscription(user_id=user.id, repo_id=repo.id, recipes=body.recipes))
        s.commit()
    for repo in repos:
        enqueue(rt.engine, 'scan', {'repo_id': repo.id}, f'scan:{repo.id}:1', requester=user.id)
        enqueue(rt.engine, 'analyze_repo', {'repo_id': repo.id}, 'analyze_repo:' + repo.id, requester=user.id)
    return {'added': [r.full_name for r in new], 'existing': [r.full_name for r in repos if r.id in existing]}


class OpportunityIn(BaseModel):
    model_config = {'extra': 'forbid'}
    max_age_days: int = Field(default=30, ge=0, le=3650)
    max_comments: int = Field(default=15, ge=0, le=10000)
    repo_pushed_within_days: int = Field(default=30, ge=0, le=3650)
    external_merge_within_days: int = Field(default=90, ge=0, le=3650)


class SubscriptionIn(BaseModel):
    paused: bool | None = None
    recipes: list[str] | None = None
    watched_issues: list[int] | None = Field(default=None, max_length=100)
    difficulty: list[str] | None = None
    keywords: str | None = Field(default=None, max_length=200)
    opportunity: OpportunityIn | None = None


@router.patch('/repos/{subscription_id}')
async def patch_subscription(subscription_id: str, body: SubscriptionIn, request: Request):
    user = require_csrf(request)
    if body.recipes is not None and (not body.recipes or set(body.recipes) - RECIPES):
        raise HTTPException(400, '无效的监控配方')
    if body.difficulty is not None and set(body.difficulty) - {'简单', '中等', '困难'}:
        raise HTTPException(400, '无效的难度')
    if body.watched_issues is not None and any(n <= 0 for n in body.watched_issues):
        raise HTTPException(400, 'Issue 编号必须为正整数')
    with Session(request.app.state.runtime.engine) as s:
        row = s.exec(select(Subscription).where(Subscription.id == subscription_id, Subscription.user_id == user.id)).first()
        if not row:
            raise HTTPException(404, '监控项不存在')
        for key, value in body.model_dump(exclude_none=True).items():
            setattr(row, key, list(dict.fromkeys(value)) if isinstance(value, list) else value)
        s.add(row)
        s.commit()
        return {'ok': True}


@router.delete('/repos/{subscription_id}')
async def delete_subscription(subscription_id: str, request: Request):
    user = require_csrf(request)
    with Session(request.app.state.runtime.engine) as s:
        row = s.exec(select(Subscription).where(Subscription.id == subscription_id, Subscription.user_id == user.id)).first()
        if not row:
            raise HTTPException(404, '监控项不存在')
        s.delete(row)
        s.commit()
    return {'ok': True}


@router.get('/candidates')
async def candidates(request: Request, source: str = 'starred', ignored: bool = False,
                     q: str = '', page: int = 1, page_size: int = 25):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        stmt = select(Candidate).where(Candidate.user_id == user.id, Candidate.source == source, Candidate.ignored == ignored, Candidate.present == True)
        if q:
            stmt = stmt.where(or_(Candidate.full_name.icontains(q), Candidate.description.icontains(q)))
        order = Candidate.starred_at.desc() if source == 'starred' else Candidate.discovered_at.desc()
        rows, meta = db_page(s, stmt.order_by(order, Candidate.id.desc()), page, page_size)
        subscribed = set(s.exec(select(Repository.github_id).join(Subscription).where(Subscription.user_id == user.id)).all())
        return {**meta, 'items': [{**r.model_dump(exclude={'user_id'}), 'monitored': r.github_id in subscribed} for r in rows]}


class SourceIn(BaseModel):
    source: str = 'starred'


@router.post('/candidates/sync')
async def refresh_candidates(body: SourceIn, request: Request):
    user = require_csrf(request)
    rt = request.app.state.runtime
    if body.source not in {'starred', 'owned'}:
        raise HTTPException(400, '无效的仓库来源')
    if user.reconnect_required or not user.access_token:
        raise HTTPException(409, '请重新连接 GitHub')
    consume(rt.engine, 'star-sync:' + user.id, 6)
    job = enqueue(rt.engine, 'candidates', {'source': body.source}, f'candidates:{user.id}:{body.source}:1', user_id=user.id, requester=user.id)
    return {'job_id': job.id}


class IgnoreIn(BaseModel):
    ignored: bool


@router.patch('/candidates/{candidate_id}')
async def ignore_candidate(candidate_id: str, body: IgnoreIn, request: Request):
    user = require_csrf(request)
    with Session(request.app.state.runtime.engine) as s:
        row = s.exec(select(Candidate).where(Candidate.id == candidate_id, Candidate.user_id == user.id)).first()
        if not row:
            raise HTTPException(404, '候选项目不存在')
        row.ignored = body.ignored
        s.add(row)
        s.commit()
    return {'ok': True}


@router.get('/issues')
async def issues(request: Request, repo: str = '', group: str = 'all', difficulty: str = '',
                 q: str = '', page: int = 1, page_size: int = 25):
    rt = request.app.state.runtime
    user = current_user(request, required=False)
    with Session(rt.engine) as s:
        subs = s.exec(select(Subscription).where(Subscription.user_id == user.id)).all() if user else []
        submap = {r.repo_id: r for r in subs}
        stmt = select(Issue, Repository).join(Repository).where(Repository.public == True)
        if repo:
            stmt = stmt.where(Repository.full_name == repo)
        elif user:
            stmt = stmt.where(Issue.repo_id.in_(list(submap)))
        else:
            return {**paged([], page, page_size), 'stats': {}, 'repos': []}
        at = now()
        reason = case(*[(Issue.repo_id == sub.repo_id, exclusion_sql(sub, at)) for sub in subs],
            else_=exclusion_sql(at=at)) if subs else exclusion_sql(at=at)
        grouping = case((Issue.state == 'closed', 'closed'), (Issue.status.startswith('taken'), 'taken'),
            (Issue.difficulty == '未分析', 'pending'), (reason != '', 'excluded'), else_='opportunity')
        watched_filter = or_(false(), *(and_(Issue.repo_id == sub.repo_id, Issue.number.in_(sub.watched_issues)) for sub in subs if sub.watched_issues))
        stats = {'all': 0, 'opportunity': 0, 'taken': 0, 'closed': 0, 'excluded': 0, 'watched': 0, 'pending': 0}
        for g, count in s.exec(stmt.with_only_columns(grouping, func.count()).group_by(grouping)).all():
            stats[g] = count
            stats['all'] += count
        stats['watched'] = s.execute(stmt.where(watched_filter).with_only_columns(func.count()).select_from(Issue)).scalar_one()
        repos = list(s.execute(stmt.with_only_columns(Repository.full_name).distinct().order_by(Repository.full_name)).scalars())
        if group == 'watched':
            stmt = stmt.where(watched_filter)
        elif group != 'all':
            stmt = stmt.where(grouping == ('excluded' if group == 'hard' else group))
        if difficulty:
            stmt = stmt.where(Issue.difficulty == difficulty)
        if q:
            stmt = stmt.where(or_(Issue.title.icontains(q, autoescape=True), and_(Issue.title_zh_source == Issue.title, Issue.title_zh.icontains(q, autoescape=True))))
        rows, meta = db_page(s, stmt.add_columns(grouping.label('group'), reason.label('excluded')).order_by(Issue.created_at.desc(), Issue.number.desc(), Issue.id.desc()), page, page_size)
        items = [{**issue.model_dump(exclude={'title_zh_source'}), 'title_zh': issue.title_zh if issue.title_zh_source == issue.title else '', 'repo': repository.full_name, 'group': g,
                  'watched': issue.number in submap[repository.id].watched_issues if repository.id in submap else False,
                  'subscription_id': submap[repository.id].id if repository.id in submap else None,
                  'excluded': excluded if g == 'excluded' else '',
                  'repo_signals': {'pushed_at': repository.pushed_at, 'external_merge_at': repository.external_merge_at,
                      'checked_at': repository.signals_checked_at, 'coverage_since': repository.signals_coverage_since}}
                 for issue, repository, g, excluded in rows]
        from app.hosted.translations import schedule_translations
        if user:
            schedule_translations(rt, [row[0] for row in rows], requester=user.id)
        return {**meta, 'items': items, 'stats': stats, 'repos': repos}


@router.post('/issues/{issue_id}/analyze')
async def analyze_issue(issue_id: str, request: Request):
    user = require_csrf(request)
    rt = request.app.state.runtime
    with Session(rt.engine) as s:
        issue = s.get(Issue, issue_id)
        if not issue or not s.get(Repository, issue.repo_id).public:
            raise HTTPException(404, 'Issue 不存在')
    consume(rt.engine, 'analysis:' + user.id, rt.settings.analysis_daily_limit, 86400)
    job = enqueue(rt.engine, 'analyze_issue', {'issue_id': issue_id}, 'analyze_issue:' + issue_id, requester=user.id)
    return {'job_id': job.id}


@router.get('/jobs')
async def jobs(request: Request, status: str = '', kind: str = '', page: int = 1, page_size: int = 25):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        stmt = select(Job).join(JobAccess).where(JobAccess.user_id == user.id)
        stats = dict(s.exec(select(Job.status, func.count()).join(JobAccess)
            .where(JobAccess.user_id == user.id).group_by(Job.status)).all())
        if status:
            stmt = stmt.where(Job.status == status)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        rows, meta = db_page(s, stmt.order_by(Job.created_at.desc(), Job.id.desc()), page, page_size)
        return {**meta, 'stats': stats, 'items': [job_view(s, j) for j in rows]}


def job_view(s, job):
    result = {key: job.result[key] for key in ('count', 'pages', 'cached', 'skipped', 'stale', 'sent', 'cancelled', 'rejected') if key in job.result}
    if job.kind == 'watch_issues':
        result.pop('count', None)  # The union of other users' watchlists is personal information.
    view = job.model_dump(exclude={'lease_owner', 'lease_until', 'active_key', 'payload', 'user_id', 'result'})
    issue = s.get(Issue, job.payload.get('issue_id', '')) if job.kind in {'analyze_issue', 'analyze_claim'} else None
    repo = s.get(Repository, issue.repo_id if issue else job.payload.get('repo_id', ''))
    view.update(result=result, repo=repo.full_name if repo and repo.public else '',
        issue_number=issue.number if issue and repo and repo.public else None)
    # Checkpoints expose progress, never raw payloads or access/identity details.
    if job.kind in {'scan', 'candidates', 'repo_signals'}:
        view['completed_pages'] = max(0, int(job.payload.get('page', 1)) - 1)
    return view


@router.get('/jobs/{job_id}')
async def job_detail(job_id: str, request: Request):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        if not s.get(JobAccess, (job_id, user.id)):
            raise HTTPException(404, '任务不存在')
        return job_view(s, s.get(Job, job_id))


@router.post('/jobs/{job_id}/retry')
async def retry_job(job_id: str, request: Request):
    user = require_csrf(request)
    rt = request.app.state.runtime
    with Session(rt.engine) as s:
        if not s.get(JobAccess, (job_id, user.id)):
            raise HTTPException(404, '任务不存在')
        job = s.get(Job, job_id)
        if job.status != 'failed':
            raise HTTPException(409, '只有失败任务可以手动重试')
        if job.kind == 'mail':
            raise HTTPException(409, '请到邮件投递记录中重试，验证邮件过期时需重新发送')
        if job.kind == 'candidates':
            if job.user_id != user.id or user.reconnect_required or not user.access_token:
                raise HTTPException(409, '请先重新连接 GitHub')
            payload = {'source': job.payload.get('source', 'starred')}
            key = f'candidates:{user.id}:{payload["source"]}:1'
        elif job.kind == 'digest':
            payload = {'date': job.payload['date']}
            key = f'digest:{user.id}:{payload["date"]}'
        elif job.kind in {'scan', 'repo_signals', 'analyze_repo', 'analyze_issue', 'analyze_claim', 'watch_issues', 'translate_titles'}:
            issue = s.get(Issue, job.payload.get('issue_id', '')) if job.kind in {'analyze_issue', 'analyze_claim'} else None
            repo_id = issue.repo_id if issue else job.payload.get('repo_id')
            repo = s.get(Repository, repo_id) if repo_id else None
            if not repo or not repo.public:
                raise HTTPException(409, '仓库已不可访问')
            if job.kind in {'analyze_repo', 'analyze_claim', 'watch_issues'}:
                sub = s.exec(select(Subscription).where(Subscription.user_id == user.id,
                    Subscription.repo_id == repo.id, Subscription.paused == False)).first()
                if not sub or (job.kind == 'watch_issues' and not sub.watched_issues and 'issue-radar' not in sub.recipes):
                    raise HTTPException(409, '请先恢复此仓库的相应监控')
            payload = {'issue_id': issue.id} if issue else {'repo_id': repo.id}
            if job.kind == 'translate_titles':
                payload['issue_ids'] = job.payload.get('issue_ids', [])[:50]
            if job.kind == 'analyze_claim':
                payload['comments'] = job.payload.get('comments', [])
            from app.hosted.corpus import fingerprint
            key = {'translate_titles': 'translate_titles:' + repo.id, 'analyze_claim': 'claim:' + fingerprint([issue.id if issue else '', payload.get('comments', [])]), 'repo_signals': 'signals:' + repo.id, 'scan': f'scan:{repo.id}:1', 'analyze_repo': 'analyze_repo:' + repo.id,
                   'watch_issues': 'watch:' + repo.id, 'analyze_issue': 'analyze_issue:' + (issue.id if issue else '')}[job.kind]
        else:
            raise HTTPException(409, '此任务不能手动重试')
    if job.kind in {'analyze_repo', 'analyze_issue', 'analyze_claim', 'translate_titles'}:
        consume(rt.engine, 'analysis:' + user.id, rt.settings.analysis_daily_limit, 86400)
    consume(rt.engine, 'job-retry:' + user.id, 20)
    fresh = enqueue(rt.engine, job.kind, payload, key,
        user_id=user.id if job.kind in {'candidates', 'digest'} else None, requester=user.id)
    return {'job_id': fresh.id}


@router.get('/notifications')
async def notifications(request: Request, page: int = 1, page_size: int = 25):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        rows, meta = db_page(s, select(Notification, Event, Repository).select_from(Notification).join(Event, Notification.event_id == Event.id).join(Repository, Event.repo_id == Repository.id).where(Notification.user_id == user.id, Repository.public == True).order_by(Notification.created_at.desc(), Notification.id.desc()), page, page_size)
        return {**meta, 'items': [{'id': n.id, 'read_at': n.read_at, 'created_at': n.created_at,
                       'kind': e.kind, 'title': e.title, 'body': e.body, 'url': e.url, 'repo': r.full_name} for n, e, r in rows]}


@router.post('/notifications/{notification_id}/read')
async def read_notification(notification_id: str, request: Request):
    user = require_csrf(request)
    with Session(request.app.state.runtime.engine) as s:
        row = s.exec(select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id)).first()
        if not row:
            raise HTTPException(404, '警报不存在')
        row.read_at = now()
        s.add(row)
        s.commit()
    return {'ok': True}


class PreferencesIn(BaseModel):
    timezone: str = 'Asia/Shanghai'
    digest_hour: int = Field(default=8, ge=0, le=23)
    email_enabled: bool = False
    email_mode: str = 'digest'
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)
    notify_kinds: list[str] = Field(default_factory=lambda: ['opportunity', 'resolved', 'taken', 'reopened', 'watch'])


@router.patch('/preferences')
async def preferences(body: PreferencesIn, request: Request):
    user = require_csrf(request)
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, '无效的时区')
    if body.email_mode not in {'digest', 'immediate', 'both'} or set(body.notify_kinds) - {'opportunity', 'taken', 'resolved', 'closed', 'reopened', 'release', 'tripwire', 'breaking', 'cve', 'watch'}:
        raise HTTPException(400, '无效的提醒设置')
    with Session(request.app.state.runtime.engine) as s:
        row = s.get(Account, user.id)
        if body.email_enabled and not row.email_verified:
            raise HTTPException(400, '请先验证收件邮箱')
        for k, v in body.model_dump().items():
            setattr(row, k, v)
        s.add(row)
        s.commit()
        s.refresh(row)
        return user_view(row)


@router.get('/daily')
async def daily(request: Request, page: int = 1, page_size: int = 10):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        rows, meta = db_page(s, select(Digest).where(Digest.user_id == user.id).order_by(Digest.date.desc()), page, page_size)
        return {**meta, 'items': [r.model_dump(exclude={'user_id'}) for r in rows]}


@router.get('/deliveries')
async def deliveries(request: Request, status: str = '', page: int = 1, page_size: int = 25):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        stmt = select(Delivery).where(Delivery.user_id == user.id)
        if status:
            stmt = stmt.where(Delivery.status == status)
        rows, meta = db_page(s, stmt.order_by(Delivery.created_at.desc(), Delivery.id.desc()), page, page_size)
        return {**meta, 'items': [row.model_dump(exclude={'body', 'key', 'user_id'}) for row in rows]}


@router.post('/deliveries/{delivery_id}/retry')
async def retry_delivery(delivery_id: str, request: Request):
    from app.hosted.mail import eligible_delivery
    user = require_csrf(request)
    rt = request.app.state.runtime
    consume(rt.engine, 'mail-retry:' + user.id, 6)
    with Session(rt.engine) as s:
        row = s.exec(select(Delivery).where(Delivery.id == delivery_id, Delivery.user_id == user.id).with_for_update()).first()
        if not row:
            raise HTTPException(404, '投递记录不存在')
        if row.status != 'failed' or not eligible_delivery(s, row, user):
            raise HTTPException(409, '当前状态不能重试；请检查邮箱、提醒设置或重新发送验证邮件')
        row.status, row.error = 'pending', ''
        s.add(row)
        s.commit()
    job = enqueue(rt.engine, 'mail', {'delivery_id': row.id}, 'mail:' + row.id, user_id=user.id, requester=user.id)
    return {'job_id': job.id}


@router.get('/legacy')
async def legacy_records(request: Request, kind: str = '', q: str = '', page: int = 1, page_size: int = 25):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        stmt = select(LegacyRecord).where(LegacyRecord.user_id == user.id)
        if kind:
            stmt = stmt.where(LegacyRecord.kind == kind)
        if q:
            stmt = stmt.where(LegacyRecord.title.icontains(q, autoescape=True))
        rows, meta = db_page(s, stmt.order_by(LegacyRecord.created_at.desc(), LegacyRecord.id.desc()), page, page_size)
        return {**meta, 'items': [row.model_dump(exclude={'user_id', 'source_key', 'content'}) for row in rows]}


@router.get('/legacy/{record_id}')
async def legacy_detail(record_id: str, request: Request):
    user = current_user(request)
    with Session(request.app.state.runtime.engine) as s:
        row = s.exec(select(LegacyRecord).where(LegacyRecord.id == record_id, LegacyRecord.user_id == user.id)).first()
        if not row:
            raise HTTPException(404, '旧版资料不存在')
        return row.model_dump(exclude={'user_id', 'source_key'})


@router.get('/artifacts')
async def artifacts(request: Request, repo: str, path: str = '', page: int = 1, page_size: int = 25):
    repo = normalize_repo(repo)
    if not repo:
        raise HTTPException(400, '请输入有效的公开仓库地址')
    with Session(request.app.state.runtime.engine) as s:
        row = s.exec(select(Repository).where(Repository.full_name == repo, Repository.public == True)).first()
        if not row:
            raise HTTPException(404, '公开项目不存在')
        stmt = select(Artifact).where(Artifact.repo_id == row.id)
        if path:
            stmt = stmt.where(Artifact.path == path)
        rows, meta = db_page(s, stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc()), page, page_size)
        paths = s.exec(select(Artifact.path).where(Artifact.repo_id == row.id).distinct().order_by(Artifact.path)).all()
        return {**meta, 'repo': row.full_name, 'paths': paths, 'items': [r.model_dump(exclude={'content', 'repo_id'}) for r in rows]}


@router.get('/artifacts/{artifact_id}')
async def artifact_detail(artifact_id: str, request: Request, compare: str = ''):
    import difflib
    with Session(request.app.state.runtime.engine) as s:
        artifact = s.get(Artifact, artifact_id)
        if not artifact or not s.get(Repository, artifact.repo_id).public:
            raise HTTPException(404, '公开档案不存在')
        if compare:
            previous = s.get(Artifact, compare)
            if not previous or previous.repo_id != artifact.repo_id or previous.path != artifact.path:
                raise HTTPException(400, '只能比较同一公开档案的版本')
        else:
            previous = s.exec(select(Artifact).where(Artifact.repo_id == artifact.repo_id, Artifact.path == artifact.path,
                or_(Artifact.created_at < artifact.created_at, and_(Artifact.created_at == artifact.created_at, Artifact.id < artifact.id)))
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())).first()
        diff = ''.join(difflib.unified_diff((previous.content if previous else '').splitlines(keepends=True),
            artifact.content.splitlines(keepends=True), fromfile=previous.revision[:12] if previous else '初始版本', tofile=artifact.revision[:12]))
        return {**artifact.model_dump(exclude={'repo_id'}), 'previous_id': previous.id if previous else None,
                'previous_revision': previous.revision if previous else None, 'diff': diff}
