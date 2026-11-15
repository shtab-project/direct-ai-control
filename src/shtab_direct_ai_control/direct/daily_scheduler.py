"""Per-campaign daily audit enqueueing; no Direct write executor."""
import hashlib
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select,update
from .db import Session,DailySchedule,AssistantJob,Campaign,User,now
from .account_lifecycle import archived_ids
from .assistant_context import Selection,scope
from .audit_jobs import connection
from .ai import project_access,ensure_enabled

TZ=ZoneInfo('Europe/Moscow')

class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    project_id:str
    enabled:bool
    campaign_ids:list[str]|None=Field(default=None,max_length=1000)

def enqueue(db,user,setting,day):
    """Caller holds the schedule row lock and owns the transaction."""
    ensure_enabled();project_access(db,user,setting.project_id)
    selected=setting.campaign_ids or []
    campaigns=list(db.scalars(select(Campaign).where(Campaign.project_id==setting.project_id,Campaign.is_demo==False,Campaign.id.in_(selected))))
    removed=archived_ids(db,user.id);created=0;skipped=0
    skipped+=len(set(selected)-{c.id for c in campaigns})
    end=day-timedelta(days=1);start=end-timedelta(days=29)
    for campaign in campaigns:
        if campaign.account_id in removed:skipped+=1;continue
        try:
            chosen=connection(db,user,campaign.account_id)
            selection=Selection(connection_id=chosen.id,account_id=campaign.account_id,campaign_id=campaign.id,date_from=start,date_to=end)
            scope(db,user,setting.project_id,selection)
        except HTTPException:
            skipped+=1;continue
        identifier=hashlib.sha256(f'daily:{user.id}:{setting.project_id}:{campaign.id}:{chosen.id}:{day.isoformat()}'.encode()).hexdigest()
        if db.get(AssistantJob,identifier):continue
        db.add(AssistantJob(id=identifier,user_id=user.id,project_id=setting.project_id,kind='audit',payload={'selection':selection.model_dump(mode='json'),'scenario':'full','account_ids':[campaign.account_id],'daily_schedule_day':day.isoformat()}))
        created+=1
    setting.last_day=day.isoformat();setting.last_error=f'Пропущено кампаний: {skipped}' if skipped else '';setting.updated_at=now()
    return {'created':created,'skipped':skipped,'day':day.isoformat()}

def schedule_due(moment=None):
    moment=moment or datetime.now(TZ)
    if moment.tzinfo is None:raise ValueError('Timezone required')
    local=moment.astimezone(TZ)
    if local.hour<7:return 0
    day=local.date();count=0
    with Session() as db:
        keys=list(db.execute(select(DailySchedule.user_id,DailySchedule.project_id).where(DailySchedule.enabled==True)))
        for uid,pid in keys:
            try:
                row=db.scalar(select(DailySchedule).where(DailySchedule.user_id==uid,DailySchedule.project_id==pid).with_for_update())
                if not row or not row.enabled or row.last_day==day.isoformat():db.rollback();continue
                user=db.get(User,uid)
                if not user or 'integration:manage' not in user.permissions:raise HTTPException(403,'Нет доступа к интеграции.')
                result=enqueue(db,user,row,day);db.commit();count+=result['created']
            except Exception:
                db.rollback()
                # Avoid repeated attempts and paid-request storms for this day.
                row=db.scalar(select(DailySchedule).where(DailySchedule.user_id==uid,DailySchedule.project_id==pid).with_for_update())
                if row:row.last_day=day.isoformat();row.last_error='Не удалось запланировать аудит. Проверьте доступ и настройки AI.';row.updated_at=now();db.commit()
    return count

def register(app):
    from .main import current_user,db_session,permitted
    @app.get('/api/ai/scheduler')
    def get_schedule(project_id:str,user=Depends(current_user),db=Depends(db_session)):
        project_access(db,user,project_id)
        row=db.get(DailySchedule,(user.id,project_id))
        jobs=list(db.scalars(select(AssistantJob).where(AssistantJob.user_id==user.id,AssistantJob.project_id==project_id,AssistantJob.kind=='audit').order_by(AssistantJob.created_at.desc()).limit(100)))
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        return {'enabled':bool(row and row.enabled),'campaign_ids':row.campaign_ids if row else [], 'time':'07:00','timezone':'Europe/Moscow','last_day':row.last_day if row else '', 'last_error':row.last_error if row else '', 'jobs':[{'id':j.id,'status':j.status,'stage':j.stage,'error':j.error,'day':j.payload['daily_schedule_day'],'campaign_id':j.payload['selection'].get('campaign_id')} for j in jobs if j.payload.get('daily_schedule_day') and j.payload.get('selection',{}).get('campaign_id') not in hidden]}
    @app.post('/api/ai/scheduler')
    def settings(body:Settings,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');project_access(db,user,body.project_id)
        row=db.scalar(select(DailySchedule).where(DailySchedule.user_id==user.id,DailySchedule.project_id==body.project_id).with_for_update())
        if not row:row=DailySchedule(user_id=user.id,project_id=body.project_id,enabled=False,campaign_ids=[],last_day='',last_error='');db.add(row)
        if body.campaign_ids is not None:
            if len(set(body.campaign_ids))!=len(body.campaign_ids):raise HTTPException(422,'Кампании не должны повторяться.')
            for cid in body.campaign_ids:
                _,selected=scope(db,user,body.project_id,Selection(campaign_id=cid))
                connection(db,user,selected[0].account_id)
            row.campaign_ids=body.campaign_ids
        if body.enabled and not row.campaign_ids:raise HTTPException(422,'Выберите хотя бы одну кампанию.')
        local=datetime.now(TZ)
        # Enabling after the scheduled time starts tomorrow; do not trigger an
        # unexpected catch-up run immediately after the user's change.
        if body.enabled and not row.enabled and local.hour>=7:row.last_day=local.date().isoformat()
        row.enabled=body.enabled;row.updated_at=now();db.commit();return {'enabled':row.enabled}
    @app.post('/api/ai/scheduler/run')
    def run(body:Settings,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');project_access(db,user,body.project_id)
        row=db.scalar(select(DailySchedule).where(DailySchedule.user_id==user.id,DailySchedule.project_id==body.project_id).with_for_update())
        if not row:raise HTTPException(409,'Сначала сохраните настройки планировщика.')
        if not row.campaign_ids:raise HTTPException(422,'Выберите хотя бы одну кампанию.')
        result=enqueue(db,user,row,datetime.now(TZ).date());db.commit();return result
    @app.post('/api/ai/scheduler/stop')
    def stop(body:Settings,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');project_access(db,user,body.project_id)
        row=db.scalar(select(DailySchedule).where(DailySchedule.user_id==user.id,DailySchedule.project_id==body.project_id).with_for_update())
        if row:row.enabled=False;row.updated_at=now()
        jobs=list(db.scalars(select(AssistantJob).where(AssistantJob.user_id==user.id,AssistantJob.project_id==body.project_id,AssistantJob.kind=='audit',AssistantJob.status.in_(['pending','collecting','waiting_reports','generating']))))
        ids=[j.id for j in jobs if j.payload.get('daily_schedule_day')]
        if ids:db.execute(update(AssistantJob).where(AssistantJob.id.in_(ids),AssistantJob.status.in_(['pending','collecting','waiting_reports','generating'])).values(status='cancelled',stage='Остановлено пользователем',updated_at=now()))
        db.commit();return {'cancelled':len(ids),'enabled':False}
