"""数据库模型。

users / vault_bindings 为多用户预留（v0 恒为 default 一行）；
runs / run_logs 是运行台账——机器自己的事，不进 vault。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    password_hash: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class VaultBinding(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    vault_url: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(default=1, index=True)
    repo: str = Field(index=True)
    trigger: str = "scheduled"  # scheduled | manual
    mode: str | None = None  # init | incremental | noop | pr-pending
    old_sha: str | None = None
    new_sha: str | None = None
    status: str = Field(default="running", index=True)  # running | published | failed
    summary: str | None = None
    error: str | None = None
    commit_sha: str | None = None
    pushed: bool = True
    pr_number: int | None = None   # PR 模式：对应的 Pull Request
    pr_url: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class Alert(SQLModel, table=True):
    """警报台账：tripwire 翻转 / 破坏性变更 / 新 release / CVE / 机会进出与关闭 /
    issue 追踪动态 / 运行失败。"""

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    repo: str = Field(default="", index=True)
    kind: str = Field(index=True)  # tripwire | breaking | release | cve | opportunity | issue-taken | issue-closed | watch | error
    title: str
    body: str = ""
    pushed: bool = False  # Bark 是否送达


class RunLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    ts: datetime = Field(default_factory=utcnow)
    level: str = "info"  # info | warn | error
    message: str
