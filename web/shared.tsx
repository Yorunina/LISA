import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';

type Kind = 'meme';
type Status = 'active' | 'candidate' | 'archived' | 'withdrawn';
type Draft = {
  kind: Kind; title: string; content: string; aliases: string[]; tags: string[];
  source_url: string; source_note: string; origin: 'public' | 'contribution';
  asset_id: string | null; status: Status; allow_chat: boolean; allow_writing: boolean;
  occurred_at: string | null; source_time: string | null; expires_at: string | null;
};
type Entry = Draft & { id: string; revision: number; updated_at: string; muted: boolean };
type Version = { revision: number; created_at: string; data: Draft };
export type SharedHandle = { requestLeave: (action: () => void) => void };
const labels: Record<Kind, string> = { meme: '表情' };
const states: Record<Status, string> = { active: '可用', candidate: '待补充', archived: '已归档', withdrawn: '已撤下' };
const empty = (kind: Kind = 'meme'): Draft => ({ kind, title: '', content: '', aliases: [], tags: [], source_url: '', source_note: '', origin: 'contribution', asset_id: null, status: 'active', allow_chat: true, allow_writing: false, occurred_at: null, source_time: null, expires_at: null });
const draftOf = (item: Entry): Draft => Object.fromEntries(Object.keys(empty()).map(key => [key, item[key as keyof Entry]])) as Draft;
const time = (value: string) => new Date(value).toLocaleString('zh-CN');

