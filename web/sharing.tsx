import { useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';

export function ShareWork({ projectId, chapterId }: { projectId: string; chapterId?: string }) {
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  async function share() {
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await request<{ url: string }>(`/projects/${projectId}/share`, {});
      setUrl(result.url);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '暂时无法分享作品。'); }
    finally { setBusy(false); }
  }

  async function copy(link: string) {
    setError(''); setNotice('');
    try { await navigator.clipboard.writeText(link); setNotice('链接已复制，可以发给其他人阅读。'); }
    catch { setNotice('请选中下方链接，手动复制。'); }
  }

  async function revoke() {
    setBusy(true); setError(''); setNotice('');
    try {
      await request(`/projects/${projectId}/share`, undefined, 'DELETE');
      setUrl(''); setNotice('此前的作品和章节链接已失效。下次分享会生成新链接。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '暂时无法收回链接。'); }
    finally { setBusy(false); }
  }

  const chapterUrl = chapterId && url ? `${url}?chapter=${encodeURIComponent(chapterId)}` : '';
  return <div className="work-share">
    <button className="text-button" disabled={busy} onClick={() => void share()}>分享作品</button>
    {url && <section className="share-panel" aria-label="作品分享链接">
      <p>拿到链接即可阅读已保存正文，无需登录。章节后续修改会同步显示。</p>
      <div className="field"><label htmlFor="work-share-url">作品链接</label><input id="work-share-url" readOnly value={url} onFocus={event => event.currentTarget.select()} /></div>
      <div className="button-row"><button className="secondary" onClick={() => void copy(url)}>复制作品链接</button><a href={url} target="_blank" rel="noreferrer">打开阅读页</a></div>
      {chapterUrl && <><div className="field"><label htmlFor="chapter-share-url">当前章节链接</label><input id="chapter-share-url" readOnly value={chapterUrl} onFocus={event => event.currentTarget.select()} /></div><button className="secondary" onClick={() => void copy(chapterUrl)}>复制章节链接</button></>}
      <div className="button-row"><button className="text-button" disabled={busy} onClick={() => void revoke()}>收回已有链接</button><button className="text-button" onClick={() => setUrl('')}>收起</button></div>
      <p className="field-hint">收回后旧链接立即失效；以后你或 LISA 再次分享时会生成新链接。</p>
    </section>}
    {error && <ErrorNotice message={error} />}
    {notice && <p className="field-hint" role="status">{notice}</p>}
  </div>;
}
