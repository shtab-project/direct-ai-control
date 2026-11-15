import {useEffect,useState} from 'react';

type Budget={day:string;timezone:string;limit_usd:number;estimated_or_reserved_usd:number;requests:number;pricing_review_after:string};
export default function AIBudget(){
 const [data,setData]=useState<Budget|null>(null),[value,setValue]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{let live=true;fetch('/api/plugins/direct-ai-control/api/ai/budget').then(r=>{if(!r.ok)throw new Error();return r.json();}).then(x=>{if(live){setData(x);setValue(x.limit_usd.toFixed(2));}}).catch(()=>{if(live)setError('Учёт лимита AI недоступен.');});return()=>{live=false;};},[]);
 if(error&&!data)return <p className="source-note">{error}</p>;
 if(!data)return <p className="source-note">Загрузка лимита AI…</p>;
 async function save(){const cents=Math.round(Number(value)*100);if(!Number.isFinite(cents)||cents<1||cents>10000){setError('Введите сумму от $0.01 до $100.00.');return;}setBusy(true);setError('');try{const r=await fetch('/api/plugins/direct-ai-control/api/ai/budget',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'DirectAI'},body:JSON.stringify({limit_cents:cents})});if(!r.ok)throw new Error();setData(d=>d?{...d,limit_usd:cents/100}:d);setValue((cents/100).toFixed(2));}catch{setError('Не удалось сохранить лимит.');}finally{setBusy(false);}}
 return <div><p className="source-note">Лимит всех запросов AI в панели: ${data.limit_usd.toFixed(2)} за {data.day} ({data.timezone}). Оценка и резервы: ${data.estimated_or_reserved_usd.toFixed(4)}; запросов: {data.requests}. Учёт начинается с включения лимита. Это оценка с запасом; фактический счёт смотрите в OpenAI Billing.</p><label>Дневной лимит API, $ <input type="number" min="0.01" max="100" step="0.01" value={value} onChange={e=>setValue(e.target.value)}/></label> <button className="btn" type="button" disabled={busy} onClick={save}>Сохранить лимит</button>{error&&<p className="alert error">{error}</p>}</div>;
}
