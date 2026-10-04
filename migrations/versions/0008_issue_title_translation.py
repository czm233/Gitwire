"""Cache faithful Chinese title translations separately from analysis summaries."""
from alembic import op
import sqlalchemy as sa

revision = '0008_issue_title_translation'
down_revision = '0007_event_observation'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('h_issue', sa.Column('title_zh', sa.String(), nullable=False, server_default=''))
    op.add_column('h_issue', sa.Column('title_zh_source', sa.String(), nullable=False, server_default=''))


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
