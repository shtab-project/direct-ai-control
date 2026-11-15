"""Conservative read-only screening; never claims full linguistic matching."""
import re
from itertools import islice
from fastapi import HTTPException
from .db import RemoteCampaignMapping
from .audit_jobs import connection
from .yandex import direct_get,access_token,DirectResult,DirectFailure
from .reports import get_dataset


def words(text):
    return re.findall(r'[а-яa-z0-9]+',text.casefold().replace('ё','е'))


def potential(negative,text):
    needles=words(negative);hay=words(re.sub(r'(?<!\S)-\S+','',text))
    # Prefix overlap flags possible inflections; this is a warning, not a match guarantee.
    return bool(needles) and all(any(a==b or (len(a)>=4 and len(b)>=4 and a[:4]==b[:4]) for b in hay) for a in needles)


def assess(db,user,campaign,row,token=None):
    p=row.payload
    if p['operation']=='replace_campaign_negative_keywords':before,after=p['before'],p['after'];group=None
    elif p['operation']=='typed_nonfinancial' and p['spec']['operation']=='group_negatives':
        before=(p['before'].get('NegativeKeywords') or {}).get('Items',[]);after=p['spec']['items'];group=p['spec']['object_id']
    else:return {'requires_confirmation':False,'complete':True,'add':[],'remove':[]}
    added=[x for x in after if x not in before];removed=[x for x in before if x not in after]
    if not added:return {'requires_confirmation':False,'complete':True,'add':[],'remove':removed}
    mapping=db.get(RemoteCampaignMapping,campaign.id);conn=connection(db,user,campaign.account_id)
    if not mapping:raise HTTPException(409,'Нет подтверждённого ID кампании.')
    rows=[];offset=0;complete=True
    try:
        while len(rows)<10000:
            result=direct_get(token or access_token(db,conn),'keywords',{'SelectionCriteria':{'CampaignIds':[mapping.remote_id],**({'AdGroupIds':[group]} if group else {})},'FieldNames':['Id','CampaignId','AdGroupId','Keyword'],'Page':{'Limit':1000,'Offset':offset}},conn.client_login)
            if not isinstance(result,DirectResult):raise DirectFailure('Неподтверждённый источник.')
            page=result.get('Keywords',[])
            if not isinstance(page,list) or len(page)>1000 or not all(isinstance(x,dict) for x in page):raise DirectFailure('Некорректные ключевые фразы.')
            if not isinstance(result,DirectResult) or result.evidence.get('http_status')!=200 or any(x.get('CampaignId')!=mapping.remote_id or (group and x.get('AdGroupId')!=group) for x in page):raise DirectFailure('Не подтверждена область ключевых фраз.')
            rows+=page;limited=result.get('LimitedBy')
            if not limited:break
            if not isinstance(limited,int) or limited<=offset:raise DirectFailure('Некорректная пагинация.')
            offset=limited
        else:complete=False
    except (DirectFailure,KeyError,TypeError):raise HTTPException(502,'Проверка минус-фраз недоступна. Согласование заблокировано.') from None
    dataset=get_dataset(db,campaign.account_id,'queries')
    queries=[r for r in dataset.rows if str(r.get('remote_campaign_id'))==str(mapping.remote_id) and (not group or str(r.get('ad_group_id'))==str(group))] if dataset else []
    overlaps=list(islice(({'negative':n,'keyword_id':k['Id'],'keyword':k['Keyword'],'group_id':k.get('AdGroupId')} for n in added for k in rows if k.get('Keyword')!='---autotargeting' and potential(n,k.get('Keyword',''))),200))
    affected=list(islice(({'negative':n,'query':r.get('query',r.get('search_query',''))} for n in added for r in queries if potential(n,r.get('query',r.get('search_query','')))),200))
    return {'requires_confirmation':True,'complete':complete,'level':'group' if group else 'campaign','group_id':group,'add':added,'remove':removed,'keyword_overlaps':overlaps,'potentially_affected_queries':affected,'keywords_checked':len(rows),'query_rows_available':len(queries),'limitations':['Проверка консервативная: формы слов и операторы требуют ручной проверки.','История запросов ограничена загруженными отчётами; будущие запросы неизвестны.','Автотаргетинг не представлен полным списком запросов.']}


def guard(db,user,campaign,row,confirmation,token=None):
    from .approvals import digest,audit
    report=assess(db,user,campaign,row,token)
    if not report.get('complete'):raise HTTPException(409,'Список ключей неполный. Применение минус-фраз заблокировано.')
    if report.get('requires_confirmation') and confirmation!=digest(report):raise HTTPException(409,{'message':'Проверьте риски и отдельно подтвердите добавляемые минус-фразы.','safety':report,'confirmation':digest(report)})
    audit(db,user,'ai.negative_safety_checked',row,{'report':report})
    return report
