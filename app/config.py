"""配置：全部来自 .env / 环境变量，系统本体无状态。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # vault：本地路径 = 纯本地模式（历史完整）；URL = clone+push 发布模式
    gitwire_vault: str = ""
    github_token: str = ""
    # LLM：openai（默认，任意兼容接口）| anthropic（GLM Coding Plan / Gemini 反代等）
    llm_protocol: str = "openai"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_max_tokens: int = 16384
    llm_thinking: str = ""  # "disabled" = 关思考块（anthropic 协议）
    secret_key: str = ""

    # Hosted service: OAuth is read-only; remote backups belong to the local client.
    hosted_enabled: bool = True
    database_url: str = ""
    redis_url: str = ""
    encryption_key: str = ""
    public_url: str = "http://127.0.0.1:10241"
    github_client_id: str = ""
    github_client_secret: str = ""
    session_days: int = 30
    repo_scan_minutes: int = 60
    issue_scan_minutes: int = 15
    max_subscriptions: int = 50
    analysis_daily_limit: int = 20
    model_daily_limit: int = 300
    model_repo_daily_limit: int = 50
    source_clone_timeout_seconds: int = Field(default=120, ge=1, le=600)
    source_clone_max_mb: int = Field(default=200, ge=1, le=2048)
    public_source_only: bool = False  # Hosted recipe adapter always overrides to True.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    mail_from: str = ""
    mail_feedback_secret: str = ""
    mail_user_hourly_limit: int = 50
    mail_global_hourly_limit: int = 1000

    watch_cron: str = "0 * * * *"
    daily_cron: str = "0 8 * * *"
    data_dir: str = "./data"
    git_author: str = ""
    tz: str = "Asia/Shanghai"

    # v1：Bark 推送（留空 = 只记台账不推送）
    bark_url: str = ""
    bark_group: str = "Gitwire"
    # v1：Obsidian 模式——给产出 markdown 注入 YAML frontmatter
    obsidian_frontmatter: bool = False

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def vault_is_url(self) -> bool:
        v = self.gitwire_vault
        return bool(v) and ("://" in v or v.startswith("git@"))

    @property
    def vault_remote(self) -> str:
        return self.gitwire_vault if self.vault_is_url else ""

    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def local_now(self) -> datetime:
        return datetime.now(self.tzinfo())

    def local_date_str(self) -> str:
        return self.local_now().strftime("%Y-%m-%d")

    def local_yesterday_str(self) -> str:
        from datetime import timedelta

        return (self.local_now() - timedelta(days=1)).strftime("%Y-%m-%d")
