"""Hosted boundaries: test two real DB identities without exposing a dev login route."""
import hashlib
import os
import uuid
from urllib.parse import urlparse, parse_qs
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, select
from app.config import Settings
from app.hosted.application import create_hosted_app
from app.hosted.models import Account, BrowserSession, Repository, Subscription, Candidate, Job, JobAccess, Notification, Event, now
from app.hosted.runtime import Runtime
from app.hosted.security import digest
from app.hosted.jobs import enqueue, claim, complete


@pytest.fixture(params=['sqlite', 'postgres'] if os.getenv('GITWIRE_TEST_POSTGRES') == '1' else ['sqlite'])
def hosted(tmp_path, request):
    cfg = Settings(_env_file=None, database_url=f'sqlite:///{tmp_path}/test.db',
        encryption_key=Fernet.generate_key().decode(), public_url='http://testserver',
        github_client_id='test-id', github_client_secret='test-secret')
    admin, schema = None, None
    if request.param == 'postgres':
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url
        url = make_url(Settings().database_url)
        assert url.get_backend_name() == 'postgresql'
        schema = 'test_gitwire_' + uuid.uuid4().hex
        admin = create_engine(url)
        with admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA "{schema}"'))
        cfg.database_url = url.update_query_dict({'options': '-csearch_path=' + schema}).render_as_string(hide_password=False)
    rt = Runtime.create(cfg)
    SQLModel.metadata.create_all(rt.engine)
    with Session(rt.engine) as s:
        a, b = Account(id='alice', github_id='100', login='alice'), Account(id='bob', github_id='200', login='bob')
        s.add_all([a, b])
        s.commit()
        s.add_all([BrowserSession(digest=digest('session-alice'), user_id='alice', csrf='csrf-alice', expires_at=now()+3600),
                   BrowserSession(digest=digest('session-bob'), user_id='bob', csrf='csrf-bob', expires_at=now()+3600)])
        repo = Repository(id='repo', github_id='10', full_name='public/project', pushed_at=now(), external_merge_at=now(), signals_checked_at=now(), signals_coverage_since=1)
        s.add(repo)
        s.commit()
        s.add_all([Subscription(id='sub-alice', user_id='alice', repo_id=repo.id),
                   Subscription(id='sub-bob', user_id='bob', repo_id=repo.id),
                   Candidate(id='candidate-bob', user_id='bob', github_id='20', full_name='bob/private-choice')])
        s.commit()
    try:
        with TestClient(create_hosted_app(cfg, rt)) as client:
            yield client, rt
    finally:
        rt.engine.dispose()
        if admin:
            with admin.begin() as c:
                c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def login(client, user='alice'):
    client.cookies.set('gitwire_session', 'session-' + user)
    client.headers.update({'X-CSRF-Token': 'csrf-' + user})


def test_guest_can_only_read_public_surface(hosted):
    client, rt = hosted
    assert client.get('/api/auth/me').json()['user'] is None
    assert client.get('/api/issues').status_code == 200
    for path in ['/api/repos', '/api/candidates', '/api/jobs', '/api/notifications', '/api/daily']:
        assert client.get(path).status_code == 401
    assert client.post('/api/repos', json={'repos': ['a/b']}).status_code == 401
    assert client.post('/api/login', json={'key': 'test'}).status_code in (404, 405)


def test_personal_data_isolation_and_csrf(hosted):
    client, rt = hosted
    login(client)
    assert [r['id'] for r in client.get('/api/repos').json()['repos']] == ['sub-alice']
    assert client.get('/api/candidates').json()['total'] == 0
    assert client.patch('/api/repos/sub-bob', json={'paused': True}).status_code == 404
    assert client.delete('/api/repos/sub-bob').status_code == 404
    assert client.patch('/api/candidates/candidate-bob', json={'ignored': True}).status_code == 404
    client.headers['X-CSRF-Token'] = 'csrf-bob'
    assert client.patch('/api/repos/sub-alice', json={'paused': True}).status_code == 403
    client.headers['X-CSRF-Token'] = 'csrf-alice'
    assert client.patch('/api/repos/sub-alice', json={'paused': True}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.patch('/api/repos/sub-alice', json={'paused': True}).status_code == 200
    login(client, 'bob')
    assert client.get('/api/repos').json()['repos'][0]['paused'] is False


def test_candidates_order_by_star_time_across_sync_batches_and_pages(hosted):
    client, rt = hosted
    login(client)
    with Session(rt.engine) as s:
        for ident, discovered, starred in [
            ('older-star', 900, '2024-01-01T01:00:00Z'),
            ('recent-star-a', 100, '2026-10-05T01:00:00Z'),
            ('recent-star-b', 90, '2026-10-05T01:00:00Z'),
            ('missing-star', 1000, ''),
        ]:
            s.add(Candidate(id=ident, user_id='alice', github_id=ident,
                full_name='test/' + ident, discovered_at=discovered, starred_at=starred))
        for ident, discovered in [('owned-old', 100), ('owned-new', 900)]:
            s.add(Candidate(id=ident, user_id='alice', github_id=ident,
                full_name='test/' + ident, source='owned', discovered_at=discovered))
        s.commit()
    first = client.get('/api/candidates?page_size=2').json()
    second = client.get('/api/candidates?page_size=2&page=2').json()
    assert first['total'] == 4
    assert [r['id'] for r in first['items']] == ['recent-star-b', 'recent-star-a']
    assert first['items'][0]['starred_at'] == '2026-10-05T01:00:00Z'
    assert [r['id'] for r in second['items']] == ['older-star', 'missing-star']
    assert second['items'][1]['starred_at'] == ''
    owned = client.get('/api/candidates?source=owned').json()
    assert [r['id'] for r in owned['items']] == ['owned-new', 'owned-old']


def test_owned_jobs_and_notification_visibility(hosted):
    client, rt = hosted
    j = enqueue(rt.engine, 'candidates', {'source': 'starred'}, 'candidates:bob', user_id='bob')
    with Session(rt.engine) as s:
        event = Event(id='event', key='event', repo_id='repo', kind='opportunity', title='A', body='B')
        s.add(event)
        s.commit()
        s.add(Notification(id='notice-bob', user_id='bob', event_id='event'))
        s.commit()
    login(client)
    assert client.get('/api/jobs').json()['items'] == []
    assert client.get('/api/notifications').json()['items'] == []
    assert client.post('/api/notifications/notice-bob/read').status_code == 404
    login(client, 'bob')
    assert client.get('/api/jobs').json()['items'][0]['id'] == j.id
    assert client.get('/api/notifications').json()['items'][0]['id'] == 'notice-bob'


def test_queue_coalesces_and_recovers_expired_lease(hosted):
    _, rt = hosted
    a = enqueue(rt.engine, 'scan', {}, 'same', requester='alice')
    b = enqueue(rt.engine, 'scan', {}, 'same', requester='bob')
    assert a.id == b.id
    first = claim(rt.engine, 'worker-one')
    assert first and claim(rt.engine, 'worker-two') is None
    with Session(rt.engine) as s:
        row = s.get(Job, first.id)
        row.lease_until = now()-10
        s.add(row)
        s.commit()
    recovered = claim(rt.engine, 'worker-two')
    assert recovered.id == first.id and recovered.attempts == 2
    complete(rt.engine, first, {'bad': True})
    with Session(rt.engine) as s:
        assert s.get(Job, first.id).status == 'running'
    complete(rt.engine, recovered, {'ok': True})
    assert enqueue(rt.engine, 'scan', {}, 'same').id != first.id


def test_oauth_uses_pkce_single_use_state_and_encrypted_tokens(hosted):
    client, rt = hosted
    async def handle(request):
        if request.url.path == '/login/oauth/access_token':
            return httpx.Response(200, json={'access_token': 'fake-sensitive-token', 'refresh_token': 'fake-refresh', 'expires_in': 28800, 'scope': ''})
        return httpx.Response(200, json={'id': 100, 'login': 'alice-renamed', 'avatar_url': 'https://github.com/avatar.png'})
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    response = client.get('/api/auth/github', follow_redirects=False)
    params = parse_qs(urlparse(response.headers['location']).query)
    assert params['code_challenge_method'] == ['S256']
    assert params['prompt'] == ['select_account']
    assert 'scope' not in params
    state = params['state'][0]
    assert client.get('/api/auth/github/callback?state=invalid&code=x').status_code == 400
    callback = f'/api/auth/github/callback?state={state}&code=test-code'
    result = client.get(callback, follow_redirects=False)
    assert result.status_code == 303
    assert result.headers['location'] == 'http://testserver/candidates?auth=success'
    me = client.get('/api/auth/me').json()
    assert me['user']['id'] == 'alice' and me['user']['login'] == 'alice-renamed'
    assert 'fake-sensitive-token' not in str(me)
    with Session(rt.engine) as s:
        row = s.get(Account, 'alice')
        assert row.access_token != 'fake-sensitive-token'
        assert rt.box.open(row.access_token) == 'fake-sensitive-token'
    assert client.get(callback).status_code == 400


def test_oauth_callback_requires_same_browser(hosted):
    client, _ = hosted
    response = client.get('/api/auth/github', follow_redirects=False)
    state = parse_qs(urlparse(response.headers['location']).query)['state'][0]
    client.cookies.clear()
    assert client.get(f'/api/auth/github/callback?state={state}&code=x').status_code == 400


def test_email_cannot_enable_before_verification(hosted):
    client, _ = hosted
    login(client)
    assert client.patch('/api/preferences', json={'email_enabled': True}).status_code == 400
    assert client.patch('/api/preferences', json={'timezone': 'bad/timezone'}).status_code == 400


def test_database_pagination_filters_and_stats(hosted):
    from app.hosted.models import Issue
    client, rt = hosted
    login(client)
    with Session(rt.engine) as s:
        for n in range(1, 61):
            s.add(Issue(repo_id='repo', number=n, title=f'Issue {n}', url='https://github.com/public/project/issues/'+str(n),
                difficulty='简单' if n % 2 else '未分析', created_at=f'2026-09-{(n-1)//3+1:02d}T00:00:00Z'))
        sub = s.get(Subscription, 'sub-alice')
        sub.watched_issues = [1, 2]
        sub.opportunity = {key: 0 for key in sub.opportunity}
        s.add(sub)
        s.commit()
    data = client.get('/api/issues?page=2&page_size=10&group=opportunity').json()
    assert data['total'] == 30 and len(data['items']) == 10
    assert data['stats']['all'] == 60 and data['stats']['watched'] == 2
    assert data['stats']['pending'] == 30 and data['repos'] == ['public/project']
    assert all(i['difficulty'] == '简单' for i in data['items'])
    watched = client.get('/api/issues?group=watched').json()
    assert {i['number'] for i in watched['items']} == {1, 2}
    assert client.get('/api/issues?q=%25').json()['total'] == 0  # literal %, not SQL wildcard
    login(client, 'bob')
    assert client.get('/api/issues?group=watched').json()['total'] == 0


def test_worker_lanes_and_deferrals_preserve_retry_budget(hosted):
    import asyncio
    from app.hosted.jobs import execute, DeferJob
    _, rt = hosted
    model = enqueue(rt.engine, 'analyze_issue', {}, 'model')
    scan = enqueue(rt.engine, 'scan', {}, 'scan')
    control = enqueue(rt.engine, 'digest', {}, 'digest')
    assert claim(rt.engine, 'model-worker', 'model').id == model.id
    assert claim(rt.engine, 'io-worker', 'io').id == scan.id
    job = claim(rt.engine, 'control-worker', 'control')
    assert job.id == control.id
    async def wait_for_quiet_end(rt, job):
        raise DeferJob('免打扰', 7200)
    asyncio.run(execute(rt, job, wait_for_quiet_end))
    with Session(rt.engine) as s:
        row = s.get(Job, job.id)
        assert row.status == 'pending' and row.attempts == 0
        assert row.available_at >= now()+7199
    assert claim(rt.engine, 'next-worker', 'control') is None


def test_failed_delivery_is_not_automatically_requeued(hosted):
    from app.hosted.models import Delivery
    from app.hosted.mail import queue_notifications
    _, rt = hosted
    with Session(rt.engine) as s:
        s.add(Delivery(id='mail', user_id='alice', key='verify:test', recipient='test@example.com', subject='test', body=rt.box.seal('test')))
        s.commit()
    job = enqueue(rt.engine, 'mail', {'delivery_id': 'mail'}, 'mail:mail')
    job = claim(rt.engine, 'worker')
    complete(rt.engine, job, error='邮件发送失败', terminal=True)
    queue_notifications(rt)
    with Session(rt.engine) as s:
        assert s.get(Delivery, 'mail').status == 'failed'
        assert len(s.exec(select(Job).where(Job.kind == 'mail')).all()) == 1


def test_outbox_rechecks_current_subscription_before_sending(hosted, monkeypatch):
    import asyncio
    from app.hosted.models import Delivery, Issue
    from app.hosted.mail import send
    _, rt = hosted
    with Session(rt.engine) as s:
        user = s.get(Account, 'alice')
        user.email, user.email_enabled, user.email_verified, user.email_mode = 'test@example.com', True, True, 'immediate'
        s.add(user)
        s.add(Issue(repo_id='repo', number=1, title='A', url='https://github.com/public/project/issues/1', difficulty='简单'))
        s.add(Event(id='e', key='e', repo_id='repo', issue_number=1, kind='opportunity', title='A', body='B'))
        s.commit()
        s.add(Notification(id='n', user_id='alice', event_id='e'))
        s.add(Delivery(id='d', user_id='alice', key='event:n', recipient=user.email, subject='test', body=rt.box.seal('test')))
        sub = s.get(Subscription, 'sub-alice')
        sub.paused = True
        s.add(sub)
        s.commit()
    def should_not_send(*args):
        pytest.fail('Paused subscription sent an email')
    monkeypatch.setattr('app.hosted.mail._smtp', should_not_send)
    assert asyncio.run(send(rt, Job(payload={'delivery_id': 'd'}, kind='mail'))) == {'cancelled': True}
    with Session(rt.engine) as s:
        assert s.get(Delivery, 'd').status == 'cancelled'


def test_concurrent_postgres_queue_and_quota(hosted):
    from concurrent.futures import ThreadPoolExecutor
    from app.hosted.limits import consume
    from fastapi import HTTPException
    _, rt = hosted
    if rt.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL row locking requires PostgreSQL')
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda n: enqueue(rt.engine, 'scan', {}, 'shared', requester='alice' if n % 2 else 'bob'), range(16)))
        assert len({j.id for j in jobs}) == 1
        claims = list(pool.map(lambda n: claim(rt.engine, 'worker-'+str(n)), range(8)))
        assert sum(j is not None for j in claims) == 1
        def attempt(n):
            try:
                consume(rt.engine, 'concurrent', 3)
                return True
            except HTTPException as exc:
                assert exc.status_code == 429
                return False
        assert sum(pool.map(attempt, range(12))) == 3
    with Session(rt.engine) as s:
        assert len(s.exec(select(JobAccess)).all()) == 2


