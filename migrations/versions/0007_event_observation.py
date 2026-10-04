"""Preserve public pre-change evidence for personalized change delivery."""
from alembic import op
import sqlalchemy as sa

revision = '0007_event_observation'
down_revision = '0006_digest_checkpoints'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('h_event', sa.Column('observation', sa.JSON(), nullable=False, server_default='{}'))
    op.alter_column('h_event', 'observation', server_default=None)


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
