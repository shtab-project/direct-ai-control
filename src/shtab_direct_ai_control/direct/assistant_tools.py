"""Scoped, read-only audit retrieval for an assistant conversation."""
from sqlalchemy import select
from .db import AssistantJob,RemoteCampaignMapping,AIProposal,ChangeRequest,PerformanceCheckpoint
from .assistant_context import scope

def latest_audit(db,user,project_id,selection):
    _,campaigns=scope(db,user,project_id,selection)
    # An aggregate selection or unknown connection cannot prove exact scope.
    if len(campaigns)!=1 or not selection.campaign_id or not selection.connection_id:return None
    campaign=campaigns[0]
    if not db.get(RemoteCampaignMapping,campaign.id):return None
    rows=db.scalars(select(AssistantJob).where(AssistantJob.user_id==user.id,AssistantJob.project_id==project_id,AssistantJob.kind=='audit',AssistantJob.status=='completed').order_by(AssistantJob.created_at.desc()).limit(100))
    for job in rows:
        result=job.result if isinstance(job.result,dict) else {}
        source=result.get('source',{})
        if not isinstance(source,dict):continue
        sel=job.payload.get('selection',{})
        audit_connection=source.get('connection_id') or sel.get('connection_id')
        if audit_connection!=selection.connection_id:continue
        if sel.get('connection_id') and sel['connection_id']!=selection.connection_id:continue
        if sel.get('campaign_id') and sel['campaign_id']!=campaign.id:continue
        if sel.get('account_id') and sel['account_id']!=campaign.account_id:continue
        if source.get('project_id') and source['project_id']!=project_id:continue
        if source.get('campaign_ids')!=[campaign.id] or source.get('account_ids')!=[campaign.account_id]:continue
        answer=result.get('answer',{})
        if not isinstance(answer,dict):continue
        evidence=source.get('evidence',{})
        if not isinstance(evidence,dict):continue
        refs=[]
        for rec in answer.get('recommendations',[])[:15]:
            if isinstance(rec,dict):refs.extend(rec.get('source_ids',[]))
        refs.extend(answer.get('source_ids',[]))
        refs=list(dict.fromkeys(x for x in refs if isinstance(x,str) and isinstance(evidence.get(x),dict)))[:40]
        return {'id':job.id,'created_at':job.created_at,'connection_id':audit_connection,'project_id':project_id,'campaign_id':campaign.id,'account_id':campaign.account_id,'period':{'from':source.get('date_from'),'to':source.get('date_to')},'answer':answer,'evidence':{ref:evidence[ref] for ref in refs}}
    return None

def related_records(db,user,project_id,selection):
    """Read only rows whose audit proves the exact selected connection."""
    _,campaigns=scope(db,user,project_id,selection)
    if len(campaigns)!=1 or not selection.campaign_id or not selection.connection_id:return None
    campaign=campaigns[0];items=[];known=set()
    proposals=db.scalars(select(AIProposal).where(AIProposal.user_id==user.id,AIProposal.project_id==project_id,AIProposal.campaign_id==campaign.id).order_by(AIProposal.created_at.desc()).limit(50))
    for proposal in proposals:
        job=db.get(AssistantJob,proposal.job_id)
        if not job or job.user_id!=user.id or job.project_id!=project_id:continue
        source=(job.result or {}).get('source',{})
        if not isinstance(source,dict) or source.get('connection_id')!=selection.connection_id or source.get('campaign_ids')!=[campaign.id] or source.get('account_ids')!=[campaign.account_id]:continue
        d=proposal.data if isinstance(proposal.data,dict) else {}
        items.append({'id':proposal.id,'audit_id':job.id if job.kind=='audit' else None,'status':proposal.status,'title':d.get('title'),'proposal':d.get('proposal'),'risk':d.get('risk'),'created_at':proposal.created_at})
        known.add(proposal.id)
    changes=[]
    rows=db.scalars(select(ChangeRequest).where(ChangeRequest.user_id==user.id,ChangeRequest.campaign_id==campaign.id).order_by(ChangeRequest.created_at.desc()).limit(50))
    for change in rows:
        payload=change.payload if isinstance(change.payload,dict) else {}
        if payload.get('analysis_id') not in known:continue
        points=db.scalars(select(PerformanceCheckpoint).where(PerformanceCheckpoint.user_id==user.id,PerformanceCheckpoint.change_id==change.id).order_by(PerformanceCheckpoint.days))
        changes.append({'id':change.id,'recommendation_id':payload.get('analysis_id'),'status':change.status,'diff':payload.get('diff'),'result':change.result,'checkpoints':[{'days':p.days,'status':p.status,'comparison':p.data.get('comparison') if isinstance(p.data,dict) else None} for p in points]})
    return {'connection_id':selection.connection_id,'project_id':project_id,'campaign_id':campaign.id,'recommendations':items,'changes':changes,'note':'Проверены только изменения, связанные с рекомендациями точного подключения. Сравнение периодов не доказывает причинность.'}