def test_public_redis_cache_visibility_and_fallback(hosted):
    import asyncio
    from app.hosted import github
    _, rt = hosted
    class Cache:
        def __init__(self):
            self.values = {}
            self.failed = False
        async def get(self, key):
            if self.failed:
                raise ConnectionError('test cache unavailable')
            return self.values.get(key)
        async def set(self, key, value, ex):
            if self.failed:
                raise ConnectionError('test cache unavailable')
            assert ex == 30
            self.values[key] = value
    cache = Cache()
    rt.redis = cache
    requests = []
    private = False
    async def handle(request):
        requests.append(request.url.path)
        if request.url.path == '/repos/public/project':
            return httpx.Response(200, json={'id': 10, 'full_name': 'public/project', 'private': private})
        if request.headers.get('if-none-match') == 'test-etag':
            return httpx.Response(304)
        return httpx.Response(200, json=[{'number': 1}], headers={'etag': 'test-etag'})
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async def scenario():
        nonlocal private
        assert await github.get(rt, '/repos/public/project/issues', cached=True) == [{'number': 1}]
        assert await github.get(rt, '/repos/public/project/issues', cached=True) == [{'number': 1}]
        assert requests.count('/repos/public/project/issues') == 1
        private = True
        with pytest.raises(github.GitHubFailure) as exc:
            await github.get(rt, '/repos/public/project/issues', cached=True)
        assert exc.value.status == 404
        private = False
        cache.failed = True
        assert await github.get(rt, '/repos/public/project/issues', cached=True) == [{'number': 1}]
        assert requests.count('/repos/public/project/issues') == 2
    asyncio.run(scenario())


def test_github_redirect_does_not_forward_token_to_other_hosts(hosted):
    import asyncio
    from app.hosted import github
    _, rt = hosted
    rt.settings.github_token = 'test-sensitive-token'
    paths = []
    async def handle(request):
        paths.append(str(request.url))
        return httpx.Response(301, headers={'location': 'https://untrusted.example/collect'})
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with pytest.raises(github.GitHubFailure):
        asyncio.run(github.public_repo(rt, 'public/project'))
    assert paths == ['https://api.github.com/repos/public/project']


def test_candidate_sync_resumes_and_reconciles_without_changing_choices(hosted, monkeypatch):
    import asyncio
    from app.hosted.corpus import sync_candidates
    from app.hosted.github import GitHubFailure
    client, rt = hosted
    page_calls = []
    fail = True
    async def get(rt, path, params=None, **kw):
        nonlocal fail
        if path == '/user':
            return {'id': 100, 'login': 'alice'}
        assert kw['accept'] == 'application/vnd.github.star+json'
        page_calls.append(params['page'])
        if params['page'] == 2 and fail:
            raise GitHubFailure(429, 60)
        n = 100 if params['page'] == 1 else 1
        return [{'repo': {'id': (params['page']-1)*100+i, 'full_name': f'public/repo{i}', 'private': False}, 'starred_at': '2026-10-04T00:00:00Z'} for i in range(n)]
    monkeypatch.setattr('app.hosted.corpus.github.get', get)
    with Session(rt.engine) as s:
        s.add(Candidate(id='removed', user_id='alice', github_id='9999', full_name='public/removed', ignored=True))
        s.add(Candidate(id='ignored', user_id='alice', github_id='0', full_name='public/repo0', ignored=True))
        s.commit()
    job = enqueue(rt.engine, 'candidates', {'source': 'starred'}, 'candidate-test', user_id='alice')
    with pytest.raises(GitHubFailure):
        asyncio.run(sync_candidates(rt, job))
    with Session(rt.engine) as s:
        assert s.get(Candidate, 'removed').present is True  # incomplete scan cannot mark missing
        resume = s.get(Job, job.id)
        assert resume.payload['page'] == 2
    fail = False
    asyncio.run(sync_candidates(rt, resume))
    assert page_calls == [1, 2, 2]
    with Session(rt.engine) as s:
        assert s.get(Candidate, 'removed').present is False
        assert s.get(Candidate, 'ignored').ignored is True
        assert s.get(Account, 'alice').stars_synced_at > 0
        assert s.get(Candidate, 'candidate-bob').present is True
    login(client)
    assert client.get('/api/candidates').json()['total'] == 100
    assert all(r['starred_at'] == '2026-10-04T00:00:00Z'
        for r in client.get('/api/candidates').json()['items'])


