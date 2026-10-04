"""Durable mail outbox. Local development uses Mailpit, never real recipients."""
import asyncio
import hashlib
import hmac
import json
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy
from urllib.parse import parse_qs, quote
import secrets
import smtplib
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError
from cryptography.fernet import InvalidToken
from sqlmodel import Session, select
from app.hosted.models import Account, Delivery, EmailVerification, Event, Issue, MailFeedback, Notification, Repository, Subscription, now
from app.hosted.security import digest, require_csrf
from app.hosted.jobs import DeferJob, enqueue
from app.hosted.limits import consume, consume_many

router = APIRouter()


class EmailIn(BaseModel):
    email: EmailStr


@router.post('/email/verify-request')
async def verification_request(body: EmailIn, request: Request):
    user = require_csrf(request)
    rt = request.app.state.runtime
    if not rt.settings.smtp_host or not rt.settings.mail_from:
        raise HTTPException(503, '邮件服务尚未配置')
    consume(rt.engine, 'email-verify:' + user.id, 3)
    token = secrets.token_urlsafe(32)
    with Session(rt.engine) as s:
        row = s.get(Account, user.id)
        row.email, row.email_verified, row.email_enabled = str(body.email), False, False
        # Superseded links are invalid; verification is always bound to the current email.
        for old in s.exec(select(EmailVerification).where(EmailVerification.user_id == user.id)).all():
            s.delete(old)
        s.add(EmailVerification(digest=digest(token), user_id=user.id, email=str(body.email), expires_at=now() + 1800))
        link = rt.settings.public_url.rstrip('/') + '/settings?verify=' + token
        delivery = Delivery(user_id=user.id, key='verify:' + digest(token), recipient=str(body.email),
                            purpose='verification', subject='Gitwire · 验证收件邮箱',
                            body=rt.box.seal(f'请在 30 分钟内打开此链接并确认邮箱：\n{link}\n\n如果不是你操作的，请忽略。'))
        s.add(row)
        s.add(delivery)
        s.commit()
        delivery_id = delivery.id
    enqueue(rt.engine, 'mail', {'delivery_id': delivery_id}, 'mail:' + delivery_id, user_id=user.id)
    return {'ok': True}


class VerifyIn(BaseModel):
    token: str


@router.post('/email/verify')
async def verify(body: VerifyIn, request: Request):
    user = require_csrf(request)
    with Session(request.app.state.runtime.engine) as s:
        record = s.get(EmailVerification, digest(body.token))
        account = s.get(Account, user.id)
        if not record or record.user_id != user.id or record.expires_at < now() or record.email != account.email:
            raise HTTPException(400, '验证链接无效或已过期')
        account.email_verified = True
        s.add(account)
        s.delete(record)
        s.commit()
    return {'ok': True}


