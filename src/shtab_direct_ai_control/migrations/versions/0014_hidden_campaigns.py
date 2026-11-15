"""Personal reversible campaign visibility."""
from alembic import op
import sqlalchemy as sa
revision='0014'
down_revision='0013'
def upgrade():
    op.create_table('hidden_campaigns',sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),primary_key=True),sa.Column('campaign_id',sa.String(64),sa.ForeignKey('campaigns.id'),primary_key=True),sa.Column('created_at',sa.String(40),nullable=False))
def downgrade():
    op.drop_table('hidden_campaigns')
