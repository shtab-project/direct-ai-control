"""Explicit approval state, scope and durable execution guard; writes disabled."""
from alembic import op
import sqlalchemy as sa

revision='0005'
down_revision='0004'

def upgrade():
    op.create_table('ai_control',sa.Column('id',sa.Integer,primary_key=True),sa.Column('writes_enabled',sa.Boolean,nullable=False))
    op.bulk_insert(sa.table('ai_control',sa.column('id',sa.Integer),sa.column('writes_enabled',sa.Boolean)),[{'id':1,'writes_enabled':False}])
    op.create_table('ai_scopes',sa.Column('campaign_id',sa.String(64),sa.ForeignKey('campaigns.id'),primary_key=True),sa.Column('enabled',sa.Boolean,nullable=False))
    op.create_table('ai_change_requests',
        sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),
        sa.Column('campaign_id',sa.String(64),sa.ForeignKey('campaigns.id'),nullable=False),sa.Column('version',sa.Integer,nullable=False),
        sa.Column('status',sa.String(30),nullable=False),sa.Column('payload',sa.JSON,nullable=False),sa.Column('diff_hash',sa.String(64),nullable=False),
        sa.Column('approved_by',sa.Integer,sa.ForeignKey('users.id')),sa.Column('session_hash',sa.String(64),nullable=False),
        sa.Column('approval_hash',sa.String(64),nullable=False),sa.Column('expires',sa.Float,nullable=False),sa.Column('attempted_at',sa.Float,nullable=False),
        sa.Column('result',sa.JSON,nullable=False),sa.Column('created_at',sa.String(40),nullable=False))
    op.create_table('ai_execution_guards',sa.Column('campaign_id',sa.String(64),sa.ForeignKey('campaigns.id'),primary_key=True),sa.Column('request_id',sa.String(64),sa.ForeignKey('ai_change_requests.id'),nullable=False))

def downgrade():
    op.drop_table('ai_execution_guards')
    op.drop_table('ai_change_requests')
    op.drop_table('ai_scopes')
    op.drop_table('ai_control')
