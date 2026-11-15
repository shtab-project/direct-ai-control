"""Reviewable advice and durable read-only performance checkpoints."""
from alembic import op
import sqlalchemy as sa
revision='0010'
down_revision='0009'
def upgrade():
    op.create_table('ai_proposals',sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),sa.Column('campaign_id',sa.String(64),sa.ForeignKey('campaigns.id')),sa.Column('job_id',sa.String(64),sa.ForeignKey('assistant_jobs.id'),nullable=False),sa.Column('version',sa.Integer,nullable=False),sa.Column('status',sa.String(30),nullable=False),sa.Column('data',sa.JSON,nullable=False),sa.Column('created_at',sa.String(40),nullable=False))
    op.create_table('performance_checkpoints',sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('change_id',sa.String(64),sa.ForeignKey('ai_change_requests.id'),nullable=False),sa.Column('days',sa.Integer,nullable=False),sa.Column('due',sa.String(10),nullable=False),sa.Column('status',sa.String(30),nullable=False),sa.Column('data',sa.JSON,nullable=False),sa.Column('updated_at',sa.String(40),nullable=False))
def downgrade():
    op.drop_table('performance_checkpoints');op.drop_table('ai_proposals')
