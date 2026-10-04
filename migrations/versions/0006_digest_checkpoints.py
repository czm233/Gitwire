"""Durable digest coverage, including late arrivals and interrupted Star syncs."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alembic import op
import sqlalchemy as sa

revision = '0006_digest_checkpoints'
down_revision = '0005_issue_observations'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('period_start', 'period_end'):
        op.add_column('h_digest', sa.Column(name, sa.Integer(), nullable=False, server_default='0'))
        op.alter_column('h_digest', name, server_default=None)
    for table in ('h_candidate', 'h_notification'):
        op.add_column(table, sa.Column('digest_id', sa.String(), nullable=True))
        op.create_foreign_key('fk_' + table + '_digest', table, 'h_digest', ['digest_id'], ['id'])
        op.create_index('ix_' + table + '_digest_id', table, ['digest_id'])
    # Old reports explicitly covered the preceding calendar day. Preserve that
    # coverage instead of treating those already reported records as new again.
    bind = op.get_bind()
    rows = bind.execute(sa.text('SELECT d.id,d.user_id,d.date,a.timezone FROM h_digest d '
                               'JOIN h_account a ON a.id=d.user_id ORDER BY d.date')).mappings().all()
    for row in rows:
        end = datetime.strptime(row['date'], '%Y-%m-%d').replace(tzinfo=ZoneInfo(row['timezone']))
        start = end - timedelta(days=1)
        params = dict(digest=row['id'], user=row['user_id'], start=int(start.timestamp()), end=int(end.timestamp()))
        bind.execute(sa.text('UPDATE h_digest SET period_start=:start,period_end=:end WHERE id=:digest'), params)
        bind.execute(sa.text('UPDATE h_notification SET digest_id=:digest WHERE user_id=:user '
                             'AND created_at>=:start AND created_at<:end AND digest_id IS NULL'), params)
        bind.execute(sa.text("UPDATE h_candidate SET digest_id=:digest WHERE user_id=:user AND source='starred' "
                             'AND ignored=false AND discovered_at>=:start AND discovered_at<:end AND digest_id IS NULL'), params)


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
