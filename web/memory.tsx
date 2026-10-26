import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';
import { LifePlan } from './life-plan';
import type { LifePlanHandle } from './life-plan';

type MemoryKind = 'fact' | 'inference' | 'fiction' | 'preference' | 'life';
type MemoryItem = {
  id: string; project_id: string | null; kind: MemoryKind; content: string; basis: string;
  allow_chat: boolean; allow_writing: boolean; source_message_id: string | null; source_task_id: string | null;
  source_deleted: boolean; created_at: string; updated_at: string;
  life_kind: 'event' | 'dream' | null; occurred_at: string | null; shared_at: string | null;
  source_chapter_id: string | null; source_version: number | null;
};
type MemoryMapEdge = { source: string; target: string; type: 'source' | 'project' | 'timeline' | 'semantic'; distance?: number };
type MemoryMapData = { nodes: MemoryItem[]; edges: MemoryMapEdge[]; semantic_enabled: boolean };
type Source = { status: 'agent' | 'manual' | 'deleted'; message: null }
  | { status: 'chapter'; chapter: { id: string; title: string; version: number; content: string } };
type Draft = Pick<MemoryItem, 'project_id' | 'kind' | 'content' | 'basis' | 'allow_chat' | 'allow_writing' | 'life_kind' | 'occurred_at'>;
type Modal = { kind: 'leave' } | { kind: 'memory-delete'; item: MemoryItem } | { kind: 'source'; source: Source };
export type MemoryHandle = { requestLeave: (action: () => void) => void };

const kinds: Record<MemoryKind, string> = { fact: '明确事实', inference: '推测', fiction: '文学想象', preference: '相处偏好', life: '虚拟生活' };
const pageSize = 30;
const emptyDraft = (): Draft => ({ content: '', kind: 'fact', basis: '', project_id: null, allow_chat: true, allow_writing: false, life_kind: null, occurred_at: null });
const draftOf = (item: MemoryItem): Draft => ({ content: item.content, kind: item.kind, basis: item.basis, project_id: item.project_id, allow_chat: item.allow_chat, allow_writing: item.allow_writing, life_kind: item.life_kind ?? null, occurred_at: item.occurred_at ?? null });
const time = (value: string) => new Date(value).toLocaleString('zh-CN');
const localTime = (value: string) => {
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
};
const sourceLabel = (item: MemoryItem) => item.source_chapter_id ? `来自日记 · 版本 ${item.source_version}` : item.source_task_id ? '由聊天 Agent 从本轮内容提升' : '手动添加';

const mapKinds: MemoryKind[] = ['fact', 'preference', 'life', 'inference', 'fiction'];
const mapColors: Record<MemoryKind, string> = { fact: '#426653', preference: '#9a6b35', life: '#6a5a8d', inference: '#8d5848', fiction: '#477783' };
const mapEdgeColors: Record<MemoryMapEdge['type'], string> = { source: '#426653', project: '#9a6b35', timeline: '#6a5a8d', semantic: '#9a9d8e' };

