"""Encrypted private Telegram binding and at-most-once notification outbox."""
from alembic import op
import sqlalchemy as sa
revision='0011'
down_revision='0010'
def upgrade():
    op.create_table('telegram_settings',sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),primary_key=True),sa.Column('encrypted',sa.Text,nullable=False),sa.Column('enabled',sa.Boolean,nullable=False),sa.Column('bound_at',sa.String(40),nullable=False),sa.Column('created_at',sa.String(40),nullable=False))
    op.create_table('notification_outbox',sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('status',sa.String(30),nullable=False),sa.Column('data',sa.JSON,nullable=False),sa.Column('error',sa.String(300),nullable=False),sa.Column('created_at',sa.String(40),nullable=False))
    op.create_index('ix_notification_pending','notification_outbox',['status','created_at'])
def downgrade():
    op.drop_table('notification_outbox');op.drop_table('telegram_settings')
