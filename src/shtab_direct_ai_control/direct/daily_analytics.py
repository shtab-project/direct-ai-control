"""Comparable daily windows; conversions are not CRM leads or proof of causality."""
from datetime import timedelta, date
import math
from fastapi import Depends,HTTPException
from sqlalchemy import select
from .db import Campaign,Membership,ChangeRequest
from .reports import get_dataset,today
from .domain import metrics
from .account_lifecycle import archived_ids


def percent(before,after):
    if not before or not after:return None
    result={}
    for k in ('cost','impressions','clicks','conversions','ctr','cpc','cpa'):
        a,b=before.get(k),after.get(k)
        if k in ('conversions','cpa') and (not before.get('conversions_known') or not after.get('conversions_known')):result[k]=None
        elif k=='cpa' and (not before.get('conversions') or not after.get('conversions')):result[k]=None
        else:result[k]=(b-a)/a*100 if a not in (0,None) and b is not None else None
    return result

def daily_report(db,campaigns,days,offset,end_date=None):
    end=end_date or today()-timedelta(days=offset)
    start=end-timedelta(days=days-1)
    datasets={c.account_id:get_dataset(db,c.account_id,'campaigns') for c in campaigns}
    mapping={(c.account_id,str(c.id.split(':')[-1])):c.id for c in campaigns}
    currencies={d.meta.get('currency','UNKNOWN') for d in datasets.values() if d}
    if len(currencies)>1: raise HTTPException(409,'Выберите аккаунт с одной валютой.')
    def window(first,last,target=None):
        full=bool(campaigns) and all(d and d.date_from<=first.isoformat() and d.date_to>=last.isoformat() for d in datasets.values())
        rows=[r for aid,d in datasets.items() if d for r in d.rows if (aid,str(r['remote_campaign_id'])) in mapping and (not target or mapping[(aid,str(r['remote_campaign_id']))]==target) and first.isoformat()<=r['date']<=last.isoformat()]
        summary={**metrics(rows),'conversions_known':all(r['conversions_known'] for r in rows)} if full else None
        return {'from':first.isoformat(),'to':last.isoformat(),'complete':full,'metrics':summary}
    current=window(start,end)
    previous=window(start-timedelta(days=days),start-timedelta(days=1))
    shift=math.ceil(days/7)*7
    weekly=window(start-timedelta(days=shift),end-timedelta(days=shift))
    daily=[]
    for i in range(days):
        d=start+timedelta(days=i)
        item=window(d,d)
        daily.append({'date':d.isoformat(),'metrics':item['metrics'],'complete':item['complete'],'provisional':(today()-d).days<=3})
    ids=[c.id for c in campaigns]
    changes=list(db.scalars(select(ChangeRequest).where(ChangeRequest.campaign_id.in_(ids),ChangeRequest.status=='applied').order_by(ChangeRequest.attempted_at))) if ids else []
    timeline=[]
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    for change in changes:
        applied=datetime.fromtimestamp(change.attempted_at,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date()
        comparisons=[]
        for length in (1,3,7,14):
            before=window(applied-timedelta(days=length),applied-timedelta(days=1),change.campaign_id)
            after=window(applied+timedelta(days=1),applied+timedelta(days=length),change.campaign_id)
            weekly_shift=math.ceil((length+1)/7)*7
            comparable=window(applied+timedelta(days=1-weekly_shift),applied+timedelta(days=length-weekly_shift),change.campaign_id)
            mature=applied+timedelta(days=length)<today()-timedelta(days=2)
            samples=[comparable.get('metrics'),after.get('metrics')]
            sufficient=bool(mature and comparable['complete'] and after['complete'] and all(m and m.get('clicks',0)>=30 for m in samples))
            conversion_sufficient=bool(sufficient and all(m.get('conversions_known') and m.get('conversions',0)>=10 for m in samples))
            comparisons.append({'insufficient_data':not sufficient,'conversion_data_sufficient':conversion_sufficient,'sample_rule':'Минимум 30 кликов в каждом полном периоде; для конверсий/CPA — минимум 10 конверсий. Это порог показа, не статистическое доказательство.','conclusion':'Данных недостаточно для вывода об эффективности.' if not sufficient else 'Доступно описательное сравнение; причинность не доказана.','days':length,'before':before,'same_weekdays_before':comparable,'after':after if mature else None,'mature':mature,'delta_percent':percent(comparable['metrics'],after['metrics']) if mature else None})
        timeline.append({'request_id':change.id,'campaign_id':change.campaign_id,'date':applied.isoformat(),'diff':change.payload['diff'],'comparisons':comparisons})
    delta=None
    if current['metrics'] and previous['metrics']:
        delta={k:current['metrics'][k]-previous['metrics'][k] for k in ('cost','clicks','impressions','conversions','ctr','cpc','cpa')}
        if not current['metrics']['conversions_known'] or not previous['metrics']['conversions_known']:
            for k in ('conversions','cpa'): delta[k]=None
    return {'source':'Яндекс Директ Reports API','timezone':'Europe/Moscow','currency':next(iter(currencies),'UNKNOWN'),'attribution':'LC · все доступные цели Директа','include_vat':False,
            'updated_at':min((d.updated_at for d in datasets.values() if d),default=None),'current':current,'previous':previous,'same_weekdays':weekly,'delta':delta,'delta_percent':percent(previous['metrics'],current['metrics']),'weekday_delta_percent':percent(weekly['metrics'],current['metrics']),'daily':daily,'timeline':timeline,
            'metrika_conversions':None,'leads':None,'qualified_leads':None,'crm_connected':False,'metrika_connected':False,
            'limitations':['Последние три дня могут уточняться. Отсутствие покрытия не означает нулевую статистику.','Конверсии Директа не являются подтверждёнными заявками или квалифицированными лидами CRM.','Сравнение периодов показывает связь во времени, а не доказанную причину изменения показателей.']}


def register(app):
    from .main import current_user,db_session
    @app.get('/api/analytics/daily')
    def report(project_id:str,days:int=7,offset:int=1,campaign_id:str|None=None,date_from:date|None=None,date_to:date|None=None,user=Depends(current_user),db=Depends(db_session)):
        if (date_from is None)!=(date_to is None):raise HTTPException(422,'Укажите обе даты.')
        if date_from:
            if date_to<date_from or date_to>today() or (date_to-date_from).days>365:raise HTTPException(422,'Неверный диапазон дат, максимум 366 дней.')
            days=(date_to-date_from).days+1
        if not 1<=days<=366 or offset not in (1,2): raise HTTPException(422,'Период: 1–366 дней, конец: вчера или позавчера.')
        if not db.get(Membership,(user.id,project_id)): raise HTTPException(404,'Проект недоступен.')
        q=select(Campaign).where(Campaign.project_id==project_id,Campaign.is_demo==False)
        if campaign_id: q=q.where(Campaign.id==campaign_id)
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        campaigns=[c for c in db.scalars(q) if c.id not in hidden]
        if campaign_id and not campaigns:raise HTTPException(404,'Кампания не принадлежит проекту.')
        result=daily_report(db,campaigns,days,offset,date_to)
        result['archived_account_ids']=sorted({c.account_id for c in campaigns}&archived_ids(db,user.id))
        result['archived']=bool(result['archived_account_ids'])
        return result
