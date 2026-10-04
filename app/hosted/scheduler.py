"""One lightweight scheduler enqueues durable jobs; web processes never schedule."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlmodel import Session, select
from app.hosted.jobs import enqueue, DeferJob
from app.hosted.models import Account, Candidate, Delivery, Digest, Event, Issue, Job, Notification, Repository, Subscription, now


def tick(rt):
    from app.hosted.mail import queue_notifications
    # One bounded cleanup per hour; completed jobs and business history remain intact.
    from app.hosted.limits import consume
    from fastapi import HTTPException
    try:
        consume(rt.engine, 'cleanup-tick', 1, 3600)
    except HTTPException:
        pass
    else:
        enqueue(rt.engine, 'cleanup', {}, 'maintenance:cleanup')
    with Session(rt.engine) as s:
        users = s.exec(select(Account)).all()
        subscriptions = s.exec(select(Subscription).where(Subscription.paused == False)).all()
        repo_ids = {r.repo_id for r in subscriptions}
        repos = s.exec(select(Repository).where(Repository.id.in_(repo_ids), Repository.public == True)).all() if repo_ids else []
        active = s.exec(select(Job).where(Job.status.in_(['pending', 'running']))).all()
        active_scans = {j.payload.get('repo_id') for j in active if j.kind == 'scan'}
    for repo in repos:
        if repo.signals_checked_at < now() - 12 * 3600:
            enqueue(rt.engine, 'repo_signals', {'repo_id': repo.id}, 'signals:' + repo.id)
        if repo.next_scan <= now() and repo.id not in active_scans:
            enqueue(rt.engine, 'scan', {'repo_id': repo.id}, f'scan:{repo.id}:1')
            enqueue(rt.engine, 'analyze_repo', {'repo_id': repo.id}, 'analyze_repo:' + repo.id)
        if any(sub.repo_id == repo.id and (sub.watched_issues or 'issue-radar' in sub.recipes) for sub in subscriptions):
            # The key is recorded in durable quota state so completed jobs aren't re-enqueued each tick.
            from app.hosted.limits import consume
            from fastapi import HTTPException
            try:
                consume(rt.engine, 'watch-tick:' + repo.id, 1, rt.settings.issue_scan_minutes * 60)
            except HTTPException:
                pass
            else:
                enqueue(rt.engine, 'watch_issues', {'repo_id': repo.id}, 'watch:' + repo.id)
    for user in users:
        day = due_date(user)
        if day:
            enqueue(rt.engine, 'digest', {'date': day}, f'digest:{user.id}:{day}', user_id=user.id, requester=user.id)
    queue_notifications(rt)


def due_date(user, at=None):
    """Catch up the most recent due report, even after restart before today's hour.

    Missed days become one report covering all unreported records, not a burst of
    emails for every day the server was offline. Dates remain unique per account.
    """
    local = datetime.fromtimestamp(now() if at is None else at, ZoneInfo(user.timezone))
    due = local.replace(hour=user.digest_hour, minute=0, second=0, microsecond=0)
    if local < due:
        due -= timedelta(days=1)
    day = due.strftime('%Y-%m-%d')
    if day <= user.digest_date or due.timestamp() < user.created_at:
        return None
    return day


def prepare_digest_stars(rt, job, user):
    """Persist the dependency before yielding the control lane to mail/ticks.

    A failed/slow GitHub request never blocks reports indefinitely. Incomplete
    generations remain unreported and are picked up after a successful sync.
    """
    if user.reconnect_required or not user.access_token:
        return 'GitHub 未连接，本次仅使用已完成同步的候选；重新连接后继续检查新增 Star。'
    dependency_id = job.payload.get('star_job_id')
    if not dependency_id:
        dependency = enqueue(rt.engine, 'candidates', {'source': 'starred'},
            f'candidates:{user.id}:starred:1', user_id=user.id)
        with Session(rt.engine) as s:
            durable = s.get(Job, job.id)
            payload = {**job.payload, 'star_job_id': dependency.id, 'star_wait_started': now()}
            if durable:
                durable.payload = payload; s.add(durable); s.commit()
            job.payload = payload
        dependency_id = dependency.id
    with Session(rt.engine) as s:
        dependency = s.get(Job, dependency_id)
        if not dependency or dependency.user_id != user.id or dependency.kind != 'candidates' or dependency.payload.get('source') != 'starred':
            return 'Star 同步记录不可用，候选暂未更新，可在候选仓库手动同步。'
        if dependency.status == 'completed':
            return ''
        if dependency.status in {'pending', 'running'} and now() - job.payload.get('star_wait_started', job.created_at) < 900:
            raise DeferJob('等待本次 Star 同步完成后生成晨报', 30)
        return '本次 Star 同步尚未完成或失败，仅汇总已完成同步的候选；遗漏的新候选会在后续晨报补入。'


async def make_digest(rt, job):
    date = job.payload['date']
    with Session(rt.engine) as s:
        user = s.get(Account, job.user_id)
        if not user:
            return {'skipped': True}
        existing = s.exec(select(Digest).where(Digest.user_id == user.id, Digest.date == date)).first()
        if existing:
            return {'digest_id': existing.id}
        if date < user.digest_date:
            return {'skipped': True}  # A delayed older job must not move the daily cursor backwards.
    star_note = prepare_digest_stars(rt, job, user)
    with Session(rt.engine) as s:
        # Serialize report creation per account, including concurrent recovery jobs.
        user = s.exec(select(Account).where(Account.id == job.user_id).with_for_update()).first()
        if not user:
            return {'skipped': True}
        existing = s.exec(select(Digest).where(Digest.user_id == user.id, Digest.date == date)).first()
        if existing:
            return {'digest_id': existing.id}
        if date < user.digest_date:
            return {'skipped': True}
        previous = s.exec(select(Digest).where(Digest.user_id == user.id)
            .order_by(Digest.period_end.desc(), Digest.created_at.desc()).limit(1)).first()
        cutoff = now()
        start_at = previous.period_end if previous and previous.period_end else user.created_at
        tz = ZoneInfo(user.timezone)
        start, end = (datetime.fromtimestamp(t, tz) for t in (start_at, cutoff))
        rows = s.exec(select(Notification, Event, Repository).select_from(Notification).join(Event, Notification.event_id == Event.id)
            .join(Repository, Event.repo_id == Repository.id).where(Notification.user_id == user.id,
                Notification.digest_id == None, Notification.created_at <= cutoff, Repository.public == True)
            .order_by(Notification.created_at.desc(), Notification.id.desc())).all()
        completed_stars = select(Job.id).where(Job.user_id == user.id, Job.kind == 'candidates', Job.status == 'completed')
        new_stars = s.exec(select(Candidate).where(Candidate.user_id == user.id, Candidate.source == 'starred',
            Candidate.ignored == False, Candidate.present == True, Candidate.digest_id == None,
            Candidate.discovered_at <= cutoff, Candidate.seen_generation.in_(completed_stars))
            .order_by(Candidate.discovered_at.desc(), Candidate.starred_at.desc(), Candidate.id.desc())).all()
        lines = [f'# {date} 开源晨报', '', f'汇总范围：{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}（{user.timezone}）',
                 '包含此前延迟到达、尚未汇总的记录；已汇总内容不会重复。', '', '## 关注动态', '']
        for _, event, repo in rows:
            lines.append(f'- [{repo.full_name} · {event.title}]({event.url})：{event.body}')
        if not rows:
            lines.append('本时段没有符合提醒规则的新动态。')
        lines += ['', '## 新增 Star 候选', '']
        if star_note:
            lines += [star_note, '']
        lines += [f'- [{c.full_name}](https://github.com/{c.full_name})：{c.description}' for c in new_stars] or ['本时段没有新增候选。']
        lines += ['', '候选项目不会自动加入监控，请在候选仓库中选择。']
        content = '\n'.join(lines)
        row = Digest(user_id=user.id, date=date, content=content, period_start=start_at, period_end=cutoff)
        s.add(row); s.flush()
        for notification, _, _ in rows:
            notification.digest_id = row.id; s.add(notification)
        for candidate in new_stars:
            candidate.digest_id = row.id; s.add(candidate)
        user.digest_date = date
        s.add(user)
        if user.email_enabled and user.email_verified and user.email_mode in {'digest', 'both'}:
            s.add(Delivery(user_id=user.id, key=f'digest:{user.id}:{date}', recipient=user.email,
                purpose='digest', subject=f'Gitwire · {date} 开源晨报', body=rt.box.seal(content)))
        s.commit()
        return {'digest_id': row.id}


async def loop(rt, stop=None):
    stop = stop or asyncio.Event()
    while not stop.is_set():
        from app.hosted.operations import heartbeat
        heartbeat(rt.engine, 'scheduler')
        enqueue(rt.engine, 'tick', {}, 'scheduler:tick')
        try:
            await asyncio.wait_for(stop.wait(), 30)
        except asyncio.TimeoutError:
            pass
