"""Operator-only aggregate health and bounded expiry of reconstructable records."""
import asyncio
from sqlalchemy import func
from sqlmodel import Session, select
from app.hosted.models import (BrowserSession, OAuthAttempt, EmailVerification,
                               HttpCache, Quota, Job, Delivery, now)

HEARTBEAT = 'ops:heartbeat:'


def heartbeat(engine, role):
    if role not in {'worker', 'scheduler'}:
        raise ValueError('Unknown service role')
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
    stamp = now()
    with Session(engine) as s:
        s.execute(insert(Quota).values(key=HEARTBEAT + role, count=stamp, expires_at=stamp + 86400)
                  .on_conflict_do_update(index_elements=['key'], set_={'count': stamp, 'expires_at': stamp + 86400}))
        s.commit()


async def pulse(engine, role, stop):
    while not stop.is_set():
        await asyncio.to_thread(heartbeat, engine, role)
        try:
            await asyncio.wait_for(stop.wait(), 15)
        except asyncio.TimeoutError:
            pass


def snapshot(engine):
    """No IDs, credentials, recipients, payloads or repository names in this view."""
    stamp = now()
    with Session(engine) as s:
        beats = {}
        for role in ('worker', 'scheduler'):
            row = s.get(Quota, HEARTBEAT + role)
            age = max(0, stamp - row.count) if row else None
            beats[role] = {'age_seconds': age, 'recent': age is not None and age <= 90}
        ready = (Job.status == 'pending', Job.available_at <= stamp)
        oldest = s.exec(select(func.min(Job.available_at)).where(*ready)).one()
        count = lambda *conditions: s.exec(select(func.count()).select_from(Job).where(*conditions)).one()
        gate = s.get(Quota, 'ops:drain')
        return {'checked_at': stamp, 'heartbeats': beats, 'draining': bool(gate and gate.expires_at > stamp),
                'queue': {'ready': count(*ready), 'oldest_ready_seconds': max(0, stamp - oldest) if oldest is not None else 0,
                          'deferred': count(Job.status == 'pending', Job.available_at > stamp),
                          'expired_leases': count(Job.status == 'running', Job.lease_until < stamp),
                          'by_kind_status': [{'kind': kind, 'status': status, 'count': total} for kind, status, total in
                              s.exec(select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status))]},
                'mail_by_status': dict(s.exec(select(Delivery.status, func.count()).group_by(Delivery.status)).all())}


def cleanup(engine, batch=500):
    """Small locked batches; preserve business history, daily dependencies and drain recovery."""
    stamp = now()
    batch = max(1, min(batch, 2000))
    policies = [(OAuthAttempt, OAuthAttempt.expires_at < stamp - 3600),
                (BrowserSession, BrowserSession.expires_at < stamp - 3600),
                (EmailVerification, EmailVerification.expires_at < stamp - 3600),
                (HttpCache, HttpCache.updated_at < stamp - 7 * 86400),
                (Quota, (Quota.expires_at < stamp - 86400) & ~Quota.key.startswith('ops:'))]
    result = {}
    for model, condition in policies:
        with Session(engine) as s:
            rows = s.exec(select(model).where(condition).limit(batch).with_for_update(skip_locked=True)).all()
            for row in rows:
                s.delete(row)
            s.commit()
            result[model.__tablename__] = len(rows)
    return result
