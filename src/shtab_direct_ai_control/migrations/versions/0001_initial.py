"""Initial stage-1 schema; frozen table names and types."""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None

def upgrade():
    # Explicit schema independent from future ORM changes.
    op.create_table('users', sa.Column('id', sa.Integer, primary_key=True), sa.Column('login', sa.String(100), nullable=False, unique=True), sa.Column('password', sa.String(300), nullable=False), sa.Column('permissions', sa.JSON, nullable=False))
    op.create_table('login_sessions', sa.Column('id', sa.String(64), primary_key=True), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('expires', sa.Float, nullable=False))
    op.create_table('projects', sa.Column('id', sa.String(64), primary_key=True), sa.Column('name', sa.String(200), nullable=False), sa.Column('description', sa.String(300), nullable=False), sa.Column('is_demo', sa.Boolean, nullable=False))
    op.create_table('ad_accounts', sa.Column('id', sa.String(64), primary_key=True), sa.Column('name', sa.String(200), nullable=False), sa.Column('is_demo', sa.Boolean, nullable=False))
    op.create_table('project_accounts', sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), primary_key=True), sa.Column('account_id', sa.String(64), sa.ForeignKey('ad_accounts.id'), primary_key=True))
    op.create_table('project_memberships', sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), primary_key=True), sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), primary_key=True))
    op.create_table('campaigns', sa.Column('id', sa.String(64), primary_key=True), sa.Column('project_id', sa.String(64), sa.ForeignKey('projects.id'), nullable=False), sa.Column('account_id', sa.String(64), sa.ForeignKey('ad_accounts.id'), nullable=False), sa.Column('name', sa.String(200), nullable=False), sa.Column('phrases', sa.JSON, nullable=False), sa.Column('updated_at', sa.String(40), nullable=False), sa.Column('is_demo', sa.Boolean, nullable=False))
    op.create_table('negative_keyword_snapshots', sa.Column('id', sa.String(64), primary_key=True), sa.Column('campaign_id', sa.String(64), sa.ForeignKey('campaigns.id'), nullable=False), sa.Column('items', sa.JSON, nullable=False), sa.Column('checksum', sa.String(64), nullable=False), sa.Column('created_at', sa.String(40), nullable=False))
    op.create_table('negative_keyword_drafts', sa.Column('id', sa.String(64), primary_key=True), sa.Column('campaign_id', sa.String(64), sa.ForeignKey('campaigns.id'), nullable=False), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('base_items', sa.JSON, nullable=False), sa.Column('items', sa.JSON, nullable=False), sa.Column('status', sa.String(60), nullable=False), sa.Column('revision', sa.Integer, nullable=False), sa.Column('updated_at', sa.String(40), nullable=False))
    op.create_table('negative_keyword_draft_items', sa.Column('id', sa.String(64), primary_key=True), sa.Column('draft_id', sa.String(64), sa.ForeignKey('negative_keyword_drafts.id'), nullable=False), sa.Column('text', sa.String(1000), nullable=False))
    for name, extra in [('negative_keyword_operations', [sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('kind', sa.String(100), nullable=False), sa.Column('created_at', sa.String(40), nullable=False)]), ('negative_keyword_recommendations', [])]:
        op.create_table(name, sa.Column('id', sa.String(64), primary_key=True), sa.Column('campaign_id', sa.String(64), sa.ForeignKey('campaigns.id'), nullable=False), sa.Column('data', sa.JSON, nullable=False), *extra)
    op.create_table('negative_keyword_audit', sa.Column('id', sa.String(64), primary_key=True), sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False), sa.Column('action', sa.String(100), nullable=False), sa.Column('data', sa.JSON, nullable=False), sa.Column('created_at', sa.String(40), nullable=False))

def downgrade():
    for table in ['negative_keyword_audit', 'negative_keyword_recommendations', 'negative_keyword_operations', 'negative_keyword_draft_items', 'negative_keyword_drafts', 'negative_keyword_snapshots', 'campaigns', 'project_memberships', 'project_accounts', 'ad_accounts', 'projects', 'login_sessions', 'users']:
        op.drop_table(table)
