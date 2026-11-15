"""Read-only Yandex Reports API. No sleeping workers: persistent jobs honor retryIn.

The UI polls while open. A pending job can be resumed after reload/restart.
"""
import csv
import hashlib
import io
import math
import time
from typing import Literal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, ConfigDict, model_validator
from sqlalchemy import select, update
from .db import ReportDataset, ReportJob, YandexConnection, Campaign, Membership, ProjectAccount, Account, Audit, DailyMetricSnapshot, now
from .domain import metrics
from .yandex import connected_mode, access_token, DirectFailure, direct_get
from .account_lifecycle import guarded,credential_http,ensure_active_account

PAGE_SIZE = 5000
MAX_ROWS = 20_000
FIELDS = ['Date', 'CampaignId', 'Impressions', 'Clicks', 'Cost', 'Conversions']
DIMENSIONS={'campaigns':[], 'queries':['AdGroupId','Query'], 'devices':['Device'], 'geography':['LocationOfPresenceId','LocationOfPresenceName'], 'keywords':['AdGroupId','CriterionId','Criterion'], 'ads':['AdGroupId','AdId']}
REPORT_TYPES={'campaigns':'CAMPAIGN_PERFORMANCE_REPORT','queries':'SEARCH_QUERY_PERFORMANCE_REPORT','devices':'CUSTOM_REPORT','geography':'CUSTOM_REPORT','keywords':'CRITERIA_PERFORMANCE_REPORT','ads':'AD_PERFORMANCE_REPORT'}

def today():
    return datetime.now(ZoneInfo('Europe/Moscow')).date()

def job_view(j):
    return {'id': j.id, 'connection_id':j.connection_id, 'kind': j.kind, 'status': j.status, 'error': j.error, 'retry_in': max(1, math.ceil(j.next_poll-time.time())), 'loaded_rows': len(j.rows), 'attempts': j.attempts, 'created_at': j.created_at}

def request_payload(kind, date_from, date_to, identifier):
    fields = FIELDS + DIMENSIONS[kind]
    # Direct rejects explicit AttributionModels without Goals (live error 4000).
    # With no selected goals use the documented default LC attribution.
    return {'params': {'SelectionCriteria': {'DateFrom': date_from, 'DateTo': date_to}, 'FieldNames': fields, 'ReportName': 'DirectAI_'+identifier+'_p0', 'ReportType': REPORT_TYPES[kind], 'DateRangeType': 'CUSTOM_DATE', 'Format': 'TSV', 'IncludeVAT': 'NO', 'Page': {'Limit': PAGE_SIZE, 'Offset': 0}, 'OrderBy': [{'Field': 'Date'}]}}

def read_report(token, payload, client_login):
    headers = {'Authorization': 'Bearer '+token, 'Accept-Language': 'ru', 'processingMode': 'auto', 'returnMoneyInMicros': 'false', 'skipReportHeader': 'true', 'skipColumnHeader': 'false', 'skipReportSummary': 'true'}
    if client_login:
        headers['Client-Login'] = client_login
    try:
        with credential_http(token), httpx.Client(timeout=40, follow_redirects=False) as client:
            response = client.post('https://api.direct.yandex.com/json/v5/reports', json=payload, headers=headers)
        if response.status_code in [201, 202, 429] or response.status_code >= 500:
            try:
                retry = int(response.headers.get('retryIn', response.headers.get('Retry-After', '10')))
            except ValueError:
                retry = 10
            return {'pending': True, 'retry_in': max(5, min(retry, 300)), 'http_status': response.status_code}
        if response.status_code in [401, 403]:
            raise DirectFailure('Нет доступа к отчётам. Проверьте OAuth и одобрение заявки API Директа.')
        if response.status_code != 200:
            try:
                code=str(response.json().get('error',{}).get('error_code',''))
                code=code if code.isdigit() and len(code)<=8 else 'unknown'
            except (ValueError,AttributeError): code='unknown'
            raise DirectFailure(f'Директ отклонил отчёт (HTTP {response.status_code}, код {code}). Проверьте совместимость полей и разрешения.')
        if len(response.content) > 20_000_000:
            raise DirectFailure('Ответ отчёта превышает 20 МБ. Сократите период.')
        return {'pending': False, 'text': response.text}
    except httpx.HTTPError:
        return {'pending': True, 'retry_in': 15, 'http_status': 0}

