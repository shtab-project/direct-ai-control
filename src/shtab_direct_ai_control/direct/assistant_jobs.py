"""Durable read-only jobs. A lost generation is never automatically repeated."""
import hashlib,time
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import select,update
from .db import Session,AssistantJob,ChatConversation,ChatMessage,User,now
from .account_lifecycle import archived_ids
from .assistant_context import Selection,scope,context,redact_long
from .assistant_provider import generate,verify
from .ai import configuration,ensure_enabled,AIError

TERMINAL={'completed','error','unknown','cancelled'}

def job_view(job,db=None,user=None):
    result={k:getattr(job,k) for k in ('id','project_id','kind','status','stage','result','error','created_at','updated_at')}
    if db is not None and user is not None:result['archived']=bool(set(job.payload.get('account_ids',[]))&archived_ids(db,user.id))
    return result

def active(db,user,job):
    if set(job.payload.get('account_ids',[]))&archived_ids(db,user.id):raise HTTPException(410,'Исходный аккаунт отключён.')
    scope(db,user,job.project_id,Selection.model_validate(job.payload['selection']))

def process_one(db,job,user):
    active(db,user,job)
    if job.kind!='chat':
        from .audit_jobs import process_audit
        return process_audit(db,job,user)
    ensure_enabled()
    selection=Selection.model_validate(job.payload['selection'])
    payload,source=context(db,user,job.project_id,selection)
    messages=list(db.scalars(select(ChatMessage).where(ChatMessage.conversation_id==job.payload['conversation_id'],ChatMessage.id!=job.payload['message_id']).order_by(ChatMessage.created_at.desc()).limit(30)))
    history=[];chars=0;omitted=0;removed=archived_ids(db,user.id)
    for m in messages:
        if m.created_at>job.created_at or set(m.source.get('account_ids',[]))&removed or not set(m.source.get('account_ids',[])).issubset(source['account_ids']):omitted+=1;continue
        if chars+len(m.content)>60000:omitted+=1;continue
        facts=[]
        for i,fact in enumerate(m.source.get('answer',{}).get('facts',[])[:10]):
            original=m.source.get('evidence',{}).get(fact['source_id'])
            if original and fact['metric'] in original.get('data',{}):
                sid=f'h{len(history)}_{i}'
                source['evidence'][sid]={**original,'historical_message_id':m.id,'date_from':m.source.get('date_from'),'date_to':m.source.get('date_to')}
                facts.append({'source_id':sid,'metric':fact['metric'],'value':original['data'][fact['metric']]})
        history.append({'role':m.role,'content':redact_long(m.content),'verified_facts':facts});chars+=len(m.content)
    history.reverse()
    question=db.get(ChatMessage,job.payload['message_id'])
    payload.update(history=history,question=redact_long(question.content))
    # Retrieve an audit only on request and only with exact, server-verified scope.
    if 'аудит' in question.content.casefold():
        from .assistant_tools import latest_audit
        audit=latest_audit(db,user,job.project_id,selection)
        if audit:
            references={key:'audit_'+str(i) for i,key in enumerate(audit['evidence'])}
            for key,value in audit['evidence'].items():source['evidence'][references[key]]={**value,'audit_id':audit['id']}
            recommendations=[{**r,'source_ids':[references[x] for x in r.get('source_ids',[]) if x in references]} for r in audit['answer'].get('recommendations',[])[:15] if isinstance(r,dict)]
            payload['latest_audit']={'id':audit['id'],'created_at':audit['created_at'],'period':audit['period'],'recommendations':recommendations}
            source['latest_audit_id']=audit['id']
        else:source['limitations'].append('Нет завершённого аудита с проверенным совпадением кампании и OAuth-подключения.')
    if any(word in question.content.casefold() for word in ('рекомендац','исправ','изменени','эффективност','до и после')):
        from .assistant_tools import related_records
        records=related_records(db,user,job.project_id,selection)
        if records:payload['related_records']=records
        else:source['limitations'].append('Для истории изменений выберите одну кампанию и её подключение.')
    source['history_messages_included']=len(history);source['history_messages_omitted']=omitted
    job.source=source;job.status='generating';job.stage='AI анализирует факты и историю';job.lease_until=time.time()+130;job.updated_at=now();db.commit()
    answer,usage=generate(configuration(db,user.id),payload)
    active(db,user,job)
    db.refresh(job)
    if job.status=='cancelled':return
    result=verify(answer,source)
    finalized=db.execute(update(AssistantJob).where(AssistantJob.id==job.id,AssistantJob.status=='generating').values(status='completed',stage='Ответ сохранён',lease_until=0,updated_at=now()).returning(AssistantJob.id)).scalar_one_or_none()
    if not finalized:db.rollback();return
    db.add(ChatMessage(id=str(uuid4()),conversation_id=job.payload['conversation_id'],role='assistant',content=result['answer_markdown'],job_id=job.id,source={**source,'answer':result,'usage':usage}))
    job.result={'usage':usage,'source':source,'answer':result}
    from .proposals import save_recommendations
    save_recommendations(db,user,job,result,source)
    db.commit()

def tick():
    with Session() as db:
        # Crash after dispatch => unknown, not another paid model request.
        db.execute(update(AssistantJob).where(AssistantJob.status=='generating',AssistantJob.lease_until<time.time()).values(status='unknown',stage='Требуется ручная проверка',error='Процесс прерван после начала генерации. Автоматический повтор запрещён.',lease_until=0,updated_at=now()))
        db.commit()
        item=db.scalar(select(AssistantJob).where(AssistantJob.status.in_(['pending','collecting','waiting_reports']),AssistantJob.next_poll<=time.time(),AssistantJob.lease_until<time.time()).order_by(AssistantJob.created_at).limit(1))
        if not item:return False
        claimed=db.execute(update(AssistantJob).where(AssistantJob.id==item.id,AssistantJob.lease_until<time.time(),AssistantJob.status.in_(['pending','collecting','waiting_reports'])).values(lease_until=time.time()+180,status='collecting',updated_at=now()).returning(AssistantJob.id)).scalar_one_or_none()
        db.commit()
        if not claimed:return False
        try:
            user=db.get(User,item.user_id)
            if not user:raise HTTPException(403,'Пользователь недоступен.')
            process_one(db,item,user)
        except (HTTPException,AIError) as e:
            db.rollback();db.refresh(item)
            if item.status!='cancelled':
                item.status='cancelled' if isinstance(e,HTTPException) and e.status_code in (403,404,410) else 'error'
                item.error=str(e.detail)[:500] if isinstance(e,HTTPException) else str(e)[:500]
                item.stage='Задание остановлено';item.lease_until=0;item.updated_at=now();db.commit()
        except Exception:
            db.rollback();db.refresh(item)
            if item.status!='cancelled':
                item.status='unknown' if item.status=='generating' else 'error';item.error='Задание не завершено. Секреты и исходные данные не выводятся в журнал.';item.lease_until=0;item.updated_at=now();db.commit()
        return True
