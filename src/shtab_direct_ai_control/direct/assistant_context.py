"""Authorized facts for chat/audit; missing statistics never become fake zeros."""
from datetime import date,timedelta
from uuid import uuid4
from pydantic import BaseModel,ConfigDict,Field,model_validator
from fastapi import HTTPException
from sqlalchemy import select
from .db import BusinessContext,Campaign,RemoteCampaignMapping,DirectCampaignInfo,YandexConnection
from .account_lifecycle import archived_ids
from .ai import project_access,redact
from .reports import today,get_dataset
from .domain import metrics

class Selection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    connection_id:str|None=None
    account_id:str|None=None
    campaign_id:str|None=None
    days:int=Field(default=30,ge=1,le=366)
    date_from:date|None=None
    date_to:date|None=None
    @model_validator(mode='after')
    def range(self):
        if bool(self.date_from)!=bool(self.date_to):raise ValueError('Задайте обе даты.')
        if self.date_from and (self.date_from>self.date_to or (self.date_to-self.date_from).days>365 or self.date_to>today()):raise ValueError('Диапазон должен быть в прошлом и не превышать 366 дней.')
        return self
    def dates(self):
        end=self.date_to or today()
        return (self.date_from or end-timedelta(days=self.days-1)).isoformat(),end.isoformat()

def redact_long(value):
    # Reuse the established privacy rules without the legacy 2000-char truncation.
    import re
    value=re.sub(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}','[email скрыт]',value)
    value=re.sub(r'(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)','[телефон скрыт]',value)
    return re.sub(r'https?://\S+','[URL скрыт]',value)

def scope(db,user,project_id,selection):
    project=project_access(db,user,project_id)
    rows=list(db.scalars(select(Campaign).where(Campaign.project_id==project_id,Campaign.is_demo==False)))
    if selection.campaign_id and not any(c.id==selection.campaign_id for c in rows):raise HTTPException(404,'Кампания не принадлежит проекту.')
    if selection.account_id and not any(c.account_id==selection.account_id for c in rows):raise HTTPException(404,'Аккаунт не принадлежит проекту.')
    connection=None
    if selection.connection_id:
        connection=db.get(YandexConnection,selection.connection_id)
        if not connection or connection.user_id!=user.id:raise HTTPException(404,'Подключение недоступно.')
        if connection.status=='disconnected' or not connection.encrypted_token:raise HTTPException(410,'Подключение отключено.')
        if not connection.account_id or not any(c.account_id==connection.account_id for c in rows):raise HTTPException(404,'Подключение не принадлежит проекту.')
        if selection.account_id and selection.account_id!=connection.account_id:raise HTTPException(422,'Подключение и аккаунт не совпадают.')
        if selection.campaign_id and next(c for c in rows if c.id==selection.campaign_id).account_id!=connection.account_id:raise HTTPException(422,'Кампания не принадлежит выбранному подключению.')
    removed=archived_ids(db,user.id)
    if selection.campaign_id and next(c for c in rows if c.id==selection.campaign_id).account_id in removed:raise HTTPException(410,'Выбранный аккаунт отключён.')
    if selection.account_id in removed:raise HTTPException(410,'Выбранный аккаунт отключён.')
    selected=[c for c in rows if c.account_id not in removed and (not connection or c.account_id==connection.account_id) and (not selection.account_id or c.account_id==selection.account_id) and (not selection.campaign_id or c.id==selection.campaign_id)]
    if not selected:raise HTTPException(410 if rows else 409,'Нет доступных активных кампаний. Архив не отправляется AI.')
    return project,selected

def business_context(db,project):
    row=db.get(BusinessContext,project.id)
    return {'content':row.content if row else project.description,'revision':row.revision if row else 0,'updated_at':row.updated_at if row else None}

