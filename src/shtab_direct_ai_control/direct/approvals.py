"""Human-only approvals. The model can create drafts, never approval or execution.

Only campaign NegativeKeywords is classified for writes. Every other field and
method, including financial changes, is rejected by the closed DTO/adapter.
"""
import hashlib
import json
import os
import secrets
import time
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update, delete, func
from sqlalchemy.exc import IntegrityError

from .session import session_hash
from .db import AIControl, AIScope, ChangeRequest, ExecutionGuard, YandexConnection, RemoteCampaignMapping, Audit, Operation, Snapshot, Campaign, Membership, now
from .domain import checksum, diff, validate
from .yandex import access_token, direct_get, DirectResult, DirectFailure, validate_campaign, CAMPAIGN_FIELDS, DIRECT_URL, connected_mode
from .account_lifecycle import ensure_active_account,credential_http


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class DraftChange(Strict):
    campaign_id: str = Field(max_length=64)
    items: list[str] = Field(max_length=1000)
    reason: str = Field(min_length=5,max_length=2000)
    period: str = Field(default='',max_length=100)
    analysis_id: str = Field(default='',max_length=64)
    analysis_version: int|None = Field(default=None,ge=1)


class EditChange(Strict):
    version: int = Field(ge=1)
    items: list[str] = Field(max_length=1000)
    reason: str = Field(min_length=5,max_length=2000)


class Confirm(Strict):
    version: int = Field(ge=1)
    diff_hash: str = Field(pattern='^[a-f0-9]{64}$')
    mass_confirmation: str = Field(default='',max_length=64)
    risk_confirmation: str = Field(default='',max_length=64)


class Execute(Confirm):
    approval_id: str = Field(min_length=32,max_length=100)


class Toggle(Strict):
    enabled: bool


class Reject(Strict):
    version: int = Field(ge=1)
    reason: str = Field(min_length=3,max_length=1000)