def parse_tsv(text, kind):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')), delimiter='\t')
    fields = reader.fieldnames or []
    conversions = next((x for x in fields if x == 'Conversions' or x.startswith('Conversions_')), None)
    required = {'Date', 'CampaignId', 'Impressions', 'Clicks', 'Cost'} | set(DIMENSIONS[kind])
    if not required.issubset(fields) or not conversions:
        raise DirectFailure('Отчёт вернул неожиданные столбцы. Старые данные сохранены.')
    rows = []
    def number(raw):
        if raw in [None, '', '--']:
            return None
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError('invalid metric')
        return value
    try:
        for row in reader:
            datetime.strptime(row['Date'], '%Y-%m-%d')
            rid = str(int(row['CampaignId']))
            clicks, impressions, cost = number(row['Clicks']), number(row['Impressions']), number(row['Cost'])
            if any(x is None for x in [clicks, impressions, cost]):
                raise ValueError('required metric missing')
            value = number(row[conversions])
            item = {'date': row['Date'], 'remote_campaign_id': rid, 'clicks': int(clicks), 'impressions': int(impressions), 'cost': cost, 'conversions': value if value is not None else 0, 'conversions_known': value is not None}
            if kind == 'queries':
                item.update(query=row['Query'][:1000], ad_group_id=row['AdGroupId'])
            item['dimensions']={f:row[f][:1000] for f in DIMENSIONS[kind]}
            rows.append(item)
            if len(rows) > PAGE_SIZE:
                raise ValueError('unexpected page size')
    except (ValueError, KeyError, TypeError):
        raise DirectFailure('Не удалось разобрать TSV. Неполный отчёт не заменяет прежние данные.') from None
    return rows

def get_dataset(db, account_id, kind):
    return db.scalar(select(ReportDataset).where(ReportDataset.account_id == account_id, ReportDataset.kind == kind))

def real_dashboard(db, campaigns, days):
    start, previous_start, end = today()-timedelta(days=days-1), today()-timedelta(days=days*2-1), today()
    datasets = {a: get_dataset(db, a, 'campaigns') for a in set(c.account_id for c in campaigns)}
    available = bool(campaigns) and all(datasets.values())
    id_map = {f'{c.account_id}:{c.id.split(":")[-1]}': c for c in campaigns}
    current, previous, currencies, updates = [], [], set(), []
    for account_id, dataset in datasets.items():
        if not dataset:
            continue
        currencies.add(dataset.meta.get('currency', 'UNKNOWN'))
        updates.append(dataset.updated_at)
        for row in dataset.rows:
            campaign = id_map.get(f'{account_id}:{row["remote_campaign_id"]}')
            if not campaign:
                continue
            enriched = {**row, 'campaign_id': campaign.id, 'campaign': campaign.name}
            if start.isoformat() <= row['date'] <= end.isoformat():
                current.append(enriched)
            elif previous_start.isoformat() <= row['date'] < start.isoformat():
                previous.append(enriched)
    if len(currencies) > 1:
        raise HTTPException(409, 'Нельзя суммировать разные валюты. Выберите один аккаунт.')
    full = available and all(d.date_from <= start.isoformat() and d.date_to >= end.isoformat() for d in datasets.values())
    previous_available = available and all(d.date_from <= previous_start.isoformat() and d.date_to >= (start-timedelta(days=1)).isoformat() for d in datasets.values())
    def summary(rows):
        return {**metrics(rows), 'conversions_known': all(r['conversions_known'] for r in rows)}
    dates = [(start+timedelta(days=i)).isoformat() for i in range(days)]
    return {'mode': 'connected', 'statistics_available': available, 'period_complete': full, 'previous_available': previous_available, 'currency': next(iter(currencies), 'UNKNOWN'), 'source': 'Яндекс Директ Reports API · НДС не включён', 'attribution': 'LC · последний переход · все доступные цели Директа; не объединяется с Метрикой', 'updated_at': min(updates) if updates else None, 'metrics': summary(current), 'previous': summary(previous), 'daily': [{'date': d, **summary([r for r in current if r['date'] == d])} for d in dates] if available else [], 'campaigns': [{'id': c.id, 'name': c.name, **summary([r for r in current if r['campaign_id'] == c.id])} for c in campaigns], 'note': 'Данные могут обновляться с задержкой. Для анализа причин используйте достаточную выборку; 0 конверсий не доказывает фрод.'}

