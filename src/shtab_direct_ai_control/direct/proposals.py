"""Human review of evidence-backed advice; endorsement never grants Direct write access."""
from typing import Literal
import hashlib
from uuid import uuid4
from fastapi import Depends,HTTPException
from pydantic import Field
from sqlalchemy import select,update
from .db import AIProposal,AssistantJob,Campaign,ChangeRequest,Audit,Project,Account,RemoteCampaignMapping,YandexConnection
from .approvals import Strict
from .ai import project_access
from .account_lifecycle import archived_ids

class FromAudit(Strict):
    job_id:str
    index:int=Field(ge=0,le=14)
    campaign_id:str|None=None
class Item(Strict):
    id:str
    version:int=Field(ge=1)
class Review(Strict):
    items:list[Item]=Field(min_length=1,max_length=30)
    decision:Literal['approved','rejected','clarification']
    reason:str=Field(min_length=3,max_length=2000)
class Edit(Strict):
    version:int=Field(ge=1)
    proposal:str=Field(min_length=3,max_length=3000)

def view(db,user,r):
    job=db.get(AssistantJob,r.job_id)
    project=db.get(Project,r.project_id)
    source=(job.result or {}).get('source',{}) if job else {}
    if not isinstance(source,dict):source={}
    campaign=db.get(Campaign,r.campaign_id) if r.campaign_id else None
    if campaign and campaign.project_id!=r.project_id:campaign=None
    account=db.get(Account,campaign.account_id) if campaign else None
    mapping=db.get(RemoteCampaignMapping,campaign.id) if campaign else None
    source_ids=source.get('campaign_ids',[])
    exact=bool(campaign and isinstance(source_ids,list) and source_ids==[campaign.id] and source.get('account_ids')==[campaign.account_id])
    before=r.data.get('before',{}) if isinstance(r.data,dict) else {}
    if isinstance(before,dict) and campaign:
        for evidence in before.values():
            if not isinstance(evidence,dict):exact=False;continue
            if evidence.get('campaign_id') and evidence['campaign_id']!=campaign.id:exact=False
            data=evidence.get('data',{})
            if isinstance(data,dict) and data.get('CampaignId') is not None and (not mapping or str(data['CampaignId'])!=str(mapping.remote_id)):exact=False
    targets=[]
    for sid,e in before.items() if isinstance(before,dict) else []:
        if not isinstance(e,dict) or e.get('kind') not in ('ads','keywords','adgroups'):continue
        d=e.get('data',{})
        if not isinstance(d,dict) or not isinstance(d.get('Id'),int):continue
        if mapping and d.get('CampaignId')!=mapping.remote_id:continue
        targets.append({'source_id':sid,'kind':e['kind'],'object_id':d['Id'],'group_id':d.get('AdGroupId',d['Id'] if e['kind']=='adgroups' else None),'group_name':d.get('Name') if e['kind']=='adgroups' else None})
    connection_id=source.get('connection_id')
    conn=db.get(YandexConnection,connection_id) if connection_id else None
    if not conn or conn.user_id!=user.id or not campaign or conn.account_id!=campaign.account_id:conn=None
    return {k:getattr(r,k) for k in ('id','project_id','campaign_id','job_id','version','status','data','created_at')}|{
        'archived':bool(job and set(job.payload.get('account_ids',[]))&archived_ids(db,user.id)),
        'execution_authorized':False,
        'workflow':workflow(db,r),
        'scope':{'project_name':project.name if project else None,'account_name':account.name if account else None,
                 'campaign_name':campaign.name if campaign else None,'campaign_id':campaign.id if campaign else None,
                 'remote_campaign_id':mapping.remote_id if mapping else None,'connection_id':conn.id if conn else None,
                 'client_login':conn.client_login if conn else None,'scope_verified':exact,
                 'targets':targets,'source_campaign_count':len(source_ids) if isinstance(source_ids,list) else None,
                 'source_type':job.kind if job else None,'source_created_at':job.created_at if job else None}}
def owned(db,user,identifier):
    r=db.scalar(select(AIProposal).where(AIProposal.id==identifier,AIProposal.user_id==user.id).with_for_update())
    if not r:raise HTTPException(404,'Предложение недоступно.')
    project_access(db,user,r.project_id)
    j=db.get(AssistantJob,r.job_id)
    if not j:raise HTTPException(409,'Исходный аудит отсутствует.')
    if set(j.payload.get('account_ids',[]))&archived_ids(db,user.id):raise HTTPException(410,'Исходный аккаунт отключён. Доступна только история.')
    return r
