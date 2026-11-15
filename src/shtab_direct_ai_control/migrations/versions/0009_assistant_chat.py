"""Long project context, durable conversations/messages and read-only jobs."""
from alembic import op
import sqlalchemy as sa
revision='0009'
down_revision='0008'
def upgrade():
    op.create_table('business_contexts',sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),primary_key=True),sa.Column('content',sa.Text,nullable=False),sa.Column('revision',sa.Integer,nullable=False),sa.Column('updated_at',sa.String(40),nullable=False))
    op.create_table('chat_conversations',sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),sa.Column('title',sa.String(160),nullable=False),sa.Column('selection',sa.JSON,nullable=False),sa.Column('created_at',sa.String(40),nullable=False))
    op.create_table('assistant_jobs',sa.Column('id',sa.String(64),primary_key=True),sa.Column('user_id',sa.Integer,sa.ForeignKey('users.id'),nullable=False),sa.Column('project_id',sa.String(64),sa.ForeignKey('projects.id'),nullable=False),sa.Column('kind',sa.String(30),nullable=False),sa.Column('status',sa.String(30),nullable=False),sa.Column('stage',sa.String(200),nullable=False),sa.Column('payload',sa.JSON,nullable=False),sa.Column('source',sa.JSON,nullable=False),sa.Column('result',sa.JSON,nullable=False),sa.Column('error',sa.String(500),nullable=False),sa.Column('lease_until',sa.Float,nullable=False),sa.Column('next_poll',sa.Float,nullable=False),sa.Column('created_at',sa.String(40),nullable=False),sa.Column('updated_at',sa.String(40),nullable=False))
    op.create_index('ix_assistant_job_queue','assistant_jobs',['status','next_poll','lease_until'])
    op.create_table('chat_messages',sa.Column('id',sa.String(64),primary_key=True),sa.Column('conversation_id',sa.String(64),sa.ForeignKey('chat_conversations.id'),nullable=False),sa.Column('role',sa.String(20),nullable=False),sa.Column('content',sa.Text,nullable=False),sa.Column('job_id',sa.String(64),sa.ForeignKey('assistant_jobs.id')),sa.Column('source',sa.JSON,nullable=False),sa.Column('created_at',sa.String(40),nullable=False))
    op.create_index('ix_chat_history','chat_messages',['conversation_id','created_at'])
def downgrade():
    op.drop_table('chat_messages');op.drop_table('assistant_jobs');op.drop_table('chat_conversations');op.drop_table('business_contexts')
