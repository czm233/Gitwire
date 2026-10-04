"""PostgreSQL is the durable queue; workers poll independent execution lanes.

Atomic leases and unique active keys make requests coalesce across processes.
Every handler must be idempotent because a lost worker may be retried.
"""
import asyncio
import contextlib
import secrets
from sqlalchemy import update, or_, case
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from app.hosted.models import Job, JobAccess, Issue, Subscription, Quota, now

LEASE_SECONDS = 120


class DeferJob(Exception):
    """Expected waiting (quota/quiet hours) must not exhaust failure retries."""
    def __init__(self, message: str, seconds: int):
        self.safe_message = message
        self.retry_after = max(1, seconds)


CONTROL_KINDS = {'tick', 'mail', 'digest', 'cleanup'}
MODEL_KINDS = {'translate_titles', 'analyze_issue', 'analyze_repo', 'analyze_claim'}


def enqueue(engine, kind: str, payload: dict, key: str, user_id: str | None = None,
            requester: str | None = None, available_at: int | None = None) -> Job:
    with Session(engine) as s:
        job = s.exec(select(Job).where(Job.active_key == key)).first()
        if job is None:
            job = Job(kind=kind, payload=payload, active_key=key, user_id=user_id,
                      available_at=available_at if available_at is not None else now())
            s.add(job)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                job = s.exec(select(Job).where(Job.active_key == key)).one()
        viewers = {v for v in (user_id, requester) if v}
        if job.user_id:
            viewers = {job.user_id}  # Personal task metadata never spreads to other accounts.
        elif kind in {'scan', 'repo_signals', 'analyze_repo', 'analyze_issue', 'analyze_claim', 'watch_issues'}:
            repo_id = payload.get('repo_id')
            if kind in {'analyze_issue', 'analyze_claim'}:
                issue = s.get(Issue, payload.get('issue_id', ''))
                repo_id = issue.repo_id if issue else None
            if repo_id:
                subs = s.exec(select(Subscription).where(Subscription.repo_id == repo_id,
                    Subscription.paused == False)).all()
                viewers.update(sub.user_id for sub in subs if
                    kind in {'scan', 'repo_signals'} or
                    (kind in {'analyze_issue', 'analyze_claim'} and 'issue-radar' in sub.recipes) or
                    (kind == 'analyze_repo' and set(sub.recipes) - {'issue-radar'}) or
                    (kind == 'watch_issues' and (sub.watched_issues or 'issue-radar' in sub.recipes)))
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
        for viewer in sorted(viewers):
            s.execute(insert(JobAccess).values(job_id=job.id, user_id=viewer).on_conflict_do_nothing())
        s.commit()
        s.refresh(job)
        return job


def claim(engine, owner: str, lane: str | None = None) -> Job | None:
    with Session(engine) as s:
        gate = s.get(Quota, 'ops:drain')
        if gate and gate.expires_at > now():
            return None
        eligible = or_(
            (Job.status == 'pending') & (Job.available_at <= now()),
            (Job.status == 'running') & (Job.lease_until < now()),
        )
        stmt = select(Job).where(eligible)
        if lane == 'control':
            stmt = stmt.where(Job.kind.in_(CONTROL_KINDS))
        elif lane == 'model':
            stmt = stmt.where(Job.kind.in_(MODEL_KINDS))
        elif lane == 'io':
            stmt = stmt.where(Job.kind.not_in(CONTROL_KINDS | MODEL_KINDS))
        stmt = stmt.order_by(case((Job.kind == 'tick', 0), (Job.kind == 'mail', 1), (Job.kind.in_(['candidates', 'analyze_claim', 'translate_titles']), 2), (Job.kind == 'watch_issues', 3), (Job.kind == 'scan', 4), else_=5), Job.available_at, Job.created_at).with_for_update(skip_locked=True).limit(1)
        job = s.exec(stmt).first()
        if not job:
            return None
        if job.attempts >= job.max_attempts:
            job.status, job.active_key, job.finished_at = 'failed', None, now()
            job.error = '任务多次中断，请手动重试'
            s.add(job)
            fail_delivery(s, job, job.error)
            s.commit()
            return None
        # Compare-and-swap also protects SQLite-based tests and a competing claimant.
        result = s.execute(update(Job).where(Job.id == job.id, eligible).values(
            status='running', lease_owner=owner, lease_until=now() + LEASE_SECONDS,
            attempts=Job.attempts + 1,
        ))
        s.commit()
        if result.rowcount != 1:
            return None
        s.refresh(job)
        return job


