import { useEffect, useRef, useState } from 'react';
import { request, styleFile } from './api';
import { pollWhileVisible } from './polling';
import { ErrorNotice } from './ui';

type Picture = { image_id: string | null };
export function WorkPicture({ path, label, preview = true, onChanged }: {
  path: string; label: string; preview?: boolean; onChanged?: (id: string) => void;
}) {
  const [picture, setPicture] = useState<Picture | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [notice, setNotice] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const writing = useRef(false);
  const revision = useRef(0);
  const pendingFile = useRef<File | null>(null);
  useEffect(() => {
    let active = true;
    setPicture(null); setNotice(''); setError(''); setReadError('');
    const stop = pollWhileVisible(async signal => {
      const epoch = revision.current;
      try {
        const next = await request<Picture>(path, undefined, undefined, { signal });
        if (active && epoch === revision.current && !writing.current) { setPicture(next); setReadError(''); }
      } catch { if (active && epoch === revision.current && !writing.current) setReadError(`${label}暂时无法读取，请稍后再试。`); }
    });
    return () => { active = false; stop(); };
  }, [path, label]);

  async function upload(file: File | undefined) {
    if (!file || writing.current) return;
    if (file.size > 12 * 1024 * 1024) { setError('请选择小于 12 MB 的图片。'); return; }
    pendingFile.current = file;
    writing.current = true; revision.current += 1;
    setBusy(true); setError(''); setNotice('');
    try {
      const body = await styleFile(file);
      const next = await request<Picture>(path, { content_base64: body.content_base64 }, 'PUT');
      setPicture(next); setNotice(`${label}已保存。`); setReadError(''); pendingFile.current = null;
      if (next.image_id) onChanged?.(next.image_id);
      if (input.current) input.current.value = '';
    } catch (cause) { setError(cause instanceof Error ? cause.message : '图片未能保存，请重试。'); }
    finally { writing.current = false; revision.current += 1; setBusy(false); }
  }

  return <section className="work-picture" aria-label={label}>
    {preview && picture?.image_id && <img className="chapter-illustration" src={`/api/private-images/${picture.image_id}`} alt={label} />}
    <div className="picture-upload">
      <label>{picture?.image_id ? `替换${label}` : `上传${label}`}<input ref={input} type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={event => void upload(event.target.files?.[0])} /></label>
      <p className="field-hint">私有展示 · PNG / JPEG / WebP，最多 12 MB。也可以在 QQ 请 LISA 绘制。</p>
    </div>
    {(busy || notice) && <p role="status">{busy ? '正在保存图片…' : notice}</p>}
    {error && <ErrorNotice message={error} />}
    {readError && <ErrorNotice message={readError} />}
    {error && pendingFile.current && <button className="text-button" disabled={busy} onClick={() => void upload(pendingFile.current ?? undefined)}>重新上传这张图</button>}
  </section>;
}