def test_scan_checkpoint_and_incremental_watermark(hosted, monkeypatch):
    import asyncio
    from app.hosted.corpus import scan_repository
    from app.hosted.github import GitHubFailure
    _, rt = hosted
    calls = []
    fail = True
    async def public_repo(rt, name):
        return {'id': 10, 'full_name': 'public/project', 'private': False}
    async def get(rt, path, params=None, **kw):
        calls.append(dict(params))
        if params['page'] == 2 and fail:
            raise GitHubFailure(429, 60)
        count = 100 if params['page'] == 1 and not params.get('since') else 0
        return [{'number': i+1, 'title': f'issue{i}', 'html_url': f'https://github.com/public/project/issues/{i+1}',
                 'created_at': '2020-01-01T00:00:00Z', 'updated_at': '2020-01-01T00:00:00Z', 'state': 'closed'} for i in range(count)]
    monkeypatch.setattr('app.hosted.corpus.github.public_repo', public_repo)
    monkeypatch.setattr('app.hosted.corpus.github.get', get)
    job = enqueue(rt.engine, 'scan', {'repo_id': 'repo'}, 'scan-test')
    with pytest.raises(GitHubFailure):
        asyncio.run(scan_repository(rt, job))
    with Session(rt.engine) as s:
        assert s.get(Repository, 'repo').issues_cursor == 0
        resume = s.get(Job, job.id)
        assert resume.payload['page'] == 2
    fail = False
    asyncio.run(scan_repository(rt, resume))
    with Session(rt.engine) as s:
        assert s.get(Repository, 'repo').issues_cursor > 0
    asyncio.run(scan_repository(rt, Job(kind='scan', payload={'repo_id': 'repo'})))
    assert [p['page'] for p in calls] == [1, 2, 2, 1]
    assert calls[-1].get('since') and calls[-1]['direction'] == 'desc'


def test_initial_inventory_does_not_flood_opportunity_notifications(hosted):
    from app.hosted.models import Issue
    from app.hosted.corpus import create_event
    _, rt = hosted
    with Session(rt.engine) as s:
        old = Issue(repo_id='repo', number=99, title='Old issue', body='', url='', difficulty='简单', created_at='2020-01-01T00:00:00Z')
        s.add(old)
        create_event(s, old, 'opportunity', '未分析', 'Old issue', 'summary')
        s.commit()
        assert not s.exec(select(Notification)).all()


def test_export_is_account_filtered_and_has_no_credentials(hosted):
    import io
    import json
    import zipfile
    from app.hosted.models import Digest, Artifact
    client, rt = hosted
    assert client.get('/api/export').status_code == 401
    with Session(rt.engine) as s:
        alice = s.get(Account, 'alice')
        alice.access_token, alice.refresh_token, alice.email = 'secret-access-marker', 'secret-refresh-marker', 'private-email-marker@example.com'
        s.add(alice)
        sub = s.get(Subscription, 'sub-alice')
        sub.opportunity = {'max_age_days': 12, 'max_comments': 7, 'repo_pushed_within_days': 0, 'external_merge_within_days': 45}
        s.add(sub)
        s.add_all([Digest(user_id='alice', date='2026-10-01', content='alice-only-digest'),
                   Digest(user_id='bob', date='2026-10-02', content='bob-private-digest'),
                   Artifact(repo_id='repo', path='overview.md', revision='test', content='public-analysis')])
        s.commit()
    login(client)
    response = client.get('/api/export')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        content = '\n'.join(archive.read(n).decode() for n in archive.namelist())
        assert 'alice-only-digest' in content and 'public-analysis' in content
        for forbidden in ['bob-private-digest', 'secret-access-marker', 'secret-refresh-marker', 'private-email-marker', 'session-alice', 'csrf-alice']:
            assert forbidden not in content
        assert json.loads(archive.read('manifest.json'))['account'] == '100'
        assert json.loads(archive.read('subscriptions.json'))[0]['opportunity'] == {
            'max_age_days': 12, 'max_comments': 7, 'repo_pushed_within_days': 0, 'external_merge_within_days': 45}
    assert client.get('/api/export?repo=bob/other').status_code == 404


def test_multiple_budgets_are_reserved_atomically(hosted):
    from fastapi import HTTPException
    from app.hosted.limits import consume_many, consume
    from app.hosted.models import Quota
    _, rt = hosted
    consume(rt.engine, 'repo-budget', 1)
    with pytest.raises(HTTPException):
        consume_many(rt.engine, [('global-budget', 10), ('repo-budget', 1)])
    with Session(rt.engine) as s:
        assert not s.exec(select(Quota).where(Quota.key.startswith('global-budget:'))).all()


def test_artifact_history_reverts_are_preserved_and_private_versions_hidden(hosted):
    from app.hosted.analysis import record_artifact
    from app.hosted.models import Artifact
    client, rt = hosted
    ids = []
    with Session(rt.engine) as s:
        for version, content in [('one', '# First\n'), ('two', '# Second\n'), ('three', '# First\n')]:
            row = record_artifact(s, 'repo', 'overview.md', content, version)
            s.commit()
            ids.append(row.id)
        unchanged = record_artifact(s, 'repo', 'overview.md', '# First\n', 'four')
        assert unchanged.id == ids[-1]
        s.commit()
    listing = client.get('/api/artifacts?repo=public/project&page_size=2').json()
    assert listing['total'] == 3 and listing['items'][0]['id'] == ids[-1]
    assert 'content' not in listing['items'][0]
    detail = client.get('/api/artifacts/' + ids[-1]).json()
    assert detail['previous_id'] == ids[-2]
    assert '-# Second' in detail['diff'] and '+# First' in detail['diff']
    first = client.get('/api/artifacts/' + ids[0]).json()
    assert first['previous_id'] is None and '+# First' in first['diff']
    with Session(rt.engine) as s:
        repo = s.get(Repository, 'repo')
        repo.public = False
        s.add(repo)
        s.commit()
    assert client.get('/api/artifacts?repo=public/project').status_code == 404
    assert client.get('/api/artifacts/' + ids[-1]).status_code == 404


def test_unsubscribe_requires_post_and_is_bound_to_recipient(hosted):
    from app.hosted.mail import unsubscribe_token
    from app.hosted.models import Delivery
    client, rt = hosted
    with Session(rt.engine) as s:
        alice = s.get(Account, 'alice')
        alice.email, alice.email_enabled, alice.email_verified = 'old@example.com', True, True
        s.add(alice)
        s.add(Delivery(id='pending-mail', user_id='alice', recipient=alice.email, key='digest:test', purpose='digest', subject='test', body=rt.box.seal('test')))
        s.commit()
        token = unsubscribe_token(rt, alice)
    assert client.get('/api/email/unsubscribe/one-click', params={'token': token}, follow_redirects=False).status_code == 303
    with Session(rt.engine) as s:
        assert s.get(Account, 'alice').email_enabled is True
    response = client.post('/api/email/unsubscribe/one-click', params={'token':token}, data={'List-Unsubscribe':'One-Click'})
    assert response.status_code == 200
    with Session(rt.engine) as s:
        assert s.get(Account, 'alice').email_enabled is False
        assert s.get(Delivery, 'pending-mail').status == 'cancelled'
        alice = s.get(Account, 'alice')
        alice.email, alice.email_enabled = 'new@example.com', True
        s.add(alice); s.commit()
    assert client.post('/api/email/unsubscribe', json={'token':token}).status_code == 200
    with Session(rt.engine) as s:
        assert s.get(Account, 'alice').email_enabled is True
    assert client.post('/api/email/unsubscribe', json={'token':'forged'}).status_code == 400
    # Multipart is the other RFC 8058 transport format.
    assert client.post('/api/email/unsubscribe/one-click', params={'token':token},
        files={'List-Unsubscribe': (None, 'One-Click')}).status_code == 200


def test_signed_mail_feedback_and_delivery_access(hosted):
    import hmac, json
    from app.hosted.models import Delivery, MailFeedback
    client, rt = hosted
    rt.settings.mail_feedback_secret = 'test-only-feedback-key'
    with Session(rt.engine) as s:
        user = s.get(Account, 'bob')
        user.email, user.email_enabled, user.email_verified = 'bob@example.com', True, True
        s.add(user)
        s.add(Delivery(id='bob-mail', user_id='bob', key='test', recipient=user.email,
            subject='Test delivery', body=rt.box.seal('private-body'), status='sent'))
        s.commit()
    login(client)
    assert client.get('/api/deliveries').json()['total'] == 0
    assert client.post('/api/deliveries/bob-mail/retry').status_code == 404
    payload = json.dumps({'event_id':'bounce-one','delivery_id':'bob-mail','status':'bounced'}).encode()
    stamp = str(now())
    signature = hmac.new(rt.settings.mail_feedback_secret.encode(), stamp.encode()+b'.'+payload, hashlib.sha256).hexdigest()
    headers = {'Content-Type':'application/json','X-Gitwire-Timestamp':stamp,'X-Gitwire-Signature':signature}
    assert client.post('/api/mail-feedback',content=payload).status_code == 401
    assert client.post('/api/mail-feedback',content=payload,headers=headers).status_code == 200
    assert client.post('/api/mail-feedback',content=payload,headers=headers).json()['duplicate'] is True
    with Session(rt.engine) as s:
        assert s.get(Delivery,'bob-mail').status == 'bounced'
        assert not s.get(Account,'bob').email_enabled
        assert not s.get(Account,'bob').email_verified
        assert len(s.exec(select(MailFeedback)).all()) == 1
    login(client,'bob')
    items=client.get('/api/deliveries').json()['items']
    assert items[0]['status']=='bounced' and 'body' not in items[0]


