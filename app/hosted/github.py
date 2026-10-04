"""Read-only GitHub transport. Tokens are never returned in errors or cached data."""
import asyncio
import hashlib
import json
import secrets
from urllib.parse import urlsplit, urljoin
import httpx
from sqlalchemy import select as sa_select
from sqlmodel import Session
from app.hosted.models import Account, HttpCache, now


class GitHubFailure(Exception):
    def __init__(self, status: int, retry_after: int = 0):
        self.status = status
        self.retry_after = retry_after
        self.safe_message = {401: 'GitHub 授权已失效，请重新连接', 403: 'GitHub 权限不足或请求受限',
                             404: '仓库不存在或不再公开', 429: 'GitHub 请求额度暂时耗尽'}.get(status, 'GitHub 暂时不可用')
        super().__init__(self.safe_message)


def token_values(data):
    """Validate the whole exchange before replacing either encrypted credential."""
    if not isinstance(data, dict) or data.get('error'):
        raise ValueError('Invalid token response')
    access, refresh = data.get('access_token'), data.get('refresh_token', '')
    scope = data.get('scope', '')
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not isinstance(scope, str):
        raise ValueError('Invalid token response')
    if set(filter(None, scope.replace(' ', ',').split(','))) - {'read:user', 'user:email'}:
        raise ValueError('Unexpected scopes')
    if 'expires_in' not in data:
        return access, refresh, 0  # GitHub App can have expiration disabled.
    seconds = data['expires_in']
    if isinstance(seconds, bool) or not isinstance(seconds, (str, int)):
        raise ValueError('Invalid token expiry')
    seconds = int(seconds)
    if seconds <= 0 or not refresh:
        raise ValueError('Invalid expiring token response')
    return access, refresh, now() + seconds


def retry_delay(response):
    try:
        return max(60, int(response.headers.get('retry-after', 0)),
                   int(response.headers.get('x-ratelimit-reset', now())) - now())
    except ValueError:
        return 60


def _refresh(runtime, user_id: str) -> str:
    # Run in a thread: row locking and network I/O must not block the async event loop.
    with Session(runtime.engine) as s:
        user = s.exec(sa_select(Account).where(Account.id == user_id).with_for_update()).scalar_one()
        if user.reconnect_required or not user.access_token:
            raise GitHubFailure(401)
        if not user.token_expires or user.token_expires > now() + 120:
            return runtime.box.open(user.access_token)
        if not user.refresh_token:
            user.reconnect_required = True
            s.add(user)
            s.commit()
            raise GitHubFailure(401)
        try:
            response = httpx.post('https://github.com/login/oauth/access_token', headers={'Accept': 'application/json'},
                json={'client_id': runtime.settings.github_client_id, 'client_secret': runtime.settings.github_client_secret,
                      'grant_type': 'refresh_token', 'refresh_token': runtime.box.open(user.refresh_token)}, timeout=30)
        except httpx.HTTPError:
            raise GitHubFailure(502) from None
        if response.status_code == 429 or (response.status_code == 403 and (
                response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers)):
            raise GitHubFailure(429, retry_delay(response))
        if response.status_code >= 500 or response.status_code == 403:
            raise GitHubFailure(response.status_code, retry_delay(response))
        try:
            data = response.json()
        except ValueError:
            raise GitHubFailure(502) from None
        if isinstance(data, dict) and data.get('error') == 'bad_refresh_token':
            user.reconnect_required = True
            s.add(user)
            s.commit()
            raise GitHubFailure(401)
        if response.status_code != 200:
            raise GitHubFailure(502)
        try:
            access, refresh, expires = token_values(data)
        except (ValueError, TypeError):
            raise GitHubFailure(502) from None
        user.access_token = runtime.box.seal(access)
        user.refresh_token = runtime.box.seal(refresh)
        user.token_expires = expires
        s.add(user)
        s.commit()
        return access


async def user_token(runtime, user_id: str) -> str:
    return await asyncio.to_thread(_refresh, runtime, user_id)


async def get(runtime, path: str, params: dict | None = None, user_id: str | None = None,
              cached: bool = False, accept: str = 'application/vnd.github+json'):
    if not path.startswith('/') or path.startswith('//') or '://' in path:
        raise ValueError('GitHub API path required')
    token = await user_token(runtime, user_id) if user_id else runtime.settings.github_token
    headers = {'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    # Never share or persist account responses in the public response cache.
    cached = cached and user_id is None and path.startswith('/repos/')
    if cached:
        # Revalidate visibility before serving even a hot cache entry. An operator's
        # token might still read a repository after its owner makes it private.
        name = '/'.join(path.split('/')[2:4])
        await public_repo(runtime, name)
    key = hashlib.sha256(json.dumps([path, params, accept], sort_keys=True).encode()).hexdigest()
    redis_key = 'gitwire:public:v1:' + key
    if cached and runtime.redis:
        try:
            hot = await runtime.redis.get(redis_key)
            if hot is not None:
                return json.loads(hot)
        except Exception:
            pass  # Redis is reconstructable; its failure never blocks the DB/API path.
    previous = None
    if cached:
        with Session(runtime.engine) as s:
            previous = s.get(HttpCache, key)
        if previous and previous.etag:
            headers['If-None-Match'] = previous.etag
    url = 'https://api.github.com' + path
    for _ in range(4):
        response = await runtime.http.get(url, params=params, headers=headers)
        if response.status_code not in (301, 302, 307, 308):
            break
        url = urljoin(url, response.headers.get('location', ''))
        target = urlsplit(url)
        if target.scheme != 'https' or target.netloc != 'api.github.com' or target.username:
            raise GitHubFailure(502)
        params = None
    if response.status_code == 304 and previous:
        data = previous.payload['data']
        if runtime.redis:
            try:
                await runtime.redis.set(redis_key, json.dumps(data), ex=30)
            except Exception:
                pass
        return data
    if response.status_code != 200:
        if response.status_code == 401 and user_id:
            with Session(runtime.engine) as s:
                user = s.exec(sa_select(Account).where(Account.id == user_id).with_for_update()).scalar_one()
                if not user.reconnect_required and user.access_token and not secrets.compare_digest(runtime.box.open(user.access_token), token):
                    # Another request rotated this token while our API request was
                    # in flight. Retry the job without invalidating the new grant.
                    raise GitHubFailure(503, 1)
                user.reconnect_required = True
                s.add(user)
                s.commit()
        retry = retry_delay(response)
        status = response.status_code
        if status == 403 and (response.headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in response.headers):
            status = 429
        raise GitHubFailure(status, retry)
    data = response.json()
    if cached:
        with Session(runtime.engine) as s:
            row = s.get(HttpCache, key) or HttpCache(key=key)
            row.etag, row.payload, row.updated_at = response.headers.get('etag', ''), {'data': data}, now()
            s.add(row)
            s.commit()
        if runtime.redis:
            try:
                await runtime.redis.set(redis_key, json.dumps(data), ex=30)
            except Exception:
                pass
    return data


async def public_repo(runtime, name: str) -> dict:
    info = await get(runtime, '/repos/' + name)
    if info.get('private') is not False or info.get('visibility', 'public') != 'public':
        raise GitHubFailure(404)
    return info