function MemoryMap({ data, projects, onOpen }: { data: MemoryMapData; projects: { id: string; title: string }[]; onOpen: (item: MemoryItem) => void }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const positions = new Map<string, { x: number; y: number }>();
  const width = 1000;
  const height = 570;
  const grouped = new Map<MemoryKind, MemoryItem[]>();
  for (const kind of mapKinds) grouped.set(kind, []);
  for (const item of data.nodes) grouped.get(item.kind)?.push(item);
  mapKinds.forEach((kind, band) => {
    const items = grouped.get(kind) ?? [];
    items.forEach((item, index) => {
      const x = items.length === 1 ? width / 2 : 72 + (index * (width - 144)) / (items.length - 1);
      positions.set(item.id, { x, y: 72 + band * 106 });
    });
  });
  const selected = data.nodes.find(item => item.id === selectedId) ?? null;
  const projectName = (id: string | null) => id ? projects.find(project => project.id === id)?.title ?? '作品记忆' : '共同记忆';
  function activate(item: MemoryItem) { setSelectedId(item.id); onOpen(item); }
  function handleKey(event: ReactKeyboardEvent<SVGCircleElement>, item: MemoryItem) {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(item); }
  }
  return <section className="memory-map" aria-labelledby="memory-map-title">
    <div className="memory-map-intro"><div><h2 id="memory-map-title">记忆地图</h2><p>位置按记忆类型排列，连线只表示来源、作品、时间或语义相近，不代表事实因果。</p></div><span className="memory-map-count">{data.nodes.length} 条记忆</span></div>
    <div className="memory-map-canvas">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby="memory-map-title memory-map-description">
        <desc id="memory-map-description">按记忆类型排列的记忆节点及其来源、作品、时间和语义关系。</desc>
        {data.edges.map((edge, index) => { const from = positions.get(edge.source); const to = positions.get(edge.target); if (!from || !to) return null; return <line key={`${edge.source}-${edge.target}-${edge.type}-${index}`} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke={mapEdgeColors[edge.type]} strokeWidth={edge.type === 'semantic' ? 1 : 1.5} strokeDasharray={edge.type === 'semantic' ? '4 5' : undefined} opacity={edge.type === 'semantic' ? 0.6 : 0.72} />; })}
        {data.nodes.map(item => { const position = positions.get(item.id)!; const active = selectedId === item.id; return <g key={item.id} className={`memory-map-node${active ? ' is-selected' : ''}`} transform={`translate(${position.x} ${position.y})`}>
          <circle r={active ? 10 : 7} fill={mapColors[item.kind]} role="button" tabIndex={0} aria-label={`${kinds[item.kind]}：${item.content}`} onClick={() => activate(item)} onKeyDown={event => handleKey(event, item)} />
          <circle r={active ? 16 : 12} fill="none" stroke={mapColors[item.kind]} opacity={active ? 0.55 : 0.18} pointerEvents="none" />
          <text x="0" y="28" textAnchor="middle">{item.content.length > 18 ? `${item.content.slice(0, 18)}…` : item.content}</text>
        </g>; })}
      </svg>
    </div>
    <div className="memory-map-legend" aria-label="记忆地图图例">{mapKinds.map(kind => <span key={kind}><i style={{ backgroundColor: mapColors[kind] }} />{kinds[kind]}</span>)}<span><i className="memory-map-line" />来源 / 作品 / 时间</span><span><i className="memory-map-line dashed" />语义相近{data.semantic_enabled ? '' : '（索引未启用）'}</span></div>
    {selected && <div className="memory-map-detail"><p className="memory-meta"><span className="memory-kind">{kinds[selected.kind]}</span><span>{selected.kind === 'life' ? 'LISA 的生活' : projectName(selected.project_id)}</span></p><p className="memory-content">{selected.content}</p><button className="text-button" onClick={() => onOpen(selected)}>打开记忆</button></div>}
  </section>;
}

