import {useEffect,useState} from 'react';
type Audit={available:boolean;reason?:string;id?:string;created_at?:string;campaign_id?:string;period?:{from:string;to:string};answer?:{recommendations?:{title:string;explanation:string;proposal:string}[]}};
export default function ChatAudit({conversationId}:{conversationId:string}){
 const [audit,setAudit]=useState<Audit|null>(null),[error,setError]=useState('');
 useEffect(()=>{let active=true;setAudit(null);setError('');if(!conversationId)return;fetch(`/api/plugins/direct-ai-control/api/ai/chats/${encodeURIComponent(conversationId)}/latest-audit`).then(async r=>{const x=await r.json();if(!r.ok)throw new Error(typeof x.detail==='string'?x.detail:'Аудит недоступен.');return x;}).then(x=>{if(active)setAudit(x);}).catch(e=>{if(active)setError(e.message);});return()=>{active=false;};},[conversationId]);
 if(!conversationId)return null;
 if(error)return <p className="source-note">{error}</p>;
 if(!audit)return <p className="source-note">Проверяем связанный аудит…</p>;
 if(!audit.available)return <p className="source-note">{audit.reason}</p>;
 return <details><summary>Последний аудит этой кампании · {audit.created_at?new Date(audit.created_at).toLocaleString('ru-RU'):''}</summary><p>Кампания {audit.campaign_id} · период {audit.period?.from} — {audit.period?.to}</p>{audit.answer?.recommendations?.map((r,i)=><article key={i}><strong>{r.title}</strong><p>{r.explanation}</p><p>{r.proposal}</p></article>)}<a className="btn" href="/plugins/direct-ai-control?page=audit">Открыть аудиты проекта</a></details>;
}
