import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';

type Mode = 'normal' | 'reduced' | 'off';
export type MemeSettingsHandle = { requestLeave: (action: () => void) => void };

export const MemeSettings = forwardRef<MemeSettingsHandle, { onBusy: (value: boolean) => void }>(function MemeSettings({ onBusy }, ref) {
  const [mode, setMode] = useState<Mode>('normal');
  const [baseline, setBaseline] = useState<Mode>('normal');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const pending = useRef<(() => void) | null>(null);
  const dirty = mode !== baseline;
  function load() {
    setLoading(true); setError('');
    void request<{ meme_mode: Mode }>('/shared/preferences').then(value => {
      setMode(value.meme_mode); setBaseline(value.meme_mode);
    }).catch(cause => setError(cause instanceof Error ? cause.message : '无法读取表情偏好。'))
      .finally(() => setLoading(false));
  }
  useEffect(load, []);
  useImperativeHandle(ref, () => ({ requestLeave(action) {
    if (busy) return;
    if (dirty) { pending.current = action; setLeaving(true); } else action();
  } }));
  useEffect(() => { if (leaving) dialog.current?.showModal(); else dialog.current?.close(); }, [leaving]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  async function save() {
    setBusy(true); onBusy(true); setError(''); setSaved(false);
    try {
      const value = await request<{ meme_mode: Mode }>('/shared/preferences', { meme_mode: mode }, 'PUT');
      setMode(value.meme_mode); setBaseline(value.meme_mode); setSaved(true); return true;
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败，选择已保留。'); return false; }
    finally { setBusy(false); onBusy(false); }
  }
  async function resolve(keep: boolean) {
    if (keep && !(await save())) { setLeaving(false); return; }
    if (!keep) setMode(baseline);
    setLeaving(false); const action = pending.current; pending.current = null; action?.();
  }
  return <section className="setup-panel" aria-labelledby="meme-settings-title">
    <h2 id="meme-settings-title">表情偏好</h2><p>只影响你自己的 LISA，共有表情图库由所有用户共同维护。</p>
    {error && <ErrorNotice message={error} />}
    {loading ? <p role="status">正在读取表情偏好…</p> : error && !saved && !dirty ? <button className="secondary" onClick={load}>重新读取表情偏好</button> : <form ref={form} onSubmit={event => { event.preventDefault(); void save(); }} onKeyDown={event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault(); event.stopPropagation(); event.nativeEvent.stopImmediatePropagation();
        if (!busy && !leaving) form.current?.requestSubmit();
      }
    }}>
      <div className="field"><label htmlFor="personal-meme-mode">我的 LISA 使用表情</label><select id="personal-meme-mode" value={mode} disabled={busy} onChange={event => { setMode(event.target.value as Mode); setSaved(false); }}><option value="normal">合适时使用</option><option value="reduced">少发一些</option><option value="off">不发送表情</option></select></div>
      <div className="save-bar"><button className="primary" disabled={busy}>{busy ? '正在保存…' : '保存表情偏好'}</button><span role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : saved ? '表情偏好已保存' : '已保存的个人偏好'}</span></div>
    </form>}
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="meme-leave-title" onCancel={event => { event.preventDefault(); setLeaving(false); }}><h2 id="meme-leave-title">有未保存的表情偏好</h2><p>保存后生效，或放弃本次修改。</p><div className="button-row"><button className="primary" disabled={busy} onClick={() => void resolve(true)}>保存</button><button className="secondary" disabled={busy} onClick={() => void resolve(false)}>放弃修改</button><button className="text-button" disabled={busy} onClick={() => setLeaving(false)}>继续编辑</button></div></dialog>
  </section>;
});
