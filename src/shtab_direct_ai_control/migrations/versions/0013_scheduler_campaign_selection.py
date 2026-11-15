"""Require explicit campaign selection before daily scheduling."""
from alembic import op
import sqlalchemy as sa
revision='0013'
down_revision='0012'
def upgrade():
    op.add_column('ai_daily_schedules',sa.Column('campaign_ids',sa.JSON,nullable=False,server_default=sa.text("'[]'")))
    op.execute(sa.text('UPDATE ai_daily_schedules SET enabled = false'))
def downgrade():
    op.drop_column('ai_daily_schedules','campaign_ids')
