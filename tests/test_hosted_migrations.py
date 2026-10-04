"""Exercise frozen migrations against an isolated schema on the project's PostgreSQL."""
import importlib.util
import os
from pathlib import Path
import uuid
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text, MetaData
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel
from app.config import Settings
from app.hosted import models


@pytest.mark.skipif(os.getenv('GITWIRE_TEST_POSTGRES') != '1', reason='Explicit PostgreSQL integration run')
def test_frozen_migrations_match_current_schema():
    url = make_url(Settings().database_url)
    assert url.get_backend_name() == 'postgresql'
    schema = 'test_gitwire_migration_' + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url.update_query_dict({'options': '-csearch_path=' + schema}))
    try:
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                for path in sorted(Path('migrations/versions').glob('*.py')):
                    spec = importlib.util.spec_from_file_location('test_migration', path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    if module.revision == '0006_digest_checkpoints':
                        seed_old_digest(conn)
                    module.upgrade()
            expected = MetaData()
            for table in SQLModel.metadata.sorted_tables:
                if table.name.startswith('h_'):
                    table.to_metadata(expected)
            assert compare_metadata(context, expected) == []
            assert conn.execute(text("SELECT period_end-period_start FROM h_digest WHERE id='old-report'")).scalar_one() == 23 * 3600
            for table in ('h_candidate', 'h_notification'):
                links = dict(conn.execute(text(f'SELECT id,digest_id FROM {table}')).all())
                assert links == {'included': 'old-report', 'boundary': None, 'other-user': None}
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def seed_old_digest(conn):
    """Seed the pre-0006 layout, including a DST boundary and another account."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    old = MetaData()
    old.reflect(conn)
    def insert(record):
        table = old.tables[record.__tablename__]
        conn.execute(table.insert().values(**{k: v for k, v in record.model_dump().items() if k in table.c}))
    for user in ('old-user', 'other-user'):
        insert(models.Account(id=user, github_id=user, login=user, timezone='America/New_York'))
    insert(models.Repository(id='repo', github_id='repo', full_name='public/project'))
    insert(models.Event(id='event', key='event', repo_id='repo', kind='release', title='Test release', body='Public'))
    insert(models.Digest(id='old-report', user_id='old-user', date='2026-03-09', content='Old calendar-day digest'))
    start = int(datetime(2026,3,8,tzinfo=ZoneInfo('America/New_York')).timestamp())
    end = int(datetime(2026,3,9,tzinfo=ZoneInfo('America/New_York')).timestamp())
    for ident, stamp, user in [('included', start, 'old-user'), ('boundary',end,'old-user'), ('other-user',start,'other-user')]:
        insert(models.Candidate(id=ident, user_id=user, github_id=ident, full_name='public/'+ident, discovered_at=stamp))
        # Each notification has its own public event to satisfy user/event uniqueness.
        if ident != 'included':
            insert(models.Event(id=ident, key=ident, repo_id='repo', kind='release', title=ident, body='Public'))
        insert(models.Notification(id=ident, user_id=user, event_id='event' if ident=='included' else ident, created_at=stamp))