def quiet(user: Account) -> bool:
    hour = datetime.now(ZoneInfo(user.timezone)).hour
    start, end = user.quiet_start, user.quiet_end
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def quiet_delay(user: Account) -> int:
    local = datetime.now(ZoneInfo(user.timezone))
    end = local.replace(hour=user.quiet_end, minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return max(1, int(end.timestamp() - local.timestamp()))


def eligible_delivery(s, delivery, user) -> bool:
    if not user or user.email != delivery.recipient:
        return False
    if delivery.purpose == 'verification':
        record = s.get(EmailVerification, delivery.key.removeprefix('verify:'))
        return bool(record and record.user_id == user.id and record.email == user.email and record.expires_at > now())
    if not user.email_enabled or not user.email_verified:
        return False
    if delivery.purpose == 'digest':
        return user.email_mode in {'digest', 'both'}
    if user.email_mode not in {'immediate', 'both'}:
        return False
    notification = s.get(Notification, delivery.key.removeprefix('event:'))
    if not notification or notification.user_id != user.id or notification.read_at:
        return False
    event = s.get(Event, notification.event_id)
    if not event or event.kind not in user.notify_kinds:
        return False
    repo = s.get(Repository, event.repo_id)
    sub = s.exec(select(Subscription).where(Subscription.user_id == user.id,
        Subscription.repo_id == event.repo_id, Subscription.paused == False)).first()
    if not repo or not repo.public or not sub:
        return False
    if event.kind != 'opportunity' and event.issue_number:
        from app.hosted.opportunities import change_matches
        issue = s.exec(select(Issue).where(Issue.repo_id == event.repo_id, Issue.number == event.issue_number)).first()
        if not issue or not change_matches(issue, repo, sub, event.observation):
            return False
    if not event.issue_number:
        from app.hosted.recipe_events import recipe_enabled
        if not recipe_enabled(sub, event.kind):
            return False
    if event.kind == 'opportunity':
        issue = s.exec(select(Issue).where(Issue.repo_id == event.repo_id, Issue.number == event.issue_number)).first()
        from app.hosted.opportunities import exclusion
        if not issue or issue.status != 'open' or 'issue-radar' not in sub.recipes or exclusion(issue, repo, sub):
            return False
    if (not event.issue_number or event.kind == 'opportunity') and sub.keywords and not any(word.casefold() in (event.title + ' ' + event.body).casefold() for word in sub.keywords.split()):
        return False
    return now() - notification.created_at <= 86400


def unsubscribe_token(rt, user):
    return rt.box.seal(json.dumps({'purpose': 'unsubscribe', 'user_id': user.id, 'email': user.email}))


def unsubscribe(rt, token):
    try:
        data = json.loads(rt.box.open(token))
        if data.get('purpose') != 'unsubscribe':
            raise ValueError()
        user_id, email = data['user_id'], data['email']
    except (InvalidToken, ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, '退订链接无效') from None
    with Session(rt.engine) as s:
        user = s.exec(select(Account).where(Account.id == user_id).with_for_update()).first()
        if user and user.email == email:
            user.email_enabled = False
            s.add(user)
            for delivery in s.exec(select(Delivery).where(Delivery.user_id == user.id,
                    Delivery.purpose != 'verification', Delivery.status == 'pending')):
                delivery.status = 'cancelled'
                s.add(delivery)
            s.commit()
    return {'ok': True}


@router.post('/email/unsubscribe')
async def unsubscribe_email(body: VerifyIn, request: Request):
    return unsubscribe(request.app.state.runtime, body.token)


@router.get('/email/unsubscribe/one-click')
async def unsubscribe_landing(request: Request, token: str = ''):
    # GET is safe for mail scanners; only an explicit POST changes preferences.
    return RedirectResponse(request.app.state.runtime.settings.public_url + '/unsubscribe?token=' + quote(token), status_code=303)


async def small_body(request, limit=65536):
    parts, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, '请求过大')
        parts.append(chunk)
    return b''.join(parts)


@router.post('/email/unsubscribe/one-click')
async def unsubscribe_one_click(request: Request, token: str = ''):
    raw = await small_body(request, 4096)
    content_type = request.headers.get('content-type', '')
    if content_type.startswith('multipart/form-data'):
        message = BytesParser(policy=policy.default).parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + raw)
        fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode()
                  for part in message.iter_parts() if not part.is_multipart()}
    else:
        fields = {key: values[0] for key, values in parse_qs(raw.decode(errors='replace')).items()}
    if fields.get('List-Unsubscribe') != 'One-Click':
        raise HTTPException(400, '无效的退订请求')
    return unsubscribe(request.app.state.runtime, token)


class FeedbackIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    delivery_id: str = Field(min_length=1, max_length=100)
    status: str


@router.post('/mail-feedback')
async def feedback(request: Request):
    rt = request.app.state.runtime
    secret = rt.settings.mail_feedback_secret
    if not secret:
        raise HTTPException(503, '邮件回执尚未配置')
    raw = await small_body(request)
    timestamp = request.headers.get('x-gitwire-timestamp', '')
    signature = request.headers.get('x-gitwire-signature', '')
    try:
        valid_time = abs(now() - int(timestamp)) <= 300
    except ValueError:
        valid_time = False
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    if not valid_time or not hmac.compare_digest(expected, signature):
        raise HTTPException(401, '回执签名无效或已过期')
    try:
        body = FeedbackIn.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(400, '回执格式无效') from None
    if body.status not in {'bounced', 'complained'}:
        raise HTTPException(400, '无效的回执状态')
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if rt.engine.dialect.name == 'postgresql' else sqlite_insert
    with Session(rt.engine) as s:
        owner = s.exec(select(Delivery.user_id).where(Delivery.id == body.delivery_id)).first()
        if not owner:
            raise HTTPException(404, '投递记录不存在')
        # Reserve event ID before locking its delivery. Concurrent retries are
        # idempotent even if a relay accidentally reuses an ID for another mail.
        added = s.execute(insert(MailFeedback).values(event_id=body.event_id,
            delivery_id=body.delivery_id, status=body.status, created_at=now())
            .on_conflict_do_nothing(index_elements=['event_id']).returning(MailFeedback.event_id)).first()
        if not added:
            previous = s.get(MailFeedback, body.event_id)
            if previous.delivery_id != body.delivery_id or previous.status != body.status:
                raise HTTPException(409, '回执编号已用于其他事件')
            return {'ok': True, 'duplicate': True}
        # Same lock order as unsubscribe: account, then deliveries.
        user = s.exec(select(Account).where(Account.id == owner).with_for_update()).first()
        delivery = s.exec(select(Delivery).where(Delivery.id == body.delivery_id).with_for_update()).first()
        if not delivery:
            raise HTTPException(404, '投递记录不存在')
        delivery.status = body.status
        delivery.error = '邮件退信，已停止此邮箱的提醒' if body.status == 'bounced' else '收件人投诉，已停止此邮箱的提醒'
        s.add(delivery)
        if user and user.email == delivery.recipient:
            user.email_enabled = False
            user.email_verified = False
            s.add(user)
        s.commit()
    return {'ok': True}