def digest(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()




def validated_items(items):
    result=validate(items)
    import re
    if not result['valid'] or any(p != p.strip() or len(p.split())>7 or any(len(w)>35 for w in re.findall(r'\w+',p)) for p in items) or sum(len(re.sub(r'[\s\-!+\[\]"()|]','',p)) for p in items)>20000:
        raise HTTPException(422,'Минус-фразы не прошли серверную проверку синтаксиса и ограничений API.')
    return result


def connection(db,user,campaign):
    connected_mode()
    ensure_active_account(db,user.id,campaign.account_id)
    if campaign.is_demo:
        raise HTTPException(403,'DEMO никогда не отправляется в рекламный кабинет.')
    mapping=db.get(RemoteCampaignMapping,campaign.id)
    conn=db.scalar(select(YandexConnection).where(YandexConnection.account_id==campaign.account_id,YandexConnection.user_id==user.id,YandexConnection.status!='disconnected'))
    if not mapping or not conn or mapping.campaign_type != 'TEXT_CAMPAIGN':
        raise HTTPException(409,'Запись классифицирована только для подключённой текстовой кампании вашего аккаунта.')
    return conn,mapping


def read_current(db,user,campaign,token=None):
    conn,mapping=connection(db,user,campaign)
    result=direct_get(token or access_token(db,conn),'campaigns',{'SelectionCriteria':{'Ids':[mapping.remote_id]},'FieldNames':CAMPAIGN_FIELDS},conn.client_login)
    rows=result.get('Campaigns',[])
    if not isinstance(result,DirectResult) or result.evidence.get('http_status') != 200 or result.evidence.get('endpoint')!=DIRECT_URL+'campaigns' or len(rows)!=1:
        raise DirectFailure('Не получено подтверждённое исходное состояние Директа.')
    row=validate_campaign(rows[0])
    if row['Id']!=mapping.remote_id or row['Type']!='TEXT_CAMPAIGN' or 'NegativeKeywords' not in row:
        raise DirectFailure('Ответ Директа не соответствует выбранному объекту.')
    raw=row['NegativeKeywords']
    return raw['Items'] if raw else []


def payload_for(db,user,campaign,items,reason,period='',analysis_id=''):
    from .proposals import validate_link,legacy_analysis
    from .db import AIProposal
    proposal=None
    if analysis_id:
        if db.get(AIProposal,analysis_id):proposal=validate_link(db,user,analysis_id,campaign)
        else:legacy_analysis(db,user,analysis_id,campaign)
    validated_items(items)
    before=read_current(db,user,campaign)
    change=diff(before,items)
    if not change['add'] and not change['remove']:
        raise HTTPException(422,'Изменений нет.')
    mapping=db.get(RemoteCampaignMapping,campaign.id)
    return {'operation':'replace_campaign_negative_keywords','campaign_id':campaign.id,'account_id':campaign.account_id,'remote_id':mapping.remote_id,
            'before':before,'after':items,'diff':change,'reason':reason,'period':period,'analysis_id':analysis_id,
            **({'proposal_version':proposal.version} if proposal else {}),'warnings':validated_items(items)['warnings'],'expected_effect':'Оценка: сокращение нецелевых показов; эффект не гарантирован.',
            'risk':'Возможна блокировка полезных запросов. Проверьте каждую добавляемую и удаляемую фразу.'}


def audit(db,user,action,row,data=None):
    db.add(Audit(id=str(uuid4()),user_id=user.id,action=action,data={'request_id':row.id,'version':row.version,'diff_hash':row.diff_hash,**(data or {})}))


def view(row):
    return {k:getattr(row,k) for k in ('id','campaign_id','version','status','payload','diff_hash','approved_by','expires','result','created_at')}


def request_access(db,user,identifier,lock=False):
    from .main import campaign_access
    query=select(ChangeRequest).where(ChangeRequest.id==identifier,ChangeRequest.user_id==user.id)
    if lock: query=query.with_for_update()
    row=db.scalar(query.execution_options(populate_existing=True))
    if not row: raise HTTPException(404,'Заявка недоступна.')
    campaign=campaign_access(db,user,row.campaign_id)
    return row,campaign


def create_change_draft(db,user,body):
    from .main import campaign_access,permitted
    permitted(user,'draft:write')
    campaign=campaign_access(db,user,body.campaign_id)
    if body.analysis_id:
        from .db import AIProposal
        linked=db.get(AIProposal,body.analysis_id)
        if linked and linked.version!=body.analysis_version:raise HTTPException(409,'Версия рекомендации изменилась.')
    try:
        payload=payload_for(db,user,campaign,body.items,body.reason,body.period,body.analysis_id)
    except DirectFailure as e:
        raise HTTPException(502,str(e)) from None
    row=ChangeRequest(id=str(uuid4()),user_id=user.id,campaign_id=campaign.id,payload=payload,diff_hash=digest(payload))
    db.add(row); db.flush()
    audit(db,user,'ai.change_draft_created',row)
    db.commit()
    return view(row)


def dispatch(db,user,row,service,method,params,token,client_login):
    """Record only closed-adapter bodies; credentials/HTTP headers are excluded."""
    body={'method':method,'params':params}
    audit(db,user,'ai.direct_request',row,{'service':service,'body':body})
    headers={'Authorization':'Bearer '+token,'Accept-Language':'ru'}
    if client_login:headers['Client-Login']=client_login
    try:
        with credential_http(token),httpx.Client(timeout=30,trust_env=False,follow_redirects=False) as client:response=client.post(DIRECT_URL+service,headers=headers,json=body)
        try:data=response.json()
        except ValueError:data={'invalid_json':True}
        # Never persist arbitrary transport exception text or credentials.
        raw=json.dumps(data,ensure_ascii=False).replace(token,'[REDACTED]')
        saved=json.loads(raw) if len(raw)<=100000 else {'truncated':True}
        receipt={'http_status':response.status_code,'request_id':response.headers.get('RequestId','')[:100],'service':service,'method':method,'response':saved}
        audit(db,user,'ai.direct_response',row,{'receipt':receipt})
        return data,receipt
    except Exception:
        audit(db,user,'ai.direct_transport_unknown',row,{'service':service,'method':method,'response_received':False})
        raise DirectFailure('Результат запроса неизвестен. Разрешена только сверка.') from None


def write_negative_keywords(db,user,campaign,items,token):
    """The only write adapter. No generic method/params/fields may enter it."""
    validated_items(items)
    guard=db.get(ExecutionGuard,campaign.id)
    approved=db.get(ChangeRequest,guard.request_id) if guard else None
    if not approved or approved.status!='executing' or approved.approved_by!=user.id or approved.user_id!=user.id or approved.payload.get('after')!=items or digest(approved.payload)!=approved.diff_hash or approved.approval_hash:
        raise HTTPException(403,'Адаптер требует уже потреблённое точное подтверждение и блокировку executor.')
    conn,mapping=connection(db,user,campaign)
    data,receipt=dispatch(db,user,approved,'campaigns','update',{'Campaigns':[{'Id':mapping.remote_id,'NegativeKeywords':{'Items':items}}]},token,conn.client_login)
    results=data.get('result',{}).get('UpdateResults',[])
    if receipt['http_status']!=200 or len(results)!=1 or results[0].get('Id')!=mapping.remote_id or results[0].get('Errors'):
        raise DirectFailure('Директ не подтвердил операцию. Требуется сверка без повторной отправки.')
    return receipt


def gates(db,campaign_id,lock=False):
    q=select(AIControl).where(AIControl.id==1)
    if lock: q=q.with_for_update()
    control=db.scalar(q.execution_options(populate_existing=True))
    scope=db.scalar(select(AIScope).where(AIScope.campaign_id==campaign_id).execution_options(populate_existing=True))
    if not control or not control.writes_enabled or not scope or not scope.enabled:
        raise HTTPException(403,'Запись остановлена или кампания не разрешена администратором.')


def execution_store(db):
    if db.get_bind().dialect.name!='postgresql':
        raise HTTPException(403,'Реальная запись требует PostgreSQL с транзакционными блокировками. Локальный SQLite работает только на чтение.')


def approved_change_executor(db,user,request,identifier,body):
    from .main import permitted
    permitted(user,'campaign:approve'); permitted(user,'campaign:write')
    execution_store(db)
    row,campaign=request_access(db,user,identifier)
    gates(db,campaign.id)
    if row.payload.get('operation')!='replace_campaign_negative_keywords' or set(row.payload)-{'rollback_of','proposal_version'}!={'operation','campaign_id','account_id','remote_id','before','after','diff','reason','period','analysis_id','warnings','expected_effect','risk'}:
        raise HTTPException(403,'Операция вне серверного allowlist.')
    if row.payload['campaign_id']!=campaign.id or row.payload['account_id']!=campaign.account_id or row.diff_hash!=digest(row.payload):
        raise HTTPException(409,'Целевой объект или содержимое заявки изменились.')
    conn,mapping=connection(db,user,campaign)
    if row.payload['remote_id']!=mapping.remote_id:
        raise HTTPException(409,'Изменилось сопоставление кампании.')
    validated_items(row.payload['after'])
    # Refresh can commit; do it before atomic consumption and final control lock.
    token=access_token(db,conn)
    if len(row.payload['diff']['add'])+len(row.payload['diff']['remove'])>10 and body.mass_confirmation!=str(mapping.remote_id):
        raise HTTPException(422,'Для массового изменения повторно введите ID кампании.')
    cutoff=time.time()-86400
    used=db.scalar(select(func.count()).select_from(ChangeRequest).where(ChangeRequest.attempted_at>=cutoff))
    if used>=30: raise HTTPException(429,'Суточный лимит: 30 операций, одна кампания за запуск.')
    claimed=db.execute(update(ChangeRequest).where(ChangeRequest.id==identifier,ChangeRequest.status=='approved',ChangeRequest.version==body.version,ChangeRequest.diff_hash==body.diff_hash,
            ChangeRequest.approved_by==user.id,ChangeRequest.session_hash==session_hash(request),ChangeRequest.approval_hash==digest(body.approval_id),ChangeRequest.expires>time.time()).values(status='executing',approval_hash='',attempted_at=time.time()).returning(ChangeRequest.id)).scalar_one_or_none()
    if not claimed:
        db.rollback(); raise HTTPException(409,'Подтверждение отсутствует, использовано, просрочено или относится к другой версии/сессии.')
    db.add(ExecutionGuard(campaign_id=campaign.id,request_id=row.id))
    audit(db,user,'ai.execution_claimed',row,{'session_hash':session_hash(request)})
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409,'Кампания уже выполняется или ожидает сверки.') from None
    # Executing state and guard are durable before any write, including process crash.
    sent=False
    try:
        current=read_current(db,user,campaign,token)
        if checksum(current)!=checksum(row.payload['before']):
            row.status='conflict'; row.result={'message':'Внешнее состояние изменилось. Нужна новая версия и новое подтверждение.'}
        else:
            from .proposals import verify_change_link
            verify_change_link(db,user,row)
            gates(db,campaign.id,lock=True)
            if row.expires<=time.time():
                raise HTTPException(409,'Подтверждение истекло до отправки.')
            recent=db.scalar(select(func.count()).select_from(ChangeRequest).where(ChangeRequest.attempted_at>=cutoff))
            if recent>30: raise HTTPException(429,'Суточный лимит исчерпан.')
            db.add(Snapshot(id=str(uuid4()),campaign_id=campaign.id,items=current,checksum=checksum(current)))
            from .negative_safety import guard
            guard(db,user,campaign,row,body.risk_confirmation,token)
            sent=True
            receipt=write_negative_keywords(db,user,campaign,row.payload['after'],token)
            observed=read_current(db,user,campaign,token)
            row.status='applied' if checksum(observed)==checksum(row.payload['after']) else 'unknown'
            row.result={'verified':row.status=='applied','receipt':receipt,'observed':observed,'message':'Подтверждено повторным чтением.' if row.status=='applied' else 'Результат не подтверждён. Повторная отправка запрещена.'}
            if row.status=='applied':
                campaign.phrases=observed; campaign.updated_at=now()
                db.add(Snapshot(id=str(uuid4()),campaign_id=campaign.id,items=observed,checksum=checksum(observed)))
                db.add(Operation(id=str(uuid4()),campaign_id=campaign.id,user_id=user.id,kind='AI · применено и проверено',data={'request_id':row.id,'before':current,'after':observed,'diff':row.payload['diff']}))
    except Exception:
        row.status='unknown' if sent else 'error'
        row.result={'verified':False,'message':'Результат неизвестен, повтор запрещён; выполните сверку.' if sent else 'Проверка перед записью не пройдена. Изменения не отправлены.'}
    if row.status not in ('unknown','executing'):
        db.execute(delete(ExecutionGuard).where(ExecutionGuard.request_id==row.id))
    audit(db,user,'ai.execution_'+row.status,row)
    db.commit()
    return view(row)