export const SharedLibrary = forwardRef<SharedHandle>(function SharedLibrary(_, ref) {
  const [items, setItems] = useState<Entry[]>([]);
  const [status, setStatus] = useState('active');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState<Entry | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(() => empty());
  const [baseline, setBaseline] = useState<Draft>(() => empty());
  const [consent, setConsent] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [versions, setVersions] = useState<{ item: Entry; list: Version[] } | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [job, setJob] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [leaving, setLeaving] = useState(false);
  const pending = useRef<(() => void) | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const dirty = editing && (JSON.stringify(draft) !== JSON.stringify(baseline) || file !== null);
  const failure = (cause: unknown) => setError(cause instanceof Error ? cause.message : '操作未完成，请重试。');
  function requestLeave(action: () => void) {
    if (busy) return;
    if (dirty) { pending.current = action; setLeaving(true); }
    else action();
  }
  useImperativeHandle(ref, () => ({ requestLeave }));
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    const params = new URLSearchParams({ query, kind: 'meme', status, offset: String(offset), limit: '30' });
    void request<Entry[]>(`/shared?${params}`).then(list => { if (active) setItems(list); })
      .catch(cause => { if (active) failure(cause); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [query, status, offset, refresh]);
  useEffect(() => {
    if (!file) { setPreview(null); return; }
    const url = URL.createObjectURL(file); setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  useEffect(() => {
    if (!job) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let attempts = 0;
    async function poll() {
      try {
        const state = await request<{ status: string; error: string | null }>(`/shared/jobs/${job}`);
        if (!active) return;
        if (state.status === 'pending' || state.status === 'running') {
          if (++attempts < 60) timer = setTimeout(poll, 2000);
          else { setJob(null); setNotice('任务仍在处理，可稍后刷新查看结果。'); }
        } else {
          setJob(null); setRefresh(value => value + 1);
          setNotice(state.status === 'completed' ? '共有资料已更新。' : '本次更新未完成，已有资料保留。可检查来源或额度后重试。');
        }
      } catch (cause) { if (active) { failure(cause); setJob(null); } }
    }
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [job]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    if (!editing) return;
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault(); if (!busy && !leaving) form.current?.requestSubmit();
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, [editing, busy, leaving]);
  useEffect(() => { if (leaving) dialog.current?.showModal(); else dialog.current?.close(); }, [leaving]);

  function open(item: Entry | null) {
    requestLeave(() => {
      const next = item ? draftOf(item) : empty();
      setSelected(item); setDraft(next); setBaseline(next); setEditing(true);
      setFile(null); setConsent(!!item); setError(''); setNotice(''); setVersions(null);
    });
  }
  async function save() {
    if (!form.current?.reportValidity()) return false;
    setBusy(true); setError('');
    try {
      let asset = draft.asset_id;
      if (file) {
        if (!consent) throw new Error('请明确同意贡献后再保存图片。');
        const response = await fetch('/api/shared/assets', { method: 'POST', credentials: 'same-origin', headers: { 'X-LISA-Request': '1', 'X-LISA-Contribute': '1', 'Content-Type': file.type || 'application/octet-stream' }, body: file });
        const uploaded = await response.json();
        if (!response.ok) throw new Error(typeof uploaded.detail === 'string' ? uploaded.detail : '图片上传失败，输入已保留。');
        asset = uploaded.id;
        setDraft(value => ({ ...value, asset_id: asset })); setFile(null);
      }
      const value = await request<Entry>(selected ? `/shared/${selected.id}` : '/shared', {
        ...draft, asset_id: draft.kind === 'meme' ? asset : null,
        aliases: draft.aliases.map(value => value.trim()).filter(Boolean),
        tags: draft.tags.map(value => value.trim()).filter(Boolean),
        contribute: consent, ...(selected ? { revision: selected.revision } : {}),
      }, selected ? 'PUT' : 'POST');
      setSelected(value); setDraft(draftOf(value)); setBaseline(draftOf(value));
      setRefresh(value => value + 1); setNotice('已保存，所有用户均可查看和维护。');
      return true;
    } catch (cause) { failure(cause); return false; }
    finally { setBusy(false); }
  }
  async function action(run: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await run(); setRefresh(value => value + 1); } catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  function changeStatus(item: Entry, next: Status) {
    requestLeave(() => { void action(async () => {
      await request(`/shared/${item.id}`, { ...draftOf(item), status: next, revision: item.revision, contribute: true }, 'PUT');
      setEditing(false); setVersions(null); setNotice(next === 'withdrawn' ? '已撤下，后续不再检索或发送。' : '条目状态已更新。');
    }); });
  }
  async function resolveLeave(keep: boolean) {
    if (keep && !(await save())) { setLeaving(false); return; }
    setLeaving(false); setEditing(false); setFile(null);
    const action = pending.current; pending.current = null; action?.();
  }

  return <main id="main" tabIndex={-1}><div className="study-page shared-page">
    <p className="eyebrow">一起积累</p><div className="panel-toolbar"><div><h1>表情图库</h1><p className="page-subtitle">一起维护表情素材与语义。每个人都能补充与纠正。</p></div><button className="primary" onClick={() => open(null)} disabled={busy}>新增表情</button></div>
    {error && <ErrorNotice message={error} />}<p role="status">{notice}</p>
    {editing && <section className="setup-panel shared-editor" aria-label="编辑共有条目">
      <div className="panel-toolbar"><h2>{selected ? '编辑条目' : '新增条目'}</h2><button className="text-button" disabled={busy} onClick={() => requestLeave(() => setEditing(false))}>返回列表</button></div>
      <form ref={form} onSubmit={event => { event.preventDefault(); void save(); }}>
        <fieldset disabled={busy}>
          <p className="field-hint">{labels[draft.kind]} · 保存后对所有用户生效</p>
          <div className="field"><label htmlFor="shared-title">标题</label><input id="shared-title" required maxLength={160} value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} /></div>
          <div className="field"><label htmlFor="shared-content">内容、含义与适用语境</label><textarea id="shared-content" required maxLength={2400} rows={7} value={draft.content} onChange={event => setDraft({ ...draft, content: event.target.value })} /><p className="field-hint">表情请写明语气和不适合使用的场景；不确定说法明确标注。</p></div>
          <div className="field"><label htmlFor="shared-tags">标签（逗号分隔）</label><input id="shared-tags" value={draft.tags.join(',')} onChange={event => setDraft({ ...draft, tags: event.target.value.split(/[,，]/) })} /></div>
          <div className="field"><label htmlFor="shared-aliases">别名（逗号分隔）</label><input id="shared-aliases" value={draft.aliases.join(',')} onChange={event => setDraft({ ...draft, aliases: event.target.value.split(/[,，]/) })} /></div>
          <div className="field"><label htmlFor="shared-origin">来源性质</label><select id="shared-origin" value={draft.origin} onChange={event => setDraft({ ...draft, origin: event.target.value as Draft['origin'] })}><option value="contribution">我明确贡献的内容</option><option value="public">具有独立公开来源的资料</option></select></div>
          <div className="field"><label htmlFor="shared-source">公开来源链接</label><input id="shared-source" type="url" required={draft.origin === 'public'} value={draft.source_url} onChange={event => setDraft({ ...draft, source_url: event.target.value })} /></div>
          <div className="field"><label htmlFor="shared-note">来源说明</label><input id="shared-note" maxLength={800} value={draft.source_note} onChange={event => setDraft({ ...draft, source_note: event.target.value })} /></div>
          {draft.kind === 'meme' && <div className="field"><label htmlFor="shared-file">贡献表情图片（最多 4 MiB）</label><input id="shared-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" onChange={event => setFile(event.target.files?.[0] ?? null)} />{(preview || draft.asset_id) && <img className="shared-meme" src={preview || `/api/shared/assets/${draft.asset_id}`} alt={draft.title || '表情预览'} />}</div>}
          <div className="field"><label htmlFor="shared-state">状态</label><select id="shared-state" value={draft.status} onChange={event => setDraft({ ...draft, status: event.target.value as Status })}>{Object.entries(states).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>
          <label className="checkbox-row"><input type="checkbox" checked={draft.allow_chat} onChange={event => setDraft({ ...draft, allow_chat: event.target.checked })} />允许交流时使用</label>
          <label className="checkbox-row"><input type="checkbox" checked={draft.allow_writing} onChange={event => setDraft({ ...draft, allow_writing: event.target.checked })} />允许相关创作任务参考</label>
          <label className="checkbox-row"><input type="checkbox" required checked={consent} onChange={event => setConsent(event.target.checked)} />我确认内容和图片可供本部署所有用户查看、维护及由 LISA 使用。</label>
        </fieldset>
        <div className="save-bar"><button className="primary" disabled={busy}>{busy ? '正在保存…' : '保存共有条目'}</button><span role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : selected ? `已保存 · 版本 ${selected.revision}` : '保存后对所有用户生效'}</span></div>
      </form>
    </section>}
    <form className="shared-filters" onSubmit={event => { event.preventDefault(); setQuery(search); setOffset(0); }}><label>搜索<input aria-label="搜索共有记忆" value={search} onChange={event => setSearch(event.target.value)} /></label><label>状态<select aria-label="筛选共有状态" value={status} onChange={event => { setStatus(event.target.value); setOffset(0); }}>{Object.entries(states).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary">搜索</button></form>
    {loading ? <p role="status">正在读取共有记忆…</p> : items.length === 0 ? <p className="empty-state">暂无符合条件的共有条目。可以新增，或调整筛选。</p> : <div className="shared-list">{items.map(item => <article className={`setup-panel shared-card ${item.kind === 'meme' ? 'shared-meme-row' : ''}`} key={item.id}>
      {item.kind === 'meme' && <div className="shared-preview">{item.asset_id ? <img src={`/api/shared/assets/${item.asset_id}`} alt={item.title} loading="lazy" /> : <span>尚未上传图片</span>}</div>}<div className="shared-details">
      <div className="panel-toolbar"><span>{labels[item.kind]} · {states[item.status]}</span><small>版本 {item.revision} · {time(item.updated_at)}</small></div><h2>{item.title}</h2>
      <p className="shared-content">{item.content}</p>
      {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer">查看公开来源</a>}<p className="field-hint">{item.source_note}{item.muted ? ' · 已在你的 LISA 中屏蔽' : ''}</p>
      <div className="button-row"><button className="secondary" disabled={busy} onClick={() => open(item)}>编辑</button><button className="text-button" disabled={busy} onClick={() => void action(async () => { const list = await request<Version[]>(`/shared/${item.id}/versions`); setVersions({ item, list }); })}>版本记录</button><button className="text-button" disabled={busy} onClick={() => void action(async () => { await request(`/shared/${item.id}/mute`, { muted: !item.muted }, 'PUT'); })}>{item.muted ? '取消个人屏蔽' : '仅在我的 LISA 中屏蔽'}</button><button className="text-button" disabled={busy || !!job} onClick={() => void action(async () => { const result = await request<{ task_id: string }>(`/shared/${item.id}/analyze`, {}); setJob(result.task_id); setNotice('已安排语义整理，完成后会保存新版本。'); })}>AI 补充语义</button><button className="text-button" disabled={busy} onClick={() => changeStatus(item, item.status === 'withdrawn' ? 'active' : 'withdrawn')}>{item.status === 'withdrawn' ? '恢复使用' : '撤下'}</button></div>
      {versions?.item.id === item.id && <div className="shared-versions">{versions.list.map(version => <details key={version.revision}><summary>版本 {version.revision} · {time(version.created_at)}</summary><p className="shared-content">{version.data.content}</p><button className="secondary" disabled={busy || item.revision === version.revision} onClick={() => requestLeave(() => { void action(async () => { await request(`/shared/${item.id}/restore`, { revision: item.revision, target_revision: version.revision }); setVersions(null); setEditing(false); setNotice('已恢复为新的当前版本。'); }); })}>恢复此版本</button></details>)}</div>}
    </div></article>)}</div>}
    <div className="button-row"><button className="secondary" disabled={offset === 0 || loading} onClick={() => setOffset(value => Math.max(0, value - 30))}>上一页</button><span>第 {offset / 30 + 1} 页</span><button className="secondary" disabled={items.length < 30 || loading} onClick={() => setOffset(value => value + 30)}>下一页</button></div>
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="shared-leave-title" onCancel={event => { event.preventDefault(); setLeaving(false); }}><h2 id="shared-leave-title">有未保存的共有条目</h2><p>保存后对所有用户生效，或放弃本次修改。</p><div className="button-row"><button className="primary" disabled={busy} onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" disabled={busy} onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" onClick={() => setLeaving(false)}>继续编辑</button></div></dialog>
  </div></main>;
});
