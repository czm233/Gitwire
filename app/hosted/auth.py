"""GitHub OAuth only. No passwords, development logins or user-supplied PATs."""
import base64
import hashlib
import secrets
from urllib.parse import urlencode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from app.hosted.models import Account, BrowserSession, OAuthAttempt, now
from app.hosted.security import COOKIE, current_user, digest, require_csrf, user_view
from app.hosted.github import token_values

router = APIRouter(prefix='/auth')
ATTEMPT_COOKIE = 'gitwire_oauth'


@router.get('/me')
async def me(request: Request):
    rt = request.app.state.runtime
    user = current_user(request, required=False)
    return {'user': user_view(user) if user else None,
            'csrf': request.state.login.csrf if user else None,
            'github_configured': bool(rt.settings.github_client_id and rt.settings.github_client_secret),
            'hosted': True}


@router.get('/github')
async def github_login(request: Request):
    rt = request.app.state.runtime
    if not rt.settings.github_client_id or not rt.settings.github_client_secret:
        raise HTTPException(503, 'GitHub 登录尚未配置，请联系应用管理员')
    state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    with Session(rt.engine) as s:
        s.add(OAuthAttempt(digest=digest(state), browser_digest=digest(browser),
                           verifier=rt.box.seal(verifier), expires_at=now() + 600))
        s.commit()
    url = 'https://github.com/login/oauth/authorize?' + urlencode({
        'client_id': rt.settings.github_client_id,
        'redirect_uri': rt.settings.public_url.rstrip('/') + '/api/auth/github/callback',
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
        'allow_signup': 'false',
        'prompt': 'select_account',
    })
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(ATTEMPT_COOKIE, browser, httponly=True, samesite='lax',
                        secure=rt.settings.public_url.startswith('https:'), max_age=600, path='/api/auth')
    return response


@router.get('/github/callback')
async def github_callback(request: Request, state: str = '', code: str = '', error: str = ''):
    rt = request.app.state.runtime
    with Session(rt.engine) as s:
        attempt = s.exec(select(OAuthAttempt).where(OAuthAttempt.digest == digest(state)).with_for_update()).first()
        if not attempt or attempt.expires_at < now() or not secrets.compare_digest(
            attempt.browser_digest, digest(request.cookies.get(ATTEMPT_COOKIE, '')),
        ):
            if 'text/html' in request.headers.get('accept', ''):
                return RedirectResponse(rt.settings.public_url.rstrip('/') + '/settings?auth=expired', status_code=303)
            raise HTTPException(400, '登录请求已过期或不匹配，请重新登录')
        verifier = rt.box.open(attempt.verifier)
        s.delete(attempt)
        s.commit()  # one-use state, even for denied authorization or failed exchanges
    if error or not code:
        return RedirectResponse(rt.settings.public_url + '/settings?auth=cancelled', status_code=303)
    try:
        response = await rt.http.post('https://github.com/login/oauth/access_token',
            headers={'Accept': 'application/json'}, json={
                'client_id': rt.settings.github_client_id, 'client_secret': rt.settings.github_client_secret,
                'code': code, 'code_verifier': verifier,
                'redirect_uri': rt.settings.public_url.rstrip('/') + '/api/auth/github/callback',
            })
        data = response.json()
        if response.status_code != 200:
            raise ValueError('exchange failed')
        access, refresh, expires = token_values(data)
        identity = await rt.http.get('https://api.github.com/user', headers={
            'Authorization': 'Bearer ' + access, 'Accept': 'application/vnd.github+json',
        })
        identity.raise_for_status()
        profile = identity.json()
        github_id, login = str(int(profile['id'])), str(profile['login'])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, 'GitHub 授权未完成，请重新登录') from None
    with Session(rt.engine) as s:
        user = s.exec(select(Account).where(Account.github_id == github_id)).first()
        if not user:
            user = Account(github_id=github_id, login=login)
            s.add(user)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                user = s.exec(select(Account).where(Account.github_id == github_id)).one()
        user.login, user.avatar_url = login, profile.get('avatar_url', '')
        user.access_token = rt.box.seal(access)
        user.refresh_token = rt.box.seal(refresh)
        user.token_expires = expires
        user.reconnect_required, user.updated_at = False, now()
        old_session = s.get(BrowserSession, digest(request.cookies.get(COOKIE, '')))
        if old_session:
            s.delete(old_session)
        raw = secrets.token_urlsafe(48)
        s.add(user)
        s.add(BrowserSession(digest=digest(raw), user_id=user.id, csrf=secrets.token_urlsafe(32),
                             expires_at=now() + rt.settings.session_days * 86400))
        s.commit()
    response = RedirectResponse(rt.settings.public_url.rstrip('/') + '/candidates?auth=success', status_code=303)
    response.delete_cookie(ATTEMPT_COOKIE, path='/api/auth')
    response.set_cookie(COOKIE, raw, httponly=True, samesite='lax',
                        secure=rt.settings.public_url.startswith('https:'), max_age=rt.settings.session_days * 86400)
    return response


@router.post('/logout')
async def logout(request: Request):
    require_csrf(request)
    with Session(request.app.state.runtime.engine) as s:
        row = s.get(BrowserSession, request.state.login.digest)
        if row:
            s.delete(row)
            s.commit()
    from fastapi.responses import JSONResponse
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE)
    return response


@router.post('/disconnect')
async def disconnect(request: Request):
    user = require_csrf(request)
    with Session(request.app.state.runtime.engine) as s:
        row = s.get(Account, user.id)
        row.access_token = row.refresh_token = ''
        row.reconnect_required = True
        s.add(row)
        s.commit()
    return {'ok': True}


import httpx
