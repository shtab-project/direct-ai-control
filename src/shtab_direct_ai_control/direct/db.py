import os
from shtab_core.config import paths, settings
from datetime import datetime, timezone
from sqlalchemy import create_engine, String, JSON, ForeignKey, Text, BigInteger, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

def now():
    return datetime.now(timezone.utc).isoformat()

class Base(DeclarativeBase):
    pass

database_url = make_url(os.getenv(
    'SHTAB_DIRECT_DATABASE_URL',
    f"sqlite:///{(paths.plugins / 'direct-ai-control' / 'direct-demo.db').as_posix()}",
))
core_url = make_url(settings.database_url)
if (
    database_url.get_backend_name(), database_url.host, database_url.port, database_url.database
) == (core_url.get_backend_name(), core_url.host, core_url.port, core_url.database):
    raise ValueError('Direct AI Control требует отдельную базу данных')
engine = create_engine(database_url)
Session = sessionmaker(engine, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(100), unique=True)
    password: Mapped[str] = mapped_column(String(300))
    permissions: Mapped[list] = mapped_column(JSON, default=list)

class LoginSession(Base):
    __tablename__ = 'login_sessions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    expires: Mapped[float]

class Project(Base):
    __tablename__ = 'projects'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(300))
    is_demo: Mapped[bool] = mapped_column(default=True)

class Account(Base):
    __tablename__ = 'ad_accounts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(default=True)

class ProjectAccount(Base):
    __tablename__ = 'project_accounts'
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('ad_accounts.id'), primary_key=True)

class Membership(Base):
    __tablename__ = 'project_memberships'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), primary_key=True)

class Campaign(Base):
    __tablename__ = 'campaigns'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'))
    account_id: Mapped[str] = mapped_column(ForeignKey('ad_accounts.id'))
    name: Mapped[str] = mapped_column(String(200))
    phrases: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    is_demo: Mapped[bool] = mapped_column(default=True)

class HiddenCampaign(Base):
    __tablename__='hidden_campaigns'
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),primary_key=True)
    campaign_id: Mapped[str]=mapped_column(ForeignKey('campaigns.id'),primary_key=True)
    created_at: Mapped[str]=mapped_column(String(40),default=now)

class Snapshot(Base):
    __tablename__ = 'negative_keyword_snapshots'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'))
    items: Mapped[list] = mapped_column(JSON)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class Draft(Base):
    __tablename__ = 'negative_keyword_drafts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    base_items: Mapped[list] = mapped_column(JSON)
    items: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(60), default='Черновик')
    revision: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)

class DraftItem(Base):
    __tablename__ = 'negative_keyword_draft_items'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(ForeignKey('negative_keyword_drafts.id'))
    text: Mapped[str] = mapped_column(String(1000))

class Operation(Base):
    __tablename__ = 'negative_keyword_operations'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class Recommendation(Base):
    __tablename__ = 'negative_keyword_recommendations'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'))
    data: Mapped[dict] = mapped_column(JSON)

class Audit(Base):
    __tablename__ = 'negative_keyword_audit'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class IntegrationConfig(Base):
    __tablename__ = 'integration_configs'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    encrypted_data: Mapped[str] = mapped_column(Text)

class OAuthState(Base):
    __tablename__ = 'oauth_states'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    session_id: Mapped[str] = mapped_column(String(64))
    encrypted_verifier: Mapped[str] = mapped_column(Text)
    expires: Mapped[float]
    reconnect_account_id: Mapped[str | None] = mapped_column(String(64),nullable=True)
    reconnect_client_login: Mapped[str | None] = mapped_column(String(200),nullable=True)