def invalidate(db,r):
    if db.scalar(select(ChangeRequest.id).where(ChangeRequest.user_id==r.user_id,ChangeRequest.payload['analysis_id'].as_string()==r.id,ChangeRequest.status.in_(['executing','unknown']))):raise HTTPException(409,'Сначала завершите сверку выполняемой операции.')
    db.execute(update(ChangeRequest).where(ChangeRequest.user_id==r.user_id,ChangeRequest.payload['analysis_id'].as_string()==r.id,ChangeRequest.status.in_(['pending','approved'])).values(status='cancelled',approval_hash='',expires=0))

def save_recommendations(db,user,job,answer,source):
    """Persist ideas as pending only. This function cannot issue write approvals."""
    ids=[]
    campaign_ids=source.get('campaign_ids',[])
    cid=campaign_ids[0] if len(campaign_ids)==1 else None
    for index,rec in enumerate(answer.get('recommendations',[])):
        key=hashlib.sha256(f'{user.id}:{job.id}:{index}:{cid}'.encode()).hexdigest()
        ids.append(key)
        if db.get(AIProposal,key):continue
        data={**rec,'before':{sid:source['evidence'][sid] for sid in rec['source_ids']},'period':{'from':source['date_from'],'to':source['date_to'],'currency':source['currency']},'application_mode':'exact_backend_draft_required','limitations':source['limitations']}
        db.add(AIProposal(id=key,user_id=user.id,project_id=job.project_id,campaign_id=cid,job_id=job.id,status='pending',data=data))
    return ids

WORKFLOW_LABELS={'new':'Новая рекомендация','pending':'Ожидает согласования','clarification':'Требуется уточнение','approved':'Одобрена','rejected':'Отклонена','ready':'Ожидает применения','executing':'Применяется','applied':'Применена и проверена','error':'Ошибка применения','conflict':'Конфликт данных'}

def validate_link(db,user,identifier,campaign):
    if not identifier:return None
    r=owned(db,user,identifier)
    if r.project_id!=campaign.project_id or r.campaign_id!=campaign.id:raise HTTPException(409,'Сначала подтвердите кампанию рекомендации.')
    if r.status in ('rejected','clarification'):raise HTTPException(409,'Рекомендация отклонена или требует уточнения.')
    if db.scalar(select(ChangeRequest.id).where(ChangeRequest.user_id==user.id,ChangeRequest.payload['analysis_id'].as_string()==r.id,ChangeRequest.status.in_(['executing','unknown']))):raise HTTPException(409,'Предыдущее изменение ожидает сверки.')
    return r

def legacy_analysis(db,user,identifier,campaign):
    from .db import AnalysisRun
    run=db.get(AnalysisRun,identifier)
    if not run or run.user_id!=user.id or run.status!='completed' or run.project_id!=campaign.project_id or not any(e.get('campaign_id')==campaign.id for e in run.source.get('evidence',{}).values() if isinstance(e,dict)):raise HTTPException(404,'Подтверждённый анализ недоступен для кампании.')
    return run

def verify_change_link(db,user,row):
    if not row.payload.get('analysis_id'):return
    if not db.get(AIProposal,row.payload['analysis_id']):
        legacy_analysis(db,user,row.payload['analysis_id'],db.get(Campaign,row.campaign_id));return
    r=owned(db,user,row.payload['analysis_id'])
    if r.version!=row.payload.get('proposal_version') or r.status in ('rejected','clarification') or r.campaign_id!=row.campaign_id:raise HTTPException(409,'Исходная рекомендация изменилась или отклонена.')

def workflow(db,r):
    rows=list(db.scalars(select(ChangeRequest).where(ChangeRequest.user_id==r.user_id,ChangeRequest.payload['analysis_id'].as_string()==r.id).order_by(ChangeRequest.created_at.desc())))
    current=next((c for c in rows if c.payload.get('proposal_version')==r.version and c.status!='cancelled'),None)
    state=r.status if r.status in ('approved','rejected','clarification') else 'new'
    if current:state={'pending':'pending','approved':'ready','unknown':'error','cancelled':'clarification'}.get(current.status,current.status)
    return {'state':state,'label':WORKFLOW_LABELS.get(state,state),'change_id':current.id if current else None,'requires_preparation':current is None,'changes':[{'id':c.id,'status':c.status,'version':c.version,'proposal_version':c.payload.get('proposal_version'),'payload':c.payload,'result':c.result} for c in rows]}

