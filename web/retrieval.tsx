import { useEffect, useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';

export type RetrievalConfig = { sources: string[]; limit: number; max_chars: number };
export type RetrievalView = { config: RetrievalConfig; defaults: RetrievalConfig; enabled: boolean };
const sources = [['memory', '可用于创作的记忆'], ['project', '作品资料'], ['chapter', '已保存章节']] as const;

export function RetrievalEditor({ view, busy, dirty, saved, onChange, onSave }: { view: RetrievalView; busy: boolean; dirty: boolean; saved: boolean; onChange: (config: RetrievalConfig) => void; onSave: () => void }) {
  const config = view.config;
  return <section className="material-paper retrieval-paper" aria-label="作者检索配置">
    <p className="eyebrow">写作时的额外参考</p><h2>检索配置</h2>
    <p className="form-copy">只影响这部作品作者的额外资料召回，不改变聊天检索、资料权限或已有索引。核心设定、约束、摘要、剧情状态、当前写作片段及近期记忆仍按原规则提供。世界书的详细条目归入「作品资料」。</p>
    {!view.enabled && <p className="contact-availability">服务尚未启用向量检索。设定条目仍可按完整标题匹配；语义召回启用后生效。</p>}
    <form onSubmit={event => { event.preventDefault(); onSave(); }}>
      <fieldset className="memory-uses retrieval-sources" disabled={busy}><legend>资料范围</legend>{sources.map(([value, label]) => <label key={value}><input type="checkbox" checked={config.sources.includes(value)} onChange={event => onChange({ ...config, sources: sources.map(item => item[0]).filter(item => item === value ? event.target.checked : config.sources.includes(item)) })} />{label}</label>)}</fieldset>
      <p className="field-hint">全部取消后，不额外召回这些资料；基础创作上下文仍保留。设定条目与直接关联共同占用条数及文本预算；放不下时整组略过，避免截断规则或例外。</p>
      <div className="contact-fields"><div className="field"><label htmlFor="retrieval-limit">召回数量（条）</label><input id="retrieval-limit" type="number" min={1} max={12} step={1} required value={config.limit} disabled={busy} onChange={event => onChange({ ...config, limit: Number(event.target.value) })} /><p className="field-hint">最多 1–12 条相关片段。</p></div><div className="field"><label htmlFor="retrieval-budget">文本预算（字符）</label><input id="retrieval-budget" type="number" min={300} max={6000} step={1} required value={config.max_chars} disabled={busy} onChange={event => onChange({ ...config, max_chars: Number(event.target.value) })} /><p className="field-hint">额外召回最多 300–6000 字符，包含来源标注；不是 token 数。</p></div></div>
      <div className="save-bar"><button className="primary" disabled={busy}>保存检索配置</button><button className="secondary" type="button" disabled={busy} onClick={() => onChange(view.defaults)}>恢复默认</button><p role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : saved ? '已保存' : '已保存的配置用于后续创作'}</p></div>
      <p className="field-hint">恢复默认只填入表单，保存后才会生效。</p>
    </form>
  </section>;
}

type Reference = { position: number; source_kind: string; revision: number | string; start: number; end: number; state: 'available' | 'changed' | 'unavailable'; title: string | null; text: string | null };
const sourceNames: Record<string, string> = { memory: '记忆', project: '作品资料', chapter: '章节正文', character: '角色知识' };
export function TaskReferences({ taskId, status }: { taskId: string; status: string }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Reference[] | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setItems(null); setError('');
    request<{ references: Reference[] }>(`/tasks/${taskId}/references`).then(result => { if (active) setItems(result.references); }).catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [taskId, open, status, retry]);
  return <div className="task-references"><button className="text-button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起参考资料' : '查看参考资料'}</button>{open && <div>
    <p className="field-hint">这里记录作者或角色在生成尝试中准备加入上下文的检索片段，包含重试；角色知识仅供对应角色使用。不代表请求已经发送或最终正文采用。</p>
    {error ? <><ErrorNotice message={error} /><button className="text-button" onClick={() => setRetry(value => value + 1)}>重新读取参考资料</button></> : !items ? <p role="status">正在读取参考资料…</p> : !items.length ? <p className="field-hint">这项任务暂无额外检索资料。</p> : <ol className="reference-list">{items.map((item, index) => <li key={index}><p className="reference-heading">{sourceNames[item.source_kind] ?? '参考资料'} · {item.state === 'available' ? item.title || '相关片段' : item.state === 'changed' ? '资料已改变' : '资料已不可用'}</p>{item.state === 'available' ? <><p className="field-hint">版本 {item.revision} · 字符 {item.start + 1}–{item.end}</p><p className="reference-text">{item.text}</p></> : <p className="field-hint">不再展示当时的内容。</p>}</li>)}</ol>}
  </div>}</div>;
}
