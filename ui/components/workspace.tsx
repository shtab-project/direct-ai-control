
import { useEffect, useRef, useState } from 'react';
import YandexSettings from './yandex-settings';
import OpenAISettings from './openai-settings';
import TelegramSettings from './telegram-settings';
import AIWorkspace from './ai-workspace';
import AuditWorkspace from './audit-workspace';
import ChatWorkspace from './chat-workspace';
import ApprovalsWorkspace from './approvals-workspace';
import DailyWorkspace from './daily-workspace';
import SchedulerWorkspace from './scheduler-workspace';
import MetrikaSettings from './metrika-settings';
import CampaignVisibility from './campaign-visibility';
import LiveDashboard from './live-dashboard';
import { Activity, ArrowDownLeft, ArrowRight, ArrowUpRight, BarChart3, Bell, BookOpen, Check, CheckCheck, ChevronDown, ChevronRight, CircleHelp, Download, FileText, FolderKanban, History, LayoutDashboard, Loader2, LogOut, Minus, Plus, Pencil, RefreshCw, Search, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Target, Trash2, Upload, X } from 'lucide-react';

type Project = { archived?:boolean;is_demo: boolean; id: string; name: string; description: string };
type Campaign = { account_name: string; id: string; name: string; account_id: string; project_id: string };
type Metrics = { cost: number; clicks: number; impressions: number; conversions: number; ctr: number; cpc: number; cpa: number };
type Dashboard = { metrics: Metrics; previous: Metrics; daily: (Metrics & { date: string })[]; campaigns: (Metrics & { id: string; name: string })[]; source: string; attribution: string; updated_at: string };
type Draft = { id: string; items: string[]; revision: number; status: string };
type Recommendation = { currency?: string; model?: string; phrase: string; cost: number; clicks: number; conversions: number; confidence: number; reason: string; category: string; risk: string };
type KeywordData = { items: string[]; updated_at: string; source: string; draft: Draft | null; recommendations: Recommendation[] };
type Preview = { demo: boolean; add: string[]; keep: string[]; remove: string[]; warnings: string[]; campaign: string; campaign_id: string; account: string };
type Operation = { id: string; kind: string; created_at: string; data: { diff: { add: string[]; remove: string[] } } };
export type View = 'dashboard' | 'negative' | 'recommendations' | 'projects' | 'settings' | 'approvals' | 'daily' | 'chat' | 'audit' | 'scheduler';

const sections = [
  { label: 'Обзор', icon: LayoutDashboard, pages: ['dashboard', 'daily'], links: [
    ['dashboard', 'Сводка'], ['daily', 'Динамика по дням'],
  ] },
  { label: 'Минус-фразы', icon: SlidersHorizontal, pages: ['negative'], links: [] },
  { label: 'AI-аналитик', icon: Sparkles, pages: ['chat', 'recommendations', 'audit', 'scheduler'], links: [
    ['chat', 'Чат'], ['recommendations', 'Рекомендации'], ['audit', 'Аудит'],
  ] },
  { label: 'Согласования', icon: ShieldCheck, pages: ['approvals'], links: [] },
  { label: 'Настройки', icon: Settings2, pages: ['settings', 'projects'], links: [
    ['settings', 'Подключения'], ['projects', 'Проекты'],
  ] },
] as const satisfies ReadonlyArray<{
  label: string;
  icon: typeof LayoutDashboard;
  pages: readonly [View, ...View[]];
  links: readonly (readonly [View, string])[];
}>;
const fmt = (n: number, digits = 0) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(n);
const money = (n: number) => `${fmt(n)} ₽`;
const dateTime = (value: string) => new Date(value).toLocaleString('ru-RU', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });

async function api<T>(path: string, data?: unknown): Promise<T> {
  const response = await fetch(`/api/plugins/direct-ai-control/api${path}`, { credentials: 'same-origin', method: data === undefined ? 'GET' : 'POST', headers: { 'X-Requested-With': 'DirectAI', ...(data instanceof FormData ? {} : { 'Content-Type': 'application/json' }) }, body: data === undefined ? undefined : data instanceof FormData ? data : JSON.stringify(data) });
  if (!response.ok) {
    const result = await response.json().catch(() => ({ detail: `Ошибка сервера (${response.status})` }));
    throw new Error(Array.isArray(result.detail) ? result.detail.map((x: unknown) => typeof x === 'string' ? x : JSON.stringify(x)).join(' · ') : result.detail);
  }
  return response.json();
}

