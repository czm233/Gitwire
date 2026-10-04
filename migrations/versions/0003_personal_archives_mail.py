"""Private legacy archives and authenticated mail feedback receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0003_personal_archives_mail'
down_revision = '0002_sync_checkpoints'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('h_mail_feedback',
        sa.Column('event_id', sa.String(), primary_key=True),
        sa.Column('delivery_id', sa.String(), sa.ForeignKey('h_delivery.id'), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.Integer(), nullable=False))
    op.create_index('ix_h_mail_feedback_delivery_id', 'h_mail_feedback', ['delivery_id'])
    op.create_table('h_legacy_record',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('user_id', sa.String(), sa.ForeignKey('h_account.id'), nullable=False),
        sa.Column('source_key', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('repo', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('created_at', sa.Integer(), nullable=False),
        sa.UniqueConstraint('user_id', 'source_key'))
    op.create_index('ix_h_legacy_record_user_id', 'h_legacy_record', ['user_id'])
    op.create_index('ix_h_legacy_owner_kind_created', 'h_legacy_record', ['user_id', 'kind', 'created_at'])


def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
