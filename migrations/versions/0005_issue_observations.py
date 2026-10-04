"""Public issue observation baselines and expiring comment claims."""
from alembic import op
import sqlalchemy as sa

revision = '0005_issue_observations'
down_revision = '0004_opportunity_rules'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('watch_snapshot', 'claim'):
        op.add_column('h_issue', sa.Column(name, sa.JSON(), nullable=False, server_default='{}'))
        op.alter_column('h_issue', name, server_default=None)
    op.add_column('h_issue', sa.Column('comments_cursor', sa.String(), nullable=False, server_default=''))
    op.alter_column('h_issue', 'comments_cursor', server_default=None)
    # New event type: existing users could not have opted out of it yet.
    accounts = sa.table('h_account', sa.column('id', sa.String), sa.column('notify_kinds', sa.JSON))
    bind = op.get_bind()
    for row in bind.execute(sa.select(accounts.c.id, accounts.c.notify_kinds)).mappings():
        if 'watch' not in row['notify_kinds']:
            bind.execute(accounts.update().where(accounts.c.id == row['id']).values(notify_kinds=[*row['notify_kinds'], 'watch']))


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