def register(app):
    from .main import current_user,db_session,permitted,campaign_access

    @app.get('/api/ai-approvals')
    def listing(user=Depends(current_user),db=Depends(db_session)):
        rows=db.scalars(select(ChangeRequest).join(Campaign,Campaign.id==ChangeRequest.campaign_id).join(Membership,Membership.project_id==Campaign.project_id).where(ChangeRequest.user_id==user.id,Membership.user_id==user.id).order_by(ChangeRequest.created_at.desc()).limit(100))
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        return [view(r) for r in rows if r.campaign_id not in hidden and r.payload.get('operation')=='replace_campaign_negative_keywords']

    @app.post('/api/ai-approvals')
    def create(body:DraftChange,user=Depends(current_user),db=Depends(db_session)):
        return create_change_draft(db,user,body)

    @app.get('/api/ai-approvals/{identifier}/history')
    def history(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        request_access(db,user,identifier)
        return [{'action':r.action,'created_at':r.created_at,'data':{k:v for k,v in r.data.items() if k!='session_hash'}} for r in db.scalars(select(Audit).where(Audit.user_id==user.id,Audit.data['request_id'].as_string()==identifier).order_by(Audit.created_at))]

    @app.get('/api/ai-controls')
    def controls(user=Depends(current_user),db=Depends(db_session)):
        control=db.get(AIControl,1)
        return {'writes_enabled':bool(control and control.writes_enabled),'automation_mode':'approval_required','auto_apply':False,
                'scopes':[{'campaign_id':s.campaign_id,'enabled':s.enabled} for s in db.scalars(select(AIScope).join(Campaign,Campaign.id==AIScope.campaign_id).join(Membership,Membership.project_id==Campaign.project_id).where(Membership.user_id==user.id))]}

    @app.post('/api/ai-controls')
    def toggle(body:Toggle,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage')
        control=db.scalar(select(AIControl).where(AIControl.id==1).with_for_update())
        if not control: control=AIControl(id=1); db.add(control)
        control.writes_enabled=body.enabled
        if not body.enabled:
            db.execute(update(ChangeRequest).where(ChangeRequest.status=='approved').values(status='pending',approval_hash='',expires=0))
        db.add(Audit(id=str(uuid4()),user_id=user.id,action='ai.writes_enabled' if body.enabled else 'ai.kill_switch',data={}))
        db.commit(); return {'writes_enabled':body.enabled}

    @app.post('/api/ai-controls/scopes/{campaign_id}')
    def scope(campaign_id:str,body:Toggle,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage'); campaign_access(db,user,campaign_id)
        # Serialize with dispatch and kill switch; scope revocation blocks new writes.
        db.scalar(select(AIControl).where(AIControl.id==1).with_for_update())
        row=db.get(AIScope,campaign_id)
        if not row: row=AIScope(campaign_id=campaign_id); db.add(row)
        row.enabled=body.enabled
        if not body.enabled:
            db.execute(update(ChangeRequest).where(ChangeRequest.campaign_id==campaign_id,ChangeRequest.status=='approved').values(status='pending',approval_hash='',expires=0))
        db.add(Audit(id=str(uuid4()),user_id=user.id,action='ai.scope_changed',data={'campaign_id':campaign_id,'enabled':body.enabled}))
        db.commit(); return {'ok':True}

    @app.post('/api/ai-approvals/{identifier}/edit')
    def edit(identifier:str,body:EditChange,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write'); row,campaign=request_access(db,user,identifier,True)
        if row.status not in ('pending','approved','rejected','conflict','error') or row.version!=body.version:
            raise HTTPException(409,'Эту версию нельзя редактировать.')
        try: payload=payload_for(db,user,campaign,body.items,body.reason,row.payload['period'],row.payload['analysis_id'])
        except DirectFailure as e: raise HTTPException(502,str(e)) from None
        audit(db,user,'ai.change_previous_version',row,{'payload':row.payload})
        row.payload=payload; row.diff_hash=digest(payload); row.version+=1; row.status='pending'; row.approval_hash=''; row.expires=0; row.approved_by=None; row.result={}
        audit(db,user,'ai.change_edited',row); db.commit(); return view(row)

    @app.post('/api/ai-approvals/{identifier}/reject')
    def reject(identifier:str,body:Reject,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'campaign:approve'); row,_=request_access(db,user,identifier,True)
        if row.version!=body.version or row.status not in ('pending','approved'): raise HTTPException(409,'Версия уже изменена или выполняется.')
        row.status='rejected'; row.approval_hash=''; row.result={'reason':body.reason}
        audit(db,user,'ai.change_rejected',row,{'reason':body.reason}); db.commit(); return view(row)

    @app.get('/api/ai-approvals/{identifier}/safety')
    def safety(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        row,campaign=request_access(db,user,identifier)
        from .negative_safety import assess
        report=assess(db,user,campaign,row)
        return {'report':report,'confirmation':digest(report),'diff_hash':row.diff_hash,'version':row.version}

    @app.post('/api/ai-approvals/{identifier}/approve')
    def approve(identifier:str,body:Confirm,request:Request,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'campaign:approve'); permitted(user,'campaign:write')
        row,campaign=request_access(db,user,identifier)
        # Refreshing OAuth during screening may commit: screen before acquiring locks.
        from .negative_safety import guard
        guard(db,user,campaign,row,body.risk_confirmation)
        # Same lock order as kill/scope revocation and final dispatch.
        gates(db,campaign.id,lock=True)
        row,campaign=request_access(db,user,identifier,True)
        if row.status!='pending' or row.version!=body.version or row.diff_hash!=body.diff_hash or digest(row.payload)!=body.diff_hash:
            raise HTTPException(409,'Подтверждать можно только точную ожидающую версию.')
        change=row.payload['diff']
        if len(change['add'])+len(change['remove'])>10 and body.mass_confirmation!=str(row.payload['remote_id']):
            raise HTTPException(422,'Для массовой операции повторно введите ID кампании.')
        token=secrets.token_urlsafe(32)
        row.status='approved'; row.approved_by=user.id; row.session_hash=session_hash(request); row.approval_hash=digest(token); row.expires=time.time()+300
        audit(db,user,'ai.change_approved',row,{'session_hash':row.session_hash}); db.commit()
        return {**view(row),'approval_id':token}

    @app.post('/api/ai-approvals/{identifier}/execute')
    def execute(identifier:str,body:Execute,request:Request,user=Depends(current_user),db=Depends(db_session)):
        return approved_change_executor(db,user,request,identifier,body)

    @app.post('/api/ai-approvals/{identifier}/reconcile')
    def reconcile(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        row,campaign=request_access(db,user,identifier,True)
        if row.payload.get('operation')!='replace_campaign_negative_keywords': raise HTTPException(403,'Для этого типа используйте отдельный адаптер сверки.')
        if row.status not in ('unknown','executing'): raise HTTPException(409,'Заявка не ожидает сверки.')
        try: observed=read_current(db,user,campaign)
        except DirectFailure: raise HTTPException(502,'Сверка временно недоступна.') from None
        if checksum(observed)==checksum(row.payload['after']):
            row.status='applied'; row.result={'verified':True,'observed':observed,'message':'Подтверждено отдельной сверкой.'}
            campaign.phrases=observed; campaign.updated_at=now()
            db.execute(delete(ExecutionGuard).where(ExecutionGuard.request_id==row.id))
        else:
            row.status='unknown'; row.result={'verified':False,'observed':observed,'message':'Нет совпадения. Блокировка и запрет повтора сохранены.'}
        audit(db,user,'ai.reconciled',row); db.commit(); return view(row)
