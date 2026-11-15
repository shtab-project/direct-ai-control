"""Resumable Direct read collector. No advertising write method is reachable here."""
import hashlib,json,time
from types import SimpleNamespace
from uuid import uuid4
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from sqlalchemy import select,update
from .db import AssistantJob,YandexConnection,RemoteCampaignMapping,ReportJob,now
from .assistant_context import Selection,scope,context,redact_long
from .assistant_jobs import active,job_view
from .assistant_provider import generate,verify
from .ai import ensure_enabled,configuration,project_access
from .yandex import access_token,direct_get,DirectResult,DirectFailure
from .account_lifecycle import connection_lease
from .reports import start_reports,poll_report,StartReports,get_dataset
from .domain import metrics

SCENARIOS={'full':'Полный аудит рекламы','queries':'Анализ поисковых запросов','negatives':'Поиск минус-фраз','ads':'Улучшение объявлений','leads':'Анализ стоимости заявок','traffic':'Анализ подозрительного трафика','strategies':'Сравнение рекламных стратегий','changes':'Проверка эффективности последних изменений','temir':'Темир Гидро 8.0'}
SPECS={
 'campaigns':('Campaigns',{'FieldNames':['Id','Name','Type','State','Status','NegativeKeywords','TimeTargeting','TimeZone'],'TextCampaignFieldNames':['BiddingStrategy'],'UnifiedCampaignFieldNames':['BiddingStrategy']}),
 'adgroups':('AdGroups',{'FieldNames':['Id','CampaignId','Name','RegionIds','NegativeKeywords','Status','ServingStatus','Type']}),
 'keywords':('Keywords',{'FieldNames':['Id','CampaignId','AdGroupId','Keyword','State','Status','ServingStatus']}),
 'ads':('Ads',{'FieldNames':['Id','CampaignId','AdGroupId','Type','State','Status'],'TextAdFieldNames':['Title','Title2','Text','Href']})}

class StartAudit(BaseModel):
    model_config=ConfigDict(extra='forbid')
    project_id:str
    selection:Selection=Field(default_factory=Selection)
    scenario:Literal['full','queries','negatives','ads','leads','traffic','strategies','changes','temir']='full'
    request_id:str=Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')

def connection(db,user,account,connection_id=None):
    query=select(YandexConnection).where(YandexConnection.user_id==user.id,YandexConnection.account_id==account,YandexConnection.status!='disconnected',YandexConnection.encrypted_token!='')
    if connection_id:query=query.where(YandexConnection.id==connection_id)
    c=db.scalar(query.order_by(YandexConnection.updated_at.desc()))
    if not c:raise HTTPException(410,'Нет активного OAuth-подключения выбранного аккаунта.')
    return c

def inventory_page(db,user,task):
    c=connection(db,user,task['account_id'],task.get('connection_id'));service=task['service'];key,fields=SPECS[service]
    criteria={'Ids' if service=='campaigns' else 'CampaignIds':task['ids']}
    with connection_lease(c.id,user.id):
        result=direct_get(access_token(db,c),service,{'SelectionCriteria':criteria,**fields,'Page':{'Limit':1000,'Offset':task.get('offset',0)}},c.client_login)
    if not isinstance(result,DirectResult) or result.evidence.get('http_status')!=200:raise DirectFailure('Не подтверждено происхождение ответа Директа.')
    rows=result[key]
    if len(rows)>1000 or any(not isinstance(r,dict) or r.get('Id' if service=='campaigns' else 'CampaignId') not in task['ids'] for r in rows):raise DirectFailure('Директ вернул объекты вне выбранной кампании.')
    return rows,result.get('LimitedBy'),result.evidence

def wait(db,job,state,stage,delay=1):
    # Disconnect can race the read, but must never resurrect a cancelled job.
    changed=db.execute(update(AssistantJob).where(AssistantJob.id==job.id,AssistantJob.status.in_(['collecting','waiting_reports','pending'])).values(source=state,status='waiting_reports',stage=stage,lease_until=0,next_poll=time.time()+delay,updated_at=now()).returning(AssistantJob.id)).scalar_one_or_none()
    db.commit();return bool(changed)

