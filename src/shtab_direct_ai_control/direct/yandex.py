"""Server-only OAuth (PKCE + session-bound one-time state) and Direct read access.

There is deliberately no Direct update/add/delete method in this module.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from .session import session_hash
from .db import IntegrationConfig, OAuthState, YandexConnection, RemoteCampaignMapping, DirectCampaignInfo, DirectSyncReceipt, Snapshot, Account, Project, Membership, ProjectAccount, Campaign, Audit, AccountLifecycle, now
from .domain import checksum
from .account_lifecycle import guarded,credential,credential_http,archived,ensure_active_account

AUTHORIZE_URL = 'https://oauth.yandex.ru/authorize'
TOKEN_URL = 'https://oauth.yandex.ru/token'
DIRECT_URL = 'https://api.direct.yandex.com/json/v5/'
from ..paths import MOUNT_PATH, PAGE_PATH
from shtab_core.config import settings as shtab_settings

CALLBACK_PATH = '/api/auth/yandex/callback'

def cipher():
    try:
        return Fernet(os.environ['SHTAB_DIRECT_TOKEN_ENCRYPTION_KEY'].encode())
    except (KeyError, ValueError):
        raise HTTPException(503, 'Серверный ключ шифрования не настроен') from None

def seal(value):
    return cipher().encrypt(json.dumps(value).encode()).decode()

def unseal(value):
    try:
        return json.loads(cipher().decrypt(value.encode()))
    except InvalidToken:
        raise HTTPException(503, 'Ключ шифрования не соответствует сохранённым данным') from None

def app_config(db):
    env_id = os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_ID', '').strip()
    env_secret = os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_SECRET', '').strip()
    if env_id and env_secret:
        return {'client_id': env_id, 'client_secret': env_secret}
    row = db.get(IntegrationConfig, 'yandex')
    if row:
        stored = unseal(row.encrypted_data)
        # Never combine a secret for another app with the newly configured Client ID.
        if not env_id or stored.get('client_id') == env_id:
            return stored
    return {'client_id': env_id, 'client_secret': env_secret}

def connected_mode():
    if os.getenv('SHTAB_DIRECT_APP_MODE', 'demo') != 'connected':
        raise HTTPException(403, 'DEMO не обращается к внешним API. Подключение доступно на сервере в режиме connected.')

def callback_url():
    origin = shtab_settings.public_url.rstrip('/')
    if not origin.startswith('https://'):
        raise HTTPException(503, 'Для OAuth требуется APP_ORIGIN с HTTPS')
    public_path = MOUNT_PATH + CALLBACK_PATH
    target = os.getenv('SHTAB_DIRECT_YANDEX_REDIRECT_URI', '').strip() or origin + public_path
    parsed, base = urlsplit(target), urlsplit(origin)
    if parsed.scheme != 'https' or parsed.netloc != base.netloc or parsed.path != public_path or parsed.query or parsed.fragment or parsed.username:
        raise HTTPException(503, 'YANDEX_REDIRECT_URI должен точно совпадать с HTTPS callback текущего приложения.')
    return target

class DirectFailure(Exception):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}

class DirectResult(dict):
    def __init__(self, result, evidence):
        super().__init__(result)
        self.evidence = evidence

def safe_direct_error(code):
    # Never forward arbitrary remote error text (it might contain submitted secrets).
    messages = {53: 'Нет доступа к API Директа. Проверьте одобрение заявки приложения.', 54: 'Недействительный OAuth-токен. Подключите аккаунт повторно.', 52: 'Метод Директа временно недоступен.', 56: 'Превышен лимит запросов Директа.', 58: 'Превышен лимит одновременных запросов.'}
    return messages.get(code, f'Яндекс Директ отклонил запрос (код {code}). Проверьте доступ приложения и выбранного клиента.')

def direct_get(token, service, params, client_login=''):
    connected_mode()
    if service not in ['clients', 'campaigns', 'keywords', 'adgroups', 'ads']:
        raise ValueError('Unsupported read service')
    headers = {'Authorization': f'Bearer {token}', 'Accept-Language': 'ru'}
    if client_login:
        headers['Client-Login'] = client_login
    try:
        with credential_http(token), httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.post(DIRECT_URL + service, headers=headers, json={'method': 'get', 'params': params})
        request_id = getattr(response,'headers',{}).get('RequestId','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id): request_id = None
        diagnostic = {'endpoint':DIRECT_URL+service,'method':'get','http_status':response.status_code,'request_id':request_id}
        if response.status_code in [401, 403]:
            raise DirectFailure('Нет доступа: проверьте OAuth и одобрение заявки API Директа.', diagnostic)
        if response.status_code == 429 or response.status_code >= 500:
            raise DirectFailure('Директ временно недоступен или ограничил запросы. Повторите синхронизацию позднее.', diagnostic)
        data = response.json()
        if not isinstance(data,dict):
            raise DirectFailure('Директ вернул неожиданный формат ответа.', diagnostic)
        if 'error' in data:
            error = data['error'] if isinstance(data['error'],dict) else {}
            code = error.get('error_code')
            code = code if isinstance(code,int) else 'unknown'
            raise DirectFailure(safe_direct_error(code), {**diagnostic,'api_error_code':code})
        key = {'clients':'Clients','campaigns':'Campaigns','keywords':'Keywords','adgroups':'AdGroups','ads':'Ads'}[service]
        if response.status_code != 200 or not isinstance(data.get('result'), dict) or not isinstance(data['result'].get(key),list):
            raise DirectFailure('Директ вернул неожиданный формат ответа.', diagnostic)
        return DirectResult(data['result'],{**diagnostic,'source':'Yandex Direct API v5','received_at':now()})
    except (httpx.HTTPError, ValueError):
        raise DirectFailure('Не удалось прочитать Директ: сеть или формат ответа. Никаких изменений не отправлено.') from None

@guarded('token')
def access_token(db, connection):
    if not connection.expires or connection.expires > time.time()+120:
        return credential(unseal(connection.encrypted_token),connection)
    if not connection.encrypted_refresh:
        raise DirectFailure('Срок действия OAuth истёк. Подключите аккаунт повторно.')
    config = app_config(db)
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            response = client.post(TOKEN_URL, data={'grant_type': 'refresh_token', 'refresh_token': unseal(connection.encrypted_refresh), **config})
        data = response.json()
        if response.status_code != 200 or not isinstance(data,dict) or not isinstance(data.get('access_token'),str) or not data['access_token']:
            raise DirectFailure('Не удалось обновить OAuth. Подключите аккаунт повторно.')
        expires=int(data.get('expires_in',31536000))
        refresh=data.get('refresh_token')
        if expires<=0 or (refresh is not None and not isinstance(refresh,str)):
            raise DirectFailure('Яндекс вернул недействительный ответ обновления OAuth. Подключите аккаунт повторно.')
        encrypted_token=seal(data['access_token'])
        encrypted_refresh=seal(refresh) if refresh else connection.encrypted_refresh
        connection.encrypted_token,connection.encrypted_refresh=encrypted_token,encrypted_refresh
        connection.expires = time.time()+expires
        db.commit()
        return credential(data['access_token'],connection)
    except (httpx.HTTPError, ValueError, TypeError, OverflowError):
        raise DirectFailure('Не удалось обновить OAuth из-за сетевой ошибки.') from None

def validate_campaign(remote):
    if not isinstance(remote,dict) or type(remote.get('Id')) is not int or remote['Id'] <= 0 or not isinstance(remote.get('Name'),str) or not isinstance(remote.get('Type'),str):
        raise DirectFailure('Campaigns.get вернул недействительную кампанию. DEMO или повреждённые данные не импортируются.')
    for field in ['State','Status','StatusPayment']:
        if field in remote and (not isinstance(remote[field],str) or not re.fullmatch(r'[A-Z_]{1,40}',remote[field])):
            raise DirectFailure('Campaigns.get вернул недействительный статус кампании.')
    if 'NegativeKeywords' in remote and remote['NegativeKeywords'] is not None:
        negative=remote['NegativeKeywords']
        if not isinstance(negative,dict) or not isinstance(negative.get('Items'),list) or not all(isinstance(x,str) for x in negative['Items']):
            raise DirectFailure('Директ вернул недействительный список минус-фраз. Старый список сохранён.')
    return remote

CAMPAIGN_FIELDS=['Id','Name','Type','State','Status','StatusPayment','NegativeKeywords']

def persist_campaign(db,account,project_id,remote):
    cid=f'{account.id}:{remote["Id"]}'
    existing=db.get(Campaign,cid)
    raw=remote.get('NegativeKeywords')
    phrases=raw.get('Items',[]) if isinstance(raw,dict) else []
    available='NegativeKeywords' in remote
    if not existing:
        existing=Campaign(id=cid,account_id=account.id,project_id=project_id,name=remote['Name'],phrases=phrases,is_demo=False)
        db.add(existing)
        db.flush()
        db.add(RemoteCampaignMapping(campaign_id=cid,remote_id=remote['Id'],campaign_type=remote['Type']))
    else:
        existing.name,existing.updated_at=remote['Name'],now()
        if available: existing.phrases=phrases
        mapping=db.get(RemoteCampaignMapping,cid)
        if mapping: mapping.campaign_type=remote['Type']
    info=db.get(DirectCampaignInfo,cid)
    if not info:
        info=DirectCampaignInfo(campaign_id=cid)
        db.add(info)
    info.state,info.status,info.payment_status=remote.get('State','UNKNOWN'),remote.get('Status','UNKNOWN'),remote.get('StatusPayment','UNKNOWN')
    info.negative_keywords_available=available
    if available: info.phrases_read_at=now()
    return existing

@guarded('sync')
def sync_campaigns(db,user,connection,reconnect_account_id=None):
    expected=None
    if reconnect_account_id:
        expected=db.scalar(select(Account).join(ProjectAccount).join(Membership,Membership.project_id==ProjectAccount.project_id).where(Account.id==reconnect_account_id,Membership.user_id==user.id))
        if not expected:raise DirectFailure('Повторное подключение недоступно для выбранного аккаунта.')
    token=access_token(db,connection)
    result=direct_get(token,'clients',{'FieldNames':['Login','ClientId','Type']},connection.client_login)
    clients=result.get('Clients',[])
    if not clients or not isinstance(clients[0],dict) or not isinstance(clients[0].get('Login'),str):
        raise DirectFailure('Яндекс не вернул действительный рекламный аккаунт.')
    identity=clients[0]
    if identity.get('Type')=='AGENCY':
        raise DirectFailure('Это аккаунт агентства. Укажите Client-Login рекламодателя и повторите чтение.',{'requires_client_login':True})
    login=identity['Login']
    if expected and expected.name.casefold()!=login.casefold():
        raise DirectFailure('Авторизован другой аккаунт. Выбранный аккаунт остаётся отключённым.',{'reconnect_mismatch':True})
    known=list(db.scalars(select(Account).join(ProjectAccount).join(Membership,Membership.project_id==ProjectAccount.project_id).where(Membership.user_id==user.id,Account.name==login)))
    for account in known:
        if not expected or account.id!=expected.id:ensure_active_account(db,user.id,account.id)
    all_campaigns,offset,proof=[],0,[]
    while True:
        page=direct_get(token,'campaigns',{'SelectionCriteria':{},'FieldNames':CAMPAIGN_FIELDS,'Page':{'Limit':1000,'Offset':offset}},connection.client_login)
        if not isinstance(page,DirectResult) or page.evidence.get('http_status')!=200 or page.evidence.get('endpoint')!=DIRECT_URL+'campaigns':
            raise DirectFailure('Нет подтверждения ответа официального API. Непроверенные данные не импортированы.')
        all_campaigns.extend(validate_campaign(remote) for remote in page['Campaigns'])
        if isinstance(page,DirectResult): proof.append(page.evidence)
        next_offset=page.get('LimitedBy')
        if not next_offset: break
        if type(next_offset) is not int or next_offset<=offset or len(all_campaigns)>100_000:
            raise DirectFailure('Неожиданная пагинация Директа. Синхронизация остановлена.')
        offset=next_offset
    if len({r['Id'] for r in all_campaigns})!=len(all_campaigns):
        raise DirectFailure('В ответе Директа повторяются ID кампаний. Данные не заменены.')
    if not proof:
        raise DirectFailure('Нет подтверждения ответа официального API. Непроверенные данные не импортированы.')
    if not connection.account_id:
        candidate=expected or db.scalar(select(Account).join(ProjectAccount).join(Membership,Membership.project_id==ProjectAccount.project_id).where(Membership.user_id==user.id,Account.is_demo==False,Account.name==login))
        if candidate:
            connection.account_id=candidate.id
        else:
            suffix=uuid4().hex
            project=Project(id='live-'+suffix,name='Яндекс Директ / '+login,description='Реальный рекламный аккаунт. Статистика ещё не загружена.',is_demo=False)
            account=Account(id='ya-'+suffix,name=login,is_demo=False)
            db.add_all([project,account]);db.flush()
            db.add_all([ProjectAccount(project_id=project.id,account_id=account.id),Membership(user_id=user.id,project_id=project.id)])
            connection.account_id=account.id;db.flush()
    account=db.get(Account,connection.account_id)
    if expected:
        marker=db.get(AccountLifecycle,(user.id,expected.id))
        if marker:db.delete(marker)
        db.add(Audit(id=str(uuid4()),user_id=user.id,action='yandex.account_reconnected',data={'account_id':expected.id,'connection_id':connection.id,'new_oauth_consent':True}))
    link=db.scalar(select(ProjectAccount).where(ProjectAccount.account_id==account.id))
    if not link or not db.get(Membership,(user.id,link.project_id)):
        raise DirectFailure('Нет доступа к проекту этого аккаунта.')
    for remote in all_campaigns: persist_campaign(db,account,link.project_id,remote)
    connection.status,connection.last_error,connection.updated_at='synced','',now()
    evidence={'verified':True,'source':'Yandex Direct API v5','endpoint':DIRECT_URL+'campaigns','method':'get','http_status':200,'campaign_count':len(all_campaigns),'pages':proof,'received_at':now(),'client_login_used':bool(connection.client_login)}
    db.add(DirectSyncReceipt(id=str(uuid4()),connection_id=connection.id,user_id=user.id,evidence=evidence))
    db.add(Audit(id=str(uuid4()),user_id=user.id,action='yandex.read_sync',data={'connection_id':connection.id,'campaign_count':len(all_campaigns),'verified_response':True}))
    db.commit()
    return {'campaign_count':len(all_campaigns),'login':login,'project_id':link.project_id,'verification':evidence}

def connection_view(db,user,connection):
    errors=db.scalars(select(Audit).where(Audit.user_id==user.id,Audit.action=='yandex.read_error').order_by(Audit.created_at.desc()).limit(20))
    diagnostic=next((e.data.get('diagnostic',{}) for e in errors if e.data.get('connection_id')==connection.id),{})
    receipt=db.scalar(select(DirectSyncReceipt).where(DirectSyncReceipt.connection_id==connection.id,DirectSyncReceipt.user_id==user.id).order_by(DirectSyncReceipt.created_at.desc()))
    return {'projects':[{'id':p.id,'name':p.name} for p in db.scalars(select(Project).join(ProjectAccount,ProjectAccount.project_id==Project.id).where(ProjectAccount.account_id==connection.account_id))],'last_sync_at':receipt.created_at if receipt else None,'id':connection.id,'account_id':connection.account_id,'can_report':bool(connection.account_id) and connection.status!='disconnected' and not archived(db,user.id,connection.account_id),'archived':connection.status=='disconnected' or archived(db,user.id,connection.account_id),'account':db.get(Account,connection.account_id).name if connection.account_id else 'OAuth сохранён; Директ ещё не проверен','status':connection.status,'last_error':connection.last_error,'updated_at':connection.updated_at,'client_login':connection.client_login,'last_diagnostic':diagnostic if connection.status in ['error','needs_client_login'] else {},'verification':receipt.evidence if receipt else None}

def sync_failure(db,user,connection_id,exc):
    db.rollback()
    connection=db.get(YandexConnection,connection_id)
    if connection.status=='disconnected':return
    connection.status='needs_client_login' if exc.diagnostic.get('requires_client_login') else 'error'
    connection.last_error,connection.updated_at=str(exc),now()
    db.add(Audit(id=str(uuid4()),user_id=user.id,action='yandex.read_error',data={'connection_id':connection_id,'diagnostic':exc.diagnostic}))
    db.commit()

class ConfigBody(BaseModel):
    client_id: str = Field(pattern=r'^[a-zA-Z0-9]{16,100}$')
    client_secret: str = Field(min_length=10, max_length=300)

class ConnectionBody(BaseModel):
    connection_id: str
    client_login: str | None = Field(default=None, max_length=200, pattern=r'^[a-zA-Z0-9@._-]*$')

def start_authorization(db,user,request,reconnect=None):
    config = app_config(db)
    if not config['client_id'] or not config['client_secret']:
        raise HTTPException(422, 'Client Secret отсутствует в защищённом окружении сервера. Завершите безопасную серверную настройку.')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    db.execute(delete(OAuthState).where(OAuthState.expires < time.time()))
    db.add(OAuthState(id=hashlib.sha256(state.encode()).hexdigest(), user_id=user.id, reconnect_account_id=reconnect.account_id if reconnect else None,reconnect_client_login=reconnect.client_login if reconnect else None,session_id=session_hash(request), encrypted_verifier=seal(verifier), expires=time.time()+600))
    db.commit()
    query = {'response_type': 'code', 'client_id': config['client_id'], 'redirect_uri': callback_url(), 'state': state, 'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(), 'code_challenge_method': 'S256', 'force_confirm': 'yes'}
    return {'url': AUTHORIZE_URL+'?'+urlencode(query)}

def register(app):
    from .main import current_user, db_session, permitted

    @app.get('/api/integrations/yandex/status')
    def status(user=Depends(current_user), db=Depends(db_session)):
        config = app_config(db)
        connections = list(db.scalars(select(YandexConnection).where(YandexConnection.user_id == user.id).order_by(YandexConnection.updated_at.desc())))
        server_managed=bool(os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_ID','').strip())
        secret_source='environment' if os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_SECRET','').strip() and os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_ID','').strip() else 'encrypted_database' if config['client_secret'] else 'missing'
        return {'mode':os.getenv('SHTAB_DIRECT_APP_MODE','demo'),'configured':bool(config['client_id'] and config['client_secret']),'client_id':config['client_id'],'redirect_uri':os.getenv('SHTAB_DIRECT_YANDEX_REDIRECT_URI','').strip() or shtab_settings.public_url.rstrip('/')+MOUNT_PATH+CALLBACK_PATH,'server_managed':server_managed,'secret_source':secret_source,'secret_present':bool(config['client_secret']),'connection_state':'oauth_required' if config['client_secret'] and not connections else 'secret_missing' if not config['client_secret'] else 'configured','connections':[connection_view(db,user,c) for c in connections]}


    @app.post('/api/integrations/yandex/configure')
    def configure(body: ConfigBody, user=Depends(current_user), db=Depends(db_session)):
        connected_mode()
        permitted(user, 'integration:manage')
        callback_url()
        expected_id=os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_ID','').strip()
        if expected_id and body.client_id!=expected_id:
            raise HTTPException(409,'Client ID закреплён конфигурацией сервера. Нельзя сохранить секрет для другого приложения.')
        if os.getenv('SHTAB_DIRECT_YANDEX_CLIENT_SECRET','').strip():
            raise HTTPException(409,'Секрет уже задан переменной окружения. Его замена выполняется через защищённую серверную настройку.')
        row = db.get(IntegrationConfig, 'yandex')
        if not row:
            row = IntegrationConfig(id='yandex', encrypted_data=seal(body.model_dump()))
            db.add(row)
        else:
            row.encrypted_data = seal(body.model_dump())
        db.execute(delete(OAuthState))  # Old flows use obsolete client configuration.
        db.add(Audit(id=str(uuid4()), user_id=user.id, action='yandex.configured', data={'client_id': body.client_id}))
        db.commit()
        return {'ok': True}

    @app.post('/api/auth/yandex/start')
    @app.post('/api/auth/yandex/authorize')
    @app.post('/api/integrations/yandex/authorize')
    def authorize(request: Request, user=Depends(current_user), db=Depends(db_session)):
        connected_mode()
        permitted(user, 'integration:manage')
        return start_authorization(db,user,request)

    @app.post('/api/integrations/yandex/connections/{identifier}/reconnect')
    def reconnect(identifier:str,request:Request,user=Depends(current_user),db=Depends(db_session)):
        connected_mode();permitted(user,'integration:manage')
        c=db.scalar(select(YandexConnection).where(YandexConnection.id==identifier,YandexConnection.user_id==user.id))
        if not c or not c.account_id:raise HTTPException(404,'Подключение недоступно.')
        if c.status!='disconnected':raise HTTPException(409,'Аккаунт уже подключён. Повторное подключение требует нового явного OAuth-согласия.')
        return start_authorization(db,user,request,c)

    @app.get(CALLBACK_PATH)
    @app.get('/api/integrations/yandex/callback')
    def callback(request: Request, state: str = '', code: str = '', error: str = '', user=Depends(current_user), db=Depends(db_session)):
        connected_mode()
        permitted(user, 'integration:manage')
        if not state or len(state) > 1024 or len(code) > 4096:
            raise HTTPException(400, 'Недействительный OAuth state')
        # Atomic delete binds a short-lived single-use state to this user and login session.
        flow = db.execute(delete(OAuthState).where(OAuthState.id == hashlib.sha256(state.encode()).hexdigest(), OAuthState.user_id == user.id, OAuthState.session_id == session_hash(request), OAuthState.expires > time.time()).returning(OAuthState.encrypted_verifier,OAuthState.reconnect_account_id,OAuthState.reconnect_client_login)).first()
        db.commit()
        if not flow:
            raise HTTPException(400, 'OAuth-ссылка истекла, использована или открыта в другой сессии. Начните подключение снова.')
        destination = PAGE_PATH + '?page=settings'
        if error or not code:
            return RedirectResponse(destination+'&oauth=denied', status_code=303)
        config = app_config(db)
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                response = client.post(TOKEN_URL, data={'grant_type': 'authorization_code', 'code': code, 'code_verifier': unseal(flow.encrypted_verifier), **config})
            data = response.json()
            if not isinstance(data, dict) or response.status_code != 200 or not isinstance(data.get('access_token'), str) or not data['access_token']:
                return RedirectResponse(destination+'&oauth=failed', status_code=303)
            if data.get('refresh_token') is not None and not isinstance(data['refresh_token'], str):
                return RedirectResponse(destination+'&oauth=failed', status_code=303)
            expires_in = int(data['expires_in']) if data.get('expires_in') is not None else None
            if expires_in is not None and expires_in <= 0:
                return RedirectResponse(destination+'&oauth=failed', status_code=303)
        except (httpx.HTTPError, ValueError, TypeError, OverflowError):
            return RedirectResponse(destination+'&oauth=failed', status_code=303)
        connection = YandexConnection(id=str(uuid4()), user_id=user.id, client_login=flow.reconnect_client_login or '', encrypted_token=seal(data['access_token']), encrypted_refresh=seal(data['refresh_token']) if data.get('refresh_token') else None, expires=time.time()+expires_in if expires_in is not None else None)
        db.add(connection)
        db.add(Audit(id=str(uuid4()), user_id=user.id, action='yandex.oauth_connected', data={'connection_id': connection.id}))
        db.commit()
        # The owner authorized a real, read-only connection check after OAuth.
        try:
            sync_campaigns(db,user,connection,flow.reconnect_account_id)
        except DirectFailure as exc:
            if flow.reconnect_account_id:
                db.rollback();connection=db.get(YandexConnection,connection.id);connection.encrypted_token='';connection.encrypted_refresh=None;connection.expires=None;connection.status='disconnected';connection.last_error=str(exc);db.commit()
                return RedirectResponse(destination+'&oauth=reconnect_failed',status_code=303)
            sync_failure(db,user,connection.id,exc)
        return RedirectResponse(destination+'&oauth=connected', status_code=303)

    @app.post('/api/integrations/yandex/sync')
    @guarded('body')
    def sync(body: ConnectionBody, user=Depends(current_user), db=Depends(db_session)):
        connected_mode()
        permitted(user, 'integration:manage')
        connection = db.scalar(select(YandexConnection).where(YandexConnection.id == body.connection_id, YandexConnection.user_id == user.id).with_for_update())
        if not connection:
            raise HTTPException(404, 'Подключение недоступно')
        if body.client_login is not None:
            if connection.account_id and connection.client_login != body.client_login:
                raise HTTPException(409, 'Нельзя менять клиента существующего аккаунта. Создайте отдельное подключение.')
            connection.client_login = body.client_login
        try:
            return sync_campaigns(db, user, connection)
        except DirectFailure as exc:
            attempted_login=body.client_login
            sync_failure(db,user,connection.id,exc)
            if attempted_login is not None and not connection.account_id:
                connection.client_login=attempted_login
                db.commit()
            raise HTTPException(502,str(exc)) from None

    @app.get('/api/integrations/yandex/campaigns')
    def connected_campaigns(user=Depends(current_user),db=Depends(db_session)):
        query=select(Campaign).join(Membership,Membership.project_id==Campaign.project_id).where(Membership.user_id==user.id,Campaign.is_demo==False)
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        return [campaign_view(db,c,user.id) for c in db.scalars(query) if c.id not in hidden]

    @app.post('/api/negative-keywords/{campaign_id}/sync')
    @guarded('campaign')
    def read_negative(campaign_id:str,user=Depends(current_user),db=Depends(db_session)):
        from .main import campaign_access
        connected_mode();permitted(user,'integration:manage')
        c=campaign_access(db,user,campaign_id)
        connection=db.scalar(select(YandexConnection).where(YandexConnection.user_id==user.id,YandexConnection.account_id==c.account_id,YandexConnection.status!='disconnected').order_by(YandexConnection.updated_at.desc()).with_for_update())
        mapping=db.get(RemoteCampaignMapping,c.id)
        if c.is_demo or not connection or not mapping:
            raise HTTPException(409,'Для этой кампании нет реального подключения Директа.')
        try:
            result=direct_get(access_token(db,connection),'campaigns',{'SelectionCriteria':{'Ids':[mapping.remote_id]},'FieldNames':CAMPAIGN_FIELDS},connection.client_login)
            rows=result['Campaigns']
            if not isinstance(result,DirectResult) or len(rows)!=1 or rows[0].get('Id')!=mapping.remote_id:
                raise DirectFailure('Директ не подтвердил доступ к выбранной кампании.')
            remote=validate_campaign(rows[0])
            if 'NegativeKeywords' not in remote:
                raise DirectFailure('Директ не вернул минус-фразы уровня кампании для этого типа. Локальный набор сохранён.')
            c=persist_campaign(db,db.get(Account,c.account_id),c.project_id,remote)
            db.add(Snapshot(id=str(uuid4()),campaign_id=c.id,items=list(c.phrases),checksum=checksum(c.phrases)))
            db.add(Audit(id=str(uuid4()),user_id=user.id,action='negative_keywords.read',data={'campaign_id':c.id,'remote_id':mapping.remote_id,'evidence':result.evidence}))
            db.commit()
            return {'items':c.phrases,'updated_at':c.updated_at,'remote_id':mapping.remote_id,'verified':True,'source':'Yandex Direct API v5','write_performed':False}
        except DirectFailure as exc:
            sync_failure(db,user,connection.id,exc)
            raise HTTPException(502,str(exc)) from None


def campaign_view(db,c,user_id=None):
    mapping=db.get(RemoteCampaignMapping,c.id)
    info=db.get(DirectCampaignInfo,c.id)
    return {'archived':archived(db,user_id,c.account_id) if user_id is not None else False,'id':c.id,'remote_id':mapping.remote_id if mapping else None,'name':c.name,'project_id':c.project_id,'account_id':c.account_id,'account_name':db.get(Account,c.account_id).name,'is_demo':c.is_demo,'state':info.state if info else 'UNKNOWN','status':info.status if info else 'UNKNOWN','payment_status':info.payment_status if info else 'UNKNOWN','updated_at':c.updated_at,'source':'DEMO' if c.is_demo else 'Yandex Direct API v5','negative_keywords_available':bool(info and info.negative_keywords_available),'phrases_read_at':info.phrases_read_at if info else None}
