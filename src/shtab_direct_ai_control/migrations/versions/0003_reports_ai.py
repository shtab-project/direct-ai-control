"""Read-only reports datasets, persistent report polling and auditable AI analysis."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'

def upgrade():
    op.create_table('report_datasets', sa.Column('id', sa.String(64), primary_key=True), sa.Column('account_id', sa.String(64), sa.ForeignKey('ad_accounts.id'), nullable=False), sa.Column('kind', sa.String(40), nullable=False), sa.Column('date_from', sa.String(10), nullable=False), sa.Column('date_to', sa.String(10), nullable=False), sa.Column('rows', sa.JSON, nullable=False), sa.Column('meta', sa.JSON, nullable=False), sa.Column('updated_at', sa.String(40), nullable=False))
    op.create_table('report_jobs', sa.Column('id', sa.String(64), primary_key=True), sa.Column('connection_id', sa.String(64), sa.ForeignKey('yandex_connections.id'), nullable=False), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('kind', sa.String(40), nullable=False), sa.Column('payload', sa.JSON, nullable=False), sa.Column('rows', sa.JSON, nullable=False), sa.Column('status', sa.String(30), nullable=False), sa.Column('error', sa.String(300), nullable=False), sa.Column('next_poll', sa.Float, nullable=False), sa.Column('lease_until', sa.Float, nullable=False), sa.Column('attempts', sa.Integer, nullable=False), sa.Column('created_at', sa.String(40), nullable=False))
    op.create_table('analysis_runs', sa.Column('id', sa.String(64), primary_key=True), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False), sa.Column('model', sa.String(100), nullable=False), sa.Column('kind', sa.String(30), nullable=False), sa.Column('question', sa.Text, nullable=False), sa.Column('status', sa.String(30), nullable=False), sa.Column('source', sa.JSON, nullable=False), sa.Column('result', sa.JSON, nullable=False), sa.Column('usage', sa.JSON, nullable=False), sa.Column('error', sa.String(300), nullable=False), sa.Column('created_at', sa.String(40), nullable=False))
    op.create_table('analysis_leases', sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), primary_key=True), sa.Column('lease_until', sa.Float, nullable=False))

def downgrade():
    for table in ['analysis_leases', 'analysis_runs', 'report_jobs', 'report_datasets']:
        op.drop_table(table)
