"""Paginated public evidence for repository activity and external contributions."""
from datetime import datetime, timezone
from sqlmodel import Session, select
from app.hosted import github
from app.hosted.models import Repository, Subscription, Job, Issue, now
from app.hosted.opportunities import DEFAULTS, epoch

EXTERNAL = {'NONE', 'FIRST_TIMER', 'FIRST_TIME_CONTRIBUTOR', 'CONTRIBUTOR'}


async def refresh_signals(rt, job):
    with Session(rt.engine) as s:
        repo = s.get(Repository, job.payload['repo_id'])
        subs = s.exec(select(Subscription).where(Subscription.repo_id == repo.id, Subscription.paused == False)).all() if repo else []
    if not repo or not repo.public or not subs:
        return {'skipped': True}
    info = await github.public_repo(rt, repo.full_name)
    days = max([DEFAULTS['external_merge_within_days']] + [sub.opportunity.get('external_merge_within_days', 90) for sub in subs])
    started = int(job.payload.get('started', now()))
    # One extra day makes the completed window usable as wall time advances.
    cutoff = int(job.payload.get('cutoff', started - (days + 1) * 86400))
    latest = int(job.payload.get('latest', 0))
    page = int(job.payload.get('page', 1))
    while True:
        pulls = await github.get(rt, f'/repos/{info["full_name"]}/pulls',
            {'state': 'closed', 'sort': 'updated', 'direction': 'desc', 'per_page': 100, 'page': page}, cached=True)
        for pr in pulls:
            if pr.get('author_association') in EXTERNAL and pr.get('merged_at'):
                latest = max(latest, epoch(pr['merged_at']))
        finished = len(pulls) < 100 or any(0 < epoch(pr.get('updated_at')) < cutoff for pr in pulls)
        with Session(rt.engine) as s:
            durable = s.get(Job, job.id)
            if finished:
                row = s.get(Repository, repo.id)
                row.pushed_at = epoch(info.get('pushed_at'))
                row.external_merge_at, row.signals_checked_at = latest, started
                row.signals_coverage_since = 1 if len(pulls) < 100 else cutoff
                s.add(row)
                s.flush()
                # Analysis can finish before repository evidence. Re-evaluate
                # eligibility then, without duplicating anyone's notification.
                from app.hosted.corpus import create_event
                for issue in s.exec(select(Issue).where(Issue.repo_id == row.id,
                        Issue.status == 'open', Issue.difficulty != '未分析')):
                    create_event(s, issue, 'opportunity', '证据更新',
                        f'#{issue.number} 发现可参与的 Issue', issue.summary)
            elif durable:
                durable.payload = {**job.payload, 'page': page + 1, 'started': started, 'latest': latest, 'cutoff': cutoff}
                s.add(durable)
            s.commit()
        if finished:
            return {'pages': page}
        page += 1
