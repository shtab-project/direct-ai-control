"""Closed nonfinancial adapters. No free-form service/method/params are accepted."""
import json,secrets,time
from typing import Literal,Annotated,Union
from uuid import uuid4
from urllib.parse import urlsplit
import httpx
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field,TypeAdapter,field_validator
from sqlalchemy import select,update,delete,func
from sqlalchemy.exc import IntegrityError
from .db import ChangeRequest,Campaign,RemoteCampaignMapping,ExecutionGuard,Operation,Audit,Membership,now
from .approvals import Strict,Confirm,Execute,request_access,digest,session_hash,gates,execution_store,audit,view,validated_items
from .audit_jobs import connection
from .account_lifecycle import connection_lease,credential_http,ensure_active_account
from .yandex import direct_get,access_token,DirectResult,DirectFailure,DIRECT_URL

class Keyword(Strict):
    operation:Literal['keyword_text']
    object_id:int=Field(gt=0)
    keyword:str=Field(min_length=1,max_length=4096)
    @field_validator('keyword')
    @classmethod
    def syntax(cls,v):
        if len(v.split())>7 or v.strip()!=v or any(ord(c)<32 for c in v):raise ValueError('Неверная ключевая фраза.')
        return v
class TextAd(Strict):
    operation:Literal['text_ad']
    object_id:int=Field(gt=0)
    title:str=Field(min_length=1,max_length=56)
    title2:str=Field(max_length=30)
    text:str=Field(min_length=1,max_length=81)
    href:str=Field(min_length=8,max_length=1024)
    @field_validator('href')
    @classmethod
    def url(cls,v):
        p=urlsplit(v)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:raise ValueError('Нужна HTTP(S) ссылка без учётных данных.')
        return v
class AdState(Strict):
    operation:Literal['ad_state']
    object_id:int=Field(gt=0)
    state:Literal['ON','OFF']
class Regions(Strict):
    operation:Literal['group_regions']
    object_id:int=Field(gt=0)
    region_ids:list[int]=Field(min_length=1,max_length=1000)
    @field_validator('region_ids')
    @classmethod
    def regions(cls,v):
        if any(x==0 for x in v) or len(set(v))!=len(v):raise ValueError('Неверный список регионов.')
        return v
class GroupNegatives(Strict):
    operation:Literal['group_negatives']
    object_id:int=Field(gt=0)
    items:list[str]=Field(max_length=1000)
    @field_validator('items')
    @classmethod
    def phrases(cls,v):validated_items(v);return v
class GroupName(Strict):
    operation:Literal['group_name']
    object_id:int=Field(gt=0)
    name:str=Field(min_length=1,max_length=255)
Spec=Annotated[Union[Keyword,TextAd,AdState,Regions,GroupNegatives,GroupName],Field(discriminator='operation')]
adapter=TypeAdapter(Spec)
class DraftTyped(Strict):
    campaign_id:str
    spec:Spec
    reason:str=Field(min_length=5,max_length=2000)
    proposal_id:str=Field(default='',max_length=64)
    proposal_version:int|None=Field(default=None,ge=1)
class EditTyped(DraftTyped):
    version:int=Field(ge=1)