def register(app):
    from .main import current_user,db_session,permitted,campaign_access
    @app.get('/api/ai/proposals')
    def listing(user=Depends(current_user),db=Depends(db_session)):
        rows=list(db.scalars(select(AIProposal).where(AIProposal.user_id==user.id).order_by(AIProposal.created_at.desc()).limit(100)))
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        result=[]
        for r in rows:
            try:project_access(db,user,r.project_id)
            except HTTPException:continue
            job=db.get(AssistantJob,r.job_id)
            source=(job.result or {}).get('source',{}) if job else {}
            if r.campaign_id in hidden or set(source.get('campaign_ids',[]))&hidden:continue
            result.append(view(db,user,r))
        return result
    @app.post('/api/ai/proposals/from-audit')
    def create(body:FromAudit,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');j=db.get(AssistantJob,body.job_id,with_for_update=True)
        if not j or j.user_id!=user.id or j.kind!='audit' or j.status!='completed':raise HTTPException(404,'Завершённый аудит недоступен.')
        project_access(db,user,j.project_id)
        if set(j.payload.get('account_ids',[]))&archived_ids(db,user.id):raise HTTPException(410,'Исходный аккаунт отключён.')
        recs=j.result['answer']['recommendations']
        if body.index>=len(recs):raise HTTPException(422,'Рекомендация отсутствует.')
        cid=body.campaign_id
        if cid:
            c=campaign_access(db,user,cid)
            if c.id not in j.result['source']['campaign_ids']:raise HTTPException(403,'Кампания вне исходного аудита.')
        elif len(j.result['source']['campaign_ids'])==1:cid=j.result['source']['campaign_ids'][0]
        key=hashlib.sha256(f'{user.id}:{j.id}:{body.index}:{cid}'.encode()).hexdigest()
        candidates=[None,*j.result['source']['campaign_ids']]
        for prior_cid in candidates:
            prior_key=hashlib.sha256(f'{user.id}:{j.id}:{body.index}:{prior_cid}'.encode()).hexdigest()
            old=db.get(AIProposal,prior_key)
            if old:
                if body.campaign_id and old.campaign_id and body.campaign_id!=old.campaign_id:raise HTTPException(409,'Рекомендация уже связана с другой кампанией. Измените привязку отдельным действием.')
                return view(db,user,old)
        rec=recs[body.index];source=j.result['source']
        data={**rec,'before':{sid:source['evidence'][sid] for sid in rec['source_ids']},'period':{'from':source['date_from'],'to':source['date_to'],'currency':source['currency']},'application_mode':'exact_backend_draft_required','limitations':source['limitations']}
        row=AIProposal(id=key,user_id=user.id,project_id=j.project_id,campaign_id=cid,job_id=j.id,data=data);db.add(row);db.commit();return view(db,user,row)
    @app.post('/api/ai/proposals/review')
    def review(body:Review,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'campaign:approve')
        if len({i.id for i in body.items})!=len(body.items):raise HTTPException(422,'Повторяющиеся предложения.')
        rows=[owned(db,user,i.id) for i in sorted(body.items,key=lambda x:x.id)]
        versions={i.id:i.version for i in body.items}
        if any(r.version!=versions[r.id] or r.status not in ('pending','approved','clarification') for r in rows):raise HTTPException(409,'Одна из версий изменилась. Перезагрузите список.')
        for r in rows:
            r.status=body.decision
            if body.decision in ('rejected','clarification'):invalidate(db,r)
            db.add(Audit(id=str(uuid4()),user_id=user.id,action='ai.proposal_'+body.decision,data={'proposal_id':r.id,'version':r.version,'reason':body.reason,'execution_authorized':False}))
        db.commit();return {'reviewed':len(rows),'execution_authorized':False}
    class Bind(Strict):
        version:int=Field(ge=1)
        campaign_id:str
    @app.post('/api/ai/proposals/{identifier}/bind')
    def bind(identifier:str,body:Bind,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');r=owned(db,user,identifier)
        if r.version!=body.version:raise HTTPException(409,'Версия изменилась.')
        job=db.get(AssistantJob,r.job_id);c=campaign_access(db,user,body.campaign_id)
        if c.project_id!=r.project_id or c.id not in job.result.get('source',{}).get('campaign_ids',[]):raise HTTPException(403,'Кампания не подтверждена исходным аудитом.')
        if db.scalar(select(ChangeRequest.id).where(ChangeRequest.user_id==user.id,ChangeRequest.payload['analysis_id'].as_string()==r.id,ChangeRequest.status.in_(['executing','unknown']))):raise HTTPException(409,'Сначала завершите сверку операции.')
        invalidate(db,r);r.campaign_id=c.id;r.version+=1;r.status='pending'
        db.add(Audit(id=str(uuid4()),user_id=user.id,action='ai.proposal_bound',data={'proposal_id':r.id,'campaign_id':c.id,'version':r.version}))
        db.commit();return view(db,user,r)

    @app.post('/api/ai/proposals/{identifier}/edit')
    def edit(identifier:str,body:Edit,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');r=owned(db,user,identifier)
        if r.version!=body.version:raise HTTPException(409,'Версия изменилась.')
        db.add(Audit(id=str(uuid4()),user_id=user.id,action='ai.proposal_previous_version',data={'proposal_id':r.id,'version':r.version,'data':r.data}))
        invalidate(db,r);r.data={**r.data,'proposal':body.proposal};r.version+=1;r.status='pending';db.commit();return view(db,user,r)
