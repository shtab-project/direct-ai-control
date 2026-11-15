"""History and fresh, confirmed rollback for existing closed change adapters."""
from uuid import uuid4
from fastapi import Depends,HTTPException
from sqlalchemy import select
from .db import ChangeRequest,Audit,Campaign,User,Account,RemoteCampaignMapping
from .approvals import Strict,request_access,digest,view,audit,payload_for
from .typed_changes import adapter,desired,read_object,make_payload,DraftTyped
from .yandex import DirectFailure

class Rollback(Strict):
    version:int


def inverse(payload):
    spec=payload['spec'];old=payload['before'];op=spec['operation']
    fields={'keyword_text':{'keyword':old.get('Keyword')},'text_ad':{k.lower() if k!='Href' else 'href':old.get(k,'') for k in ('Title','Title2','Text','Href')},'ad_state':{'state':old.get('State')},'group_regions':{'region_ids':old.get('RegionIds')},'group_negatives':{'items':(old.get('NegativeKeywords') or {}).get('Items',[])},'group_name':{'name':old.get('Name')}}
    if op not in fields:raise HTTPException(422,'Тип отката не поддерживается.')
    try:return adapter.validate_python({'operation':op,'object_id':spec['object_id'],**fields[op]})
    except ValueError:raise HTTPException(422,'Исходные значения нельзя безопасно восстановить этим адаптером.') from None


def register(app):
    from .main import current_user,db_session,permitted
    @app.get('/api/ai-change-history')
    def history(user=Depends(current_user),db=Depends(db_session)):
        from .main import campaign_access
        result=[]
        rows=db.scalars(select(ChangeRequest).where(ChangeRequest.user_id==user.id).order_by(ChangeRequest.created_at.desc()).limit(200))
        for row in rows:
            try:c=campaign_access(db,user,row.campaign_id)
            except HTTPException:continue
            account=db.get(Account,c.account_id);mapping=db.get(RemoteCampaignMapping,c.id)
            actor=db.get(User,row.approved_by) if row.approved_by else None
            events=list(db.scalars(select(Audit).where(Audit.user_id==user.id,Audit.data['request_id'].as_string()==row.id).order_by(Audit.created_at)))
            result.append({**view(row),'campaign_name':c.name,'account_name':account.name if account else None,'remote_campaign_id':mapping.remote_id if mapping else None,'approved_by_name':actor.login if actor else None,'events':[{'action':e.action,'created_at':e.created_at,'data':{k:v for k,v in e.data.items() if k not in ('session_hash','approval_id')}} for e in events]})
        return result

    @app.post('/api/ai-changes/{identifier}/rollback')
    def rollback(identifier:str,body:Rollback,user=Depends(current_user),db=Depends(db_session)):
        permitted(user,'draft:write');original,campaign=request_access(db,user,identifier,True)
        if original.version!=body.version or original.status!='applied' or not original.result.get('verified'):raise HTTPException(409,'Откат доступен только для проверенной применённой версии.')
        # Retry returns the same draft, never a second rollback/write.
        prior=db.scalar(select(ChangeRequest).where(ChangeRequest.user_id==user.id,ChangeRequest.payload['rollback_of'].as_string()==identifier,ChangeRequest.status.in_(['pending','approved','executing','unknown'])))
        if prior:return view(prior)
        try:
            if original.payload['operation']=='typed_nonfinancial':
                spec=inverse(original.payload)
                current=read_object(db,user,campaign,spec)
                if digest(current)!=digest(original.payload['after']):raise HTTPException(409,'Объект изменился после применения. Автоматическая подготовка отката заблокирована.')
                payload=make_payload(db,user,campaign,DraftTyped(campaign_id=campaign.id,spec=spec,reason='Откат проверенного изменения '+identifier))
            elif original.payload['operation']=='replace_campaign_negative_keywords':
                payload=payload_for(db,user,campaign,original.payload['before'],'Откат проверенного изменения '+identifier)
                if digest(payload['before'])!=digest(original.payload['after']):raise HTTPException(409,'Список изменился после применения. Откат заблокирован.')
            else:raise HTTPException(422,'Неподдерживаемая операция.')
        except DirectFailure:raise HTTPException(502,'Не удалось проверить актуальное состояние для отката.') from None
        if digest(payload['before'])!=digest(original.payload['after']):raise HTTPException(409,'Состояние изменилось во время подготовки отката. Повторите проверку.')
        payload={**payload,'rollback_of':identifier}
        row=ChangeRequest(id=str(uuid4()),user_id=user.id,campaign_id=campaign.id,payload=payload,diff_hash=digest(payload))
        db.add(row);db.flush();audit(db,user,'ai.rollback_prepared',row,{'original_request_id':identifier});db.commit();return view(row)