def test_legacy_import_is_explicit_private_idempotent_and_non_destructive(hosted, tmp_path, monkeypatch):
    import asyncio, sqlite3
    from app.hosted.legacy import import_legacy
    from app.hosted.models import LegacyRecord
    client, rt = hosted
    config=tmp_path/'gitwire.yml'; vault=tmp_path/'vault'; vault.mkdir()
    config.write_text('repos:\n  - name: public/new\n    recipes: [docs-sync, issue-radar]\n    watch_issues: [17]\n')
    document=vault/'notes.md'; document.write_text('Personal notes test-sensitive-source-key')
    rt.settings.github_token='test-sensitive-source-key'
    database=tmp_path/'legacy.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE run(id INTEGER PRIMARY KEY,repo TEXT,status TEXT,summary TEXT,started_at TEXT)')
        db.execute("INSERT INTO run VALUES (1,'public/new','published','legacy summary','2020-01-01T00:00:00Z')")
    originals={p:p.read_bytes() for p in (config,document,database)}
    async def public_repo(rt,name):
        return {'id':11,'full_name':'public/new','private':False}
    monkeypatch.setattr('app.hosted.legacy.github.public_repo',public_repo)
    preview=asyncio.run(import_legacy(rt,'100',config,vault,database))
    assert preview['account']=='alice' and not preview['applied']
    with Session(rt.engine) as s:
        assert not s.exec(select(LegacyRecord)).all()
    result=asyncio.run(import_legacy(rt,'100',config,vault,database,apply=True))
    assert result['added_subscriptions']==1 and result['added_archive_records']==3
    repeat=asyncio.run(import_legacy(rt,'100',config,vault,database,apply=True))
    assert repeat['added_subscriptions']==0 and repeat['added_archive_records']==0
    login(client)
    items=client.get('/api/legacy').json()['items']
    assert len(items)==3
    document_id=next(r['id'] for r in items if r['kind']=='document')
    detail=client.get('/api/legacy/'+document_id).json()
    assert 'Personal notes' in detail['content'] and 'test-sensitive-source-key' not in detail['content']
    login(client,'bob')
    assert client.get('/api/legacy').json()['total']==0
    assert client.get('/api/legacy/'+document_id).status_code==404
    assert all(p.read_bytes()==value for p,value in originals.items())


