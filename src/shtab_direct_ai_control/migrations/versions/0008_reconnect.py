"""Explicit account-bound OAuth reconnect purpose."""
from alembic import op
import sqlalchemy as sa
revision='0008'
down_revision='0007'
def upgrade():
    op.add_column('oauth_states',sa.Column('reconnect_account_id',sa.String(64),nullable=True))
    op.add_column('oauth_states',sa.Column('reconnect_client_login',sa.String(200),nullable=True))
def downgrade():
    op.drop_column('oauth_states','reconnect_client_login')
    op.drop_column('oauth_states','reconnect_account_id')
