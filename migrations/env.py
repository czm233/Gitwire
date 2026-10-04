from alembic import context
from sqlalchemy import pool
from sqlmodel import SQLModel, create_engine
from app.config import Settings
from app.hosted import models  # noqa: F401

settings = Settings()
if not settings.database_url:
    raise RuntimeError('DATABASE_URL required')

def run_migrations():
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=SQLModel.metadata)
        with context.begin_transaction():
            context.run_migrations()

run_migrations()
