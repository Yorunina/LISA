import { useEffect, useState } from 'react';
import type { JSONContent } from '@tiptap/react';
import { request } from './api';
import { ErrorNotice } from './ui';

export type Chapter = { id: string; title: string; content: JSONContent; text: string; version: number; position: number };
type VersionInfo = { version: number; title: string; source: string; created_at: string };
type VersionDetail = VersionInfo & { text: string };
const sourceName = (source: string) => source.startsWith('restore:') ? `恢复自版本 ${source.split(':')[1]}` : source === 'generated' ? 'LISA 创作' : source === 'rewrite' ? 'LISA 局部重写' : '手动保存';

export function Versions({ chapter, busy, setBusy, onRestored }: { chapter: Chapter; busy: boolean; setBusy: (busy: boolean) => void; onRestored: (chapter: Chapter) => void }) {
  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    request<VersionInfo[]>(`/chapters/${chapter.id}/versions`).then(items => {
      if (active) { setVersions(items); setSelected(previous => items.some(item => item.version === previous) ? previous : items[0]?.version ?? null); }
    }).catch(cause => { if (active) setError(cause.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [chapter.id, chapter.version, retry]);
  useEffect(() => {
    if (selected === null) return;
    let active = true;
    setDetail(null); setError('');
    request<VersionDetail>(`/chapters/${chapter.id}/versions/${selected}`).then(value => {
      if (active) setDetail(value);
    }).catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [chapter.id, selected, retry]);
  async function restore() {
    if (!detail) return;
    setBusy(true); setError('');
    try {
      const next = await request<Chapter>(`/chapters/${chapter.id}/versions/${detail.version}/restore`, {});
      onRestored(next);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '恢复失败，请重试。'); }
    finally { setBusy(false); }
  }
  return <section className="version-paper" aria-label="正文版本记录">
    <p className="eyebrow">已保存的文字</p><h2>版本记录 · {chapter.title}</h2>
    <p className="form-copy">只记录正式保存的正文。恢复将替换当前章节的标题和正文，保留历史；摘要、剧情状态及生活记忆请另行维护。</p>
    {error && <><ErrorNotice message={error} /><button className="text-button" disabled={busy} onClick={() => setRetry(value => value + 1)}>重新读取版本</button></>}
    {loading ? <p role="status">正在读取版本…</p> : !versions.length ? !error && <p>暂无已保存版本。</p> : <>
      <div className="field"><label htmlFor="chapter-version">选择历史版本</label><select id="chapter-version" value={selected ?? ''} disabled={busy} onChange={event => setSelected(Number(event.target.value))}>{versions.map(item => <option value={item.version} key={item.version}>版本 {item.version}{item.version === chapter.version ? '（当前正文）' : ''} · {sourceName(item.source)} · {new Date(item.created_at).toLocaleString('zh-CN')}</option>)}</select></div>
      {detail ? <article className="version-preview"><p className="field-hint">{detail.version === chapter.version ? '当前已保存正文' : '历史正文，仅供查看'} · 版本 {detail.version} · {sourceName(detail.source)}</p><h3>{detail.title}</h3><div className="version-body">{detail.text || '这一版正文为空。'}</div>
        <div className="save-bar"><button className="primary" disabled={busy || detail.version === chapter.version} onClick={() => void restore()}>恢复此版本为当前正文</button><p>内容实际改变时新增版本，不覆盖历史记录。</p></div></article> : !error && <p role="status">正在读取正文…</p>}
    </>}
  </section>;
}
