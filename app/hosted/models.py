"""Durable hosted state. Public corpus and user-owned records are separate tables."""
from __future__ import annotations

import time
import uuid
from sqlalchemy import Column, JSON, UniqueConstraint, Index
from sqlmodel import Field, SQLModel


def now() -> int:
    return int(time.time())


def uid() -> str:
    return uuid.uuid4().hex


class Account(SQLModel, table=True):
    __tablename__ = 'h_account'
    id: str = Field(default_factory=uid, primary_key=True)
    github_id: str = Field(unique=True, index=True)
    login: str
    avatar_url: str = ''
    access_token: str = ''  # Fernet ciphertext, never returned by API
    refresh_token: str = ''
    token_expires: int = 0
    reconnect_required: bool = False
    created_at: int = Field(default_factory=now)
    updated_at: int = Field(default_factory=now)
    timezone: str = 'Asia/Shanghai'
    digest_hour: int = 8
    email: str = ''
    email_verified: bool = False
    email_enabled: bool = False
    email_mode: str = 'digest'
    quiet_start: int = 22
    quiet_end: int = 8
    notify_kinds: list[str] = Field(default_factory=lambda: ['opportunity', 'resolved', 'taken', 'reopened', 'watch'], sa_column=Column(JSON, nullable=False))
    stars_synced_at: int = 0
    digest_date: str = ''