def read_object(db,user,campaign,spec,token=None):
    ensure_active_account(db,user.id,campaign.account_id)
    mapping=db.get(RemoteCampaignMapping,campaign.id)
    if campaign.is_demo or not mapping:raise HTTPException(403,'Требуется реальная сопоставленная кампания.')
    c=connection(db,user,campaign.account_id);service='keywords' if isinstance(spec,Keyword) else 'ads' if isinstance(spec,(TextAd,AdState)) else 'adgroups'
    field='Keyword' if isinstance(spec,Keyword) else 'State' if isinstance(spec,AdState) else 'RegionIds' if isinstance(spec,Regions) else 'NegativeKeywords' if isinstance(spec,GroupNegatives) else 'Name'
    params={'SelectionCriteria':{'Ids':[spec.object_id]},'FieldNames':['Id','CampaignId']+(['Type'] if isinstance(spec,TextAd) else [field])}
    if service in ('ads','keywords'):params['FieldNames']+=['AdGroupId']
    if service=='ads':params['FieldNames']+=['Status']
    if service=='adgroups' and field!='Name':params['FieldNames']+=['Name']
    if isinstance(spec,TextAd):params['TextAdFieldNames']=['Title','Title2','Text','Href']
    result=direct_get(token or access_token(db,c),service,params,c.client_login)
    rows=result[{'keywords':'Keywords','ads':'Ads','adgroups':'AdGroups'}[service]]
    if not isinstance(result,DirectResult) or result.evidence.get('http_status')!=200 or len(rows)!=1 or rows[0].get('Id')!=spec.object_id or rows[0].get('CampaignId')!=mapping.remote_id:raise DirectFailure('Не подтверждена принадлежность объекта выбранной кампании.')
    r=rows[0]
    group_name=r.get('Name') if service=='adgroups' else None
    if service in ('ads','keywords') and isinstance(r.get('AdGroupId'),int):
        group_result=direct_get(token or access_token(db,c),'adgroups',{'SelectionCriteria':{'Ids':[r['AdGroupId']]},'FieldNames':['Id','CampaignId','Name']},c.client_login)
        groups=group_result.get('AdGroups',[])
        if not isinstance(group_result,DirectResult) or group_result.evidence.get('http_status')!=200 or len(groups)!=1 or groups[0].get('Id')!=r['AdGroupId'] or groups[0].get('CampaignId')!=mapping.remote_id:raise DirectFailure('Не подтверждена группа объекта.')
        group_name=groups[0].get('Name')
    db.add(Audit(id=str(uuid4()),user_id=user.id,action='ai.object_read',data={'campaign_id':campaign.id,'remote_campaign_id':mapping.remote_id,'object_id':spec.object_id,'group_id':r.get('AdGroupId',spec.object_id if service=='adgroups' else None),'group_name':group_name,'moderation_status':r.get('Status') if service=='ads' else None,'service':service}))
    if isinstance(spec,TextAd):
        if r.get('Type')!='TEXT_AD' or not isinstance(r.get('TextAd'),dict):raise HTTPException(422,'Адаптер разрешён только для TEXT_AD. Комбинаторные объявления требуют отдельного адаптера.')
        t=r['TextAd'];return {k:t.get(k) or '' for k in ('Title','Title2','Text','Href')}
    if field not in r:raise DirectFailure('Директ не вернул редактируемое поле.')
    if isinstance(spec,GroupNegatives):return {'NegativeKeywords':{'Items':(r[field] or {}).get('Items',[])}}
    return {field:r[field]}

def desired(spec):
    if isinstance(spec,Keyword):return {'Keyword':spec.keyword}
    if isinstance(spec,TextAd):return {'Title':spec.title,'Title2':spec.title2,'Text':spec.text,'Href':spec.href}
    if isinstance(spec,AdState):return {'State':spec.state}
    if isinstance(spec,Regions):return {'RegionIds':spec.region_ids}
    if isinstance(spec,GroupNegatives):return {'NegativeKeywords':{'Items':spec.items}}
    return {'Name':spec.name}

def write_object(db,user,campaign,row,token):
    spec=adapter.validate_python(row.payload['spec']);expected=desired(spec)
    guard=db.get(ExecutionGuard,campaign.id)
    if not guard or guard.request_id!=row.id or row.status!='executing' or row.approved_by!=user.id or row.user_id!=user.id or row.approval_hash or digest(row.payload)!=row.diff_hash or row.payload['after']!=expected:raise HTTPException(403,'Нет потреблённого точного подтверждения.')
    conn=connection(db,user,campaign.account_id)
    if isinstance(spec,AdState):service='ads';method='resume' if spec.state=='ON' else 'suspend';params={'SelectionCriteria':{'Ids':[spec.object_id]}}
    else:
        service='keywords' if isinstance(spec,Keyword) else 'ads' if isinstance(spec,TextAd) else 'adgroups';method='update'
        fields={'TextAd':expected} if isinstance(spec,TextAd) else expected
        params={{'keywords':'Keywords','ads':'Ads','adgroups':'AdGroups'}[service]:[{'Id':spec.object_id,**fields}]}
    from .approvals import dispatch
    data,receipt=dispatch(db,user,row,service,method,params,token,conn.client_login)
    results=data.get('result',{}).get(method.capitalize()+'Results',[])
    if receipt['http_status']!=200 or len(results)!=1 or results[0].get('Id')!=spec.object_id or results[0].get('Errors'):raise DirectFailure('Результат записи не подтверждён. Автоматического повтора нет.')
    return receipt