class StartReports(BaseModel):
    model_config=ConfigDict(extra='forbid')
    connection_id: str
    days: int = Field(default=90, ge=1, le=366)
    date_from: str|None=None
    date_to: str|None=None
    kinds: list[Literal['campaigns','queries','devices','geography','keywords','ads']] = Field(default_factory=lambda:['campaigns','queries'],min_length=1,max_length=2)
    @model_validator(mode='after')
    def dates_valid(self):
        from .assistant_context import Selection
        Selection(days=self.days,date_from=self.date_from,date_to=self.date_to)
        return self


def own_connection(db,user,cid):
    c = db.scalar(select(YandexConnection).where(YandexConnection.id == cid, YandexConnection.user_id == user.id))
    if not c or not c.account_id:
        raise HTTPException(404, 'Сначала прочитайте кампании подключённого аккаунта.')
    if c.status=='disconnected':raise HTTPException(410,'Аккаунт отключён. Синхронизация запрещена.')
    ensure_active_account(db,user.id,c.account_id)
    membership = db.scalar(select(Membership).join(ProjectAccount, ProjectAccount.project_id == Membership.project_id).where(Membership.user_id == user.id, ProjectAccount.account_id == c.account_id))
    if not membership:
        raise HTTPException(403, 'Нет доступа к проекту аккаунта.')
    return c

@guarded('body')
def start_reports(body,user,db):
    from .main import permitted
    connected_mode()
    permitted(user, 'integration:manage')
    c = own_connection(db, user, body.connection_id)
    from .assistant_context import Selection
    date_from,date_to=Selection(days=body.days,date_from=body.date_from,date_to=body.date_to).dates()
    if len(set(body.kinds))!=len(body.kinds):
        raise HTTPException(422,'Типы отчётов не должны повторяться.')
    # Serialize job creation per connection to avoid filling Direct's report queue.
    db.execute(select(YandexConnection).where(YandexConnection.id == c.id).with_for_update())
    active = list(db.scalars(select(ReportJob).where(ReportJob.connection_id == c.id, ReportJob.status == 'pending',ReportJob.kind.in_(body.kinds))))
    if active:
        if any(j.payload['params']['SelectionCriteria']!={'DateFrom':date_from,'DateTo':date_to} for j in active):raise HTTPException(409,'Ожидается отчёт за другой период. Завершите его перед новым запуском.')
        return {'jobs': [job_view(j) for j in active]}
    if len(list(db.scalars(select(ReportJob).where(ReportJob.user_id == user.id, ReportJob.status == 'pending')))) + len(body.kinds) > 4:
        raise HTTPException(409, 'Завершите ожидающие отчёты перед загрузкой другого аккаунта.')
    jobs = []
    for kind in body.kinds:
        identifier = uuid4().hex
        j = ReportJob(id=identifier, connection_id=c.id, user_id=user.id, kind=kind, payload=request_payload(kind, date_from, date_to, identifier), rows=[])
        db.add(j)
        jobs.append(j)
    db.commit()
    return {'jobs': [job_view(j) for j in jobs]}