class YandexConnection(Base):
    __tablename__ = 'yandex_connections'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    encrypted_token: Mapped[str] = mapped_column(Text)
    encrypted_refresh: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires: Mapped[float | None] = mapped_column(nullable=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey('ad_accounts.id'), nullable=True)
    client_login: Mapped[str] = mapped_column(String(200), default='')
    status: Mapped[str] = mapped_column(String(60), default='oauth_connected')
    last_error: Mapped[str] = mapped_column(String(300), default='')
    updated_at: Mapped[str] = mapped_column(String(40), default=now)

class RemoteCampaignMapping(Base):
    __tablename__ = 'remote_campaign_mappings'
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'), primary_key=True)
    remote_id: Mapped[int] = mapped_column(BigInteger)
    campaign_type: Mapped[str] = mapped_column(String(80))

class ReportDataset(Base):
    __tablename__ = 'report_datasets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('ad_accounts.id'))
    kind: Mapped[str] = mapped_column(String(40))
    date_from: Mapped[str] = mapped_column(String(10))
    date_to: Mapped[str] = mapped_column(String(10))
    rows: Mapped[list] = mapped_column(JSON)
    meta: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)

class ReportJob(Base):
    __tablename__ = 'report_jobs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey('yandex_connections.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    rows: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    error: Mapped[str] = mapped_column(String(300), default='')
    next_poll: Mapped[float] = mapped_column(default=0)
    lease_until: Mapped[float] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class AnalysisRun(Base):
    __tablename__ = 'analysis_runs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'))
    model: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    question: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='running')
    source: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class AnalysisLease(Base):
    __tablename__ = 'analysis_leases'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    lease_until: Mapped[float] = mapped_column(default=0)

class DirectCampaignInfo(Base):
    __tablename__ = 'direct_campaign_info'
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'), primary_key=True)
    state: Mapped[str] = mapped_column(String(40), default='UNKNOWN')
    status: Mapped[str] = mapped_column(String(40), default='UNKNOWN')
    payment_status: Mapped[str] = mapped_column(String(40), default='UNKNOWN')
    negative_keywords_available: Mapped[bool] = mapped_column(default=False)
    phrases_read_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

class DirectSyncReceipt(Base):
    __tablename__ = 'direct_sync_receipts'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey('yandex_connections.id'))
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    evidence: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class AIControl(Base):
    __tablename__ = 'ai_control'
    id: Mapped[int] = mapped_column(primary_key=True)
    writes_enabled: Mapped[bool] = mapped_column(default=False)

class AIScope(Base):
    __tablename__ = 'ai_scopes'
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=False)

class ChangeRequest(Base):
    __tablename__ = 'ai_change_requests'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default='pending')
    payload: Mapped[dict] = mapped_column(JSON)
    diff_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    session_hash: Mapped[str] = mapped_column(String(64), default='')
    approval_hash: Mapped[str] = mapped_column(String(64), default='')
    expires: Mapped[float] = mapped_column(default=0)
    attempted_at: Mapped[float] = mapped_column(default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)

class ExecutionGuard(Base):
    __tablename__ = 'ai_execution_guards'
    campaign_id: Mapped[str] = mapped_column(ForeignKey('campaigns.id'), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey('ai_change_requests.id'))

class DailyMetricSnapshot(Base):
    __tablename__ = 'daily_metric_snapshots'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('ad_accounts.id'))
    remote_campaign_id: Mapped[str] = mapped_column(String(40))
    date: Mapped[str] = mapped_column(String(10))
    data: Mapped[dict] = mapped_column(JSON)
    source: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String(40),default=now)

class AccountLifecycle(Base):
    __tablename__ = 'account_lifecycle'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'),primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('ad_accounts.id'),primary_key=True)
    archived_at: Mapped[str] = mapped_column(String(40),default=now)

class DisconnectConfirmation(Base):
    __tablename__ = 'disconnect_confirmations'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    connection_id: Mapped[str] = mapped_column(ForeignKey('yandex_connections.id'))
    session_hash: Mapped[str] = mapped_column(String(64))
    impact_hash: Mapped[str] = mapped_column(String(64))
    expires: Mapped[float]

