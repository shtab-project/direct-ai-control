"""Real campaign statuses and read receipts; no campaign write functionality."""
from alembic import op
import sqlalchemy as sa

revision='0004'
down_revision='0003'

def upgrade():
    op.create_table('direct_campaign_info',sa.Column('campaign_id',sa.String(64),sa.ForeignKey('campaigns.id'),primary_key=True),sa.Column('state',sa.String(40),nullable=False),sa.Column('status',sa.String(40),nullable=False),sa.Column('payment_status',sa.String(40),nullable=False),sa.Column('negative_keywords_available',sa.Boolean,nullable=False),sa.Column('phrases_read_at',sa.String(40)))
    op.create_table('direct_sync_receipts',sa.Column('id',sa.String(64),primary_key=True),sa.Column('connection_id',sa.String(64),sa.ForeignKey('yandex_connections.id'),nullable=False),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('evidence',sa.JSON,nullable=False),sa.Column('created_at',sa.String(40),nullable=False))

def downgrade():
    op.drop_table('direct_sync_receipts')
    op.drop_table('direct_campaign_info')
