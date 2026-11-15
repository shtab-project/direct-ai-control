"""Per-project daily read-only AI audits."""
from alembic import op
import sqlalchemy as sa
revision='0012'
down_revision='0011'
def upgrade():
    op.create_table('ai_daily_schedules',sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),primary_key=True),sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),primary_key=True),sa.Column('enabled',sa.Boolean,nullable=False),sa.Column('last_day',sa.String(10),nullable=False),sa.Column('last_error',sa.String(300),nullable=False),sa.Column('updated_at',sa.String(40),nullable=False))
def downgrade():
    op.drop_table('ai_daily_schedules')
