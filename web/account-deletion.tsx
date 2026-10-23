import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';

export function AccountDeletion({ open, busy, onOpen, onClose, onBusy, onExport, onDeleted }: {
  open: boolean; busy: boolean; onOpen: () => void; onClose: () => void;
  onBusy: (busy: boolean) => void; onExport: () => void; onDeleted: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    if (open) dialog.current?.showModal();
    else { dialog.current?.close(); setPassword(''); setError(''); }
  }, [open]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onBusy(true);
    setError('');
    try {
      await request('/auth/account', { password }, 'DELETE');
      onDeleted();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '账号删除失败，请重试。');
    } finally { onBusy(false); }
  }

  return <>
    <section className="account-contact-entry" aria-labelledby="delete-account-title">
      <div><h2 id="delete-account-title">删除账号</h2><p>清除 LISA 保存的全部关联个人数据，包括作品、历史版本、聊天、记忆和性格设置，且无法恢复。</p><button className="text-button" onClick={onExport} disabled={busy}>先去我的作品导出正文</button></div>
      <button className="secondary danger" onClick={onOpen} disabled={busy}>删除账号</button>
    </section>
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="delete-account-confirm-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
      <h2 id="delete-account-confirm-title">确认删除账号</h2>
      <p>删除后将退出登录，停止任务与消息发送，并清除全部关联个人数据，包括所有作品及历史版本。已交给 QQ 的在途消息无法撤回。此操作无法撤销；如需保留正文，请取消后先导出。</p>
      {error && <ErrorNotice message={error} />}
      <form onSubmit={submit} aria-busy={busy}>
        <div className="field"><label htmlFor="delete-account-password">当前密码</label><input id="delete-account-password" type="password" autoComplete="current-password" autoFocus required minLength={10} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} disabled={busy} /></div>
        <div className="button-row"><button className="secondary danger" disabled={busy}>{busy ? '正在删除账号…' : '永久删除账号'}</button><button type="button" className="secondary" onClick={onClose} disabled={busy}>取消</button></div>
      </form>
    </dialog>
  </>;
}
