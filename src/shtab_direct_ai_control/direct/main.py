import csv
import hashlib
import hmac
import io
import os
import secrets
import time
from collections import defaultdict, deque
from threading import Lock
from datetime import date, timedelta
from uuid import uuid4

from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from .db import Base, engine, Session, User, LoginSession, Project, Account, ProjectAccount, Membership, Campaign, Draft, DraftItem, Operation, Snapshot, Audit, Recommendation, now
from .domain import INITIAL, TEST_PHRASES, checksum, diff, validate, metrics
from ..paths import MOUNT_PATH
from .session import cookie_name, session_hash
from shtab_core.config import settings as shtab_settings
from .account_lifecycle import archived,archived_ids,ensure_active_account

def password_hash(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    return salt + ':' + hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()

def seed():
    with Session.begin() as db:
        user = db.scalar(select(User).where(User.login == os.getenv('SHTAB_DIRECT_ADMIN_LOGIN', 'admin')))
        if not user:
            user = User(login=os.getenv('SHTAB_DIRECT_ADMIN_LOGIN', 'admin'), password=password_hash(os.getenv('SHTAB_DIRECT_ADMIN_PASSWORD', 'demo-admin-2026')), permissions=['project:read', 'draft:write', 'campaign:write', 'integration:manage'])
            db.add(user)
            db.flush()
        if 'integration:manage' not in user.permissions:
            user.permissions = [*user.permissions, 'integration:manage']
        if 'campaign:approve' not in user.permissions:
            user.permissions = [*user.permissions, 'campaign:approve']
        if os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') == 'connected':
            return
        projects = [('service', 'Сервисный центр', 'Ремонт сварочного оборудования и шкафы управления'), ('cnc', 'ПСМ / HITIC CNC', 'Станки ЧПУ и фрезерные центры'), ('hydro', 'Темир Гидро', 'Подъёмники и качество заявок'), ('fitness', 'Т-Фит', 'Фитнес-центр и абонементы')]
        for key, name, description in projects:
            pid, aid, cid = f'demo-{key}', f'demo-account-{key}', f'demo-campaign-{key}'
            if not db.get(Project, pid):
                db.add(Project(id=pid, name=name, description=description))
                db.add(Account(id=aid, name=f'{name} · DEMO'))
                db.flush()
                db.add(ProjectAccount(project_id=pid, account_id=aid))
                db.add(Campaign(id=cid, project_id=pid, account_id=aid, name=f'{name} / Поиск', phrases=INITIAL.copy()))
                db.flush()
                for i, phrase in enumerate(TEST_PHRASES):
                    db.add(Recommendation(id=f'{cid}-{i}', campaign_id=cid, data={'phrase': phrase, 'category': 'Информационный', 'cost': 1260 + 480*i, 'clicks': 18 + i*7, 'conversions': 0, 'confidence': 91-i*4, 'risk': 'Проверить контекст запроса', 'reason': 'Демонстрационный пример: запрос может не соответствовать заказу платного ремонта. Рекомендация требует проверки.'}))
            if not db.get(Membership, (user.id, pid)):
                db.add(Membership(user_id=user.id, project_id=pid))

_startup_lock = Lock()
_initialized = False


def initialize():
    global _initialized
    with _startup_lock:
        if not _initialized:
            initialize_database()
            _initialized = True


def initialize_database():
    if os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') not in ['demo', 'connected']:
        raise RuntimeError('APP_MODE: demo или connected')
    if os.getenv('SHTAB_DIRECT_APP_MODE') == 'connected':
        from .yandex import cipher, callback_url
        cipher()
        callback_url()
    if (shtab_settings.secure_cookies or os.getenv('SHTAB_DIRECT_APP_MODE') == 'connected') and os.getenv('SHTAB_DIRECT_ADMIN_PASSWORD', '') in ['', 'demo-admin-2026', 'change-this-before-deployment']:
        raise RuntimeError('Для HTTPS задайте уникальный ADMIN_PASSWORD до первого запуска.')
    # PostgreSQL schema is managed by Alembic; SQLite is the local quick-start mode.
    if engine.dialect.name == 'sqlite':
        Base.metadata.create_all(engine)
    seed()

app = FastAPI(title='Direct AI Control', version='0.1.0', docs_url='/api/docs', openapi_url='/api/openapi.json', redoc_url=None)
attempts = defaultdict(deque)

@app.exception_handler(RequestValidationError)
async def safe_validation_error(request,exc):
    # Validation errors must never echo submitted OAuth secrets, passwords or tokens.
    return JSONResponse(status_code=422,content={'detail':[{'loc':list(e['loc']),'type':e['type'],'msg':e['msg']} for e in exc.errors()]})

@app.middleware('http')
async def security(request: Request, call_next):
    if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
        if request.headers.get('x-requested-with') != 'DirectAI':
            return Response('Требуется X-Requested-With: DirectAI', status_code=403)
        origin = request.headers.get('origin')
        request_origin = f'{request.url.scheme}://{request.url.netloc}'
        if origin and origin not in {request_origin, shtab_settings.public_url.rstrip('/')}:
            return Response('Недопустимый Origin', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Frame-Options'] = 'DENY'
    return response

def db_session():
    with Session() as db:
        yield db

def current_user(request: Request, db=Depends(db_session)):
    session = db.get(LoginSession, session_hash(request))
    if not session or session.expires < time.time():
        raise HTTPException(401, 'Войдите в приложение')
    return db.get(User, session.user_id)

def permitted(user, permission):
    if permission not in user.permissions:
        raise HTTPException(403, 'Недостаточно прав')

def campaign_access(db, user, cid):
    campaign = db.get(Campaign, cid)
    if not campaign or not db.get(Membership, (user.id, campaign.project_id)):
        raise HTTPException(404, 'Кампания недоступна')
    return campaign

def get_draft(db, user, did):
    draft = db.get(Draft, did)
    if not draft or draft.user_id != user.id:
        raise HTTPException(404, 'Черновик недоступен')
    campaign_access(db, user, draft.campaign_id)
    return draft

def serialize_draft(draft):
    return {k: getattr(draft, k) for k in ['id', 'campaign_id', 'items', 'base_items', 'status', 'revision', 'updated_at']}

class Credentials(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=300)

@app.post('/api/auth/login')
def login(body: Credentials, request: Request, response: Response, db=Depends(db_session)):
    key = request.client.host
    bucket = attempts[key]
    while bucket and bucket[0] < time.time() - 60:
        bucket.popleft()
    if len(bucket) >= 10:
        raise HTTPException(429, 'Слишком много попыток. Подождите минуту.')
    bucket.append(time.time())
    user = db.scalar(select(User).where(User.login == body.login))
    if not user or not hmac.compare_digest(password_hash(body.password, user.password.split(':')[0]), user.password):
        raise HTTPException(401, 'Неверный логин или пароль')
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(id=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, expires=time.time()+28800))
    db.commit()
    response.set_cookie(cookie_name(request), token, httponly=True, secure=shtab_settings.secure_cookies, samesite='lax', max_age=28800, path=MOUNT_PATH)
    return {'login': user.login, 'mode': os.getenv('SHTAB_DIRECT_APP_MODE', 'demo')}

@app.get('/api/auth/me')
def me(user=Depends(current_user)):
    return {'login': user.login, 'permissions': user.permissions, 'mode': os.getenv('SHTAB_DIRECT_APP_MODE', 'demo')}

@app.post('/api/auth/logout')
def logout(request: Request, response: Response, db=Depends(db_session)):
    db.execute(delete(LoginSession).where(LoginSession.id == session_hash(request)))
    db.commit()
    response.delete_cookie(cookie_name(request), path=MOUNT_PATH)
    return {'ok': True}

@app.get('/api/health')
def health(db=Depends(db_session)):
    db.execute(select(1))
    return {'api': 'ok', 'database': 'ok', 'mode': os.getenv('SHTAB_DIRECT_APP_MODE', 'demo'), 'integrations': 'not_connected', 'worker': 'reserved_for_stage_2'}

@app.get('/api/projects')
def projects(user=Depends(current_user), db=Depends(db_session)):
    return [{'id': p.id, 'name': p.name, 'description': p.description, 'is_demo': p.is_demo, 'archived':bool(list(db.scalars(select(ProjectAccount.account_id).where(ProjectAccount.project_id==p.id)))) and all(archived(db,user.id,a) for a in db.scalars(select(ProjectAccount.account_id).where(ProjectAccount.project_id==p.id))), 'archived_account_ids':[a for a in db.scalars(select(ProjectAccount.account_id).where(ProjectAccount.project_id==p.id)) if archived(db,user.id,a)]} for p in db.scalars(select(Project).join(Membership).where(Membership.user_id == user.id, Project.is_demo == (os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') == 'demo')))]

@app.get('/api/accounts')
def accounts(user=Depends(current_user), db=Depends(db_session)):
    return [{'id': a.id, 'name': a.name, 'archived':archived(db,user.id,a.id)} for a in db.scalars(select(Account).join(ProjectAccount).join(Membership, Membership.project_id == ProjectAccount.project_id).where(Membership.user_id == user.id))]

@app.get('/api/negative-keywords/campaigns')
def campaigns(project_id: str | None = None, account_id: str | None = None, include_hidden: bool = False, user=Depends(current_user), db=Depends(db_session)):
    query = select(Campaign).join(Membership, Membership.project_id == Campaign.project_id).where(Membership.user_id == user.id, Campaign.is_demo == (os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') == 'demo'))
    if project_id:
        query = query.where(Campaign.project_id == project_id)
    if account_id:
        query = query.where(Campaign.account_id == account_id)
    from .yandex import campaign_view
    from .campaign_visibility import hidden_ids
    hidden=hidden_ids(db,user.id)
    return [{**campaign_view(db,c,user.id),'hidden':c.id in hidden} for c in db.scalars(query) if include_hidden or c.id not in hidden]

def statistics(campaigns, days):
    rows = []
    for c in campaigns:
        seed_number = sum(map(ord, c.id))
        for i in range(days*2):
            factor = 1 + ((i*17 + seed_number) % 23)/25
            rows.append({'date': (date.today()-timedelta(days=days*2-i-1)).isoformat(), 'campaign_id': c.id, 'campaign': c.name, 'cost': round(1730*factor, 2), 'impressions': int(11400*factor), 'clicks': int(97*factor), 'conversions': int(3*factor)+(i%3)})
    boundary = (date.today()-timedelta(days=days-1)).isoformat()
    return [r for r in rows if r['date'] >= boundary], [r for r in rows if r['date'] < boundary]

@app.get('/api/dashboard')
def dashboard(days: int = 30, project_id: str | None = None, campaign_id: str | None = None, user=Depends(current_user), db=Depends(db_session)):
    if days not in [7, 30, 90]:
        raise HTTPException(422, 'Допустимый период: 7, 30 или 90 дней')
    query = select(Campaign).join(Membership, Membership.project_id == Campaign.project_id).where(Membership.user_id == user.id, Campaign.is_demo == (os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') == 'demo'))
    if project_id:
        query = query.where(Campaign.project_id == project_id)
    if campaign_id:
        query = query.where(Campaign.id == campaign_id)
    from .campaign_visibility import hidden_ids
    hidden=hidden_ids(db,user.id)
    cs = [c for c in db.scalars(query) if c.id not in hidden]
    if os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') == 'connected':
        from .reports import real_dashboard
        data=real_dashboard(db, cs, days)
        data['archived_account_ids']=sorted({c.account_id for c in cs if archived(db,user.id,c.account_id)})
        data['archived']=bool(data['archived_account_ids'])
        return data
    rows, previous = statistics(cs, days)
    dates = sorted(set(r['date'] for r in rows))
    return {'mode': os.getenv('SHTAB_DIRECT_APP_MODE', 'demo'), 'source': 'Синтетические данные DEMO · Директ не подключён', 'attribution': 'Учебная модель, конверсии только условного Директа', 'updated_at': now(), 'metrics': metrics(rows), 'previous': metrics(previous), 'daily': [{'date': d, **metrics([r for r in rows if r['date'] == d])} for d in dates], 'campaigns': [{'id': c.id, 'name': c.name, **metrics([r for r in rows if r['campaign_id'] == c.id])} for c in cs]}

class DraftBody(BaseModel):
    campaign_id: str
    items: list[str] = Field(max_length=1000)
    draft_id: str | None = None
    revision: int | None = None

def save_draft(db, user, campaign, items, draft=None):
    permitted(user, 'draft:write')
    if not campaign.is_demo:ensure_active_account(db,user.id,campaign.account_id)
    if not draft:
        draft = Draft(id=str(uuid4()), campaign_id=campaign.id, user_id=user.id, base_items=list(campaign.phrases), items=items)
        db.add(draft)
        db.add(Snapshot(id=str(uuid4()), campaign_id=campaign.id, items=list(campaign.phrases), checksum=checksum(campaign.phrases)))
        db.flush()
    else:
        draft.items, draft.status, draft.updated_at = items, 'Черновик', now()
        draft.revision += 1
        db.execute(delete(DraftItem).where(DraftItem.draft_id == draft.id))
    for item in items:
        db.add(DraftItem(id=str(uuid4()), draft_id=draft.id, text=item))
    db.add(Audit(id=str(uuid4()), user_id=user.id, action='draft.saved', data={'draft_id': draft.id, 'campaign_id': campaign.id, 'demo': campaign.is_demo}))
    db.commit()
    return serialize_draft(draft)

@app.post('/api/negative-keywords/drafts')
def drafts(body: DraftBody, user=Depends(current_user), db=Depends(db_session)):
    c = campaign_access(db, user, body.campaign_id)
    draft = get_draft(db, user, body.draft_id) if body.draft_id else None
    if draft and (draft.campaign_id != c.id or draft.revision != body.revision):
        raise HTTPException(409, 'Черновик изменён в другой вкладке. Перезагрузите список.')
    return save_draft(db, user, c, [p.strip() for p in body.items], draft)

class DraftRef(BaseModel):
    draft_id: str

@app.post('/api/negative-keywords/validate')
def validate_draft(body: DraftRef, user=Depends(current_user), db=Depends(db_session)):
    d = get_draft(db, user, body.draft_id)
    result = validate(d.items)
    d.status = 'Проверено' if result['valid'] else 'Ошибка'
    db.commit()
    return {**result, 'status': d.status}

@app.post('/api/negative-keywords/preview')
def preview(body: DraftRef, user=Depends(current_user), db=Depends(db_session)):
    d = get_draft(db, user, body.draft_id)
    c = campaign_access(db, user, d.campaign_id)
    if checksum(c.phrases) != checksum(d.base_items):
        raise HTTPException(409, 'Исходный список изменился. Создайте новый черновик.')
    result = validate(d.items)
    if not result['valid']:
        raise HTTPException(422, result['errors'])
    d.status = 'Проверено'
    change = diff(d.base_items, d.items)
    db.add(Operation(id=str(uuid4()), campaign_id=c.id, user_id=user.id, kind='DEMO · Предпросмотр' if c.is_demo else 'Локальный предпросмотр', data={'before': d.base_items, 'after': d.items, 'diff': change, 'draft_id': d.id}))
    db.commit()
    return {**change, **result, 'campaign': c.name, 'campaign_id': c.id, 'account': db.get(Account, c.account_id).name, 'demo': c.is_demo, 'can_apply': False, 'confirmation_token': None}

@app.post('/api/negative-keywords/apply')
def apply(user=Depends(current_user)):
    permitted(user, 'campaign:write')
    raise HTTPException(403, 'Отправка в Яндекс Директ отключена. Подключение работает только на чтение; запись требует этапа 3.')

@app.post('/api/negative-keywords/cancel')
def cancel(body: DraftRef, user=Depends(current_user), db=Depends(db_session)):
    d = get_draft(db, user, body.draft_id)
    permitted(user, 'draft:write')
    d.status = 'Отменён'
    db.add(Audit(id=str(uuid4()), user_id=user.id, action='draft.cancelled', data={'draft_id': d.id}))
    db.commit()
    return {'ok': True}

@app.get('/api/negative-keywords/history')
def history(campaign_id: str, user=Depends(current_user), db=Depends(db_session)):
    campaign_access(db, user, campaign_id)
    return [{'archived':archived(db,user.id,o.campaign_id and db.get(Campaign,o.campaign_id).account_id),'id': o.id, 'kind': o.kind, 'data': o.data, 'created_at': o.created_at} for o in db.scalars(select(Operation).where(Operation.campaign_id == campaign_id, Operation.user_id == user.id).order_by(Operation.created_at.desc()).limit(100))]

class RollbackBody(BaseModel):
    operation_id: str

@app.post('/api/negative-keywords/rollback-preview')
def rollback(body: RollbackBody, user=Depends(current_user), db=Depends(db_session)):
    o = db.get(Operation, body.operation_id)
    if not o or o.user_id != user.id:
        raise HTTPException(404, 'Операция недоступна')
    c = campaign_access(db, user, o.campaign_id)
    return save_draft(db, user, c, list(o.data['before']))

@app.post('/api/negative-keywords/import')
async def import_file(file: UploadFile, user=Depends(current_user)):
    permitted(user, 'draft:write')
    content = await file.read(2_000_001)
    if len(content) > 2_000_000:
        raise HTTPException(413, 'Максимальный размер файла: 2 МБ')
    name = (file.filename or '').lower()
    try:
        if name.endswith('.xlsx'):
            from openpyxl import load_workbook
            from zipfile import ZipFile
            with ZipFile(io.BytesIO(content)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 20_000_000:
                    raise ValueError('Слишком большой XLSX после распаковки')
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            items = [str(row[0]).strip() for row in wb.active.iter_rows(values_only=True) if row and row[0] is not None]
            wb.close()
        elif name.endswith(('.csv', '.txt')):
            text = content.decode('utf-8-sig')
            items = [row[0].strip() for row in csv.reader(io.StringIO(text)) if row and row[0].strip()] if name.endswith('.csv') else [x.strip() for x in text.splitlines() if x.strip()]
        else:
            raise ValueError('Поддерживаются TXT, CSV, XLSX')
        if len(items) > 1000:
            raise ValueError('Не более 1000 строк')
        return {'items': items, **validate(items)}
    except Exception as e:
        raise HTTPException(422, f'Не удалось импортировать файл: {e}') from None

@app.get('/api/reports/export')
def export(project_id: str | None = None, days: int = 30, format: str = 'csv', user=Depends(current_user), db=Depends(db_session)):
    report = dashboard(days, project_id, None, user, db)
    if os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') != 'demo' and not report['statistics_available']:
        raise HTTPException(409, 'Статистика ещё не загружена. Экспорт пустых значений запрещён.')
    data = report['campaigns']
    header = ['Кампания DEMO' if os.getenv('SHTAB_DIRECT_APP_MODE','demo') == 'demo' else 'Кампания · LC · НДС не включён · '+report.get('currency','UNKNOWN'), 'Расход', 'Показы', 'Клики', 'Конверсии', 'CPA']
    rows = [[r['name'], r['cost'], r['impressions'], r['clicks'], r['conversions'], round(r['cpa'], 2)] for r in data]
    if format == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        wb.active.append(header)
        for row in rows:
            wb.active.append(row)
        output = io.BytesIO()
        wb.save(output)
        content, mime, ext = output.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'
    elif format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(header)
        writer.writerows(rows)
        content, mime, ext = output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', 'csv'
    else:
        raise HTTPException(422, 'Формат: csv или xlsx')
    return StreamingResponse(io.BytesIO(content), media_type=mime, headers={'Content-Disposition': f'attachment; filename="direct-demo.{ext}"'})

@app.get('/api/negative-keywords/{campaign_id}')
def keywords(campaign_id: str, user=Depends(current_user), db=Depends(db_session)):
    c = campaign_access(db, user, campaign_id)
    draft = db.scalar(select(Draft).where(Draft.campaign_id == c.id, Draft.user_id == user.id, Draft.status != 'Отменён').order_by(Draft.updated_at.desc()))
    from .yandex import campaign_view
    metadata=campaign_view(db,c,user.id)
    return {'campaign':metadata,'items': c.phrases, 'updated_at': c.updated_at, 'demo': c.is_demo, 'source': 'DEMO · локальный исходный набор' if c.is_demo else 'Яндекс Директ API · сохранённый результат чтения', 'draft': serialize_draft(draft) if draft else None, 'recommendations': [{**r.data,'archived':archived(db,user.id,c.account_id)} for r in db.scalars(select(Recommendation).where(Recommendation.campaign_id == c.id))]}

from .yandex import register
register(app)

from .reports import register as register_reports
from .ai import register as register_ai
register_reports(app)
register_ai(app)

from .approvals import register as register_approvals
register_approvals(app)
from .daily_analytics import register as register_daily
register_daily(app)
from .disconnect import register as register_disconnect
register_disconnect(app)
from .chat import register as register_chat
register_chat(app)
from .audit_jobs import register as register_audits
register_audits(app)
from .typed_changes import register as register_typed
register_typed(app)
from .proposals import register as register_proposals
register_proposals(app)
from .checkpoints import register as register_checkpoints
register_checkpoints(app)
from .telegram import register as register_telegram
register_telegram(app)
from .daily_scheduler import register as register_daily_scheduler
register_daily_scheduler(app)
from .metrika import register as register_metrika
register_metrika(app)
from .campaign_visibility import register as register_campaign_visibility
register_campaign_visibility(app)

from .change_history import register as register_change_history
register_change_history(app)