def test_expired_oauth_browser_returns_to_retry_without_bypassing_validation(hosted):
    client, rt = hosted
    response = client.get('/api/auth/github/callback?state=invalid&code=unused',
                          headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == 'http://testserver/settings?auth=expired'
    assert client.get('/api/auth/me').json()['user'] is None
    assert client.get('/api/auth/github/callback?state=invalid&code=unused').status_code == 400


def test_mail_feedback_conflicts_and_smtp_race(hosted, monkeypatch):
    import asyncio, hmac, json
    from app.hosted.models import Delivery
    from app.hosted.mail import send
    client, rt = hosted
    rt.settings.mail_feedback_secret = 'test-feedback-secret'
    with Session(rt.engine) as s:
        alice = s.get(Account, 'alice')
        alice.email, alice.email_enabled, alice.email_verified = 'alice@example.com', True, True
        alice.quiet_start = alice.quiet_end = 0
        s.add(alice)
        for ident in ['mail-one', 'mail-two']:
            s.add(Delivery(id=ident, user_id='alice', key='digest:' + ident, purpose='digest',
                recipient=alice.email, subject='Test', body=rt.box.seal('test')))
        s.commit()
    def feedback(ident='mail-one', event='same-event'):
        payload = json.dumps({'event_id': event, 'delivery_id': ident, 'status': 'bounced'}).encode()
        timestamp = str(now())
        signature = hmac.new(rt.settings.mail_feedback_secret.encode(), timestamp.encode()+b'.'+payload, hashlib.sha256).hexdigest()
        return client.post('/api/mail-feedback', content=payload, headers={
            'X-Gitwire-Timestamp': timestamp, 'X-Gitwire-Signature': signature})
    def smtp_receives_then_bounces(settings, message):
        assert '/unsubscribe?token=' in message.get_content()
        assert feedback().status_code == 200
    monkeypatch.setattr('app.hosted.mail._smtp', smtp_receives_then_bounces)
    assert asyncio.run(send(rt, Job(kind='mail', payload={'delivery_id': 'mail-one'}))) == {'sent': True}
    with Session(rt.engine) as s:
        assert s.get(Delivery, 'mail-one').status == 'bounced'
        assert s.get(Delivery, 'mail-one').sent_at > 0
        assert not s.get(Account, 'alice').email_enabled
    assert feedback('mail-two').status_code == 409
    assert feedback('missing', 'new-event').status_code == 404
    login(client)
    assert client.get('/api/deliveries?status=bounced').json()['total'] == 1
    assert client.get('/api/deliveries?status=failed').json()['total'] == 0
    assert client.post('/api/deliveries/mail-one/retry').status_code == 409


def test_automatic_job_visibility_and_private_task_boundaries(hosted):
    client, rt = hosted
    with Session(rt.engine) as s:
        bob = s.get(Subscription, 'sub-bob')
        bob.paused = True
        s.add(bob); s.commit()
    shared = enqueue(rt.engine, 'scan', {'repo_id': 'repo', 'page': 3}, 'auto-scan')
    personal = enqueue(rt.engine, 'candidates', {'source': 'starred'}, 'bob-stars', user_id='bob', requester='alice')
    login(client)
    result = client.get('/api/jobs?kind=scan').json()
    assert result['total'] == 1 and result['stats']['pending'] == 1
    row = result['items'][0]
    assert row['repo'] == 'public/project' and row['completed_pages'] == 2
    assert not {'payload', 'user_id', 'lease_owner', 'active_key', 'lease_until'} & row.keys()
    assert client.get('/api/jobs/' + personal.id).status_code == 404
    login(client, 'bob')
    assert client.get('/api/jobs/' + shared.id).status_code == 404
    assert client.get('/api/jobs/' + personal.id).status_code == 200
    assert client.post('/api/jobs/' + shared.id + '/retry').status_code == 404


def test_job_retry_checks_current_access_and_preserves_failed_history(hosted):
    client, rt = hosted
    original = enqueue(rt.engine, 'analyze_repo', {'repo_id': 'repo'}, 'analyze_repo:repo')
    with Session(rt.engine) as s:
        job = s.get(Job, original.id)
        job.status, job.active_key = 'failed', None
        s.add(job); s.commit()
    login(client)
    response = client.post('/api/jobs/' + original.id + '/retry')
    assert response.status_code == 200
    new_id = response.json()['job_id']
    assert new_id != original.id
    assert client.get('/api/jobs/' + original.id).json()['status'] == 'failed'
    assert client.post('/api/jobs/' + original.id + '/retry').json()['job_id'] == new_id
    assert client.post('/api/jobs/' + new_id + '/retry').status_code == 409
    with Session(rt.engine) as s:
        sub = s.get(Subscription, 'sub-alice')
        sub.paused = True
        s.add(sub); s.commit()
    assert client.post('/api/jobs/' + original.id + '/retry').status_code == 409
    login(client, 'bob')
    assert client.post('/api/jobs/' + original.id + '/retry').status_code == 200
    with Session(rt.engine) as s:
        repo = s.get(Repository, 'repo')
        repo.public = False
        s.add(repo); s.commit()
    assert client.post('/api/jobs/' + original.id + '/retry').status_code == 409
    assert client.get('/api/jobs/' + original.id).json()['repo'] == ''


def test_opportunity_rules_personal_sql_pagination_and_notification_agree(hosted):
    from datetime import datetime, timezone
    from app.hosted.models import Issue
    from app.hosted.opportunities import exclusion
    client, rt = hosted
    timestamp = now()
    iso = lambda age: datetime.fromtimestamp(timestamp - age * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with Session(rt.engine) as s:
        s.add_all([Issue(id='fresh', repo_id='repo', number=1, title='Fresh', url='https://github.com/public/project/issues/1', difficulty='简单', created_at=iso(1), updated_at=iso(1)),
                   Issue(id='hot', repo_id='repo', number=2, title='Hot', url='https://github.com/public/project/issues/2', difficulty='简单', comments=20, created_at=iso(1), updated_at=iso(1)),
                   Issue(id='old', repo_id='repo', number=3, title='Old', url='https://github.com/public/project/issues/3', difficulty='简单', created_at=iso(60), updated_at=iso(20))])
        s.commit()
    login(client)
    assert client.get('/api/issues?group=opportunity').json()['total'] == 1
    excluded = client.get('/api/issues?group=excluded&page_size=1').json()
    assert excluded['total'] == 2 and excluded['pages'] == 2
    assert '评论超过' in excluded['items'][0]['excluded']
    with Session(rt.engine) as s:
        sub, repo = s.get(Subscription, 'sub-alice'), s.get(Repository, 'repo')
        for row in client.get('/api/issues').json()['items']:
            assert row['excluded'] == exclusion(s.get(Issue, row['id']), repo, sub, timestamp)
    values = {'max_age_days': 0, 'max_comments': 0, 'repo_pushed_within_days': 0, 'external_merge_within_days': 0}
    assert client.patch('/api/repos/sub-bob', json={'opportunity': values}).status_code == 404
    assert client.patch('/api/repos/sub-alice', json={'opportunity': {'max_comments': -1}}).status_code == 422
    assert client.patch('/api/repos/sub-alice', json={'opportunity': values}).status_code == 200
    assert client.get('/api/issues?group=opportunity').json()['total'] == 3
    login(client, 'bob')
    assert client.get('/api/issues?group=opportunity').json()['total'] == 1
    with Session(rt.engine) as s:
        repo = s.get(Repository, 'repo'); repo.signals_checked_at = 0; s.add(repo); s.commit()
    unknown = client.get('/api/issues?group=excluded').json()
    assert unknown['total'] == 3 and all('证据待更新' in r['excluded'] for r in unknown['items'])


def test_repository_evidence_paginates_and_unblocks_only_eligible_notifications(hosted, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from app.hosted.models import Issue
    from app.hosted.signals import refresh_signals
    from app.hosted.corpus import create_event
    client, rt = hosted
    iso = datetime.fromtimestamp(now()+1, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with Session(rt.engine) as s:
        repo=s.get(Repository,'repo');repo.signals_checked_at=0;s.add(repo)
        issue=Issue(id='new-issue',repo_id='repo',number=1,title='New',url='https://github.com/public/project/issues/1',
            difficulty='简单',created_at=iso,updated_at=iso,summary='A new issue',analysis_hash='version-one')
        s.add(issue);s.flush()
        create_event(s,issue,'opportunity','未分析','New','New issue')
        s.commit()
        assert not s.exec(select(Notification)).all()
    calls=[]
    async def public_repo(rt, name):
        return {'id':10,'full_name':name,'private':False,'pushed_at':iso}
    async def get(rt,path,params,**kwargs):
        calls.append(params['page'])
        if params['page']==1:
            return [{'number':n,'author_association':'MEMBER','merged_at':iso,'updated_at':iso} for n in range(100)]
        return [{'number':101,'author_association':'CONTRIBUTOR','merged_at':iso,'updated_at':iso}]
    monkeypatch.setattr('app.hosted.signals.github.public_repo',public_repo)
    monkeypatch.setattr('app.hosted.signals.github.get',get)
    job=Job(kind='repo_signals',payload={'repo_id':'repo'})
    asyncio.run(refresh_signals(rt,job))
    assert calls == [1,2]
    with Session(rt.engine) as s:
        repo=s.get(Repository,'repo')
        assert repo.external_merge_at > 0 and repo.signals_coverage_since == 1
        assert len(s.exec(select(Notification)).all()) == 2
    asyncio.run(refresh_signals(rt,job))
    with Session(rt.engine) as s:
        assert len(s.exec(select(Notification)).all()) == 2


def test_closing_references_are_scoped_and_ignore_quotes():
    from app.hosted.observations import closes_issue
    assert closes_issue('Fixes #12', 'a/b', 'a/b', 12)
    assert not closes_issue('Fixes #12', 'other/repo', 'a/b', 12)
    assert closes_issue('Resolves a/b#12', 'other/repo', 'a/b', 12)
    assert closes_issue('Closes https://github.com/a/b/issues/12', 'other/repo', 'a/b', 12)
    for text in ['See #12', 'Fixes #123', '> Fixes #12', '`Fixes #12`', '```\nFixes #12\n```']:
        assert not closes_issue(text, 'a/b', 'a/b', 12)


def test_issue_observations_follow_current_pr_and_incremental_comments(hosted, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from app.hosted.models import Issue
    from app.hosted.observations import watch_issues
    client, rt = hosted
    iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with Session(rt.engine) as s:
        alice=s.get(Subscription,'sub-alice');alice.watched_issues=[1];s.add(alice)
        bob=s.get(Subscription,'sub-bob');bob.paused=True;s.add(bob);s.commit()
    raw={'number':1,'title':'Track me','body':'Example','html_url':'https://github.com/public/project/issues/1',
        'state':'open','created_at':iso,'updated_at':iso,'comments':1,'labels':[],'assignees':[]}
    pr={'number':5,'body':'Fixes #1','state':'open','html_url':'https://github.com/public/project/pull/5','user':{'login':'contributor'}}
    timeline_pages=[];comment_requests=[]
    async def public_repo(rt,name):
        return {'id':10,'full_name':name,'private':False}
    async def get(rt,path,params=None,**kwargs):
        if path.endswith('/timeline'):
            timeline_pages.append(params['page'])
            if params['page']==1: return [{'event':'commented'}]*100
            return [{'event':'cross-referenced','source':{'issue':{'state':'open','body':'Fixes #1','pull_request':{'url':'https://api.github.com/repos/public/project/pulls/5'}}}}]
        if path.endswith('/pulls/5'): return pr.copy()
        if path.endswith('/comments'):
            comment_requests.append(params)
            return [{'id':42,'body':'I am working on this','html_url':'https://github.com/public/project/issues/1#issuecomment-42','user':{'login':'helper'},'updated_at':iso}]
        return raw.copy()
    monkeypatch.setattr('app.hosted.observations.github.public_repo',public_repo)
    monkeypatch.setattr('app.hosted.observations.github.get',get)
    asyncio.run(watch_issues(rt,Job(kind='watch_issues',payload={'repo_id':'repo'})))
    with Session(rt.engine) as s:
        issue=s.exec(select(Issue)).one()
        assert issue.status=='taken-pr' and issue.evidence[0]['author']=='contributor'
        assert not s.exec(select(Notification)).all()  # first observation is a baseline
    assert timeline_pages==[1,2] and comment_requests==[]
    pr['state']='closed'  # Timeline snapshot still says open.
    raw['comments']=2;raw['labels']=[{'name':'help wanted'}]
    asyncio.run(watch_issues(rt,Job(kind='watch_issues',payload={'repo_id':'repo'})))
    with Session(rt.engine) as s:
        issue=s.exec(select(Issue)).one()
        assert issue.status=='open' and not issue.evidence[0]['open']
        notices=s.exec(select(Notification,Event).join(Event)).all()
        assert {e.kind for n,e in notices}=={'reopened','watch'}
        assert all(n.user_id=='alice' for n,e in notices)
        assert s.exec(select(Job).where(Job.kind=='analyze_claim')).one().payload['comments'][0]['id']==42
    assert comment_requests[0]['since']
    # No duplicate alerts on an unchanged recheck.
    asyncio.run(watch_issues(rt,Job(kind='watch_issues',payload={'repo_id':'repo'})))
    with Session(rt.engine) as s:
        assert len(s.exec(select(Notification)).all())==2


def test_claims_reference_real_comments_cache_and_expire(hosted, monkeypatch):
    import asyncio, json
    from datetime import datetime,timezone
    from app.hosted.models import Issue,Analysis
    from app.hosted.observations import analyze_claim,status_with_claim,CLAIM_TTL
    client,rt=hosted
    iso=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with Session(rt.engine) as s:
        s.add(Issue(id='claim-issue',repo_id='repo',number=1,title='Issue',url='https://github.com/public/project/issues/1',
                    difficulty='简单',created_at=iso,updated_at=iso))
        s.commit()
    calls=[]; answer={'action':'claim','comment_id':99}
    class FakeLLM:
        def __init__(self,*args): self.client=self
        async def chat(self,*args): calls.append(1);return json.dumps(answer)
        async def aclose(self): pass
    async def public_repo(rt,name): return {'full_name':name,'private':False}
    monkeypatch.setattr('app.hosted.analysis.BudgetedLLM',FakeLLM)
    monkeypatch.setattr('app.hosted.observations.github.public_repo',public_repo)
    job=Job(kind='analyze_claim',payload={'issue_id':'claim-issue','comments':[{'id':42,'author':'helper','body':'I can do this',
                'url':'https://github.com/public/project/issues/1#issuecomment-42','updated_at':iso}]})
    with pytest.raises(ValueError): asyncio.run(analyze_claim(rt,job))
    with Session(rt.engine) as s: assert not s.exec(select(Analysis)).all()  # invalid output is never cached
    answer['comment_id']=42
    asyncio.run(analyze_claim(rt,job));asyncio.run(analyze_claim(rt,job))
    assert len(calls)==2
    with Session(rt.engine) as s:
        issue=s.get(Issue,'claim-issue')
        assert issue.status=='taken-claim' and issue.claim['author']=='helper'
        assert issue.claim['url'].endswith('issuecomment-42')
        assert len(s.exec(select(Notification)).all())==2
    assert status_with_claim({'state':'open'},[],{'until':now()-1})=='open'
    assert status_with_claim({'state':'closed','state_reason':'completed'},[],{'until':now()+CLAIM_TTL})=='resolved'


def test_queue_drain_waits_for_running_and_restores_original_schedule(hosted):
    from app.hosted.maintenance import drain,resume
    client,rt=hosted
    running=enqueue(rt.engine,'scan',{},'running')
    held=claim(rt.engine,'worker')
    first=enqueue(rt.engine,'scan',{},'pending-one')
    later=enqueue(rt.engine,'scan',{},'pending-later',available_at=now()+600)
    assert drain(rt.engine)==1
    assert claim(rt.engine,'other') is None
    created_during_drain=enqueue(rt.engine,'scan',{},'new')
    assert drain(rt.engine)==1
    complete(rt.engine,held,{'ok':True})
    assert drain(rt.engine)==0
    resume(rt.engine)
    with Session(rt.engine) as s:
        assert s.get(Job,first.id).available_at==first.available_at
        assert s.get(Job,later.id).available_at==later.available_at
        assert s.get(Job,created_during_drain.id).available_at==created_during_drain.available_at
        assert s.get(Job,running.id).status=='completed'
    assert claim(rt.engine,'next') is not None


def test_recipe_ledger_replay_routes_personal_alerts_and_rechecks_mail(hosted, tmp_path):
    from datetime import datetime, timezone, timedelta
    from sqlmodel import create_engine
    from app.models import Alert
    from app.hosted.models import Delivery
    from app.hosted.recipe_events import import_recipe_events
    from app.hosted.mail import eligible_delivery
    client, rt = hosted
    source = create_engine(f'sqlite:///{tmp_path}/recipe-ledger.db')
    Alert.__table__.create(source)
    with Session(rt.engine) as s:
        alice = s.get(Account, 'alice')
        alice.notify_kinds = ['release', 'tripwire', 'breaking', 'cve']
        alice.email = 'alice@example.test'
        alice.email_verified = alice.email_enabled = True
        alice.email_mode = 'immediate'
        bob = s.get(Account, 'bob'); bob.notify_kinds = alice.notify_kinds
        sub = s.get(Subscription, 'sub-alice')
        sub.recipes = ['feature-tripwire', 'bug-watch', 'cve-scan']
        sub.keywords = 'api'; sub.created_at = now() - 100
        other = s.get(Subscription, 'sub-bob'); other.recipes = ['issue-radar']
        s.add_all([alice, bob, sub, other]); s.commit()
    with Session(source) as s:
        for kind in ['release', 'tripwire', 'breaking', 'cve', 'error', 'opportunity']:
            s.add(Alert(repo='public/project', kind=kind, title='API change', body='public evidence'))
        s.add(Alert(repo='public/project', kind='release', title='Other feature'))
        s.add(Alert(repo='public/project', kind='release', title='API old', ts=datetime.now(timezone.utc)-timedelta(days=2)))
        s.add(Alert(repo='unrelated/repo', kind='release', title='API private marker'))
        s.commit()
    assert import_recipe_events(source, rt.engine, 'repo') == 6
    assert import_recipe_events(source, rt.engine, 'repo') == 0
    login(client)
    notices = client.get('/api/notifications').json()['items']
    assert {n['kind'] for n in notices} == {'release', 'tripwire', 'breaking', 'cve'}
    assert len(notices) == 4
    assert client.patch('/api/preferences', json={'notify_kinds':['tripwire']}).status_code == 200
    with Session(rt.engine) as s:
        user = s.get(Account, 'alice'); user.notify_kinds = ['release', 'tripwire', 'breaking', 'cve']
        user.email_enabled = True; user.email_mode = 'immediate'; s.add(user); s.commit()
        release = next(n for n in notices if n['kind']=='release')
        mail = Delivery(user_id='alice', recipient=user.email, key='event:'+release['id'], subject='test', body='test')
        assert eligible_delivery(s, mail, user)
        sub = s.get(Subscription, 'sub-alice'); sub.recipes=['issue-radar']; s.add(sub); s.commit()
        assert not eligible_delivery(s, mail, user)
        sub.recipes=['docs-sync']; sub.paused=True; s.add(sub); s.commit()
        assert not eligible_delivery(s, mail, user)
    login(client, 'bob')
    assert client.get('/api/notifications').json()['items'] == []
    with Session(rt.engine) as s:
        sub=s.get(Subscription,'sub-bob'); sub.recipes=['docs-sync']; s.add(sub); s.commit()
    assert import_recipe_events(source, rt.engine, 'repo') == 0
    assert client.get('/api/notifications').json()['items'] == []
    source.dispose()


def test_repository_noop_recovers_recipe_alerts_after_hosted_crash(hosted, monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from sqlmodel import create_engine
    from app.models import Alert
    from app.hosted.analysis import analyze_repository
    from app.hosted import github
    from app.hosted.models import Artifact
    _, rt = hosted
    source = create_engine(f'sqlite:///{tmp_path}/replay.db')
    Alert.__table__.create(source)
    with Session(source) as s:
        s.add(Alert(repo='public/project', kind='release', title='v2 API', body='Release evidence')); s.commit()
    with Session(rt.engine) as s:
        a=s.get(Account,'alice');a.notify_kinds=['release'];s.add(a);s.commit()
    async def close(): pass
    async def public(*args): return {'private':False}
    vault = SimpleNamespace(remote='unused', github_token='unused', write_config=lambda cfg: None,
        list_files=lambda slug:['README.md'], read_file=lambda path:'# Public artifact')
    svc = SimpleNamespace(engine=source, vault=vault, gh=SimpleNamespace(aclose=close))
    async def build(*args, **kwargs): return svc
    async def sync(*args):
        assert vault.remote == vault.github_token == ''
        return SimpleNamespace(status='published', mode='noop', id=1)
    monkeypatch.setattr(github,'public_repo', public)
    monkeypatch.setattr('app.services.build_services', build)
    monkeypatch.setattr('app.engine.runner.sync_repo', sync)
    job=Job(kind='analyze_repo',payload={'repo_id':'repo'})
    result=asyncio.run(analyze_repository(rt,job))
    assert result['events']==1
    assert asyncio.run(analyze_repository(rt,job))['events']==0
    with Session(rt.engine) as s:
        assert len(s.exec(select(Artifact)).all())==1
        assert len(s.exec(select(Notification).where(Notification.user_id=='alice')).all())==1


def test_digest_schedule_catches_latest_missed_day_and_respects_timezone():
    from datetime import datetime, timezone
    from app.hosted.scheduler import due_date
    stamp = lambda value: int(datetime.fromisoformat(value).timestamp())
    user = Account(github_id='schedule', login='schedule', created_at=0, timezone='Asia/Shanghai', digest_hour=8, digest_date='2026-10-01')
    assert due_date(user, stamp('2026-10-04T06:00:00+08:00')) == '2026-10-03'
    assert due_date(user, stamp('2026-10-04T08:00:00+08:00')) == '2026-10-04'
    user.digest_date='2026-10-04'
    assert due_date(user, stamp('2026-10-04T15:00:00+08:00')) is None
    user.digest_date='';user.created_at=stamp('2026-10-04T06:00:00+08:00')
    assert due_date(user, stamp('2026-10-04T07:00:00+08:00')) is None
    assert due_date(user, stamp('2026-10-04T08:00:00+08:00')) == '2026-10-04'
    user.created_at=0; user.timezone='America/New_York';user.digest_hour=2
    # The spring-forward gap does not permanently skip the report.
    assert due_date(user, stamp('2026-03-08T07:30:00+00:00')) == '2026-03-08'
    user.digest_hour=1;user.digest_date='2026-11-01'
    assert due_date(user, stamp('2026-11-01T06:30:00+00:00')) is None


def test_digest_waits_for_stars_and_claims_late_records_once(hosted, monkeypatch):
    import asyncio
    from app.hosted.scheduler import make_digest
    from app.hosted.jobs import DeferJob
    from app.hosted.models import Digest, Delivery
    _, rt = hosted
    at = now()
    monkeypatch.setattr('app.hosted.scheduler.now', lambda:at)
    with Session(rt.engine) as s:
        user=s.get(Account,'alice');user.access_token='encrypted-fixture';user.created_at=at-4*86400
        user.email='alice@example.test';user.email_enabled=user.email_verified=True
        s.add(user);s.commit()
    # An own-repository sync must not suppress the daily Star sync.
    enqueue(rt.engine,'candidates',{'source':'owned'},'candidates:alice:owned:1',user_id='alice')
    job=enqueue(rt.engine,'digest',{'date':'2026-10-04'},'digest:alice:2026-10-04',user_id='alice')
    with pytest.raises(DeferJob): asyncio.run(make_digest(rt,job))
    with Session(rt.engine) as s:
        durable=s.get(Job,job.id)
        star=s.get(Job,durable.payload['star_job_id'])
        assert star.payload['source']=='starred' and star.user_id=='alice'
        assert not s.exec(select(Digest)).all()
        star.status='completed';star.active_key=None;s.add(star)
        s.add(Candidate(id='morning-star',user_id='alice',github_id='morning',full_name='public/morning',
            discovered_at=at,starred_at='2026-10-04T01:00:00Z',seen_generation=star.id))
        s.add(Candidate(id='aaa-newer-star',user_id='alice',github_id='newer',full_name='public/newer',
            discovered_at=at,starred_at='2026-10-04T02:00:00Z',seen_generation=star.id))
        s.add(Event(id='late-event',key='late-event',repo_id='repo',kind='release',title='Late event',body='Public'))
        s.commit()
        s.add(Notification(id='late-note',user_id='alice',event_id='late-event',created_at=at-2*86400))
        s.add(Notification(id='bob-note',user_id='bob',event_id='late-event',created_at=at))
        s.commit()
    first=asyncio.run(make_digest(rt,job))
    assert asyncio.run(make_digest(rt,job))==first
    with Session(rt.engine) as s:
        report=s.get(Digest,first['digest_id'])
        assert 'public/morning' in report.content and 'Late event' in report.content
        assert report.content.index('public/newer') < report.content.index('public/morning')
        assert report.period_end==at and report.period_start==at-4*86400
        assert s.get(Candidate,'morning-star').digest_id==report.id
        assert s.get(Notification,'late-note').digest_id==report.id
        assert s.get(Notification,'bob-note').digest_id is None
        assert len(s.exec(select(Delivery)).all())==1
        user=s.get(Account,'alice');user.access_token='';s.add(user);s.commit()
    at+=86400
    following=enqueue(rt.engine,'digest',{'date':'2026-10-05'},'digest:alice:2026-10-05',user_id='alice')
    result=asyncio.run(make_digest(rt,following))
    with Session(rt.engine) as s:
        report=s.get(Digest,result['digest_id'])
        assert 'public/morning' not in report.content and 'Late event' not in report.content
        assert 'GitHub 未连接' in report.content
        assert report.period_start==at-86400
    stale=Job(kind='digest',user_id='alice',payload={'date':'2026-10-02'})
    assert asyncio.run(make_digest(rt,stale))=={'skipped':True}


def test_digest_sync_timeout_does_not_publish_partial_candidates(hosted, monkeypatch):
    import asyncio
    from app.hosted.scheduler import make_digest
    from app.hosted.jobs import DeferJob
    from app.hosted.models import Digest
    _,rt=hosted
    at=now()
    monkeypatch.setattr('app.hosted.scheduler.now',lambda:at)
    with Session(rt.engine) as s:
        user=s.get(Account,'alice');user.access_token='encrypted-fixture';s.add(user);s.commit()
    job=enqueue(rt.engine,'digest',{'date':'2026-10-04'},'digest:alice:2026-10-04',user_id='alice')
    with pytest.raises(DeferJob): asyncio.run(make_digest(rt,job))
    star_id=job.payload['star_job_id']
    with Session(rt.engine) as s:
        for ident, ignored, present in [('partial',False,True),('ignored',True,True),('unstarred',False,False)]:
            s.add(Candidate(id=ident,user_id='alice',github_id=ident,full_name='public/'+ident,ignored=ignored,present=present,
                discovered_at=at,seen_generation=star_id))
        s.commit()
    at+=901
    result=asyncio.run(make_digest(rt,job))
    with Session(rt.engine) as s:
        report=s.get(Digest,result['digest_id'])
        assert '同步尚未完成或失败' in report.content and 'public/partial' not in report.content
        assert s.get(Candidate,'partial').digest_id is None
        star=s.get(Job,star_id);star.status='completed';star.active_key=None;s.add(star)
        user=s.get(Account,'alice');user.access_token='';s.add(user);s.commit()
    at+=86400
    following=enqueue(rt.engine,'digest',{'date':'2026-10-05'},'digest:alice:2026-10-05',user_id='alice')
    result=asyncio.run(make_digest(rt,following))
    with Session(rt.engine) as s:
        report=s.get(Digest,result['digest_id'])
        assert 'public/partial' in report.content
        assert 'public/ignored' not in report.content and 'public/unstarred' not in report.content


def test_digest_postgres_concurrent_recovery_commits_once(hosted):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from app.hosted.scheduler import make_digest
    from app.hosted.models import Digest,Delivery
    _,rt=hosted
    if rt.engine.dialect.name!='postgresql':
        pytest.skip('Row lock concurrency requires PostgreSQL')
    with Session(rt.engine) as s:
        user=s.get(Account,'alice');user.email='alice@example.test';user.email_enabled=user.email_verified=True
        s.add(user);s.commit()
    def generate(_):
        return asyncio.run(make_digest(rt,Job(kind='digest',user_id='alice',payload={'date':'2026-10-04'})))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(generate,range(4)))
    assert len({r['digest_id'] for r in results})==1
    with Session(rt.engine) as s:
        assert len(s.exec(select(Digest)).all())==len(s.exec(select(Delivery)).all())==1


def test_recipe_transport_cannot_write_or_reuse_private_cache(hosted):
    import asyncio, base64
    from app.hosted.recipe_github import PublicRecipeGithub
    from app.github import GithubError
    _,rt=hosted
    state={'private':False};calls=[]
    async def scenario():
        await rt.http.aclose()
        def respond(request):
            calls.append((request.method,request.url.path))
            path=request.url.path
            if path=='/repos/public/project':
                return httpx.Response(200,json={'id':10,'full_name':'public/project','private':state['private'],'default_branch':'main'})
            if path.endswith('/contents/README.md'):
                assert request.url.params['ref']=='a'*40
                return httpx.Response(200,json={'encoding':'base64','content':base64.b64encode(b'public source').decode()},headers={'ETag':'fixture'})
            return httpx.Response(404,json={})
        rt.http=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client=PublicRecipeGithub(rt)
        try:
            assert await client.file_content('public/project','README.md',ref='a'*40)=='public source'
            assert await client.latest_release('public/project') is None
            count=len(calls)
            with pytest.raises(GithubError):
                await client.create_pull('public/project','test','test','main','test')
            assert len(calls)==count and all(method=='GET' for method,_ in calls)
            state['private']=True
            assert await client.file_content('public/project','README.md',ref='a'*40) is None
            assert calls[-1]==('GET','/repos/public/project')
        finally: await client.aclose()
    asyncio.run(scenario())


def test_worker_graceful_stop_finishes_active_job_without_claiming_next(hosted):
    import asyncio
    from app.hosted.jobs import worker_loop
    _,rt=hosted
    first=enqueue(rt.engine,'scan',{'repo_id':'repo'},'stop:first')
    second=enqueue(rt.engine,'scan',{'repo_id':'repo'},'stop:second')
    handled=[]
    async def scenario():
        stop=asyncio.Event()
        async def handler(runtime,job):
            handled.append(job.id)
            stop.set()
            await asyncio.sleep(.02)
            return {'finished':True}
        await asyncio.wait_for(worker_loop(rt,handler,stop),5)
    asyncio.run(scenario())
    assert handled==[first.id]
    with Session(rt.engine) as s:
        assert s.get(Job,first.id).status=='completed'
        assert s.get(Job,second.id).status=='pending'

@pytest.mark.parametrize('closed', [False, True])
def test_pool_exit_survives_scan_before_watch_and_mail_rechecks_rules(hosted, monkeypatch, closed):
    import asyncio
    from datetime import datetime, timezone
    from app.hosted.models import Issue, Delivery
    from app.hosted.observations import watch_issues
    from app.hosted.corpus import scan_repository
    from app.hosted.mail import eligible_delivery
    _, rt = hosted
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    raw = {'number': 7, 'title': 'Relevant issue', 'body': 'keyword-in-body',
           'html_url': 'https://github.com/public/project/issues/7', 'state': 'open',
           'created_at': stamp, 'updated_at': stamp, 'comments': 14, 'labels': [], 'assignees': []}
    calls = []
    async def public_repo(rt, name):
        return {'id': 10, 'full_name': name, 'private': False}
    async def get(rt, path, params=None, **kwargs):
        calls.append(path)
        if path.endswith('/timeline') or path.endswith('/comments'):
            return []
        if path.endswith('/issues'):
            return [raw.copy()]
        return raw.copy()
    monkeypatch.setattr('app.hosted.github.public_repo', public_repo)
    monkeypatch.setattr('app.hosted.github.get', get)
    with Session(rt.engine) as s:
        s.add(Issue(id='exit-issue', repo_id='repo', number=7, title=raw['title'], body=raw['body'],
                    url=raw['html_url'], created_at=stamp, updated_at=stamp, comments=14, difficulty='简单'))
        user = s.get(Account, 'alice')
        user.email = 'alice@example.test'; user.email_verified = user.email_enabled = True
        user.email_mode = 'both'; s.add(user)
        alice = s.get(Subscription, 'sub-alice'); alice.keywords = 'keyword-in-body'; s.add(alice)
        bob = s.get(Subscription, 'sub-bob')
        bob.opportunity = {**bob.opportunity, 'max_comments': 10}; s.add(bob)
        s.commit()
    asyncio.run(watch_issues(rt, Job(kind='watch_issues', payload={'repo_id': 'repo'})))
    raw['comments'] = 16
    if closed:
        raw['state'], raw['state_reason'] = 'closed', 'completed'
    # Hourly inventory wins the race and makes the current issue ineligible first.
    asyncio.run(scan_repository(rt, Job(kind='scan_repo', payload={'repo_id': 'repo'})))
    calls.clear()
    asyncio.run(watch_issues(rt, Job(kind='watch_issues', payload={'repo_id': 'repo'})))
    assert '/repos/public/project/issues/7' in calls
    with Session(rt.engine) as s:
        pairs = s.exec(select(Notification, Event).join(Event)).all()
        assert {e.kind for n, e in pairs} == ({'watch', 'resolved'} if closed else {'watch'})
        assert {n.user_id for n, e in pairs} == {'alice'}
        notice, event = next((n, e) for n, e in pairs if e.kind == 'watch')
        assert event.observation['issue']['comments'] == 14
        assert 'user_id' not in str(event.observation) and 'alice' not in str(event.observation)
        delivery = Delivery(user_id='alice', key='event:' + notice.id,
                            recipient='alice@example.test', subject='Change', body='Test')
        user = s.get(Account, 'alice'); sub = s.get(Subscription, 'sub-alice')
        assert eligible_delivery(s, delivery, user)
        sub.opportunity = {**sub.opportunity, 'max_comments': 5}; s.add(sub); s.flush()
        assert not eligible_delivery(s, delivery, user)
        sub.watched_issues = [7]; s.add(sub); s.flush()
        assert eligible_delivery(s, delivery, user)  # explicit watch bypasses pool thresholds
        sub.keywords = 'unrelated-new-preference'; s.add(sub); s.flush()
        assert not eligible_delivery(s, delivery, user)
        sub.keywords = ''; sub.paused = True; s.add(sub); s.flush()
        assert not eligible_delivery(s, delivery, user)
        s.rollback()
    calls.clear()
    asyncio.run(watch_issues(rt, Job(kind='watch_issues', payload={'repo_id': 'repo'})))
    assert '/repos/public/project/issues/7' not in calls  # final observation consumed, not watched forever
    with Session(rt.engine) as s:
        assert len(s.exec(select(Notification)).all()) == (2 if closed else 1)


def test_ops_expiry_is_bounded_preserves_history_and_reports_backlog(hosted):
    from app.hosted.models import OAuthAttempt, EmailVerification, HttpCache, Quota, Delivery
    from app.hosted.operations import cleanup, heartbeat, snapshot
    _, rt = hosted
    stamp = now()
    with Session(rt.engine) as s:
        for n in range(3):
            s.add(OAuthAttempt(digest=f'expired-{n}', browser_digest='test', verifier='secret-test', expires_at=stamp-7200))
        s.add_all([
            OAuthAttempt(digest='active-oauth', browser_digest='test', verifier='secret-test', expires_at=stamp+600),
            BrowserSession(digest='expired-session', user_id='alice', csrf='test', expires_at=stamp-7200),
            EmailVerification(digest='expired-verify', user_id='alice', email='private@example.test', expires_at=stamp-7200),
            HttpCache(key='stale-cache', payload={'old': True}, updated_at=stamp-8*86400),
            HttpCache(key='fresh-cache', payload={'new': True}, updated_at=stamp),
            Quota(key='stale-budget', count=5, expires_at=stamp-2*86400),
            Quota(key='active-budget', count=1, expires_at=stamp+3600),
            Quota(key='ops:drain-job:test', count=123, expires_at=stamp-2*86400),
            Job(id='history-job', kind='candidates', status='completed', created_at=1, finished_at=2),
            Job(id='ready-job', kind='scan', available_at=stamp-120),
            Job(id='deferred-job', kind='analyze_issue', available_at=stamp+3600),
            Job(id='expired-job', kind='scan', status='running', lease_until=stamp-1),
            Delivery(user_id='alice', key='historical-mail', recipient='private@example.test',
                     subject='Private subject', body='Private body', status='failed', created_at=1)])
        s.commit()
    heartbeat(rt.engine, 'worker'); heartbeat(rt.engine, 'worker')
    heartbeat(rt.engine, 'scheduler')
    with Session(rt.engine) as s:
        scheduler = s.get(Quota, 'ops:heartbeat:scheduler'); scheduler.count = stamp-120; s.add(scheduler); s.commit()
    status = snapshot(rt.engine)
    assert status['heartbeats']['worker']['recent']
    assert not status['heartbeats']['scheduler']['recent']
    assert status['queue']['ready'] == status['queue']['deferred'] == status['queue']['expired_leases'] == 1
    assert status['queue']['oldest_ready_seconds'] >= 120
    assert status['mail_by_status'] == {'failed': 1}
    assert not any(secret in str(status) for secret in ['private@example.test', 'secret-test', 'Private body', 'alice'])
    assert cleanup(rt.engine, batch=2) == {'h_oauth_attempt': 2, 'h_session': 1,
        'h_email_verification': 1, 'h_http_cache': 1, 'h_quota': 1}
    assert cleanup(rt.engine, batch=2)['h_oauth_attempt'] == 1
    with Session(rt.engine) as s:
        assert s.get(OAuthAttempt, 'active-oauth')
        assert s.get(BrowserSession, digest('session-alice'))
        assert s.get(HttpCache, 'fresh-cache')
        assert s.get(Quota, 'active-budget') and s.get(Quota, 'ops:drain-job:test')
        assert s.get(Job, 'history-job')
        assert s.exec(select(Delivery)).one().body == 'Private body'


def expiring_test_grant(rt, who='alice'):
    with Session(rt.engine) as s:
        user = s.get(Account, who)
        user.access_token = rt.box.seal('test-old-access-' + who)
        user.refresh_token = rt.box.seal('test-old-refresh-' + who)
        user.token_expires = now() + 30
        user.reconnect_required = False
        s.add(user); s.commit()


@pytest.mark.parametrize('scenario', ['rate', 'secondary', 'server', 'html', 'malformed', 'scope', 'timeout', 'invalid', 'client'])
def test_refresh_only_invalid_grant_requires_reconnect(hosted, monkeypatch, scenario):
    from app.hosted.github import _refresh, GitHubFailure
    _, rt = hosted
    expiring_test_grant(rt)
    with Session(rt.engine) as s:
        original = s.get(Account, 'alice').model_dump()
    def exchange(url, **kwargs):
        assert url == 'https://github.com/login/oauth/access_token'
        assert kwargs['json']['refresh_token'] == 'test-old-refresh-alice'
        if scenario == 'timeout':
            raise httpx.ReadTimeout('test-sensitive-server-message')
        if scenario in {'rate', 'secondary'}:
            return httpx.Response(429 if scenario == 'rate' else 403, headers={'retry-after': '180'}, json={})
        if scenario == 'server': return httpx.Response(503, json={})
        if scenario == 'html': return httpx.Response(200, text='<html>temporary failure</html>')
        if scenario == 'invalid': return httpx.Response(400, json={'error': 'bad_refresh_token'})
        if scenario == 'client': return httpx.Response(401, json={'error': 'incorrect_client_credentials'})
        return httpx.Response(200, json={'access_token': 'test-new', 'refresh_token': 'test-new-refresh',
            'expires_in': 28800 if scenario == 'scope' else 'not-an-integer', 'scope': 'repo' if scenario == 'scope' else ''})
    monkeypatch.setattr('app.hosted.github.httpx.post', exchange)
    with pytest.raises(GitHubFailure) as error:
        _refresh(rt, 'alice')
    assert 'test-sensitive' not in str(error.value)
    if scenario in {'rate', 'secondary'}:
        assert error.value.status == 429 and error.value.retry_after == 180
    with Session(rt.engine) as s:
        user = s.get(Account, 'alice')
        assert user.reconnect_required == (scenario == 'invalid')
        assert user.access_token == original['access_token'] and user.refresh_token == original['refresh_token']
        assert user.token_expires == original['token_expires']


def test_refresh_rotation_and_disconnect_are_account_scoped(hosted, monkeypatch):
    from app.hosted.github import _refresh, GitHubFailure
    client, rt = hosted
    expiring_test_grant(rt); expiring_test_grant(rt, 'bob')
    calls = []
    def exchange(url, **kwargs):
        calls.append(kwargs['json']['refresh_token'])
        return httpx.Response(200, json={'access_token': 'test-new-access', 'refresh_token': 'test-new-refresh', 'expires_in': 28800, 'scope': ''})
    monkeypatch.setattr('app.hosted.github.httpx.post', exchange)
    assert _refresh(rt, 'alice') == _refresh(rt, 'alice') == 'test-new-access'
    assert calls == ['test-old-refresh-alice']
    with Session(rt.engine) as s:
        user = s.get(Account, 'alice')
        assert user.access_token != 'test-new-access' and rt.box.open(user.refresh_token) == 'test-new-refresh'
        assert user.token_expires > now() + 28000
        assert rt.box.open(s.get(Account, 'bob').access_token) == 'test-old-access-bob'
    assert client.post('/api/auth/disconnect').status_code == 401
    login(client)
    assert client.post('/api/auth/disconnect', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post('/api/auth/disconnect').status_code == 200
    assert client.get('/api/auth/me').json()['user']['reconnect_required'] is True
    with pytest.raises(GitHubFailure): _refresh(rt, 'alice')
    assert len(calls) == 1  # disconnected grants never contact GitHub
    with Session(rt.engine) as s:
        user = s.get(Account, 'alice')
        assert not user.access_token and not user.refresh_token
        assert s.get(Subscription, 'sub-alice')
        assert rt.box.open(s.get(Account, 'bob').access_token) == 'test-old-access-bob'
    assert client.post('/api/auth/logout').status_code == 200
    assert client.get('/api/auth/me').json()['user'] is None
    login(client, 'bob')
    assert client.get('/api/auth/me').json()['user']['login'] == 'bob'


def test_stale_api_401_does_not_invalidate_rotated_token(hosted):
    import asyncio
    from app.hosted.github import get, GitHubFailure
    _, rt = hosted
    expiring_test_grant(rt)
    with Session(rt.engine) as s:
        user = s.get(Account, 'alice'); user.token_expires = now()+3600; s.add(user); s.commit()
    async def request(req):
        assert req.headers['Authorization'] == 'Bearer test-old-access-alice'
        with Session(rt.engine) as s:
            user = s.get(Account, 'alice'); user.access_token = rt.box.seal('test-rotated-during-request'); s.add(user); s.commit()
        return httpx.Response(401, json={'message': 'Bad credentials'})
    rt.http = httpx.AsyncClient(transport=httpx.MockTransport(request))
    with pytest.raises(GitHubFailure) as error:
        asyncio.run(get(rt, '/user', user_id='alice'))
    assert error.value.status == 503
    with Session(rt.engine) as s:
        assert not s.get(Account, 'alice').reconnect_required


def test_postgres_refresh_serializes_single_use_refresh_token(hosted, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.hosted.github import _refresh
    _, rt = hosted
    if rt.engine.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL row-locking contract')
    expiring_test_grant(rt)
    calls = []
    start = Barrier(2)
    def exchange(url, **kwargs):
        calls.append(kwargs['json']['refresh_token'])
        assert len(calls) == 1
        return httpx.Response(200, json={'access_token': 'test-concurrent-access', 'refresh_token': 'test-concurrent-refresh', 'expires_in': 28800})
    def run():
        start.wait(timeout=5)
        return _refresh(rt, 'alice')
    monkeypatch.setattr('app.hosted.github.httpx.post', exchange)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(run), pool.submit(run)]
        assert [future.result(timeout=10) for future in results] == ['test-concurrent-access'] * 2
    assert len(calls) == 1


def test_bulk_import_deduplicates_repository_aliases_and_preserves_settings(hosted, monkeypatch):
    client, rt = hosted
    with Session(rt.engine) as s:
        repo = s.get(Repository, 'repo')
        s.delete(s.get(Subscription, 'sub-alice')); s.commit()
        s.refresh(repo)
    async def resolve(runtime, name):
        assert name in {'public/project', 'old-owner/project'}
        return repo
    monkeypatch.setattr('app.hosted.api.resolve_repository', resolve)
    login(client)
    payload = {'repos': ['public/project', 'old-owner/project', 'https://github.com/public/project']}
    response = client.post('/api/repos', json=payload)
    assert response.status_code == 200
    assert response.json() == {'added': ['public/project'], 'existing': []}
    rows = client.get('/api/repos').json()['repos']
    assert len(rows) == 1 and rows[0]['recipes'] == ['docs-sync', 'issue-radar']
    ident = rows[0]['id']
    assert client.patch('/api/repos/' + ident, json={'paused': True, 'keywords': 'mine', 'difficulty': ['简单']}).status_code == 200
    assert client.post('/api/repos', json=payload).json() == {'added': [], 'existing': ['public/project']}
    same = client.get('/api/repos').json()['repos'][0]
    assert same['id'] == ident and same['paused'] and same['keywords'] == 'mine' and same['difficulty'] == ['简单']
    assert client.delete('/api/repos/' + ident).status_code == 200
    assert not client.get('/api/repos').json()['repos']
    login(client, 'bob')
    assert client.get('/api/repos').json()['repos'][0]['id'] == 'sub-bob'


def test_issue_model_cache_versions_and_initial_watch_baseline(hosted, monkeypatch):
    import asyncio, json
    from app.hosted.analysis import analyze_issue
    from app.hosted.corpus import fingerprint
    from app.hosted.models import Issue, Analysis, Quota
    from app.hosted.opportunities import observation
    _, rt = hosted
    rt.settings.llm_api_key = 'test-model-key'
    rt.settings.llm_model = 'test-model'
    calls = []
    class Model:
        def __init__(self, *args, **kwargs): pass
        async def chat(self, system, prompt, temperature=0.3):
            calls.append(prompt)
            return json.dumps({'difficulty': '简单', 'summary': 'Public summary', 'problem': 'Public evidence', 'plan': 'Public direction'})
        async def aclose(self): pass
    monkeypatch.setattr('app.hosted.analysis.LLMClient', Model)
    with Session(rt.engine) as s:
        repo = s.get(Repository, 'repo')
        issue = Issue(id='model-issue', repo_id=repo.id, number=99, title='Example', body='Version one',
                      url='https://github.com/public/project/issues/99', comments=14)
        issue.content_hash = fingerprint([issue.title, issue.body, issue.labels])
        issue.watch_snapshot = {'status': 'open', 'labels': [], 'comments': 14, 'eligibility': observation(issue, repo)}
        s.add(issue); s.commit()
    job = Job(kind='analyze_issue', payload={'issue_id': 'model-issue'})
    assert not asyncio.run(analyze_issue(rt, job))['cached']
    assert asyncio.run(analyze_issue(rt, job))['cached']
    assert len(calls) == 1
    with Session(rt.engine) as s:
        issue = s.get(Issue, 'model-issue')
        assert issue.watch_snapshot['eligibility']['issue']['difficulty'] == '简单'
        issue.body = 'Version two'
        issue.content_hash = fingerprint([issue.title, issue.body, issue.labels])
        s.add(issue); s.commit()
    assert not asyncio.run(analyze_issue(rt, job))['cached']
    assert len(calls) == 2
    with Session(rt.engine) as s:
        assert len(s.exec(select(Analysis)).all()) == 2
        assert s.exec(select(Quota).where(Quota.key.startswith('model:global:'))).one().count == 2
