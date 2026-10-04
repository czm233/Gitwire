"""Database creation is a migration command, never a web-worker side effect."""
from sqlalchemy import event
from sqlmodel import create_engine
from app.config import Settings


def engine_for(settings: Settings):
    if not settings.database_url:
        raise RuntimeError('DATABASE_URL is required; run scripts/acceptance.py up after configuring .env')
    kwargs = {'pool_pre_ping': True}
    if settings.database_url.startswith('sqlite:'):
        kwargs['connect_args'] = {'check_same_thread': False}
    engine = create_engine(settings.database_url, **kwargs)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def foreign_keys(dbapi_connection, _):
            dbapi_connection.execute('PRAGMA foreign_keys=ON')
    return engine
