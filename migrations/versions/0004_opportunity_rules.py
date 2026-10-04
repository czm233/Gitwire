"""Preserve personal opportunity thresholds and shared repository evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0004_opportunity_rules'
down_revision = '0003_personal_archives_mail'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('pushed_at', 'external_merge_at', 'signals_checked_at', 'signals_coverage_since'):
        op.add_column('h_repository', sa.Column(name, sa.Integer(), nullable=False, server_default='0'))
        op.alter_column('h_repository', name, server_default=None)
    op.add_column('h_subscription', sa.Column('opportunity', sa.JSON(), nullable=False,
        server_default='{"max_age_days":30,"max_comments":15,"repo_pushed_within_days":30,"external_merge_within_days":90}'))
    op.alter_column('h_subscription', 'opportunity', server_default=None)


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
