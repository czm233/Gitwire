"""Durable sync checkpoints and corpus/queue lookup indexes."""
from alembic import op
import sqlalchemy as sa

revision = '0002_sync_checkpoints'
down_revision = '0001_hosted'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('h_repository', sa.Column('issues_cursor', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('h_candidate', sa.Column('seen_generation', sa.String(), nullable=False, server_default=''))
    op.create_index('ix_h_issue_repo_created', 'h_issue', ['repo_id', 'created_at'])
    op.create_index('ix_h_job_status_available', 'h_job', ['status', 'available_at'])


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
