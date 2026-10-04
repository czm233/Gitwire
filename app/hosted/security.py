"""Opaque sessions, encrypted OAuth credentials and per-session CSRF protection."""
import hashlib
import secrets
from urllib.parse import urlsplit
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from sqlmodel import Session
from app.hosted.models import Account, BrowserSession, now

COOKIE = 'gitwire_session'


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SecretBox:
    def __init__(self, key: str):
        if not key:
            raise RuntimeError('ENCRYPTION_KEY required; generate a Fernet key in local .env')
        self.fernet = Fernet(key.encode())

    def seal(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode() if value else ''

    def open(self, value: str) -> str:
        return self.fernet.decrypt(value.encode()).decode() if value else ''


def current_user(request: Request, required: bool = True) -> Account | None:
    token = request.cookies.get(COOKIE, '')
    with Session(request.app.state.runtime.engine) as session:
        login = session.get(BrowserSession, digest(token)) if token else None
        if login and login.expires_at > now():
            user = session.get(Account, login.user_id)
            if user:
                request.state.login = login
                return user
    if required:
        raise HTTPException(401, '请先使用 GitHub 登录')
    return None


def require_csrf(request: Request) -> Account:
    user = current_user(request)
    supplied = request.headers.get('x-csrf-token', '')
    if not supplied or not secrets.compare_digest(supplied, request.state.login.csrf):
        raise HTTPException(403, '页面会话已更新，请刷新后重试')
    origin = request.headers.get('origin')
    configured = urlsplit(request.app.state.runtime.settings.public_url)
    expected = f'{configured.scheme}://{configured.netloc}'
    if origin and origin != expected:
        raise HTTPException(403, '不允许跨站操作')
    return user


def user_view(user: Account) -> dict:
    return {k: getattr(user, k) for k in (
        'id', 'github_id', 'login', 'avatar_url', 'reconnect_required',
        'timezone', 'digest_hour', 'email', 'email_verified', 'email_enabled',
        'email_mode', 'quiet_start', 'quiet_end', 'notify_kinds', 'stars_synced_at',
    )}
