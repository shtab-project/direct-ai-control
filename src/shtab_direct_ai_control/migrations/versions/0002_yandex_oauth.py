"""Encrypted OAuth state and read-only Yandex connections."""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'

def upgrade():
    op.create_table('integration_configs', sa.Column('id', sa.String(40), primary_key=True), sa.Column('encrypted_data', sa.Text, nullable=False))
    op.create_table('oauth_states', sa.Column('id', sa.String(64), primary_key=True), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('session_id', sa.String(64), nullable=False), sa.Column('encrypted_verifier', sa.Text, nullable=False), sa.Column('expires', sa.Float, nullable=False))
    op.create_table('yandex_connections', sa.Column('id', sa.String(64), primary_key=True), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('encrypted_token', sa.Text, nullable=False), sa.Column('encrypted_refresh', sa.Text), sa.Column('expires', sa.Float), sa.Column('account_id', sa.String(64), sa.ForeignKey('ad_accounts.id')), sa.Column('client_login', sa.String(200), nullable=False), sa.Column('status', sa.String(60), nullable=False), sa.Column('last_error', sa.String(300), nullable=False), sa.Column('updated_at', sa.String(40), nullable=False))
    op.create_table('remote_campaign_mappings', sa.Column('campaign_id', sa.String(64), sa.ForeignKey('campaigns.id'), primary_key=True), sa.Column('remote_id', sa.BigInteger, nullable=False), sa.Column('campaign_type', sa.String(80), nullable=False))

def downgrade():
    for table in ['remote_campaign_mappings', 'yandex_connections', 'oauth_states', 'integration_configs']:
        op.drop_table(table)