export default function Workspace({ initialPage }: { initialPage: View }) {
  const [mode, setMode] = useState('demo');
  const [user, setUser] = useState<string | null>(null), [checking, setChecking] = useState(true);
  const [login, setLogin] = useState('admin'), [password, setPassword] = useState('');
  const [page, setPage] = useState<View>(initialPage), [projects, setProjects] = useState<Project[]>([]), [project, setProject] = useState('');
  const [campaigns, setCampaigns] = useState<Campaign[]>([]), [campaign, setCampaign] = useState('');
  const [days, setDays] = useState(30), [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [keywords, setKeywords] = useState<KeywordData | null>(null), [draft, setDraft] = useState<Draft | null>(null), [items, setItems] = useState<string[]>([]);
  const [text, setText] = useState(''), [search, setSearch] = useState(''), [tab, setTab] = useState('draft');
  const [editing, setEditing] = useState<string | null>(null), [editText, setEditText] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null), [history, setHistory] = useState<Operation[]>([]);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [warnings, setWarnings] = useState<string[]>([]), [refresh, setRefresh] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { api<{ login: string; mode: string }>('/auth/me').then(r => { setUser(r.login); setMode(r.mode);  }).catch(() => {}).finally(() => setChecking(false)); api<{mode:string}>('/health').then(r => setMode(r.mode)).catch(() => {}); }, []);
  useEffect(() => {
    if (!user) return;
    api<Project[]>('/projects').then(p => { setProjects(p); setProject(current => p.some(x => x.id === current) ? current : p.find(x => x.id === 'demo-service')?.id || p.find(x => !x.archived)?.id || p[0]?.id || ''); }).catch(e => setError(e.message));
  }, [user, refresh]);
  useEffect(() => {
    if (!user || !project) return;
    let active = true;
    setCampaign(''); setKeywords(null); setDraft(null); setItems([]); setDashboard(null); setSearch(''); setWarnings([]);
    api<Campaign[]>(`/negative-keywords/campaigns?project_id=${project}`).then(cs => { if (active) { setCampaigns(cs); setCampaign(cs[0]?.id || ''); } }).catch(e => active && setError(e.message));
    return () => { active = false; };
  }, [project, user]);
  useEffect(() => {
    if (!user || !project) return;
    let active = true;
    api<Dashboard>(`/dashboard?project_id=${project}&days=${days}`).then(r => active && setDashboard(r)).catch(e => active && setError(e.message));
    return () => { active = false; };
  }, [user, project, days, refresh]);
  useEffect(() => {
    if (!user || !campaign) return;
    let active = true;
    setKeywords(null); setDraft(null); setItems([]); setText(''); setWarnings([]); setPreview(null);
    Promise.all([api<KeywordData>(`/negative-keywords/${campaign}`), api<Operation[]>(`/negative-keywords/history?campaign_id=${campaign}`)]).then(([data, ops]) => {
      if (!active) return;
      setKeywords(data); setDraft(data.draft); setItems(data.draft?.items || data.items); setHistory(ops);
    }).catch(e => active && setError(e.message));
    return () => { active = false; };
  }, [user, campaign, refresh]);
  useEffect(() => { if (!notice) return; const timer = setTimeout(() => setNotice(''), 5500); return () => clearTimeout(timer); }, [notice]);

  const run = async (fn: () => Promise<void>) => { setBusy(true); setError(''); try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const navigate = (next: View) => { setPage(next); window.history.replaceState(window.history.state, '', `/plugins/direct-ai-control?page=${next}`); setError(''); };
  const section = sections.find(({ pages }) => pages.some((view) => view === page));
  const save = async (nextItems = items) => {
    const result = await api<Draft>('/negative-keywords/drafts', { campaign_id: campaign, items: nextItems, draft_id: draft?.id, revision: draft?.revision });
    setDraft(result); setItems(result.items); setWarnings([]); setPreview(null); return result;
  };
  const add = async (phrases: string[]) => {
    const unique = [...new Set([...items, ...phrases.map(x => x.trim()).filter(Boolean)])];
    await save(unique); setText(''); setNotice('Черновик сохранён. Рекламная кампания не изменена.');
  };
  const preparePreview = () => run(async () => { const saved = await save(); setPreview(await api<Preview>('/negative-keywords/preview', { draft_id: saved.id })); setDraft({ ...saved, status: 'Проверено' }); setHistory(await api(`/negative-keywords/history?campaign_id=${campaign}`)); });
  const selectedProject = projects.find(p => p.id === project);
  const isDemo = mode === 'demo';
  const title = { dashboard: 'Обзор рекламы', negative: 'Минус-фразы', recommendations: 'AI-рекомендации', projects: 'Ваши проекты', settings: 'Подключения и настройки', approvals:'Согласование изменений', daily:'Эффективность по дням', chat:'AI-аналитик', audit:'AI-аудит', scheduler:'Планировщик AI' }[page];

  if (checking) return <div className="loading-screen"><Loader2 className="spin" /> Загружаем рабочее пространство…</div>;
  if (!user) return <section className="direct-login">
    <h1>Direct AI Control</h1>
    <form onSubmit={e => {
      e.preventDefault();
      run(async () => {
        const result = await api<{ login: string; mode: string }>('/auth/login', { login, password });
        setUser(result.login); setMode(result.mode); setPassword('');
      });
    }}>
      <p>Войдите в рекламный кабинет.</p>
      <label>Логин<input value={login} onChange={e => setLogin(e.target.value)} autoComplete="username" required /></label>
      <label>Пароль<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required /></label>
      {error && <p role="alert" className="alert error">{error}</p>}
      <button className="btn primary" disabled={busy}>Войти в Direct AI Control</button>
    </form>
  </section>;

  return <div className="app-shell">
    <div className="direct-navigation"><strong>Direct AI Control</strong>

      <nav aria-label="Разделы Direct AI Control">
        {sections.map(({ label, icon: Icon, pages }) => (
          <button key={label} type="button" aria-pressed={section?.label === label} className="nav-item" onClick={() => navigate(pages[0])}>
            <Icon aria-hidden="true" size={15} /><span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="direct-navigation-actions"><button className="icon-button" title="Выйти из Direct" aria-label="Выйти из Direct" onClick={() => run(async () => { await api("/auth/logout", {}); setUser(null); })}><LogOut size={17} /></button></div>
      {section && section.links.length > 0 && (
        <nav className="direct-subnav" aria-label={`Вкладки раздела «${section.label}»`}>
          {section.links.map(([view, label]) => (
            <button key={view} type="button" className="nav-item"
              aria-pressed={page === view || (view === 'audit' && page === 'scheduler')}
              onClick={() => navigate(view)}>
              {label}
            </button>
          ))}
        </nav>
      )}
    </div>
    <div className="main-shell">
      <main><div className="page-heading"><h1>{title}</h1><div className="heading-actions">{page === 'dashboard' && isDemo && <a className="btn" href={`/api/plugins/direct-ai-control/api/reports/export?project_id=${project}&days=${days}&format=xlsx`}><Download size={16} /> Экспорт</a>}<button className="btn" disabled={busy} onClick={() => { setRefresh(x => x+1); setNotice(isDemo ? 'Локальные DEMO-данные перечитаны. Внешние API не вызываются.' : 'Перечитаны сохранённые данные. Для чтения из Яндекса откройте «Подключения».'); }}><RefreshCw size={16} /> {isDemo ? 'Обновить DEMO' : 'Обновить'}</button></div></div>
      {isDemo ? <div className="demo-banner"><div className="demo-symbol"><BookOpen size={17} /></div><div><strong>Знакомьтесь с возможностями на демо-данных</strong><span>Директ и Метрика не подключены. Все показатели и рекомендации — учебные.</span></div><button onClick={() => navigate('settings')}>О подключении <ArrowRight size={15} /></button></div> : <div className="alert neutral"><ShieldCheck size={18} /><div>Рабочий режим · Яндекс подключается через OAuth. AI готовит предложения; изменения требуют отдельного подтверждения и проверки доступа.</div></div>}
      {error && <div role="alert" className="alert error">{error}<button aria-label="Закрыть ошибку" onClick={() => setError('')}><X size={16} /></button></div>}
      {notice && <div role="status" className="toast"><CheckCheck size={18} />{notice}</div>}

      {(isDemo || projects.length > 0) && <div className="filterbar"><div className="filter-main"><label><span>Проект</span><select aria-label="Проект" disabled={busy} value={project} onChange={e => setProject(e.target.value)}>{projects.map(p => <option key={p.id} value={p.id}>{p.name}{p.archived?' · Архив':''}</option>)}</select></label>{page === 'negative' || page === 'recommendations' ? <><label><span>Аккаунт</span><select aria-label="Аккаунт" disabled={busy} value={campaigns.find(c => c.id === campaign)?.account_id || ''} onChange={e => setCampaign(campaigns.find(c => c.account_id === e.target.value)?.id || '')}>{[...new Set(campaigns.map(c => c.account_id))].map(a => <option key={a} value={a}>{campaigns.find(c => c.account_id === a)?.account_name || selectedProject?.name}{isDemo ? ' · DEMO' : ''}</option>)}</select></label><label><span>Кампания</span><select aria-label="Кампания" disabled={busy} value={campaign} onChange={e => setCampaign(e.target.value)}>{campaigns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label></> : <label><span>Источник</span><div className="source-filter"><span className="yandex-icon">Я</span> Директ {isDemo ? '· DEMO' : '· API'}</div></label>}</div>{page === 'dashboard' && <div className="period-tabs">{[7, 30, 90].map(n => <button key={n} className={days === n ? 'selected' : ''} onClick={() => setDays(n)}>{n} дней</button>)}</div>}{page === 'negative' && <span className="muted small">Уровень: кампания</span>}</div>}

      {page === 'dashboard' && isDemo && (!dashboard ? <div className="empty"><Loader2 className="spin" />Загружаем аналитику…</div> : <>
        <div className="metrics-grid">{([
          ['Расход', 'cost', money, false], ['Клики', 'clicks', fmt, true], ['Конверсии', 'conversions', fmt, true], ['Стоимость заявки', 'cpa', money, false],
        ] as const).map(([label, key, format, positive]) => { const change = dashboard.previous[key] ? (dashboard.metrics[key]/dashboard.previous[key]-1)*100 : 0; const good = positive ? change >= 0 : change <= 0; return <div className="metric-card" key={key}><div className="metric-label">{label}<span>{key === 'cost' ? '₽' : key === 'clicks' ? '↗' : key === 'conversions' ? '◎' : '◷'}</span></div><div className="metric-value">{format(dashboard.metrics[key])}</div><div className="metric-foot"><span className={good ? 'trend good' : 'trend bad'}>{change >= 0 ? <ArrowUpRight size={14} /> : <ArrowDownLeft size={14} />}{fmt(Math.abs(change), 1)}%</span><span>к предыдущему периоду</span></div></div>; })}</div>
        <div className="chart-layout"><section className="panel chart-panel"><div className="panel-heading"><div><h2>Динамика эффективности</h2><p>Расход и конверсии по дням</p></div><span className="badge subtle">Директ · DEMO</span></div><div className="chart-legend"><span><i className="legend-swatch green" />Расход</span><span><i className="legend-swatch blue" />Конверсии</span><div className="chart-summary">CTR <b>{fmt(dashboard.metrics.ctr, 2)}%</b><span>·</span>CPC <b>{money(dashboard.metrics.cpc)}</b></div></div><Chart data={dashboard.daily} /><div className="chart-footer"><span><span className="status-dot" /> Сгенерировано {dateTime(dashboard.updated_at)}</span><span>Синтетическая статистика</span></div></section>
        <section className="panel insight-panel"><div className="panel-heading"><h2><Sparkles size={18} /> В фокусе AI</h2><span className="badge mint">3 гипотезы</span></div><div className="insight-block"><span className="insight-icon"><Target size={20} /></span><span className="badge subtle">ПОИСКОВЫЕ ЗАПРОСЫ</span><h3>У трафика есть потенциал</h3><p>В учебной выборке есть 3 запроса, которые стоит проверить перед добавлением в минус-фразы.</p><div className="insight-number">5 220 ₽<span>расход по этим запросам · DEMO</span></div><button className="btn primary" onClick={() => navigate('recommendations')}>Посмотреть рекомендации <ArrowRight size={16} /></button></div><div className="insight-note"><ShieldCheck size={17} />Изменения только после вашего решения</div></section></div>
        <section className="panel"><div className="panel-heading"><div><h2>Кампании в работе</h2><p>{selectedProject?.name} · показатели выбранного периода</p></div><span className="badge subtle">{dashboard.campaigns.length} кампания</span></div><div className="table-wrap"><table><thead><tr><th>Кампания</th><th>Статус</th><th>Расход</th><th>Клики</th><th>CTR</th><th>Конверсии</th><th>CPA</th><th /></tr></thead><tbody>{dashboard.campaigns.map(c => <tr key={c.id}><td><div className="campaign-name"><div className="table-icon"><Search size={17} /></div><div>{c.name}<small>Поиск · учебная кампания</small></div></div></td><td><span className="badge mint"><span className="status-dot" /> DEMO</span></td><td>{money(c.cost)}</td><td>{fmt(c.clicks)}</td><td>{fmt(c.ctr, 2)}%</td><td>{c.conversions}</td><td>{money(c.cpa)}</td><td><button aria-label="Открыть минус-фразы" className="icon-button" onClick={() => { setCampaign(c.id); navigate('negative'); }}><ArrowUpRight size={17} /></button></td></tr>)}</tbody></table></div><div className="source-note">{dashboard.source} · {dashboard.attribution}</div></section>
      </>)}

      {project && <CampaignVisibility projectId={project}/>}
      {page === 'dashboard' && !isDemo && <LiveDashboard projectId={project} days={days} refresh={refresh} onSettings={() => navigate('settings')} onCampaign={id => {setCampaign(id);navigate('negative');}} />}
      {page === 'audit' && <>
        <div className="direct-audit-actions">
          <button className="btn" type="button" onClick={() => navigate('scheduler')}><History aria-hidden="true" size={16} />Планировщик аудита</button>
        </div>
        <AuditWorkspace projectId={project} />
      </>}
      {page === 'scheduler' && <>
        <div className="direct-audit-actions">
          <button className="btn" type="button" onClick={() => navigate('audit')}>К аудиту</button>
        </div>
        <SchedulerWorkspace projectId={project} />
      </>}
      {page === 'chat' && <ChatWorkspace project={selectedProject} />}
      {page === 'approvals' && <ApprovalsWorkspace />}
      {page === 'daily' && <DailyWorkspace projectId={project} />}
      {page === 'recommendations' && !isDemo && <AIWorkspace project={selectedProject} days={days} onDays={setDays} onSettings={() => navigate('settings')} />}
      {(page === 'negative' || (page === 'recommendations' && isDemo)) && (!keywords ? <div className="empty">{campaign ? <><Loader2 className="spin" />Загружаем минус-фразы…</> : <>Подключите аккаунт и прочитайте кампании в разделе «Подключения».</>}</div> : <>
        {page === 'negative' && <><div className="negative-summary"><div><span className="summary-number">{keywords.items.length}</span><span>исходных фраз</span></div><div><span className="summary-number green-text">+{items.filter(x => !keywords.items.includes(x)).length}</span><span>добавлено в черновик</span></div><div><span className="summary-number">{keywords.items.filter(x => !items.includes(x)).length}</span><span>к удалению</span></div><div className="summary-status"><span className="badge mint">{draft?.status || 'Исходный список'}</span><small>Сохранено на сервере {isDemo ? '· DEMO' : '· API'}</small></div></div><div className="tab-row">{[['draft', 'Черновик', items.length], ['current', 'Исходные фразы', keywords.items.length], ['ai', 'AI-рекомендации', keywords.recommendations.length], ['history', 'История', history.length]].map(([id, label, count]) => <button key={id} onClick={() => setTab(String(id))} className={tab === id ? 'active' : ''}>{label}<span>{count}</span></button>)}</div></>}
        {(page === 'recommendations' || tab === 'ai') ? <div className="recommendation-grid">{!keywords.recommendations.length && <div className="panel setting-card">Готовых минус-фраз пока нет. В разделе «AI-рекомендации» доступен анализ после активации OpenAI.</div>}{keywords.recommendations.map(r => <section key={r.phrase} className="panel recommendation-card"><div className="recommendation-top"><Sparkles size={19} /><span className="badge subtle">{isDemo ? 'DEMO · пример AI' : `GPT · ${r.model || 'OpenAI'}`}</span></div><h2>«{r.phrase}»</h2><span className="badge amber">{r.category}</span><p>{r.reason}</p><div className="rec-metrics"><div><b>{isDemo ? money(r.cost) : `${fmt(r.cost)} ${r.currency || 'валюта аккаунта'}`}</b><span>Расход</span></div><div><b>{r.clicks}</b><span>Клики</span></div><div><b>{r.conversions}</b><span>Конверсии</span></div></div><div className="confidence"><span>Учебная уверенность</span><b>{r.confidence}%</b></div><div className="confidence-bar"><i style={{ width: `${r.confidence}%` }} /></div><small className="risk-note">{r.risk}. Малая выборка не доказывает неэффективность.</small><button className="btn" disabled={busy || items.includes(r.phrase)} onClick={() => run(async () => { await add([r.phrase]); })}>{items.includes(r.phrase) ? <><Check size={16} />В черновике</> : <><Plus size={16} />Добавить в черновик</>}</button></section>)}</div> : tab === 'history' ? <section className="panel"><div className="panel-heading"><div><h2>История проверок</h2><p>Предпросмотры и черновики. Отправок в Директ не было.</p></div><History size={20} /></div>{!history.length ? <div className="empty">Здесь появятся сохранённые предпросмотры.</div> : history.map(op => <div key={op.id} className="history-row"><div className="table-icon"><History size={18} /></div><div><strong>{op.kind}</strong><small>{dateTime(op.created_at)} · Добавить: {op.data.diff.add.length} · Удалить: {op.data.diff.remove.length}</small></div><button className="btn" disabled={busy} onClick={() => run(async () => { const d = await api<Draft>('/negative-keywords/rollback-preview', { operation_id: op.id }); setDraft(d); setItems(d.items); setTab('draft'); setNotice('Создан черновик возврата к исходному набору. Ничего не отправлено.'); })}>Черновик возврата</button></div>)}</section> : <div className="negative-layout"><section className="panel phrase-panel"><div className="panel-heading"><div><h2>{tab === 'current' ? (isDemo ? 'Исходный список DEMO' : 'Прочитанный список Директа') : 'Минус-фразы в черновике'}</h2><p>Уровень кампании · {keywords.source}</p></div><span className="badge subtle">{tab === 'current' ? keywords.items.length : items.length} фраз</span></div><div className="search-field"><Search size={17} /><input aria-label="Поиск минус-фраз" placeholder="Найти минус-фразу…" value={search} onChange={e => setSearch(e.target.value)} /></div><div className="table-wrap"><table><thead><tr><th>Минус-фраза</th><th>Уровень</th><th>Статус</th><th /></tr></thead><tbody>{(tab === 'current' ? keywords.items : items).filter(x => x.toLowerCase().includes(search.toLowerCase())).map((phrase, i) => <tr key={`${phrase}-${i}`}><td>{editing === phrase ? <div className="inline-edit"><input aria-label="Редактирование фразы" value={editText} onChange={e => setEditText(e.target.value)} /><button className="icon-button" aria-label="Сохранить фразу" disabled={busy || !editText.trim()} onClick={() => run(async () => { await save(items.map(x => x === phrase ? editText.trim() : x)); setEditing(null); })}><Check size={15} /></button><button className="icon-button" aria-label="Отменить редактирование" onClick={() => setEditing(null)}><X size={15} /></button></div> : <><span className="phrase-minus">−</span>{phrase}</>}</td><td className="muted">Кампания</td><td><span className={`badge ${keywords.items.includes(phrase) ? 'subtle' : 'mint'}`}>{keywords.items.includes(phrase) ? 'Сохранить' : 'Добавить'}</span></td><td>{tab !== 'current' && <div className="row-actions"><button className="icon-button" aria-label={`Редактировать ${phrase}`} disabled={busy} onClick={() => { setEditing(phrase); setEditText(phrase); }}><Pencil size={14} /></button><button className="icon-button delete-button" aria-label={`Удалить ${phrase}`} disabled={busy} onClick={() => run(async () => { await save(items.filter(x => x !== phrase)); })}><Trash2 size={15} /></button></div>}</td></tr>)}</tbody></table></div><div className="source-note">{isDemo ? 'Исходный набор создан' : 'Прочитано из Директа'} {dateTime(keywords.updated_at)}. {isDemo ? 'Внешней синхронизации нет.' : 'Это кэш последнего чтения.'}</div></section>{tab !== 'current' && <aside className="panel editor-panel"><div className="panel-heading"><h2><Plus size={18} /> Добавить фразы</h2></div><div className="editor-body"><p>По одной фразе в строке. Операторы сохраняются без изменений.</p><textarea aria-label="Новые минус-фразы" placeholder={'своими руками\nбесплатно\nскачать схему'} value={text} onChange={e => setText(e.target.value)} rows={7} /><button className="btn primary full-width" disabled={busy || !text.trim()} onClick={() => run(async () => { await add(text.split('\n')); })}><Plus size={16} />В черновик</button><div className="or-divider">или</div><input hidden type="file" ref={fileRef} accept=".txt,.csv,.xlsx" onChange={e => { const file = e.target.files?.[0]; if (!file) return; run(async () => { const body = new FormData(); body.set('file', file); const result = await api<{ items: string[]; errors: string[] }>('/negative-keywords/import', body); if (result.errors.length) throw new Error(result.errors.join(' · ')); await add(result.items); }); e.target.value = ''; }} /><button className="btn full-width" disabled={busy} onClick={() => fileRef.current?.click()}><Upload size={16} />Импорт файла</button><small className="file-hint">TXT, CSV или XLSX · до 2 МБ<br />Первая колонка, без заголовка · UTF-8</small>{isDemo && <button className="text-button" disabled={busy} onClick={() => run(async () => { await add(['своими руками', 'бесплатно', 'скачать схему']); })}><Sparkles size={15} />Вставить тестовые фразы из ТЗ</button>}</div><div className="editor-safety"><ShieldCheck size={18} /><span>Черновик не меняет рекламу</span></div></aside>}</div>}
        {page === 'negative' && tab === 'draft' && <><div className="alert neutral"><ShieldCheck size={18} /><div>Проверка в DEMO использует базовые правила. Ограничения API по типам кампаний и полный анализ операторов будут добавлены вместе с интеграцией.</div></div>{warnings.map((w, i) => <div key={i} className="alert warning">{w}</div>)}<div className="actionbar"><div><ShieldCheck size={20} /><span>Изменения сохраняются только в локальном черновике</span></div><div><button className="btn" disabled={busy || !draft} onClick={() => run(async () => { if (draft) await api('/negative-keywords/cancel', { draft_id: draft.id }); setDraft(null); setItems(keywords.items); setNotice('Черновик отменён. Исходный список сохранён.'); })}>Отмена</button><button className="btn" disabled={busy} onClick={() => run(async () => { const d = await save(); const r = await api<{ warnings: string[]; errors: string[]; valid: boolean; status: string }>('/negative-keywords/validate', { draft_id: d.id }); setWarnings(r.warnings); setDraft({ ...d, status: r.status }); if (!r.valid) throw new Error(r.errors.join(' · ')); setNotice('Базовая проверка пройдена. Можно открыть предпросмотр.'); })}><CheckCheck size={16} />Проверить</button><button className="btn primary" disabled={busy} onClick={preparePreview}>Предпросмотр <ArrowRight size={16} /></button></div></div></>}
      </>)}

      {page === 'projects' && <div className="project-grid">{projects.map((p, i) => <button key={p.id} className={`panel project-card ${project === p.id ? 'selected-project' : ''}`} onClick={() => { setProject(p.id); navigate('dashboard'); }}><div className={`project-icon project-color-${i}`}><FolderKanban size={26} /></div><span className={`badge ${p.is_demo ? 'demo' : 'mint'}`}>{p.archived?'АРХИВ':p.is_demo ? 'DEMO' : 'ДИРЕКТ API'}</span><h2>{p.name}</h2><p>{p.description}</p><div className="project-card-footer"><span>{p.is_demo ? '1 учебная кампания' : 'Открыть реальные кампании'}</span><ArrowUpRight size={19} /></div></button>)}</div>}
      {page === 'settings' && <><YandexSettings onSynced={() => setRefresh(x => x+1)} /><MetrikaSettings /><OpenAISettings /><TelegramSettings /><section className="panel settings-panel"><div className="panel-heading"><div><h2>Источники данных</h2><p>Директ и OpenAI настраиваются выше. Остальные интеграции будут добавлены отдельно.</p></div><ShieldCheck size={21} /></div>{[['CRM и телефония', 'Квалификация лидов · не настроены', '↗']].map(([name, desc, letter]) => <div className="integration-row" key={name}><div className="integration-icon">{letter}</div><div><h3>{name}</h3><p>{desc}</p></div><span className="badge subtle">Не подключён</span></div>)}</section><div className="settings-grid"><section className="panel setting-card"><ShieldCheck size={25} /><h2>Безопасность черновиков</h2><p>Сессии хранятся на сервере, cookie недоступна JavaScript. Каждая кампания проверяется по правам пользователя. Все сохранения и отмены попадают в аудит.</p></section><section className="panel setting-card"><FileText size={25} /><h2>Подключение реального аккаунта</h2><p>Создайте OAuth-приложение Яндекса и заявку API Директа. Сохраните Client ID и Client Secret в форме выше, затем подтвердите доступ на стороне Яндекса.</p></section></div></>}
      <footer className="page-footer"><span>direct / ai control<span>·</span> Работайте с данными. Решайте осознанно.</span><span><ShieldCheck size={14} /> Без автоматических изменений</span></footer>
      </main>
    </div>
    {preview && <div className="modal-backdrop" onClick={() => setPreview(null)}><section role="dialog" aria-modal="true" aria-labelledby="preview-title" className="modal" onClick={e => e.stopPropagation()}><div className="panel-heading"><div><span className="badge demo">{preview.demo ? 'DEMO · ТОЛЬКО ПРЕДПРОСМОТР' : 'ТОЛЬКО ПРЕДПРОСМОТР'}</span><h2 id="preview-title">Проверьте изменения</h2></div><button aria-label="Закрыть предпросмотр" className="icon-button" onClick={() => setPreview(null)}><X size={20} /></button></div><div className="modal-body"><div className="preview-account"><strong>{preview.campaign}</strong><span>{preview.account}</span><small>ID: {preview.campaign_id} {preview.demo ? '· учебный идентификатор' : '· внутренний идентификатор'}</small></div><div className="diff-grid">{([['add', 'Добавить', 'green-text'], ['keep', 'Оставить', ''], ['remove', 'Удалить', 'red-text']] as const).map(([key, label, color]) => <div key={key}><h3 className={color}>{label} <span>{preview[key].length}</span></h3><div className="diff-list">{preview[key].map(x => <div key={x}>{key === 'add' ? '+' : key === 'remove' ? '−' : '·'} {x}</div>)}{!preview[key].length && <span className="muted">Нет фраз</span>}</div></div>)}</div>{preview.warnings.map(w => <div key={w} className="alert warning">{w}</div>)}<div className="alert neutral"><ShieldCheck size={19} />Отправка отключена. Доступ только на чтение: токен подтверждения не выдаётся, реклама не изменяется.</div></div><div className="modal-footer"><button className="btn" onClick={() => setPreview(null)}>Вернуться к черновику</button><button className="btn primary" disabled>Применить в Яндекс Директе</button></div></section></div>}
  </div>;
}

function Chart({ data }: { data: Dashboard['daily'] }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(...data.map(d => d.cost), 1)*1.2;
  const maxConv = Math.max(...data.map(d => d.conversions), 1)*1.3;
  const width = 800, height = 220, left = 52, right = 18, top = 16, bottom = 30;
  const x = (i: number) => left + i / Math.max(data.length-1, 1)*(width-left-right);
  const y = (n: number, m: number) => height-bottom - n/m*(height-top-bottom);
  const line = (key: 'cost' | 'conversions', m: number) => data.map((d, i) => `${i ? 'L' : 'M'} ${x(i)} ${y(d[key], m)}`).join(' ');
  const area = `${line('cost', max)} L ${x(data.length-1)} ${height-bottom} L ${left} ${height-bottom} Z`;
  return <div className="chart-container"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="График демонстрационного расхода и конверсий по дням" onMouseLeave={() => setHover(null)}><defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop stopColor="#16a577" stopOpacity=".18" /><stop offset="1" stopColor="#16a577" stopOpacity=".01" /></linearGradient></defs>{[0,1,2,3,4].map(i => { const value = max*i/4; return <g key={i}><line x1={left} x2={width-right} y1={y(value, max)} y2={y(value, max)} stroke="#e9eeec" strokeDasharray="4 5" /><text x={left-12} y={y(value, max)+4} textAnchor="end" fill="#8d9895" fontSize="11">{fmt(value/1000, 1)}к</text></g>; })}<path d={area} fill="url(#area)" /><path d={line('cost', max)} fill="none" stroke="#13976e" strokeWidth="2.6" strokeLinejoin="round" /><path d={line('conversions', maxConv)} fill="none" stroke="#8da6ee" strokeWidth="2" strokeLinejoin="round" strokeDasharray="5 5" />{data.map((d, i) => <g key={d.date}>{i % Math.max(Math.floor(data.length/6), 1) === 0 && <text x={x(i)} y={height-7} textAnchor="middle" fill="#8d9895" fontSize="11">{new Date(d.date).toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })}</text>}<rect x={x(i)-(width-left-right)/data.length/2} y={top} width={(width-left-right)/data.length+2} height={height-bottom-top} fill="transparent" onMouseEnter={() => setHover(i)} /></g>)}{hover !== null && <g pointerEvents="none"><line x1={x(hover)} x2={x(hover)} y1={top} y2={height-bottom} stroke="#13976e" strokeDasharray="3 3" /><circle cx={x(hover)} cy={y(data[hover].cost, max)} r="4" fill="#13976e" stroke="white" strokeWidth="2" /></g>}</svg>{hover !== null && <div className="chart-tooltip">{data[hover].date} · {money(data[hover].cost)} · {data[hover].conversions} конв.</div>}</div>;
}
