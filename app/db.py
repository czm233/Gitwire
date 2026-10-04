"""SQLite（SQLModel）：引擎、建表、种子数据。"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from app.models import User, VaultBinding


def make_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )


def init_db(engine, vault_url: str = "") -> None:
    import app.models  # noqa: F401 确保表全部注册

    SQLModel.metadata.create_all(engine)
    _migrate(engine)
    with Session(engine) as session:
        if session.get(User, 1) is None:
            session.add(User(id=1, name="default"))
        binding = session.exec(
            select(VaultBinding).where(VaultBinding.user_id == 1)
        ).first()
        if binding is None:
            session.add(VaultBinding(user_id=1, vault_url=vault_url))
        elif vault_url and binding.vault_url != vault_url:
            binding.vault_url = vault_url
        session.commit()


def recover_orphan_runs(engine) -> int:
    """服务重启会把 worker 杀在半路，留下永远 running 的僵尸 run，
    触发防重入跳过。启动时全部标记 failed，下一轮 watch 自动重跑。"""
    from sqlmodel import Session, select

    from app.models import Run, utcnow

    count = 0
    with Session(engine) as session:
        orphans = session.exec(select(Run).where(Run.status == "running")).all()
        for r in orphans:
            r.status = "failed"
            r.error = "服务重启导致中断，下一轮 watch 自动重跑"
            r.finished_at = utcnow()
            session.add(r)
            count += 1
        if orphans:
            session.commit()
    return count


def _migrate(engine) -> None:
    """轻量加列迁移：create_all 不改既有表，新字段在这里补。"""
    from sqlalchemy import text

    adds = {
        "run": {
            "pr_number": "INTEGER",
            "pr_url": "TEXT",
        },
    }
    with engine.connect() as conn:
        for table, columns in adds.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for col, ddl in columns.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
        conn.commit()