def process_audit(db,job,user):
    ensure_enabled();active(db,user,job)
    sel=Selection.model_validate(job.payload['selection']);_,campaigns=scope(db,user,job.project_id,sel)
    state=dict(job.source or {});accounts=sorted({c.account_id for c in campaigns})
    if not state:
        tasks=[]
        for aid in accounts:
            ids=[]
            for c in campaigns:
                if c.account_id!=aid:continue
                m=db.get(RemoteCampaignMapping,c.id)
                if not m:raise HTTPException(409,'Отсутствует подтверждённый ID кампании Директа.')
                ids.append(m.remote_id)
            for service in SPECS:
                for i in range(0,len(ids),10):tasks.append({'account_id':aid,'connection_id':sel.connection_id,'service':service,'ids':ids[i:i+10],'offset':0})
        state={'tasks':tasks,'cursor':0,'inventory':{},'receipts':[],'unavailable':{},'reports':{},'report_cursor':0}
    if state['cursor']<len(state['tasks']):
        task=state['tasks'][state['cursor']];bucket=task['account_id']+':'+task['service']
        try:
            rows,limited,receipt=inventory_page(db,user,task)
            stored=state['inventory'].get(bucket,[]);remaining=10000-len(stored)
            state['inventory'][bucket]=stored+rows[:remaining];state['receipts'].append(receipt)
            if limited and remaining>len(rows) and isinstance(limited,int) and limited>task['offset']:
                task={**task,'offset':limited};state['tasks'][state['cursor']]=task
            else:
                if limited:state['unavailable'][bucket]='Достигнут предел 10 000 объектов; выборка неполная.'
                state['cursor']+=1
        except DirectFailure as exc:
            state['unavailable'][bucket]=str(exc);state['cursor']+=1
        wait(db,job,state,f"Структура кампаний: {state['cursor']}/{len(state['tasks'])}");return
    report_tasks=[(aid,kind) for aid in accounts for kind in ('campaigns','queries','devices','geography','keywords','ads')]
    if state['report_cursor']<len(report_tasks):
        aid,kind=report_tasks[state['report_cursor']];key=aid+':'+kind
        try:
            cid=connection(db,user,aid,sel.connection_id).id
            if key not in state['reports']:
                start,end=sel.dates()
                created=start_reports(StartReports(connection_id=cid,date_from=start,date_to=end,kinds=[kind]),user,db)
                state['reports'][key]={'id':created['jobs'][0]['id']}
            r=poll_report(state['reports'][key]['id'],user,db)
            if r['status']=='pending':
                wait(db,job,state,f"Загрузка {kind}: {r['loaded_rows']} строк",max(2,r['retry_in']));return
            state['reports'][key].update(status=r['status'],rows=r['loaded_rows'])
            if r['status']!='completed':state['unavailable'][key]=r['error'] or 'Отчёт не завершён.'
            state['report_cursor']+=1
        except (DirectFailure,HTTPException) as exc:
            if isinstance(exc,HTTPException) and exc.status_code in (403,404,410):raise
            state['unavailable'][key]=str(exc)[:500];state['report_cursor']+=1
        wait(db,job,state,f"Отчёты: {state['report_cursor']}/{len(report_tasks)}");return
    payload,source=context(db,user,job.project_id,sel)
    source['collector']={'source':'Yandex Direct API v5 · реальные ответы','receipts':state['receipts'],'reports':state['reports'],'unavailable':state['unavailable'],'inventory_counts':{k:len(v) for k,v in state['inventory'].items()}}
    source['limitations']+=['CRM, Метрика и данные поведения не подключены. Проверка бот-заявок и качества лидов недоступна.','Содержимое посадочных страниц не загружалось. Тексты и ссылки позволяют сформировать гипотезы, но не оценить конверсию сайта.','Срез настроек текущий, статистика относится к выбранным датам. Объекты и сегменты передаются AI ограниченной выборкой.']
    for b,items in state['inventory'].items():
        service=b.split(':')[-1]
        for i,item in enumerate(items[:80]):
            # Strings are data, not instructions. URLs retained for landing page hypotheses.
            source['evidence'][f"{b}_{i}"]={'kind':service,'data':item}
    start,end=sel.dates()
    for aid in accounts:
        ids={str(db.get(RemoteCampaignMapping,c.id).remote_id) for c in campaigns if c.account_id==aid}
        for kind in ('devices','geography','keywords','ads'):
            d=get_dataset(db,aid,kind)
            if not d:continue
            groups={}
            for r in d.rows:
                if str(r['remote_campaign_id']) not in ids or not start<=r['date']<=end:continue
                k=json.dumps(r.get('dimensions',{}),sort_keys=True,ensure_ascii=False)
                groups.setdefault(k,[]).append(r)
            for i,(dims,rows) in enumerate(sorted(groups.items(),key=lambda x:-sum(r['cost'] for r in x[1]))[:80]):
                m=metrics(rows)
                if not all(r['conversions_known'] for r in rows):m.update(conversions=None,cpa=None)
                elif not m['conversions']:m['cpa']=None
                source['evidence'][f'{aid}_{kind}_{i}']={'kind':kind,'data':{**json.loads(dims),**m},'date_from':d.date_from,'date_to':d.date_to}
    payload['source']=source;payload['task']=SCENARIOS[job.payload['scenario']]
    if job.payload['scenario']=='temir':payload['business_goal']='Проверь актуальный ассортимент по бизнес-контексту. Промышленные ножничные/мачтовые/гидравлические подъёмники, производство под ТЗ — ориентиры, не подтверждённый каталог. Цель: реальные коммерческие заявки без увеличения бюджета. Бот-фрод не утверждать без CRM/поведения. Сравни стратегии только аналитически; если данных мало — план контролируемого эксперимента.'
    # Include only classified historical changes, never execute them.
    from .db import ChangeRequest
    payload['recent_changes']=[{'id':x.id,'result':x.result,'diff':x.payload.get('diff')} for x in db.scalars(select(ChangeRequest).where(ChangeRequest.user_id==user.id,ChangeRequest.campaign_id.in_([c.id for c in campaigns]),ChangeRequest.status=='applied').order_by(ChangeRequest.created_at.desc()).limit(10))]
    active(db,user,job)
    changed=db.execute(update(AssistantJob).where(AssistantJob.id==job.id,AssistantJob.status.in_(['collecting','waiting_reports'])).values(status='generating',stage='AI анализирует собранные данные',source=source,lease_until=time.time()+130,updated_at=now()).returning(AssistantJob.id)).scalar_one_or_none()
    db.commit()
    if not changed:return
    # Limit data volume honestly; never silently drop source evidence after generation.
    if len(json.dumps(payload,ensure_ascii=False).encode())>420000:raise HTTPException(422,'Объём аудита превышает лимит. Выберите одну кампанию.')
    answer,usage=generate(configuration(db,user.id),payload);active(db,user,job);verify(answer,source)
    saved=db.execute(update(AssistantJob).where(AssistantJob.id==job.id,AssistantJob.status=='generating').values(status='completed',stage='Аудит сохранён',result={'answer':answer,'source':source,'usage':usage},lease_until=0,updated_at=now()).returning(AssistantJob.id)).scalar_one_or_none()
    if saved:
        from .proposals import save_recommendations
        save_recommendations(db,user,job,answer,source)
    db.commit()