export const Memory = forwardRef<MemoryHandle>(function Memory(_, ref) {
  const [tab, setTab] = useState<'memory' | 'plan'>('memory');
  const [view, setView] = useState<'list' | 'map'>('list');
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [projects, setProjects] = useState<{ id: string; title: string }[]>([]);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [mapData, setMapData] = useState<MemoryMapData | null>(null);
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState(false);
  const [selected, setSelected] = useState<MemoryItem | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [baseline, setBaseline] = useState<Draft>(emptyDraft);
  const [modal, setModal] = useState<Modal | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const contentInput = useRef<HTMLTextAreaElement>(null);
  const pending = useRef<(() => void) | null>(null);
  const lifePlan = useRef<LifePlanHandle>(null);
  const dirty = editing && JSON.stringify(draft) !== JSON.stringify(baseline);

  function failure(cause: unknown) { setError(cause instanceof Error ? cause.message : '操作失败，请重试。'); }
  function changeDraft(next: Draft) { setDraft(next); setNotice(''); }
  function changeKind(next: MemoryKind) {
    changeDraft({ ...draft, kind: next, basis: next === 'inference' ? draft.basis : '',
      project_id: next === 'life' ? null : draft.project_id,
      life_kind: next === 'life' ? draft.life_kind ?? 'event' : null,
      occurred_at: next === 'life' ? draft.occurred_at : null });
  }
  function requestLeave(action: () => void) {
    if (busy) return;
    if (tab === 'plan') { lifePlan.current?.requestLeave(action); return; }
    if (dirty) { pending.current = action; setModal({ kind: 'leave' }); }
    else action();
  }
  useImperativeHandle(ref, () => ({ requestLeave }));

  useEffect(() => {
    let active = true;
    void request<{ id: string; title: string }[]>('/projects').then(items => { if (active) setProjects(items); }).catch(cause => { if (active) failure(cause); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    if (tab === 'plan') { setLoading(false); return () => { active = false; }; }
    const params = new URLSearchParams({ query, limit: String(pageSize), offset: String(offset) });
    if (kind) params.set('kind', kind);
    async function load() {
      try {
        if (tab === 'memory') {
          const items = await request<MemoryItem[]>(`/memories?${params}`);
          if (active) setMemories(items);
        }
      } catch (cause) { if (active) failure(cause); }
      finally { if (active) setLoading(false); }
    }
    void load();
    return () => { active = false; };
  }, [tab, query, kind, offset, refresh]);
  useEffect(() => {
    if (tab !== 'memory' || view !== 'map') return;
    let active = true;
    setMapLoading(true); setMapError('');
    const params = new URLSearchParams({ query, limit: '120' });
    if (kind) params.set('kind', kind);
    request<MemoryMapData>(`/memory-map?${params}`).then(value => { if (active) setMapData(value); }).catch(cause => { if (active) setMapError(cause instanceof Error ? cause.message : '无法读取记忆地图。'); }).finally(() => { if (active) setMapLoading(false); });
    return () => { active = false; };
  }, [tab, view, query, kind, refresh]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    if (!editing) return;
    const shortcut = (event: globalThis.KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (!busy && !modal) form.current?.requestSubmit();
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, [editing, busy, modal]);
  useEffect(() => { if (modal) dialog.current?.showModal(); else dialog.current?.close(); }, [modal]);
  useEffect(() => { if (editing) contentInput.current?.focus(); }, [editing, selected?.id]);

  function openEditor(item: MemoryItem | null) {
    const next = item ? draftOf(item) : emptyDraft();
    setSelected(item); setDraft(next); setBaseline(item ? next : emptyDraft());
    setEditing(true); setTab('memory'); setError(''); setNotice('');
  }
  function switchTab(next: 'memory' | 'plan') {
    requestLeave(() => { setTab(next); setView('list'); setEditing(false); setSearch(''); setQuery(''); setOffset(0); setNotice(''); });
  }
  async function save() {
    if (!form.current?.reportValidity()) return false;
    setBusy(true); setError(''); setNotice('');
    try {
      const item = selected
        ? await request<MemoryItem>(`/memories/${selected.id}`, draft, 'PUT')
        : await request<MemoryItem>('/memories', draft);
      setSelected(item); setDraft(draftOf(item)); setBaseline(draftOf(item));
      setNotice('记忆已保存，后续交流与创作按已保存的用途使用。'); setRefresh(value => value + 1);
      return true;
    } catch (cause) { failure(cause); return false; }
    finally { setBusy(false); }
  }
  async function resolveLeave(shouldSave: boolean) {
    setModal(null);
    if (shouldSave && !(await save())) return;
    pending.current?.(); pending.current = null;
  }
  async function viewSource(item: MemoryItem) {
    setBusy(true); setError('');
    try { setModal({ kind: 'source', source: await request<Source>(`/memories/${item.id}/source`) }); }
    catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  async function confirmDelete() {
    if (!modal || modal.kind === 'leave' || modal.kind === 'source') return;
    setBusy(true); setError(''); setNotice('');
    try {
      await request(`/memories/${modal.item.id}`, undefined, 'DELETE');
      if (selected?.id === modal.item.id) setEditing(false);
      setModal(null); setOffset(0); setRefresh(value => value + 1);
    } catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  const count = memories.length;

  return <main id="main" className="works-page memory-page" tabIndex={-1}>
    <div className="memory-heading"><div><p className="eyebrow">记得，也可以重新理解</p><h1>记忆与日常</h1><p className="page-subtitle">哪些事留下，如何使用，由你决定。</p></div>{tab !== 'plan' && <button className="primary" disabled={busy} onClick={() => requestLeave(() => openEditor(null))}>添加记忆</button>}</div>
    <nav className="work-tabs" aria-label="记忆页面"><button aria-current={tab === 'memory' ? 'page' : undefined} onClick={() => { if (tab !== 'memory') switchTab('memory'); }}>记忆条目</button><button aria-current={tab === 'plan' ? 'page' : undefined} onClick={() => { if (tab !== 'plan') switchTab('plan'); }}>每周提案</button></nav>
    {error && !modal && <ErrorNotice message={error} />}
    {notice && <p className="memory-notice" role="status">{notice}</p>}
    {tab === 'plan' && <LifePlan ref={lifePlan} />}
    {tab !== 'plan' && editing && <section className="material-paper memory-editor" aria-labelledby="memory-editor-title">
      <div className="panel-toolbar"><h2 id="memory-editor-title">{selected ? '纠正与用途' : '添加记忆'}</h2><button className="text-button" disabled={busy} onClick={() => requestLeave(() => setEditing(false))}>返回列表</button></div>
      <p className="form-copy">保存后生效。LISA 不保存原始聊天；这里的记忆只来自你的明确输入，或聊天 Agent 在本轮内容中主动提升的条目。</p>
      <form ref={form} onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={busy}>
          <div className="field"><label htmlFor="memory-content">记忆内容</label><textarea id="memory-content" ref={contentInput} required maxLength={2000} rows={5} value={draft.content} onChange={event => changeDraft({ ...draft, content: event.target.value })} /><p className="field-hint">最多 2,000 个字符，只保留需要记住的要点。</p></div>
          <div className="memory-fields"><div className="field"><label htmlFor="memory-kind">记忆类型</label><select id="memory-kind" value={draft.kind} onChange={event => changeKind(event.target.value as MemoryKind)}>{Object.entries(kinds).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div><div className="field"><label htmlFor="memory-project">作用范围</label><select id="memory-project" disabled={draft.kind === 'life'} value={draft.project_id ?? ''} onChange={event => changeDraft({ ...draft, project_id: event.target.value || null })}><option value="">{draft.kind === 'life' ? 'LISA 的虚拟生活（跨作品）' : '我与 LISA 的共同记忆'}</option>{projects.map(project => <option key={project.id} value={project.id}>{project.title}</option>)}</select></div></div>
          {draft.kind === 'inference' && <div className="field"><label htmlFor="memory-basis">推测依据</label><textarea id="memory-basis" required maxLength={1000} rows={3} value={draft.basis} onChange={event => changeDraft({ ...draft, basis: event.target.value })} /><p className="field-hint">简述推测的理由，避免把推测写成已经发生的事实。</p></div>}
          {draft.kind === 'life' && <>
            <div className="memory-fields"><div className="field"><label htmlFor="life-kind">生活性质</label><select id="life-kind" value={draft.life_kind ?? 'event'} onChange={event => changeDraft({ ...draft, life_kind: event.target.value as 'event' | 'dream' })}><option value="event">已发生的虚拟事件</option><option value="dream">做过的梦</option></select></div><div className="field"><label htmlFor="life-occurred">发生时间</label><input id="life-occurred" type="datetime-local" step="1" required value={draft.occurred_at ? localTime(draft.occurred_at) : ''} onChange={event => changeDraft({ ...draft, occurred_at: event.target.value ? new Date(event.target.value).toISOString() : null })} /><p className="field-hint">按当前设备的本地时区填写。</p></div></div>
            <p className="work-warning">记录 LISA 的虚拟经历，跨作品保留。梦境只表示做过这个梦，不表示梦中事件实际发生。计划、想象与比喻请使用其他记忆类型；手动改稿不会自动修改这里的记录。</p>
            <p className="life-sharing">{selected?.shared_at ? `已分享 · ${time(selected.shared_at)}` : '尚无成功分享记录'}<span>事件成立与是否已经向你分享分别记录，分享状态由实际 QQ 发送结果更新。</span></p>
          </>}
          <fieldset className="memory-uses"><legend>允许使用的场景</legend><label><input type="checkbox" checked={draft.allow_chat} onChange={event => changeDraft({ ...draft, allow_chat: event.target.checked })} />日常交流</label><label><input type="checkbox" checked={draft.allow_writing} onChange={event => changeDraft({ ...draft, allow_writing: event.target.checked })} />作品创作</label></fieldset>
          <p className="field-hint">关闭作品创作后，这条记忆不会再作为后续作品素材。两个用途都关闭时，仅保留供你查看。</p>
          <p className="memory-source-label">来源：{selected ? sourceLabel(selected) : '手动添加'}</p>
        </fieldset>
        <div className="save-bar"><button className="primary" disabled={busy}>{busy ? '正在处理…' : '保存记忆'}</button><p role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : selected ? '已保存' : '填写后保存 · Ctrl+S'}</p></div>
      </form>
    </section>}
    {tab === 'memory' && !editing && <>
      <div className="memory-view-tabs" role="tablist" aria-label="记忆呈现方式"><button role="tab" aria-selected={view === 'list'} className={view === 'list' ? 'is-selected' : ''} onClick={() => setView('list')}>列表</button><button role="tab" aria-selected={view === 'map'} className={view === 'map' ? 'is-selected' : ''} onClick={() => setView('map')}>记忆地图</button></div>
      <form className="memory-search" onSubmit={event => { event.preventDefault(); setQuery(search.trim()); setOffset(0); setRefresh(value => value + 1); }}><div className="field"><label htmlFor="memory-search">搜索记忆</label><input id="memory-search" type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="输入想找的内容" /></div><div className="field"><label htmlFor="memory-filter">类型筛选</label><select id="memory-filter" value={kind} onChange={event => { setKind(event.target.value); setOffset(0); }}><option value="">全部类型</option>{Object.entries(kinds).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></div><button className="secondary" disabled={loading}>搜索</button></form>
      {kind === 'life' && <p className="field-hint life-timeline-note">虚拟生活时间线 · 按发生时间由近到远排列。尚无成功分享记录，不代表事件尚未成立。</p>}
      {view === 'map' ? mapLoading ? <p className="loading-note" role="status">正在读取记忆地图…</p> : mapError ? <><ErrorNotice message={mapError} /><button className="secondary" onClick={() => setRefresh(value => value + 1)}>重新读取</button></> : !mapData?.nodes.length ? <div className="empty-paper"><h2>{query || kind ? '没有符合条件的记忆' : '还没有留下记忆'}</h2><p>记忆地图会根据已保存的记忆显示节点和关系。</p></div> : <MemoryMap data={mapData} projects={projects} onOpen={openEditor} /> : loading ? <p className="loading-note" role="status">正在读取记忆…</p> : error ? <button className="secondary" onClick={() => setRefresh(value => value + 1)}>重新读取</button> : count === 0 ? <div className="empty-paper"><h2>{query || kind || offset ? '没有符合条件的记录' : '还没有留下记忆'}</h2><p>可以手动添加记忆；聊天 Agent 也只会在本轮内容中主动提升明确条目。</p></div> : <div className="memory-list" aria-label="记忆条目列表">
        {memories.map(item => <article className="memory-entry" key={item.id}>
          <div className="memory-meta"><span className="memory-kind">{kinds[item.kind]}</span><span>{item.kind === 'life' ? 'LISA 的生活' : item.project_id ? projects.find(project => project.id === item.project_id)?.title ?? '作品记忆' : '共同记忆'}</span><time dateTime={item.updated_at}>{time(item.updated_at)}</time></div>
          {item.kind === 'life' && <div className="life-meta"><span>{item.life_kind === 'dream' ? '做过的梦' : '已发生的虚拟事件'}</span>{item.occurred_at && <span>发生于 {time(item.occurred_at)}</span>}<span>{item.shared_at ? `已分享 · ${time(item.shared_at)}` : '尚无成功分享记录'}</span></div>}
          <p className="memory-content">{item.content}</p>{item.kind === 'inference' && <p className="memory-basis">推测依据：{item.basis}</p>}
          <div className="memory-entry-footer"><p>{[item.allow_chat ? '用于交流' : '不用于交流', item.allow_writing ? '用于创作' : '不用于创作'].join(' · ')}<span>{sourceLabel(item)}</span></p><div className="memory-actions">{(item.source_chapter_id || item.source_task_id) && !item.source_deleted && <button className="text-button" disabled={busy} onClick={() => void viewSource(item)}>查看来源</button>}<button className="text-button" disabled={busy} onClick={() => openEditor(item)}>纠正与用途</button><button className="text-button danger" disabled={busy} onClick={() => { setError(''); setModal({ kind: 'memory-delete', item }); }}>删除记忆</button></div></div>
        </article>)}
      </div>}
      {!loading && view === 'list' && (offset > 0 || count === pageSize) && <nav className="memory-pagination" aria-label="记录分页"><button className="secondary" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页</button><span>第 {Math.floor(offset / pageSize) + 1} 页</span><button className="secondary" disabled={count < pageSize} onClick={() => setOffset(offset + pageSize)}>下一页</button></nav>}
    </>}
    <dialog ref={dialog} className="leave-dialog memory-dialog" aria-labelledby="memory-dialog-title" onCancel={event => { event.preventDefault(); if (!busy) setModal(null); }}>
      {modal?.kind === 'leave' ? <><h2 id="memory-dialog-title">有未保存的记忆修改</h2><p>保存后再离开，或放弃本次修改。未保存的内容与用途不会生效。</p><div className="button-row"><button className="primary" onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" onClick={() => setModal(null)} autoFocus>继续编辑</button></div></> : modal?.kind === 'source' ? <><h2 id="memory-dialog-title">记忆来源</h2>{modal.source.status === 'chapter' ? <><p className="field-hint">{modal.source.chapter.title} · 版本 {modal.source.chapter.version}</p><p className="source-content">{modal.source.chapter.content}</p><p className="field-hint">这是形成记忆时采用的日记版本，之后的改稿不会自动改写这条记忆。</p></> : modal.source.status === 'agent' ? <p>这条记忆由聊天 Agent 根据当轮内容主动提升；原始聊天不会被保存。</p> : <p>这条记忆由你手动添加。</p>}<button className="secondary" onClick={() => setModal(null)} autoFocus>关闭来源</button></> : modal && <><h2 id="memory-dialog-title">删除这条记忆？</h2><p>这条记忆将停止后续使用。LISA 不保存原始聊天，也不会从聊天记录重新提取它；已有作品需另行修改。</p>{error && <ErrorNotice message={error} />}<div className="button-row"><button className="secondary danger" disabled={busy} onClick={() => void confirmDelete()}>{busy ? '正在删除…' : '确认删除记忆'}</button><button className="text-button" disabled={busy} onClick={() => setModal(null)} autoFocus>取消</button></div></>}
    </dialog>
  </main>;
});
