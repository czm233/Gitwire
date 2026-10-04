#!/usr/bin/env python3
"""Local real-GitHub → scheduled digest → Mailpit smoke in a disposable schema.

Uses an existing account's unexpired access token read-only, never copies its
refresh token, never changes the source account, and never sends external mail.
"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, select
from app.config import Settings
from app.hosted.models import Account, Candidate, Delivery, Digest, Job, now
from app.hosted.runtime import Runtime
from app.hosted.scheduler import loop
from app.hosted.jobs import worker_loop
from app.hosted.worker import handle
from acceptance import registered, ROOT


async def verify(login):
    registered()
    settings = Settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() != 'postgresql' or url.host not in {'localhost', '127.0.0.1'} or url.port != 10243:
        raise RuntimeError('This verification only uses registered local Gitwire PostgreSQL')
    admin = create_engine(url)
    schema = 'acceptance_digest_' + uuid.uuid4().hex
    rt = None
    stop = asyncio.Event()
    tasks = []
    try:
        with Session(admin) as s:
            source = s.exec(select(Account).where(Account.login == login)).one()
            if source.reconnect_required or not source.access_token or (source.token_expires and source.token_expires < now()+600):
                raise RuntimeError('Sign in first; existing grant must remain valid for at least ten minutes')
            # No source preferences, contact details, private history or refresh grant are copied.
            grant = {'github_id': source.github_id, 'login': source.login,
                     'access_token': source.access_token, 'token_expires': source.token_expires}
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        cfg = settings.model_copy(update={
            'database_url': url.update_query_dict({'options': '-csearch_path='+schema}).render_as_string(hide_password=False),
            'redis_url': '', 'smtp_host': '127.0.0.1', 'smtp_port': 10245,
            'smtp_username': '', 'smtp_password': '', 'smtp_starttls': False,
            'mail_from': 'Gitwire acceptance <acceptance@example.test>',
            'github_token': '', 'llm_api_key': '', 'bark_url': '', 'gitwire_vault': '',
        })
        rt = Runtime.create(cfg)
        SQLModel.metadata.create_all(rt.engine, tables=[t for t in SQLModel.metadata.sorted_tables if t.name.startswith('h_')])
        local = datetime.now(ZoneInfo('Asia/Shanghai'))
        user = Account(**grant, timezone='Asia/Shanghai', digest_hour=local.hour, created_at=now()-86400,
                       email='gitwire-digest-acceptance@example.test', email_enabled=True, email_verified=True,
                       email_mode='digest', quiet_start=0, quiet_end=0)
        with Session(rt.engine) as s:
            s.add(user); s.commit()
        tasks = [asyncio.create_task(loop(rt, stop)), asyncio.create_task(worker_loop(rt, handle, stop))]
        deadline, previous = time.monotonic()+180, None
        while time.monotonic() < deadline:
            for task in tasks:
                if task.done():
                    task.result()
                    raise RuntimeError('Acceptance background service stopped early')
            with Session(rt.engine) as s:
                delivery = s.exec(select(Delivery).where(Delivery.purpose == 'digest')).first()
                jobs = s.exec(select(Job)).all()
                progress = sorted((j.kind, j.status) for j in jobs)
                if progress != previous:
                    print('Progress:', progress, flush=True); previous = progress
                if delivery and delivery.status == 'sent':
                    reports = s.exec(select(Digest)).all()
                    candidates = s.exec(select(Candidate).order_by(Candidate.discovered_at.desc(),
                        Candidate.starred_at.desc(), Candidate.id.desc())).all()
                    sync = [j for j in jobs if j.kind == 'candidates' and j.status == 'completed']
                    assert len(reports) == 1 and sync and candidates
                    assert all(c.digest_id == reports[0].id for c in candidates)
                    assert all(c.full_name in reports[0].content for c in candidates)
                    positions = [reports[0].content.index('[' + c.full_name + ']') for c in candidates]
                    assert positions == sorted(positions), 'Digest candidates are not newest first'
                    assert reports[0].date == local.strftime('%Y-%m-%d')
                    # Another actual tick must not add a second report/outbox.
                    from app.hosted.scheduler import tick
                    tick(rt)
                    assert len(s.exec(select(Delivery)).all()) == 1
                    result = {'ok': True, 'date': reports[0].date, 'candidates': len(candidates),
                              'star_sync_pages': sync[0].result.get('pages'), 'reports': 1,
                              'mail_status': delivery.status, 'scope': 'isolated local schema; real OAuth read; Mailpit only'}
                    with httpx.Client() as client:
                        messages = client.get('http://127.0.0.1:10246/api/v1/messages').raise_for_status().json()['messages']
                    matches = [m for m in messages if any(t.get('Address') == delivery.recipient for t in m['To'])
                               and m['Subject'] == delivery.subject
                               and m['MessageID'].strip('<>') == delivery.id + '@gitwire.local']
                    assert matches, 'SMTP accepted, but no Mailpit receipt found'
                    result['mailpit_message_id'] = matches[0]['ID']
                    directory = ROOT / 'data' / 'acceptance-digest'
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path = directory / ('verification-' + uuid.uuid4().hex[:8] + '.json')
                    path.write_text(json.dumps(result, ensure_ascii=False, indent=2)); path.chmod(0o600)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    print('Evidence:', path, flush=True)
                    return
                if any(j.status == 'failed' for j in jobs) or (delivery and delivery.status in {'failed', 'cancelled'}):
                    raise RuntimeError('Acceptance job failed; no credentials or job payloads printed')
            await asyncio.sleep(1)
        raise RuntimeError('Acceptance did not finish within three minutes')
    finally:
        stop.set()
        try:
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 45)
        finally:
            if rt:
                await rt.close()
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            admin.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--login', required=True, help='Existing local GitHub login, never a token')
    asyncio.run(verify(parser.parse_args().login))