def context(db,user,project_id,selection):
    project,campaigns=scope(db,user,project_id,selection)
    start,end=selection.dates();accounts={c.account_id for c in campaigns}
    idmap={(c.account_id,str(db.get(RemoteCampaignMapping,c.id).remote_id if db.get(RemoteCampaignMapping,c.id) else c.id.split(':')[-1])):c for c in campaigns}
    datasets={a:get_dataset(db,a,'campaigns') for a in accounts};querysets={a:get_dataset(db,a,'queries') for a in accounts}
    rows=[];queryrows=[];currencies=set();coverage=[]
    for aid,d in datasets.items():
        if d:
            currencies.add(d.meta.get('currency','UNKNOWN'))
            coverage.append({'account_id':aid,'from':d.date_from,'to':d.date_to,'updated_at':d.updated_at})
            rows.extend({**r,'campaign_id':idmap[(aid,str(r['remote_campaign_id']))].id} for r in d.rows if (aid,str(r['remote_campaign_id'])) in idmap and start<=r['date']<=end)
        q=querysets[aid]
        if q:queryrows.extend({**r,'campaign_id':idmap[(aid,str(r['remote_campaign_id']))].id} for r in q.rows if (aid,str(r['remote_campaign_id'])) in idmap and start<=r['date']<=end)
    if len(currencies)>1:raise HTTPException(409,'Выберите аккаунт с одной валютой.')
    complete=all(d and d.date_from<=start and d.date_to>=end for d in datasets.values())
    available=bool(datasets) and all(d and d.date_from<=end and d.date_to>=start for d in datasets.values())
    def summary(records):
        if not available:return {k:None for k in ('cost','impressions','clicks','conversions','ctr','cpc','cpa')}
        m=metrics(records)
        if not all(r['conversions_known'] for r in records):m['conversions']=None;m['cpa']=None
        elif not m['conversions']:m['cpa']=None
        return m
    evidence={'totals':{'kind':'metrics','data':summary(rows),'coverage_complete':complete,'label':'Показатели доступной части периода' if not complete else 'Показатели полного периода'}}
    for i,c in enumerate(campaigns[:100]):
        info=db.get(DirectCampaignInfo,c.id);data={'campaign':redact(c.name),'negative_keywords':c.phrases[:300],**summary([r for r in rows if r['campaign_id']==c.id])}
        evidence[f'c{i+1}']={'kind':'campaign','campaign_id':c.id,'data':data,'negative_keywords_updated_at':info.phrases_read_at if info else None}
    grouped={}
    for r in queryrows:grouped.setdefault((r['campaign_id'],r.get('ad_group_id',''),r['query']),[]).append(r)
    for i,((cid,group,query),records) in enumerate(sorted(grouped.items(),key=lambda x:-sum(r['cost'] for r in x[1]))[:120]):
        evidence[f'q{i+1}']={'kind':'query','campaign_id':cid,'data':{'query':redact(query),'ad_group_id':group,**summary(records)}}
    evidence['unavailable']={'kind':'availability','data':{'leads':None,'qualified_leads':None,'metrika_conversions':None},'label':'CRM/телефония/Метрика не подключены'}
    limitations=['Конверсии Директа не являются подтверждёнными или качественными лидами.','Поисковые запросы — ограниченная выборка. Причины изменений и бот-заявки не доказаны агрегированными данными.']
    if not complete:limitations.append('Запрошенный период покрыт не полностью. Числа относятся только к доступным строкам; нельзя считать их полным итогом.')
    if not queryrows:limitations.append('Поисковые запросы за выбранный период недоступны или отчёт пуст.')
    source={'connection_id':selection.connection_id,'account_ids':sorted(accounts),'campaign_ids':[c.id for c in campaigns],'project_id':project_id,'date_from':start,'date_to':end,'currency':next(iter(currencies),'UNKNOWN'),'timezone':'Europe/Moscow','attribution':'LC · все доступные цели Директа','coverage':coverage,'period_complete':complete,'statistics_available':available,'evidence':evidence,'limitations':limitations,'query_rows_available':len(queryrows),'query_groups_included':min(len(grouped),120)}
    payload={'project':{'name':redact(project.name),'business_context':redact_long(business_context(db,project)['content'])},'source':source}
    return payload,source