class BusinessContext(Base):
    __tablename__ = 'business_contexts'
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'),primary_key=True)
    content: Mapped[str] = mapped_column(Text,default='')
    revision: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[str] = mapped_column(String(40),default=now)

class ChatConversation(Base):
    __tablename__ = 'chat_conversations'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'))
    title: Mapped[str] = mapped_column(String(160))
    selection: Mapped[dict] = mapped_column(JSON,default=dict)
    created_at: Mapped[str] = mapped_column(String(40),default=now)

class AssistantJob(Base):
    __tablename__ = 'assistant_jobs'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30),default='pending')
    stage: Mapped[str] = mapped_column(String(200),default='В очереди')
    payload: Mapped[dict] = mapped_column(JSON)
    source: Mapped[dict] = mapped_column(JSON,default=dict)
    result: Mapped[dict] = mapped_column(JSON,default=dict)
    error: Mapped[str] = mapped_column(String(500),default='')
    lease_until: Mapped[float] = mapped_column(default=0)
    next_poll: Mapped[float] = mapped_column(default=0)
    created_at: Mapped[str] = mapped_column(String(40),default=now)
    updated_at: Mapped[str] = mapped_column(String(40),default=now)

class ChatMessage(Base):
    __tablename__ = 'chat_messages'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey('chat_conversations.id'))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(ForeignKey('assistant_jobs.id'),nullable=True)
    source: Mapped[dict] = mapped_column(JSON,default=dict)
    created_at: Mapped[str] = mapped_column(String(40),default=now)

class AIProposal(Base):
    __tablename__='ai_proposals'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'))
    campaign_id: Mapped[str|None]=mapped_column(ForeignKey('campaigns.id'),nullable=True)
    job_id: Mapped[str]=mapped_column(ForeignKey('assistant_jobs.id'))
    version: Mapped[int]=mapped_column(default=1)
    status: Mapped[str]=mapped_column(String(30),default='pending')
    data: Mapped[dict]=mapped_column(JSON)
    created_at: Mapped[str]=mapped_column(String(40),default=now)

class DailySchedule(Base):
    __tablename__='ai_daily_schedules'
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),primary_key=True)
    project_id: Mapped[str]=mapped_column(ForeignKey('projects.id'),primary_key=True)
    enabled: Mapped[bool]=mapped_column(default=False)
    campaign_ids: Mapped[list]=mapped_column(JSON,default=list)
    last_day: Mapped[str]=mapped_column(String(10),default='')
    last_error: Mapped[str]=mapped_column(String(300),default='')
    updated_at: Mapped[str]=mapped_column(String(40),default=now)

class PerformanceCheckpoint(Base):
    __tablename__='performance_checkpoints'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    change_id: Mapped[str]=mapped_column(ForeignKey('ai_change_requests.id'))
    days: Mapped[int]
    due: Mapped[str]=mapped_column(String(10))
    status: Mapped[str]=mapped_column(String(30),default='pending')
    data: Mapped[dict]=mapped_column(JSON,default=dict)
    updated_at: Mapped[str]=mapped_column(String(40),default=now)

class TelegramSettings(Base):
    __tablename__='telegram_settings'
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'),primary_key=True)
    encrypted: Mapped[str]=mapped_column(Text)
    enabled: Mapped[bool]=mapped_column(default=False)
    bound_at: Mapped[str]=mapped_column(String(40),default='')
    created_at: Mapped[str]=mapped_column(String(40),default=now)

class NotificationOutbox(Base):
    __tablename__='notification_outbox'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id: Mapped[int]=mapped_column(ForeignKey('users.id'))
    status: Mapped[str]=mapped_column(String(30),default='pending')
    data: Mapped[dict]=mapped_column(JSON)
    error: Mapped[str]=mapped_column(String(300),default='')
    created_at: Mapped[str]=mapped_column(String(40),default=now)