@guarded('job')
def poll_report(job_id,user,db):
    from .main import permitted
    connected_mode()
    permitted(user, 'integration:manage')
    j = db.get(ReportJob, job_id)
    if not j or j.user_id != user.id:
        raise HTTPException(404, 'Задание недоступно')
    c = own_connection(db, user, j.connection_id)
    if j.status != 'pending' or j.next_poll > time.time():
        return job_view(j)
    claimed = db.execute(update(ReportJob).where(ReportJob.id == job_id, ReportJob.status == 'pending', ReportJob.lease_until < time.time(), ReportJob.next_poll <= time.time()).values(lease_until=time.time()+60).returning(ReportJob.id)).scalar_one_or_none()
    db.commit()
    if not claimed:
        return job_view(j)
    try:
        token = access_token(db, c)
        result = read_report(token, j.payload, c.client_login)
        j.attempts += 1
        if result['pending']:
            j.next_poll = time.time()+result['retry_in']
            if j.attempts >= 60:
                j.status, j.error = 'error', 'Отчёт не готов после 60 попыток. Повторите загрузку позднее.'
        else:
            rows = parse_tsv(result['text'], j.kind)
            criteria = j.payload['params']['SelectionCriteria']
            if any(not criteria['DateFrom'] <= r['date'] <= criteria['DateTo'] for r in rows):
                raise DirectFailure('В отчёте обнаружены даты вне запрошенного периода.')
            combined = j.rows + rows
            if len(combined) > MAX_ROWS:
                raise DirectFailure('Больше 20 000 строк: сократите период. Предыдущие данные сохранены.')
            j.rows = combined
            if len(rows) == PAGE_SIZE:
                payload = {**j.payload, 'params': {**j.payload['params'], 'ReportName':'DirectAI_'+j.id+'_p'+str(j.payload['params']['Page']['Offset']+PAGE_SIZE), 'Page': {'Limit': PAGE_SIZE, 'Offset': j.payload['params']['Page']['Offset']+PAGE_SIZE}}}
                j.payload, j.next_poll, j.attempts = payload, time.time()+5, 0
            else:
                info = direct_get(token, 'clients', {'FieldNames': ['Currency']}, c.client_login)
                currency = next(iter(info.get('Clients', [])), {}).get('Currency') or 'UNKNOWN'
                dataset = get_dataset(db, c.account_id, j.kind)
                if not dataset:
                    dataset = ReportDataset(id=hashlib.sha256(f'{c.account_id}:{j.kind}'.encode()).hexdigest(), account_id=c.account_id, kind=j.kind, rows=[], meta={}, date_from=criteria['DateFrom'], date_to=criteria['DateTo'])
                    db.add(dataset)
                unique = {}
                for item in combined:
                    key = (item['date'], item['remote_campaign_id'], str(sorted(item.get('dimensions',{}).items())), item.get('ad_group_id',''), item.get('query',''))
                    unique[key] = item
                combined = list(unique.values())
                # A 7-day refresh must not erase an existing 90-day cache.
                # Merge only overlapping/adjacent coverage; never invent a gap.
                if dataset.date_from<=criteria['DateTo'] and dataset.date_to>=criteria['DateFrom']:
                    outside=[r for r in dataset.rows if not criteria['DateFrom']<=r['date']<=criteria['DateTo']]
                    if len(outside)+len(combined)>100000:raise DirectFailure('Объединённый кэш слишком велик. Прежняя история сохранена.')
                    cache_rows=outside+combined
                    cache_start,cache_end=min(dataset.date_from,criteria['DateFrom']),max(dataset.date_to,criteria['DateTo'])
                else:cache_rows,cache_start,cache_end=combined,criteria['DateFrom'],criteria['DateTo']
                dataset.rows, dataset.date_from, dataset.date_to, dataset.updated_at = cache_rows, cache_start, cache_end, now()
                dataset.meta = {'source': 'Yandex Direct Reports API', 'attribution': 'LC', 'goals': 'all_available', 'include_vat': False, 'currency': currency, 'job_id': j.id}
                if j.kind == 'campaigns':
                    for record in combined:
                        identifier=hashlib.sha256(f'{c.account_id}:{record["remote_campaign_id"]}:{record["date"]}'.encode()).hexdigest()
                        snapshot=db.get(DailyMetricSnapshot,identifier)
                        if not snapshot:
                            snapshot=DailyMetricSnapshot(id=identifier,account_id=c.account_id,remote_campaign_id=str(record['remote_campaign_id']),date=record['date'],data={},source={})
                            db.add(snapshot)
                        snapshot.data=dict(record); snapshot.source=dict(dataset.meta); snapshot.updated_at=now()
                j.status = 'completed'
                db.add(Audit(id=str(uuid4()), user_id=user.id, action='reports.imported', data={'job_id':j.id, 'kind':j.kind, 'rows':len(combined), 'account_id':c.account_id}))
        j.lease_until = 0
        db.commit()
    except DirectFailure as exc:
        db.rollback()
        j = db.get(ReportJob, job_id)
        j.status, j.error, j.lease_until = 'error', str(exc), 0
        db.commit()
    return job_view(j)

def register(app):
    from .main import current_user,db_session
    @app.post('/api/reports/jobs')
    def start(body:StartReports,user=Depends(current_user),db=Depends(db_session)):
        return start_reports(body,user,db)
    @app.get('/api/reports/jobs')
    def list_jobs(user=Depends(current_user),db=Depends(db_session)):
        return [job_view(j) for j in db.scalars(select(ReportJob).where(ReportJob.user_id==user.id).order_by(ReportJob.created_at.desc()).limit(20))]
    @app.post('/api/reports/jobs/{job_id}/poll')
    def poll(job_id:str,user=Depends(current_user),db=Depends(db_session)):
        return poll_report(job_id,user,db)
