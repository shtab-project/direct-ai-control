"""Explicit UI-confirmed, account-scoped disconnect. No provider revoke calls."""
from contextlib import ExitStack
import hashlib,json,secrets,time
from uuid import uuid4
from fastapi import Depends,HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select,delete,update,func
from .session import session_hash
from .db import AssistantJob,AccountLifecycle,DisconnectConfirmation,YandexConnection,Account,Project,ProjectAccount,Campaign,ReportJob,ReportDataset,DailyMetricSnapshot,ChangeRequest,ExecutionGuard,AIControl,Audit,AnalysisRun,Recommendation,Operation,now
from .account_lifecycle import connection_lock,archived_ids,analysis_accounts,active_connection


def checksum(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()




def own(db,user,identifier):
    c=db.scalar(select(YandexConnection).where(YandexConnection.id==identifier,YandexConnection.user_id==user.id))
    if not c:raise HTTPException(404,'Подключение недоступно.')
    if not c.account_id:raise HTTPException(409,'Сначала установите, к какому аккаунту относится OAuth-подключение.')
    return c


def impact(db,user,c):
    connections=list(db.scalars(select(YandexConnection).where(YandexConnection.account_id==c.account_id,YandexConnection.user_id==user.id,YandexConnection.status!='disconnected').order_by(YandexConnection.id)))
    if not connections:raise HTTPException(410,'Аккаунт уже отключён.')
    campaigns=list(db.scalars(select(Campaign).where(Campaign.account_id==c.account_id).order_by(Campaign.id)))
    project_ids=set(db.scalars(select(ProjectAccount.project_id).where(ProjectAccount.account_id==c.account_id)))|{x.project_id for x in campaigns}
    ids=[x.id for x in connections];cids=[x.id for x in campaigns]
    jobs=list(db.scalars(select(ReportJob).where(ReportJob.connection_id.in_(ids),ReportJob.status.in_(['pending','running'])).order_by(ReportJob.id)))
    return {'account_id':c.account_id,'account':db.get(Account,c.account_id).name,'connection_ids':ids,'connection_count':len(ids),
        'projects':[{'id':p.id,'name':p.name,'campaign_count':sum(x.project_id==p.id for x in campaigns)} for p in db.scalars(select(Project).where(Project.id.in_(project_ids)).order_by(Project.id))],
        'campaigns':[{'id':x.id,'name':x.name} for x in campaigns],'campaign_count':len(campaigns),'pending_job_ids':[j.id for j in jobs],
        'retained':{'statistics_datasets':db.scalar(select(func.count()).select_from(ReportDataset).where(ReportDataset.account_id==c.account_id)),
                    'daily_snapshots':db.scalar(select(func.count()).select_from(DailyMetricSnapshot).where(DailyMetricSnapshot.account_id==c.account_id)),
                    'recommendations':db.scalar(select(func.count()).select_from(Recommendation).where(Recommendation.campaign_id.in_(cids))),
                    'operations':db.scalar(select(func.count()).select_from(Operation).where(Operation.campaign_id.in_(cids)))},
        'other_users_affected':False,'provider_access_revoked':False}


def verification(db,user,c):
    rows=list(db.scalars(select(YandexConnection).where(YandexConnection.user_id==user.id,YandexConnection.account_id==c.account_id)))
    ids=[r.id for r in rows]
    archived=bool(db.get(AccountLifecycle,(user.id,c.account_id)))
    def blocked(identifier):
        try:active_connection(identifier,user.id)
        except HTTPException as e:return e.status_code==410
        return False
    checks={'all_connections_disconnected':bool(rows) and all(r.status=='disconnected' for r in rows),
            'active_tokens_removed':bool(rows) and all(not r.encrypted_token and not r.encrypted_refresh and r.expires is None for r in rows),
            'pending_jobs_cancelled':not db.scalar(select(func.count()).select_from(ReportJob).where(ReportJob.connection_id.in_(ids),ReportJob.status.in_(['pending','running']))),
            'excluded_from_ai_and_changes':archived,'api_dispatch_blocked':bool(ids) and all(blocked(cid) for cid in ids)}
    return {'verified':all(checks.values()),'checks':checks,'external_api_calls_performed':False,'account_id':c.account_id}


class ConfirmDisconnect(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    confirmation_token:str=Field(min_length=32,max_length=100)
    impact_hash:str=Field(pattern='^[a-f0-9]{64}$')
    confirm_account:str=Field(min_length=1,max_length=200)


def register(app):
    from .main import current_user,db_session,permitted

    @app.post('/api/integrations/yandex/connections/{identifier}/disconnect-preview')
    def preview(identifier:str,request:Request,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');c=own(db,user,identifier)
        data=impact(db,user,c);digest=checksum(data);token=secrets.token_urlsafe(32)
        db.execute(delete(DisconnectConfirmation).where(DisconnectConfirmation.expires<time.time()))
        db.add(DisconnectConfirmation(id=checksum(token),user_id=user.id,connection_id=c.id,session_hash=session_hash(request),impact_hash=digest,expires=time.time()+300))
        db.commit()
        return {'impact':data,'impact_hash':digest,'confirmation_token':token,'expires_in':300,'requires_explicit_confirmation':True}

    @app.post('/api/integrations/yandex/connections/{identifier}/disconnect')
    def disconnect(identifier:str,body:ConfirmDisconnect,request:Request,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');c=own(db,user,identifier)
        # Same order as executor: control lock first, then network-operation leases.
        db.scalar(select(AIControl).where(AIControl.id==1).with_for_update())
        initial=impact(db,user,c)
        with ExitStack() as locks:
            for cid in initial['connection_ids']:locks.enter_context(connection_lock(cid))
            # Re-read after draining operations; new projects/jobs require new preview.
            db.expire_all();c=own(db,user,identifier);data=impact(db,user,c)
            if data['connection_ids']!=initial['connection_ids'] or checksum(data)!=body.impact_hash or body.confirm_account.strip().casefold()!=data['account'].casefold():
                raise HTTPException(409,'Список последствий изменился или имя аккаунта не совпало. Откройте предпросмотр заново.')
            consumed=db.execute(delete(DisconnectConfirmation).where(DisconnectConfirmation.id==checksum(body.confirmation_token),DisconnectConfirmation.connection_id==identifier,
                 DisconnectConfirmation.user_id==user.id,DisconnectConfirmation.session_hash==session_hash(request),DisconnectConfirmation.impact_hash==body.impact_hash,DisconnectConfirmation.expires>time.time()).returning(DisconnectConfirmation.id)).scalar_one_or_none()
            if not consumed:raise HTTPException(409,'Подтверждение истекло, уже использовано или относится к другой сессии.')
            ids=data['connection_ids'];campaign_ids=[x['id'] for x in data['campaigns']]
            db.execute(update(ReportJob).where(ReportJob.connection_id.in_(ids),ReportJob.status.in_(['pending','running'])).values(status='cancelled',error='Аккаунт отключён пользователем.',lease_until=0,next_poll=0))
            cancel_ids=list(db.scalars(select(ChangeRequest.id).where(ChangeRequest.user_id==user.id,ChangeRequest.campaign_id.in_(campaign_ids),ChangeRequest.status.in_(['pending','approved','executing']))))
            db.execute(update(ChangeRequest).where(ChangeRequest.id.in_(cancel_ids)).values(status='cancelled',approval_hash='',expires=0,result={'message':'Аккаунт отключён; изменения не отправлены.','archived':True}))
            db.execute(delete(ExecutionGuard).where(ExecutionGuard.request_id.in_(cancel_ids)))
            for job in db.scalars(select(AssistantJob).where(AssistantJob.user_id==user.id,AssistantJob.status.in_(['pending','collecting','waiting_reports','generating']))):
                if c.account_id in job.payload.get('account_ids',[]):
                    job.status='cancelled';job.error='Аккаунт отключён пользователем.';job.stage='Отменено';job.lease_until=0;job.updated_at=now()
            for run in db.scalars(select(AnalysisRun).where(AnalysisRun.user_id==user.id,AnalysisRun.status=='running')):
                if c.account_id in analysis_accounts(db,run):
                    run.status='cancelled';run.error='Аккаунт отключён во время анализа.'
            if not db.get(AccountLifecycle,(user.id,c.account_id)):db.add(AccountLifecycle(user_id=user.id,account_id=c.account_id))
            for row in db.scalars(select(YandexConnection).where(YandexConnection.id.in_(ids),YandexConnection.user_id==user.id)):
                row.encrypted_token='';row.encrypted_refresh=None;row.expires=None;row.status='disconnected';row.last_error='';row.updated_at=now()
            db.add(Audit(id=str(uuid4()),user_id=user.id,action='yandex.account_disconnected',data={'account_id':c.account_id,'connection_ids':ids,'projects':data['projects'],'cancelled_changes':cancel_ids,'history_retained':True}))
            db.commit()
            result=verification(db,user,c)
            if not result['verified']:raise HTTPException(500,'Отключение требует проверки. Секреты не возвращаются в ответе.')
            return {'status':'disconnected','archived':True,'impact':data,'verification':result}

    @app.get('/api/integrations/yandex/connections/{identifier}/disconnect-verification')
    def verify(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');return verification(db,user,own(db,user,identifier))
