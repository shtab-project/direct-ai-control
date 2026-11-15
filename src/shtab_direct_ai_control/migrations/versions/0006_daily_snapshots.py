"""Persist daily Direct metrics independently of rolling report imports."""
from alembic import op
import sqlalchemy as sa
revision='0006'
down_revision='0005'

def upgrade():
    op.create_table('daily_metric_snapshots',sa.Column('id',sa.String(64),primary_key=True),sa.Column('account_id',sa.String(64),sa.ForeignKey('ad_accounts.id'),nullable=False),sa.Column('remote_campaign_id',sa.String(40),nullable=False),sa.Column('date',sa.String(10),nullable=False),sa.Column('data',sa.JSON,nullable=False),sa.Column('source',sa.JSON,nullable=False),sa.Column('updated_at',sa.String(40),nullable=False))
    op.create_index('ix_daily_metrics_account_date','daily_metric_snapshots',['account_id','date'])

def downgrade():
    op.drop_table('daily_metric_snapshots')