def make_payload(db,user,campaign,body):
    from .proposals import validate_link
    proposal=validate_link(db,user,body.proposal_id,campaign)
    if proposal and body.proposal_version!=proposal.version:raise HTTPException(409,'Версия рекомендации изменилась. Перезагрузите её перед подготовкой.')
    before=read_object(db,user,campaign,body.spec);after=desired(body.spec)
    db.flush()
    target_read=db.scalar(select(Audit).where(Audit.user_id==user.id,Audit.action=='ai.object_read',Audit.data['object_id'].as_integer()==body.spec.object_id,Audit.data['campaign_id'].as_string()==campaign.id).order_by(Audit.created_at.desc()).limit(1))
    target=target_read.data if target_read else {'object_id':body.spec.object_id,'campaign_id':campaign.id}
    if digest(before)==digest(after):raise HTTPException(422,'Изменений нет.')
    return {'operation':'typed_nonfinancial','target':target,'campaign_id':campaign.id,'account_id':campaign.account_id,'remote_id':db.get(RemoteCampaignMapping,campaign.id).remote_id,'spec':body.spec.model_dump(),'before':before,'after':after,'diff':{'add':[json.dumps(after,ensure_ascii=False)],'remove':[json.dumps(before,ensure_ascii=False)],'keep':[]},'reason':body.reason,'period':'Текущее состояние Direct API','analysis_id':body.proposal_id,'proposal_version':proposal.version if proposal else None,'warnings':[],'expected_effect':'Гипотеза; эффект не гарантирован.','risk':'Изменение таргетинга/текста может снизить охват или конверсии; включение объявления возобновляет показы в существующем бюджете.'}

def execute_typed(db,user,request,identifier,body):
    from .main import permitted
    permitted(user,'campaign:approve');permitted(user,'campaign:write');execution_store(db)
    row,campaign=request_access(db,user,identifier)
    if row.payload.get('operation')!='typed_nonfinancial':raise HTTPException(403,'Неподдерживаемая операция.')
    spec=adapter.validate_python(row.payload['spec'])
    if digest(row.payload)!=row.diff_hash or row.payload['account_id']!=campaign.account_id or row.payload['campaign_id']!=campaign.id or desired(spec)!=row.payload['after']:raise HTTPException(409,'Изменилась целевая заявка.')
    mapping=db.get(RemoteCampaignMapping,campaign.id)
    if not mapping or row.payload['remote_id']!=mapping.remote_id:raise HTTPException(409,'Сопоставление изменилось.')
    conn=connection(db,user,campaign.account_id)
    # Match disconnect ordering: final control lock before dispatch credential lease.
    gates(db,campaign.id);token=access_token(db,conn)
    used=db.scalar(select(func.count()).select_from(ChangeRequest).where(ChangeRequest.attempted_at>=time.time()-86400))
    if used>=30:raise HTTPException(429,'Суточный лимит операций исчерпан.')
    claimed=db.execute(update(ChangeRequest).where(ChangeRequest.id==identifier,ChangeRequest.status=='approved',ChangeRequest.version==body.version,ChangeRequest.diff_hash==body.diff_hash,ChangeRequest.approved_by==user.id,ChangeRequest.session_hash==session_hash(request),ChangeRequest.approval_hash==digest(body.approval_id),ChangeRequest.expires>time.time()).values(status='executing',approval_hash='',attempted_at=time.time()).returning(ChangeRequest.id)).scalar_one_or_none()
    if not claimed:db.rollback();raise HTTPException(409,'Подтверждение использовано, отсутствует, просрочено или относится к другой версии/сессии.')
    db.add(ExecutionGuard(campaign_id=campaign.id,request_id=row.id));audit(db,user,'ai.typed_execution_claimed',row)
    try:db.commit()
    except IntegrityError:db.rollback();raise HTTPException(409,'Кампания занята или ожидает сверки.') from None
    sent=False
    try:
        current=read_object(db,user,campaign,spec,token)
        if digest(current)!=digest(row.payload['before']):row.status='conflict';row.result={'message':'Текущие поля изменились. Создайте новую версию и подтвердите заново.'}
        else:
            from .proposals import verify_change_link
            verify_change_link(db,user,row)
            gates(db,campaign.id,lock=True)
            db.refresh(row)
            if row.status!='executing':return view(row)
            if row.expires<=time.time():raise HTTPException(409,'Подтверждение истекло.')
            recent=db.scalar(select(func.count()).select_from(ChangeRequest).where(ChangeRequest.attempted_at>=time.time()-86400))
            if recent>30:raise HTTPException(429,'Суточный лимит исчерпан.')
            # Snapshot is durable in immutable payload plus pre-dispatch audit.
            audit(db,user,'ai.typed_before_write',row,{'before':current})
            from .negative_safety import guard
            guard(db,user,campaign,row,body.risk_confirmation,token)
            sent=True;receipt=write_object(db,user,campaign,row,token);observed=read_object(db,user,campaign,spec,token)
            row.status='applied' if digest(observed)==digest(row.payload['after']) else 'unknown'
            readback=db.scalar(select(Audit).where(Audit.user_id==user.id,Audit.action=='ai.object_read',Audit.data['object_id'].as_integer()==spec.object_id,Audit.data['campaign_id'].as_string()==campaign.id).order_by(Audit.created_at.desc()).limit(1))
            row.result={'verified':row.status=='applied','moderation':{'status':readback.data.get('moderation_status') if readback else None,'note':'Совпадение полей не означает прохождение модерации или начало показов.'},'observed':observed,'receipt':receipt,'message':'Подтверждено повторным чтением.' if row.status=='applied' else 'Требуется сверка. Повтор запрещён.'}
            if row.status=='applied':db.add(Operation(id=str(uuid4()),campaign_id=campaign.id,user_id=user.id,kind='AI · типизированное изменение',data={'request_id':row.id,'before':current,'after':observed,'diff':row.payload['diff']}))
    except Exception:row.status='unknown' if sent else 'error';row.result={'verified':False,'message':'Запрос мог быть отправлен. Только сверка, без повторной отправки.' if sent else 'Проверка до записи не пройдена.'}
    if row.status not in ('unknown','executing'):db.execute(delete(ExecutionGuard).where(ExecutionGuard.request_id==row.id))
    audit(db,user,'ai.typed_'+row.status,row);db.commit();return view(row)

