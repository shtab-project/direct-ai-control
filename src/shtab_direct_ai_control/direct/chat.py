"""Private project conversations, long business context and immutable answers."""
import hashlib,io,json
from uuid import uuid4
from fastapi import Depends,HTTPException
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .db import BusinessContext,ChatConversation,ChatMessage,AssistantJob,now
from .ai import project_access,ensure_enabled
from .assistant_context import Selection,scope,business_context
from .account_lifecycle import archived_ids
from .assistant_jobs import job_view

class CreateChat(BaseModel):
    model_config=ConfigDict(extra='forbid')
    project_id:str
    title:str=Field(default='Новый диалог',min_length=1,max_length=160)
    selection:Selection=Field(default_factory=Selection)

class SendMessage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:str=Field(min_length=1,max_length=30000)
    request_id:str=Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')

class SaveContext(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:str=Field(max_length=50000)
    revision:int=Field(ge=0)

def conversation(db,user,identifier,lock=False):
    q=select(ChatConversation).where(ChatConversation.id==identifier,ChatConversation.user_id==user.id)
    if lock:q=q.with_for_update()
    row=db.scalar(q.execution_options(populate_existing=True))
    if not row:raise HTTPException(404,'Диалог недоступен.')
    project_access(db,user,row.project_id)
    return row

def message_view(db,user,m):
    job=db.get(AssistantJob,m.job_id) if m.job_id else None
    return {'id':m.id,'role':m.role,'content':m.content,'source':m.source,'created_at':m.created_at,'job':job_view(job) if job else None,'archived':bool(set(m.source.get('account_ids',[]))&archived_ids(db,user.id))}

def export_answer(message,format):
    if format=='md':
        return Response(message.content,media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="ai-answer-{message.id}.md"'})
    if format!='xlsx':raise HTTPException(422,'Формат: md или xlsx.')
    from openpyxl import Workbook
    wb=Workbook();ws=wb.active;ws.title='Ответ AI'
    for i,line in enumerate(message.content.splitlines(),1):
        ws.cell(i,1,line);ws.cell(i,1).data_type='s'
    ws.column_dimensions['A'].width=110
    facts=wb.create_sheet('Проверенные факты');facts.append(['Источник','Показатель','Значение'])
    for f in message.source.get('answer',{}).get('facts',[]):facts.append([f['source_id'],f['metric'],f['value']])
    source=wb.create_sheet('Происхождение');source.append(['Поле','Значение'])
    for key in ('date_from','date_to','currency','timezone','attribution'):
        source.append([key,str(message.source.get(key,''))]);source.cell(source.max_row,2).data_type='s'
    stream=io.BytesIO();wb.save(stream)
    return Response(stream.getvalue(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="ai-answer-{message.id}.xlsx"'})

def register(app):
    from .main import current_user,db_session,permitted

    @app.get('/api/projects/{project_id}/business-context')
    def get_context(project_id:str,user=Depends(current_user),db=Depends(db_session)):
        return business_context(db,project_access(db,user,project_id))

    @app.post('/api/projects/{project_id}/business-context')
    def save_context(project_id:str,body:SaveContext,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');project=project_access(db,user,project_id)
        db.execute(select(type(project)).where(type(project).id==project_id).with_for_update())
        row=db.scalar(select(BusinessContext).where(BusinessContext.project_id==project_id).with_for_update())
        revision=row.revision if row else 0
        if revision!=body.revision:raise HTTPException(409,'Контекст обновлён в другой вкладке. Загрузите актуальную версию.')
        if not row:row=BusinessContext(project_id=project_id,content=body.content,revision=1);db.add(row)
        else:row.content=body.content;row.revision+=1;row.updated_at=now()
        db.commit();return business_context(db,project)

    @app.get('/api/ai/chats')
    def listing(project_id:str,user=Depends(current_user),db=Depends(db_session)):
        project_access(db,user,project_id)
        from .campaign_visibility import hidden_ids
        hidden=hidden_ids(db,user.id)
        return [{'id':c.id,'title':c.title,'selection':c.selection,'created_at':c.created_at} for c in db.scalars(select(ChatConversation).where(ChatConversation.user_id==user.id,ChatConversation.project_id==project_id).order_by(ChatConversation.created_at.desc())) if c.selection.get('campaign_id') not in hidden]

    @app.post('/api/ai/chats')
    def create(body:CreateChat,user=Depends(current_user),db=Depends(db_session)):
        _,campaigns=scope(db,user,body.project_id,body.selection)
        selection=body.selection
        if selection.campaign_id and not selection.connection_id:
            from .audit_jobs import connection
            chosen=connection(db,user,campaigns[0].account_id)
            selection=selection.model_copy(update={'connection_id':chosen.id,'account_id':campaigns[0].account_id})
        row=ChatConversation(id=str(uuid4()),user_id=user.id,project_id=body.project_id,title=body.title,selection=selection.model_dump(mode='json'))
        db.add(row);db.commit();return {'id':row.id,'title':row.title,'selection':row.selection}

    @app.get('/api/ai/chats/{identifier}/latest-audit')
    def get_latest_audit(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        c=conversation(db,user,identifier)
        from .assistant_tools import latest_audit
        item=latest_audit(db,user,c.project_id,Selection.model_validate(c.selection))
        if not item:return {'available':False,'reason':'Нет завершённого аудита с подтверждённым совпадением кампании и подключения.'}
        return {'available':True,**item}

    @app.get('/api/ai/chats/{identifier}')
    def read(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        c=conversation(db,user,identifier)
        return {'id':c.id,'title':c.title,'project_id':c.project_id,'selection':c.selection,'messages':[message_view(db,user,m) for m in db.scalars(select(ChatMessage).where(ChatMessage.conversation_id==c.id).order_by(ChatMessage.created_at))]}

    @app.post('/api/ai/chats/{identifier}/messages')
    def send(identifier:str,body:SendMessage,user=Depends(current_user),db=Depends(db_session)):
        ensure_enabled();c=conversation(db,user,identifier,True)
        if not body.content.strip():raise HTTPException(422,'Сообщение пустое.')
        key=hashlib.sha256(f'{user.id}:chat:{identifier}:{body.request_id}'.encode()).hexdigest()
        existing=db.get(AssistantJob,key)
        if existing:return job_view(existing)
        pending=db.scalar(select(AssistantJob.id).where(AssistantJob.user_id==user.id,AssistantJob.kind=='chat',AssistantJob.status.in_(['pending','collecting','waiting_reports','generating']),AssistantJob.payload['conversation_id'].as_string()==identifier))
        if pending:raise HTTPException(409,'Дождитесь ответа в этом диалоге. Другой диалог можно открыть отдельно.')
        project,campaigns=scope(db,user,c.project_id,Selection.model_validate(c.selection))
        accounts=sorted({x.account_id for x in campaigns});mid=str(uuid4())
        job=AssistantJob(id=key,user_id=user.id,project_id=project.id,kind='chat',payload={'conversation_id':c.id,'message_id':mid,'selection':c.selection,'account_ids':accounts})
        db.add(job);db.flush()
        db.add(ChatMessage(id=mid,conversation_id=c.id,role='user',content=body.content,job_id=job.id,source={'account_ids':accounts,'campaign_ids':[x.id for x in campaigns],'project_id':project.id,'connection_id':c.selection.get('connection_id')}))
        try:db.commit()
        except IntegrityError:
            db.rollback();return job_view(db.get(AssistantJob,key))
        return job_view(job)

    @app.get('/api/assistant/jobs/{identifier}')
    def job(identifier:str,user=Depends(current_user),db=Depends(db_session)):
        row=db.get(AssistantJob,identifier)
        if not row or row.user_id!=user.id:raise HTTPException(404,'Задание недоступно.')
        project_access(db,user,row.project_id);return job_view(row,db,user)

    @app.get('/api/ai/chats/{identifier}/messages/{message_id}/export')
    def export(identifier:str,message_id:str,format:str='md',user=Depends(current_user),db=Depends(db_session)):
        conversation(db,user,identifier)
        message=db.get(ChatMessage,message_id)
        if not message or message.conversation_id!=identifier or message.role!='assistant':raise HTTPException(404,'Ответ недоступен.')
        return export_answer(message,format)