def register(app):
    from .main import current_user,db_session,permitted
    @app.get('/api/ai/audits')
    def listing(project_id:str,user=Depends(current_user),db=Depends(db_session)):
        project_access(db,user,project_id)
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        return [job_view(j,db,user) for j in db.scalars(select(AssistantJob).where(AssistantJob.user_id==user.id,AssistantJob.project_id==project_id,AssistantJob.kind=='audit').order_by(AssistantJob.created_at.desc()).limit(30)) if j.payload.get('selection',{}).get('campaign_id') not in hidden and not (set((j.result or {}).get('source',{}).get('campaign_ids',[]))&hidden)]
    @app.post('/api/ai/audits')
    def start(body:StartAudit,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');ensure_enabled()
        _,campaigns=scope(db,user,body.project_id,body.selection)
        if len({c.account_id for c in campaigns})!=1:raise HTTPException(422,'Для полного аудита выберите один рекламный аккаунт.')
        if body.scenario=='temir' and not any('темир гидро 8.0' in ' '.join(c.name.casefold().split()) for c in campaigns):raise HTTPException(422,'Кампания «Темир Гидро 8.0» не найдена в выбранном наборе. Выберите её явно.')
        key=hashlib.sha256(f'{user.id}:audit:{body.request_id}'.encode()).hexdigest();existing=db.get(AssistantJob,key)
        if existing:return job_view(existing)
        account_id=campaigns[0].account_id
        chosen=connection(db,user,account_id,body.selection.connection_id)
        # Freeze the credential choice: reconnecting or adding another connection
        # must not silently redirect an already queued audit to another login.
        selection=body.selection.model_copy(update={'connection_id':chosen.id,'account_id':account_id})
        row=AssistantJob(id=key,user_id=user.id,project_id=body.project_id,kind='audit',payload={'selection':selection.model_dump(mode='json'),'scenario':body.scenario,'account_ids':[account_id]})
        db.add(row);db.commit();return job_view(row)
    @app.get('/api/ai/audits/{identifier}/export')
    def export(identifier:str,format:str='md',user=Depends(current_user),db=Depends(db_session)):
        row=db.get(AssistantJob,identifier)
        if not row or row.kind!='audit' or row.user_id!=user.id:raise HTTPException(404,'Аудит недоступен.')
        project_access(db,user,row.project_id)
        if row.status!='completed':raise HTTPException(409,'Аудит ещё не завершён.')
        from .chat import export_answer
        return export_answer(SimpleNamespace(id=row.id,content=row.result['answer']['answer_markdown'],source={**row.result['source'],'answer':row.result['answer']}),format)
