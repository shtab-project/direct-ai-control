"""Opt-in private Telegram messages. Secrets and remote exception bodies never escape."""
import hashlib,os,re,secrets,time
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4
import httpx
from fastapi import Depends,HTTPException
from pydantic import Field
from sqlalchemy import select,update
from shtab_core.config import settings as shtab_settings
from ..paths import PAGE_PATH
from .db import Session,TelegramSettings,NotificationOutbox,AssistantJob,PerformanceCheckpoint,ChangeRequest,User,Membership,Campaign,Project,YandexConnection,now
from .yandex import seal,unseal,connected_mode
from .approvals import Strict
from .account_lifecycle import archived_ids

class Bot(Strict):
    token:str=Field(pattern=r'^\d{5,20}:[A-Za-z0-9_-]{25,100}$')
class Toggle(Strict):enabled:bool
def call(token,method,body):
    if method not in ('getMe','getUpdates','sendMessage'):raise ValueError('Unsupported Telegram method')
    try:
        with httpx.Client(timeout=15,trust_env=False,follow_redirects=False) as c:r=c.post('https://api.telegram.org/bot'+token+'/'+method,json=body)
        if len(r.content)>2_000_000:raise ValueError()
        d=r.json()
        if r.status_code!=200 or d.get('ok') is not True:raise ValueError()
        return d['result']
    except Exception:raise HTTPException(502,'Telegram не подтвердил запрос. Проверьте токен, сеть и привязку. Содержимое ошибки скрыто.') from None
def status(row):
    return {'configured':bool(row),'bound':bool(row and row.bound_at),'enabled':bool(row and row.enabled),'bound_at':row.bound_at if row else None}
def enqueue(db,user,key,text,accounts=None):
    identifier=hashlib.sha256(f'{user.id}:{key}'.encode()).hexdigest()
    if not db.get(NotificationOutbox,identifier):db.add(NotificationOutbox(id=identifier,user_id=user.id,data={'text':text[:3500],'account_ids':accounts or []}))
    return identifier
last_tick=0
def tick():
    global last_tick
    if time.time()-last_tick<30:return
    last_tick=time.time()
    with Session() as db:
        settings=list(db.scalars(select(TelegramSettings).where(TelegramSettings.enabled==True,TelegramSettings.bound_at!='')))
        origin=shtab_settings.public_url.rstrip('/') + PAGE_PATH
        for s in settings:
            user=db.get(User,s.user_id)
            if not user:continue
            removed=archived_ids(db,user.id)
            audits=db.scalars(select(AssistantJob).where(AssistantJob.user_id==user.id,AssistantJob.kind=='audit',AssistantJob.status=='completed',AssistantJob.updated_at>=s.bound_at).order_by(AssistantJob.updated_at.desc()).limit(30))
            for a in audits:
                if not a.payload.get('account_ids') or not db.get(Membership,(user.id,a.project_id)) or set(a.payload.get('account_ids',[]))&removed:continue
                answer=a.result.get('answer',{});titles='\n'.join('• '+r['title'] for r in answer.get('recommendations',[])[:5])
                enqueue(db,user,'audit:'+a.id,'AI-аудит готов.\n'+titles+'\nРекомендации требуют проверки и подтверждения.\n'+origin+'?page=audit\nСогласования: '+origin+'?page=approvals',a.payload['account_ids'])
            cps=db.scalars(select(PerformanceCheckpoint).where(PerformanceCheckpoint.user_id==user.id,PerformanceCheckpoint.status=='completed',PerformanceCheckpoint.updated_at>=s.bound_at).limit(50))
            for p in cps:
                change=db.get(ChangeRequest,p.change_id);campaign=db.get(Campaign,change.campaign_id) if change else None
                if campaign and campaign.account_id not in removed and db.get(Membership,(user.id,campaign.project_id)):
                    enqueue(db,user,'checkpoint:'+p.id,f'Контрольная точка: {p.days} дней после изменения. Данные обновлены. Причинность не доказана.\n'+origin+'?page=daily',[campaign.account_id])
            errors=db.scalars(select(YandexConnection).where(YandexConnection.user_id==user.id,YandexConnection.status=='error',YandexConnection.updated_at>=s.bound_at).limit(10))
            for c in errors:
                if c.account_id not in removed:enqueue(db,user,'connection:'+c.id+':'+c.updated_at,'Ошибка подключения Директа. Проверьте раздел «Интеграции».\n'+origin+'?page=settings',[c.account_id] if c.account_id else [])
            local=datetime.now(ZoneInfo('Europe/Moscow'))
            if local.hour>=9:
                from .daily_analytics import daily_report
                projects=db.scalars(select(Project).join(Membership,Membership.project_id==Project.id).where(Membership.user_id==user.id,Project.is_demo==False))
                for project in projects:
                    campaigns=list(db.scalars(select(Campaign).where(Campaign.project_id==project.id,Campaign.is_demo==False)))
                    campaigns=[c for c in campaigns if c.account_id not in removed]
                    if not campaigns:continue
                    try:r=daily_report(db,campaigns,1,1)
                    except HTTPException:continue
                    m=r['current']['metrics'];text='Нет полного покрытия статистики.' if not m else f"Расход: {m['cost']:.2f} {r['currency']}; клики: {m['clicks']}; конверсии Директа: {m['conversions'] if m['conversions_known'] else 'нет данных'}."
                    enqueue(db,user,'daily:'+project.id+':'+local.date().isoformat(),f"Отчёт за {r['current']['from']}: {project.name[:100]}\n{text}\nКачественные лиды: нет CRM.\nИсточник обновлён: {r['updated_at'] or 'нет данных'}\n"+origin+'?page=daily',sorted({c.account_id for c in campaigns}))
        db.commit()
        row=db.scalar(select(NotificationOutbox).where(NotificationOutbox.status=='pending').order_by(NotificationOutbox.created_at).limit(1))
        if not row:return
        s=db.get(TelegramSettings,row.user_id)
        if not s or not s.enabled or not s.bound_at or row.created_at<s.bound_at or set(row.data.get('account_ids',[]))&archived_ids(db,row.user_id):row.status='cancelled';db.commit();return
        claimed=db.execute(update(NotificationOutbox).where(NotificationOutbox.id==row.id,NotificationOutbox.status=='pending').values(status='sending').returning(NotificationOutbox.id)).scalar_one_or_none();db.commit()
        if not claimed:return
        # Crash/network ambiguity => leave sending/unknown; never duplicate automatically.
        try:
            config=unseal(s.encrypted);result=call(config['token'],'sendMessage',{'chat_id':config['chat_id'],'text':row.data['text'],'link_preview_options':{'is_disabled':True}})
            if not isinstance(result,dict) or not result.get('message_id'):raise ValueError()
            row.status='sent'
        except Exception:row.status='unknown';row.error='Доставка не подтверждена. Автоматического повтора нет.'
        db.commit()

