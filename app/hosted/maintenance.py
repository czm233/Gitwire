"""Durable, reversible queue drain for graceful local service restarts."""
from sqlalchemy import update, func
from sqlmodel import Session, select
from app.hosted.models import Job, Quota, now

KEY = 'ops:drain'
PREFIX = 'ops:drain-job:'


def drain(engine, seconds=2100):
    with Session(engine) as s:
        gate = s.get(Quota, KEY)
        if not gate or gate.expires_at <= now():
            for record in s.exec(select(Quota).where(Quota.key.startswith(PREFIX))):
                s.execute(update(Job).where(Job.id == record.key.removeprefix(PREFIX), Job.status == 'pending',
                    Job.available_at == record.expires_at).values(available_at=record.count))
                s.delete(record)
            gate = Quota(key=KEY, count=0, expires_at=now()+seconds)
            s.merge(gate)
            s.flush()
        # Older workers do not know the gate yet. Temporarily defer pending jobs,
        # recording their exact original schedule in the same DB transaction.
        for job in s.exec(select(Job).where(Job.status == 'pending', Job.available_at < gate.expires_at)
                .with_for_update(skip_locked=True)):
            key = PREFIX + job.id
            if not s.get(Quota, key):
                s.add(Quota(key=key, count=job.available_at, expires_at=gate.expires_at))
            job.available_at = gate.expires_at
            s.add(job)
        s.commit()
        return s.exec(select(func.count()).select_from(Job).where(Job.status == 'running', Job.lease_until >= now())).one()


def resume(engine):
    with Session(engine) as s:
        for record in s.exec(select(Quota).where(Quota.key.startswith(PREFIX))):
            s.execute(update(Job).where(Job.id == record.key.removeprefix(PREFIX), Job.status == 'pending',
                Job.available_at == record.expires_at).values(available_at=record.count))
            s.delete(record)
        gate = s.get(Quota, KEY)
        if gate:
            s.delete(gate)
        s.commit()
