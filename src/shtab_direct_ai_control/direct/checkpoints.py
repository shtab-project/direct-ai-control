"""Read-only 3/7/14-day checkpoints. No causal claim or advertising execution."""
import hashlib,time
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from fastapi import Depends,HTTPException
from sqlalchemy import select
from .db import Session,PerformanceCheckpoint,ChangeRequest,Campaign,User,now,ReportJob,Membership
from .reports import today,StartReports,start_reports,poll_report
from .audit_jobs import connection
from .account_lifecycle import archived
from .daily_analytics import daily_report

last_tick=0
def schedule_due():
    global last_tick
    if time.time()-last_tick<30:return
    last_tick=time.time()
    with Session() as db:
        changes=list(db.scalars(select(ChangeRequest).where(ChangeRequest.status=='applied').order_by(ChangeRequest.attempted_at.desc()).limit(500)))
        for change in changes:
            applied=datetime.fromtimestamp(change.attempted_at,tz=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date()
            for length in (3,7,14):
                key=hashlib.sha256(f'{change.id}:{length}'.encode()).hexdigest()
                if not db.get(PerformanceCheckpoint,key):db.add(PerformanceCheckpoint(id=key,user_id=change.user_id,change_id=change.id,days=length,due=(applied+timedelta(days=length)).isoformat(),data={'note':'Контрольная точка не доказывает причинность. Последние 3 дня предварительные.'}))
        db.commit()
        candidates=list(db.scalars(select(PerformanceCheckpoint).where(PerformanceCheckpoint.status.in_(['pending','waiting_reports','provisional']),PerformanceCheckpoint.due<=today().isoformat()).order_by(PerformanceCheckpoint.due).limit(50)))
        item=next((c for c in candidates if c.data.get('next_poll',0)<=time.time()),None)
        if not item:return
        change=db.get(ChangeRequest,item.change_id);campaign=db.get(Campaign,change.campaign_id);user=db.get(User,item.user_id)
        if not user or not db.get(Membership,(user.id,campaign.project_id)) or archived(db,user.id,campaign.account_id):
            item.status='archived';db.commit();return
        if (today()-datetime.fromtimestamp(change.attempted_at,tz=timezone.utc).date()).days>65:
            item.status='insufficient_history';item.data={**item.data,'error':'Для старого изменения автоматический 90-дневный отчёт не покрывает сравнение. Выберите даты вручную.'};db.commit();return
        try:
            conn=connection(db,user,campaign.account_id);data=dict(item.data)
            job=db.get(ReportJob,data.get('report_id',''))
            if not job or job.status in ('completed','error','cancelled'):
                start=(today()-timedelta(days=89)).isoformat();end=today().isoformat()
                created=start_reports(StartReports(connection_id=conn.id,date_from=start,date_to=end,kinds=['campaigns']),user,db)
                data['report_id']=created['jobs'][0]['id']
            result=poll_report(data['report_id'],user,db)
            data['next_poll']=time.time()+max(30,result['retry_in'])
            if result['status']=='completed':
                report=daily_report(db,[campaign],30,1)
                timeline=next((x for x in report['timeline'] if x['request_id']==change.id),None)
                comparison=next((x for x in timeline['comparisons'] if x['days']==item.days),None) if timeline else None
                data.update(comparison=comparison,source=report['source'],currency=report['currency'],attribution=report['attribution'],factors=['Сезонность','Дни недели','Изменение спроса и аукциона','Задержка конверсий','Параллельные изменения рекламы/сайта'],leads=None,qualified_leads=None,next_poll=time.time()+21600)
                item.status='completed' if comparison and comparison['mature'] and comparison['after']['complete'] and comparison['same_weekdays_before']['complete'] else 'provisional'
            elif result['status']=='pending':item.status='waiting_reports'
            else:data.update(error='Отчёт Директа не завершён; показатели не подставлялись.',next_poll=time.time()+3600);item.status='pending'
            item.data=data;item.updated_at=now();db.commit()
        except HTTPException as exc:
            db.rollback();db.refresh(item);item.data={**item.data,'error':'Чтение временно недоступно.' if exc.status_code not in (403,404,410) else 'Нет доступа или аккаунт отключён.','next_poll':time.time()+3600};item.status='archived' if exc.status_code in (403,404,410) else 'pending';db.commit()

def register(app):
    from .main import current_user,db_session
    @app.get('/api/ai-performance/checkpoints')
    def listing(project_id:str,user=Depends(current_user),db=Depends(db_session)):
        if not db.get(Membership,(user.id,project_id)):raise HTTPException(404,'Проект недоступен.')
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        rows=db.scalars(select(PerformanceCheckpoint).join(ChangeRequest,ChangeRequest.id==PerformanceCheckpoint.change_id).join(Campaign,Campaign.id==ChangeRequest.campaign_id).where(PerformanceCheckpoint.user_id==user.id,Campaign.project_id==project_id,Campaign.id.not_in(hidden)).order_by(PerformanceCheckpoint.due.desc()).limit(100))
        return [{k:getattr(r,k) for k in ('id','change_id','days','due','status','data','updated_at')} for r in rows]
