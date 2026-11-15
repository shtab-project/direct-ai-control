"""Per-owner account archive and single-use disconnect confirmations."""
from alembic import op
import sqlalchemy as sa
revision='0007'
down_revision='0006'

def upgrade():
    op.create_table('account_lifecycle',sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),primary_key=True),sa.Column('account_id',sa.String(64),sa.ForeignKey('ad_accounts.id'),primary_key=True),sa.Column('archived_at',sa.String(40),nullable=False))
    op.create_table('disconnect_confirmations',sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('connection_id',sa.String(64),sa.ForeignKey('yandex_connections.id'),nullable=False),sa.Column('session_hash',sa.String(64),nullable=False),sa.Column('impact_hash',sa.String(64),nullable=False),sa.Column('expires',sa.Float,nullable=False))

def downgrade():
    op.drop_table('disconnect_confirmations')
    op.drop_table('account_lifecycle')
