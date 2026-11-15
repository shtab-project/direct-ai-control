"""User-specific display preferences; never alters Direct or execution settings."""
from pydantic import BaseModel,ConfigDict
from fastapi import Depends,HTTPException
from sqlalchemy import select
from .db import HiddenCampaign,Campaign,Membership

def hidden_ids(db,user_id):
    return set(db.scalars(select(HiddenCampaign.campaign_id).where(HiddenCampaign.user_id==user_id)))

class Visibility(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    campaign_id:str
    hidden:bool

def register(app):
    from .main import current_user,db_session
    @app.post('/api/campaign-visibility')
    def change(body:Visibility,user=Depends(current_user),db=Depends(db_session)):
        c=db.get(Campaign,body.campaign_id)
        if not c or not db.get(Membership,(user.id,c.project_id)):raise HTTPException(404,'Кампания недоступна.')
        row=db.get(HiddenCampaign,(user.id,c.id))
        if body.hidden and not row:db.add(HiddenCampaign(user_id=user.id,campaign_id=c.id))
        if not body.hidden and row:db.delete(row)
        db.commit();return {'campaign_id':c.id,'hidden':body.hidden}
