"""Durable quotas work even when Redis is unavailable; no raw IPs are stored."""
import hashlib
from fastapi import HTTPException, Request
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session
from app.hosted.models import Quota, now


def consume(engine, key: str, limit: int, window: int = 3600):
    consume_many(engine, [(key, limit)], window)


def consume_many(engine, limits: list[tuple[str, int]], window: int = 3600):
    """Reserve all relevant budgets atomically; a rejected repo costs no global quota."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
    current = now()
    with Session(engine) as s:
        for key, limit in sorted(limits):
            bucket = f'{key}:{current // window}'
            s.execute(insert(Quota).values(key=bucket, count=0, expires_at=(current // window + 1) * window).on_conflict_do_nothing(index_elements=['key']))
            result = s.execute(update(Quota).where(Quota.key == bucket, Quota.count < limit).values(count=Quota.count + 1))
            if not result.rowcount:
                s.rollback()
                raise HTTPException(429, '本时段请求额度已用完，请稍后再试', headers={'Retry-After': str(window - current % window)})
        s.commit()


def guest_limit(request: Request):
    rt = request.app.state.runtime
    # Use the actual peer, not an attacker-controlled X-Forwarded-For header.
    peer = request.client.host if request.client else 'unknown'
    key = hashlib.sha256((rt.settings.encryption_key + peer).encode()).hexdigest()
    consume(rt.engine, 'guest:' + key, 30, 3600)
