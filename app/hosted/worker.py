import re
from sqlmodel import Session, select
from app.hosted.models import Issue, Repository, Subscription, now
from app.hosted.corpus import scan_repository, sync_candidates, create_event, status_of
from app.hosted import github
from app.hosted.analysis import analyze_issue, analyze_repository
from app.hosted.translations import translate_titles
from app.hosted.scheduler import make_digest, tick
from app.hosted.mail import send
from app.hosted.signals import refresh_signals
from app.hosted.observations import watch_issues, analyze_claim


async def handle(rt, job):
    if job.kind == 'cleanup':
        from app.hosted.operations import cleanup
        return cleanup(rt.engine)
    handlers = {'translate_titles': translate_titles, 'analyze_claim': analyze_claim, 'repo_signals': refresh_signals, 'scan': scan_repository, 'candidates': sync_candidates, 'analyze_issue': analyze_issue,
                'analyze_repo': analyze_repository, 'mail': send, 'digest': make_digest, 'watch_issues': watch_issues}
    if job.kind == 'tick':
        tick(rt)
        return {'ok': True}
    if job.kind not in handlers:
        raise ValueError('Unknown job type')
    return await handlers[job.kind](rt, job)