def fail_delivery(session, job, error):
    if job.kind == 'mail':
        from app.hosted.models import Delivery
        delivery = session.get(Delivery, job.payload.get('delivery_id', ''))
        if delivery and delivery.status == 'pending':
            delivery.status, delivery.error = 'failed', error
            session.add(delivery)


def complete(engine, job: Job, result: dict | None = None, error: str = '', retry_after: int = 0,
             deferred: bool = False, terminal: bool = False):
    with Session(engine) as s:
        row = s.exec(select(Job).where(Job.id == job.id).with_for_update()).first()
        if not row or row.status != 'running' or row.lease_owner != job.lease_owner:
            return
        if deferred:
            row.status = 'pending'
            row.attempts = max(0, row.attempts - 1)
            row.available_at = now() + max(1, retry_after)
        elif error and not terminal and row.attempts < row.max_attempts:
            row.status = 'pending'
            row.available_at = now() + max(retry_after, min(3600, 15 * 2 ** row.attempts))
        else:
            row.status = 'failed' if error else 'completed'
            row.active_key = None
            row.finished_at = now()
            if error:
                fail_delivery(s, row, error)
        row.error = error[:300]
        row.result = result or {}
        row.lease_owner, row.lease_until = '', 0
        s.add(row)
        s.commit()


async def execute(runtime, job: Job, handler):
    async def heartbeat():
        while True:
            await asyncio.sleep(30)
            with Session(runtime.engine) as s:
                result = s.execute(update(Job).where(
                    Job.id == job.id, Job.status == 'running', Job.lease_owner == job.lease_owner,
                ).values(lease_until=now() + LEASE_SECONDS))
                s.commit()
                if result.rowcount != 1:
                    raise RuntimeError('Task lease lost')

    task = asyncio.create_task(handler(runtime, job))
    pulse = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait([task, pulse], timeout=1800, return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            raise RuntimeError('任务超时或执行租约丢失')
        result = await task
        complete(runtime.engine, job, result)
    except asyncio.CancelledError:
        raise  # lease recovery is deliberately left to the next worker
    except Exception as exc:
        # Provider exceptions may include URLs or credentials. Never persist raw messages.
        message = getattr(exc, 'safe_message', f'任务失败（{type(exc).__name__}），将按规则重试')
        from fastapi import HTTPException
        from app.hosted.github import GitHubFailure
        deferred = isinstance(exc, DeferJob) or (isinstance(exc, HTTPException) and exc.status_code == 429) or (isinstance(exc, GitHubFailure) and exc.status == 429)
        retry_after = getattr(exc, 'retry_after', 0)
        if isinstance(exc, HTTPException) and exc.status_code == 429:
            retry_after = int((exc.headers or {}).get('Retry-After', '60'))
            message = '请求额度已用完，等待下一个额度周期'
        terminal = isinstance(exc, GitHubFailure) and exc.status in {401, 404}
        complete(runtime.engine, job, error=message, retry_after=retry_after, deferred=deferred, terminal=terminal)
    finally:
        for t in (task, pulse):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


async def worker_loop(runtime, handler, stop=None):
    stop = stop or asyncio.Event()
    async def lane_loop(lane):
        owner = secrets.token_hex(16)
        while not stop.is_set():
            job = await asyncio.to_thread(claim, runtime.engine, owner, lane)
            if job:
                await execute(runtime, job, handler)
            else:
                await asyncio.sleep(1)

    # Slow model calls cannot block mail, scheduling or GitHub synchronization.
    async with asyncio.TaskGroup() as group:
        from app.hosted.operations import pulse
        group.create_task(pulse(runtime.engine, 'worker', stop))
        for lane in ('control', 'io', 'model'):
            group.create_task(lane_loop(lane))