class BrowserSession(SQLModel, table=True):
    __tablename__ = 'h_session'
    digest: str = Field(primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    csrf: str
    expires_at: int = Field(index=True)


class OAuthAttempt(SQLModel, table=True):
    __tablename__ = 'h_oauth_attempt'
    digest: str = Field(primary_key=True)
    browser_digest: str
    verifier: str
    expires_at: int


class Repository(SQLModel, table=True):
    __tablename__ = 'h_repository'
    id: str = Field(default_factory=uid, primary_key=True)
    github_id: str = Field(unique=True, index=True)
    full_name: str = Field(unique=True, index=True)
    description: str = ''
    public: bool = True
    default_branch: str = 'main'
    stars: int = 0
    checked_at: int = 0
    next_scan: int = Field(default=0, index=True)
    issues_cursor: int = 0
    unavailable_reason: str = ''
    pushed_at: int = 0
    external_merge_at: int = 0
    signals_checked_at: int = 0
    signals_coverage_since: int = 0


class Subscription(SQLModel, table=True):
    __tablename__ = 'h_subscription'
    __table_args__ = (UniqueConstraint('user_id', 'repo_id'),)
    id: str = Field(default_factory=uid, primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    repo_id: str = Field(foreign_key='h_repository.id', index=True)
    created_at: int = Field(default_factory=now)
    paused: bool = False
    recipes: list[str] = Field(default_factory=lambda: ['docs-sync', 'issue-radar'], sa_column=Column(JSON, nullable=False))
    watched_issues: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    difficulty: list[str] = Field(default_factory=lambda: ['简单', '中等'], sa_column=Column(JSON, nullable=False))
    keywords: str = ''
    opportunity: dict = Field(default_factory=lambda: {'max_age_days': 30, 'max_comments': 15,
        'repo_pushed_within_days': 30, 'external_merge_within_days': 90}, sa_column=Column(JSON, nullable=False))


class Candidate(SQLModel, table=True):
    __tablename__ = 'h_candidate'
    __table_args__ = (UniqueConstraint('user_id', 'github_id', 'source'),)
    id: str = Field(default_factory=uid, primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    github_id: str
    full_name: str
    description: str = ''
    source: str = 'starred'
    starred_at: str = ''
    discovered_at: int = Field(default_factory=now)
    ignored: bool = False
    present: bool = True
    seen_generation: str = ''
    digest_id: str | None = Field(default=None, foreign_key='h_digest.id', index=True)


class Issue(SQLModel, table=True):
    __tablename__ = 'h_issue'
    __table_args__ = (UniqueConstraint('repo_id', 'number'), Index('ix_h_issue_repo_created', 'repo_id', 'created_at'))
    id: str = Field(default_factory=uid, primary_key=True)
    repo_id: str = Field(foreign_key='h_repository.id', index=True)
    number: int
    title: str
    body: str = ''
    title_zh: str = ''
    title_zh_source: str = ''
    url: str
    created_at: str = ''
    updated_at: str = ''
    state: str = 'open'
    state_reason: str = ''
    status: str = 'open'
    labels: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    assignees: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    comments: int = 0
    evidence: list[dict] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    content_hash: str = ''
    analysis_hash: str = ''
    difficulty: str = '未分析'
    summary: str = ''
    problem: str = ''
    plan: str = ''
    analyzed_at: int = 0
    checked_at: int = Field(default_factory=now)
    watch_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    comments_cursor: str = ''
    claim: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class Analysis(SQLModel, table=True):
    __tablename__ = 'h_analysis'
    key: str = Field(primary_key=True)
    repo_id: str = Field(foreign_key='h_repository.id', index=True)
    issue_number: int
    content: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: int = Field(default_factory=now)
    model: str
    prompt_version: str


class Job(SQLModel, table=True):
    __tablename__ = 'h_job'
    __table_args__ = (Index('ix_h_job_status_available', 'status', 'available_at'),)
    id: str = Field(default_factory=uid, primary_key=True)
    kind: str = Field(index=True)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    user_id: str | None = Field(default=None, foreign_key='h_account.id', index=True)
    active_key: str | None = Field(default=None, unique=True)
    status: str = Field(default='pending', index=True)
    created_at: int = Field(default_factory=now)
    available_at: int = Field(default_factory=now, index=True)
    lease_until: int = 0
    lease_owner: str = ''
    attempts: int = 0
    max_attempts: int = 5
    finished_at: int = 0
    error: str = ''
    result: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))


class JobAccess(SQLModel, table=True):
    __tablename__ = 'h_job_access'
    job_id: str = Field(foreign_key='h_job.id', primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', primary_key=True)


class Event(SQLModel, table=True):
    __tablename__ = 'h_event'
    id: str = Field(default_factory=uid, primary_key=True)
    key: str = Field(unique=True)
    repo_id: str = Field(foreign_key='h_repository.id', index=True)
    issue_number: int = 0
    kind: str
    title: str
    body: str
    url: str = ''
    difficulty: str = ''
    observation: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: int = Field(default_factory=now, index=True)


class Notification(SQLModel, table=True):
    __tablename__ = 'h_notification'
    __table_args__ = (UniqueConstraint('user_id', 'event_id'),)
    id: str = Field(default_factory=uid, primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    event_id: str = Field(foreign_key='h_event.id', index=True)
    read_at: int = 0
    created_at: int = Field(default_factory=now)
    digest_id: str | None = Field(default=None, foreign_key='h_digest.id', index=True)


class Delivery(SQLModel, table=True):
    __tablename__ = 'h_delivery'
    id: str = Field(default_factory=uid, primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    key: str = Field(unique=True)
    recipient: str
    subject: str
    body: str
    purpose: str = 'notification'
    status: str = 'pending'
    created_at: int = Field(default_factory=now)
    sent_at: int = 0
    error: str = ''


class EmailVerification(SQLModel, table=True):
    __tablename__ = 'h_email_verification'
    digest: str = Field(primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    email: str
    expires_at: int


class MailFeedback(SQLModel, table=True):
    __tablename__ = 'h_mail_feedback'
    event_id: str = Field(primary_key=True)
    delivery_id: str = Field(foreign_key='h_delivery.id', index=True)
    status: str
    created_at: int = Field(default_factory=now)


class LegacyRecord(SQLModel, table=True):
    """Imported personal archives must never be published into the shared corpus."""
    __tablename__ = 'h_legacy_record'
    __table_args__ = (UniqueConstraint('user_id', 'source_key'),
        Index('ix_h_legacy_owner_kind_created', 'user_id', 'kind', 'created_at'))
    id: str = Field(default_factory=uid, primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    source_key: str
    kind: str
    repo: str = ''
    title: str
    content: str
    created_at: int = Field(default_factory=now)


class Digest(SQLModel, table=True):
    __tablename__ = 'h_digest'
    __table_args__ = (UniqueConstraint('user_id', 'date'),)
    id: str = Field(default_factory=uid, primary_key=True)
    user_id: str = Field(foreign_key='h_account.id', index=True)
    date: str
    content: str
    created_at: int = Field(default_factory=now)
    period_start: int = 0
    period_end: int = 0


class Quota(SQLModel, table=True):
    __tablename__ = 'h_quota'
    key: str = Field(primary_key=True)
    count: int = 0
    expires_at: int = Field(index=True)


class HttpCache(SQLModel, table=True):
    __tablename__ = 'h_http_cache'
    key: str = Field(primary_key=True)
    etag: str = ''
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    updated_at: int = Field(default_factory=now)


class Artifact(SQLModel, table=True):
    __tablename__ = 'h_artifact'
    __table_args__ = (UniqueConstraint('repo_id', 'path', 'revision'),)
    id: str = Field(default_factory=uid, primary_key=True)
    repo_id: str = Field(foreign_key='h_repository.id', index=True)
    path: str
    revision: str
    content: str
    created_at: int = Field(default_factory=now)