def _smtp(settings, message):
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            import ssl
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


async def send(rt, job):
    delivery_id = job.payload['delivery_id']
    with Session(rt.engine) as s:
        delivery = s.get(Delivery, delivery_id)
        if not delivery or delivery.status != 'pending':
            return {'skipped': True}
        user = s.get(Account, delivery.user_id)
        if not eligible_delivery(s, delivery, user):
            delivery.status = 'cancelled'
            s.add(delivery)
            s.commit()
            return {'cancelled': True}
        if delivery.purpose != 'verification' and quiet(user):
            raise DeferJob('当前处于免打扰时段，结束后发送', quiet_delay(user))
        token = unsubscribe_token(rt, user) if delivery.purpose != 'verification' else ''
    consume_many(rt.engine, [('mail:global', rt.settings.mail_global_hourly_limit),
        ('mail:user:' + delivery.user_id, rt.settings.mail_user_hourly_limit)])
    message = EmailMessage()
    message['From'], message['To'], message['Subject'] = rt.settings.mail_from, delivery.recipient, delivery.subject
    message['Message-ID'] = f'<{delivery.id}@gitwire.local>'
    footer = '\n\n提醒设置：' + rt.settings.public_url + '/settings'
    if token:
        footer += '\n停止邮件提醒：' + rt.settings.public_url + '/unsubscribe?token=' + token
        if rt.settings.public_url.startswith('https://'):
            message['List-Unsubscribe'] = '<' + rt.settings.public_url + '/api/email/unsubscribe/one-click?token=' + token + '>'
            message['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    message.set_content(rt.box.open(delivery.body) + footer)
    try:
        await asyncio.to_thread(_smtp, rt.settings, message)
    except smtplib.SMTPRecipientsRefused:
        with Session(rt.engine) as s:
            row = s.get(Delivery, delivery_id)
            row.status, row.error = 'failed', '邮件服务器拒绝收件地址，请检查邮箱后重试'
            s.add(row)
            s.commit()
        return {'rejected': True}
    with Session(rt.engine) as s:
        row = s.exec(select(Delivery).where(Delivery.id == delivery_id).with_for_update()).one()
        # A bounce/complaint can arrive before SMTP returns. Preserve it (and a
        # concurrent unsubscribe cancellation), while recording the handoff time.
        if row.status == 'pending':
            row.status = 'sent'
        row.sent_at = now()
        s.add(row)
        s.commit()
    return {'sent': True}


def queue_notifications(rt):
    with Session(rt.engine) as s:
        rows = s.exec(select(Notification, Event, Account, Repository).select_from(Notification)
            .join(Event, Notification.event_id == Event.id).join(Account, Notification.user_id == Account.id)
            .join(Repository, Event.repo_id == Repository.id)
            .where(Account.email_enabled == True, Account.email_verified == True, Repository.public == True)).all()
        for notification, event, user, repo in rows:
            if user.email_mode not in {'immediate', 'both'} or event.kind not in user.notify_kinds or quiet(user):
                continue
            sub = s.exec(select(Subscription).where(Subscription.user_id == user.id, Subscription.repo_id == repo.id, Subscription.paused == False)).first()
            if not sub or notification.read_at or now() - notification.created_at > 86400:
                continue
            key = 'event:' + notification.id
            if s.exec(select(Delivery).where(Delivery.key == key)).first():
                continue
            s.add(Delivery(user_id=user.id, key=key, recipient=user.email, subject='Gitwire · ' + event.title,
                           body=rt.box.seal(f'{repo.full_name}\n{event.title}\n{event.body}\n{event.url}')))
        s.commit()
        pending = s.exec(select(Delivery).where(Delivery.status == 'pending')).all()
    for delivery in pending:
        enqueue(rt.engine, 'mail', {'delivery_id': delivery.id}, 'mail:' + delivery.id, user_id=delivery.user_id)
