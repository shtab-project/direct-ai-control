"""Read-only Yandex Metrika access through an owned OAuth connection."""
from datetime import date,timedelta
import math
import httpx
from fastapi import Depends,HTTPException
from .db import YandexConnection
from .account_lifecycle import connection_lease,ensure_active_account
from .yandex import access_token,DirectFailure

BASE='https://api-metrika.yandex.net'

def fetch(db,user,identifier,path,params=None):
    c=db.get(YandexConnection,identifier)
    if not c or c.user_id!=user.id:raise HTTPException(404,'Подключение недоступно.')
    if c.status=='disconnected' or not c.encrypted_token or not c.account_id:raise HTTPException(410,'Аккаунт отключён.')
    ensure_active_account(db,user.id,c.account_id)
    try:
        with connection_lease(c.id,user.id):
            token=access_token(db,c)
            with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:
                response=client.get(BASE+path,headers={'Authorization':'OAuth '+token},params=params)
        if response.status_code in (401,403):raise HTTPException(403,'Нет доступа к Метрике. Добавьте разрешение metrika:read в OAuth-приложении и повторно подключите этот аккаунт.')
        if response.status_code!=200:raise HTTPException(502,f'Метрика вернула HTTP {response.status_code}.')
        result=response.json()
        if not isinstance(result,dict) or result.get('errors'):raise HTTPException(502,'Метрика вернула неподтверждённый ответ.')
        return result
    except (httpx.HTTPError,ValueError,DirectFailure):raise HTTPException(502,'Метрика недоступна или ответ некорректен.') from None

def counters(db,user,connection_id):
    data=fetch(db,user,connection_id,'/management/v1/counters',{'per_page':100})
    rows=data.get('counters')
    if not isinstance(rows,list):raise HTTPException(502,'Метрика не вернула список счётчиков.')
    return [{'id':x['id'],'name':str(x.get('name',''))[:200],'site':str(x.get('site',''))[:250]} for x in rows if isinstance(x,dict) and type(x.get('id')) is int and x['id']>0]

def register(app):
    from .main import current_user,db_session,permitted
    @app.get('/api/metrika/counters')
    def get_counters(connection_id:str,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');return {'source':'Yandex Metrika API','counters':counters(db,user,connection_id)}
    @app.get('/api/metrika/goals')
    def get_goals(connection_id:str,counter_id:int,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage')
        if not any(x['id']==counter_id for x in counters(db,user,connection_id)):raise HTTPException(404,'Счётчик недоступен.')
        data=fetch(db,user,connection_id,f'/management/v1/counter/{counter_id}/goals')
        goals=data.get('goals')
        if not isinstance(goals,list):raise HTTPException(502,'Метрика не вернула список целей.')
        return {'source':'Yandex Metrika API','counter_id':counter_id,'goals':[{'id':x['id'],'name':str(x.get('name',''))[:255],'type':str(x.get('type',''))[:80]} for x in goals if isinstance(x,dict) and type(x.get('id')) is int and x['id']>0]}
    @app.get('/api/metrika/report')
    def report(connection_id:str,counter_id:int,date_from:date,date_to:date,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage')
        if date_from>date_to or date_to>date.today() or date_to-date_from>timedelta(days=365):raise HTTPException(422,'Проверьте период отчёта.')
        if not any(x['id']==counter_id for x in counters(db,user,connection_id)):raise HTTPException(404,'Счётчик недоступен.')
        data=fetch(db,user,connection_id,'/stat/v1/data',{'ids':str(counter_id),'metrics':'ym:s:visits,ym:s:users,ym:s:bounceRate','date1':date_from.isoformat(),'date2':date_to.isoformat(),'accuracy':'full'})
        totals=data.get('totals')
        if not isinstance(totals,list) or len(totals)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) for v in totals):raise HTTPException(502,'Метрика не вернула подтверждённые показатели.')
        sampled=data.get('sampled');sampled=sampled if type(sampled) is bool else None
        return {'source':'Yandex Metrika API','connection_id':connection_id,'counter_id':counter_id,'date_from':date_from.isoformat(),'date_to':date_to.isoformat(),'visits':totals[0],'users':totals[1],'bounce_rate':totals[2],'sampled':sampled,'note':'Данные по счётчику сайта, не по выбранной кампании Директа.'}