def register(app):
    from .main import current_user,db_session,permitted,campaign_access
    @app.get('/api/ai-typed-changes')
    def listing(user=Depends(current_user),db=Depends(db_session)):
        rows=db.scalars(select(ChangeRequest).where(ChangeRequest.user_id==user.id,ChangeRequest.payload['operation'].as_string()=='typed_nonfinancial').order_by(ChangeRequest.created_at.desc()).limit(100))
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        return [view(r) for r in rows if r.campaign_id not in hidden and db.scalar(select(Campaign.id).join(Membership,Membership.project_id==Campaign.project_id).where(Campaign.id==r.campaign_id,Membership.user_id==user.id))]
    @app.post('/api/ai-typed-changes')
    def create(body:DraftTyped,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');c=campaign_access(db,user,body.campaign_id)
        try:payload=make_payload(db,user,c,body)
        except DirectFailure as e:raise HTTPException(502,str(e)) from None
        row=ChangeRequest(id=str(uuid4()),user_id=user.id,campaign_id=c.id,payload=payload,diff_hash=digest(payload));db.add(row);db.flush();audit(db,user,'ai.typed_draft_created',row);db.commit();return view(row)
    @app.post('/api/ai-typed-changes/{identifier}/edit')
    def edit(identifier:str,body:EditTyped,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');row,c=request_access(db,user,identifier,True)
        if row.payload.get('operation')!='typed_nonfinancial' or row.status not in ('pending','approved','rejected','conflict','error') or row.version!=body.version or body.campaign_id!=c.id:raise HTTPException(409,'Версия изменилась или её нельзя редактировать.')
        payload=make_payload(db,user,c,body);audit(db,user,'ai.typed_previous_version',row,{'payload':row.payload});row.payload=payload;row.diff_hash=digest(payload);row.version+=1;row.status='pending';row.approval_hash='';row.expires=0;row.approved_by=None;row.result={};db.commit();return view(row)
    @app.post('/api/ai-typed-changes/{identifier}/execute')
    def execute(identifier:str,body:Execute,request:Request,user=Depends(current_user),db=Depends(db_session)):return execute_typed(db,user,request,identifier,body)
    @app.post('/api/ai-typed-changes/{identifier}/reconcile')
    def reconcile(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'campaign:approve');row,c=request_access(db,user,identifier,True)
        if row.payload.get('operation')!='typed_nonfinancial' or row.status not in ('unknown','executing'):raise HTTPException(409,'Эта заявка не ожидает сверки.')
        observed=read_object(db,user,c,adapter.validate_python(row.payload['spec']))
        if digest(observed)==digest(row.payload['after']):
            row.status='applied';row.result={'verified':True,'observed':observed,'message':'Подтверждено отдельной сверкой.'};db.execute(delete(ExecutionGuard).where(ExecutionGuard.request_id==row.id))
        else:row.result={'verified':False,'observed':observed,'message':'Совпадения нет. Повторная запись заблокирована.'}
        audit(db,user,'ai.typed_reconciled',row);db.commit();return view(row)