def register(app):
    from .main import current_user,db_session,permitted
    @app.get('/api/integrations/telegram')
    def info(user=Depends(current_user),db=Depends(db_session)):
        return {**status(db.get(TelegramSettings,user.id)),'notifications':[{'id':r.id,'status':r.status,'error':r.error,'created_at':r.created_at} for r in db.scalars(select(NotificationOutbox).where(NotificationOutbox.user_id==user.id).order_by(NotificationOutbox.created_at.desc()).limit(20))]}
    @app.post('/api/integrations/telegram/configure')
    def configure(body:Bot,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');connected_mode()
        bot=call(body.token,'getMe',{})
        username=bot.get('username','')
        if not re.fullmatch('[A-Za-z0-9_]{5,100}',username):raise HTTPException(502,'Не подтверждён Telegram-бот.')
        code=secrets.token_urlsafe(24);row=db.get(TelegramSettings,user.id)
        data={'token':body.token,'nonce_hash':hashlib.sha256(code.encode()).hexdigest(),'expires':time.time()+600}
        if not row:row=TelegramSettings(user_id=user.id,encrypted=seal(data));db.add(row)
        else:row.encrypted=seal(data);row.enabled=False;row.bound_at=''
        db.execute(update(NotificationOutbox).where(NotificationOutbox.user_id==user.id,NotificationOutbox.status=='pending').values(status='cancelled'))
        db.commit();return {**status(row),'binding_url':'https://t.me/'+username+'?start='+code,'expires_in':600}
    @app.post('/api/integrations/telegram/connect')
    def connect(user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');row=db.get(TelegramSettings,user.id)
        if not row:raise HTTPException(409,'Сначала настройте бота.')
        data=unseal(row.encrypted)
        if data.get('expires',0)<time.time():raise HTTPException(409,'Код истёк. Повторите настройку бота.')
        events=call(data['token'],'getUpdates',{'limit':100,'timeout':0,'allowed_updates':['message']})
        for event in events if isinstance(events,list) else []:
            message=event.get('message',{});chat=message.get('chat',{});text=message.get('text','').split()
            if chat.get('type')=='private' and len(text)==2 and text[0].split('@')[0]=='/start' and hashlib.sha256(text[1].encode()).hexdigest()==data['nonce_hash']:
                row.encrypted=seal({'token':data['token'],'chat_id':chat['id']});row.bound_at=now();row.enabled=False;db.commit();return status(row)
        raise HTTPException(409,'Откройте ссылку привязки и нажмите Start в личном чате, затем повторите проверку.')
    @app.post('/api/integrations/telegram/toggle')
    def toggle(body:Toggle,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');row=db.get(TelegramSettings,user.id)
        if not row or not row.bound_at:raise HTTPException(409,'Сначала подтвердите личный чат.')
        row.enabled=body.enabled
        if not body.enabled:db.execute(update(NotificationOutbox).where(NotificationOutbox.user_id==user.id,NotificationOutbox.status=='pending').values(status='cancelled'))
        db.commit();return status(row)
    @app.post('/api/integrations/telegram/test')
    def test(user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'integration:manage');row=db.get(TelegramSettings,user.id)
        if not row or not row.enabled or not row.bound_at:raise HTTPException(409,'Подтвердите чат и включите уведомления.')
        key=enqueue(db,user,'test:'+uuid4().hex,'Direct AI Control: тест уведомлений. Операции с рекламой требуют подтверждения только в панели.');db.commit();return {'id':key,'status':'pending'}
