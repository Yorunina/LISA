import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { request } from './api';
import type { User } from './api';
import { ErrorNotice } from './ui';

export function AuthForm({ onLogin, notice = '' }: { onLogin: (user: User) => void; notice?: string }) {
  const [register, setRegister] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [recovering, setRecovering] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [register, recovering]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError('');
    try {
      const body = {
        username: String(data.get('username')).trim(),
        password: String(data.get('password')),
        ...(register ? { invite_code: String(data.get('invite_code')).trim() } : {}),
      };
      onLogin(await request<User>(`/auth/${register ? 'register' : 'login'}`, body));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '操作失败，请重试。');
    } finally {
      setBusy(false);
    }
  }

  function switchMode() {
    setRegister(!register);
    setError('');
    form.current?.reset();
  }

  if (recovering) return <RecoveryForm onBack={() => setRecovering(false)} />;

  return <section className="auth-panel" aria-labelledby="auth-title">
    {notice && <p className="setup-note" role="status">{notice}</p>}
    <div className="panel-heading">
      <p className="eyebrow">{register ? '一间属于你的书房' : '欢迎回来'}</p>
      <h2 id="auth-title" ref={heading} tabIndex={-1}>{register ? '创建账号' : '登录书房'}</h2>
      <p>{register ? '使用邀请码，开启与 LISA 的相处。' : '使用用户名和密码，回到你的书房。'}</p>
    </div>
    <form onSubmit={submit} ref={form} aria-busy={busy}>
      <fieldset disabled={busy}>
        <div className="field">
          <label htmlFor="username">用户名</label>
          <input id="username" name="username" autoComplete="username" autoCapitalize="none"
            spellCheck={false} required minLength={3} maxLength={32} pattern="[A-Za-z0-9_.\-]+"
            aria-describedby="username-hint" />
          <p className="field-hint" id="username-hint">3–32 位英文字母、数字或 _ . -，不区分大小写</p>
        </div>
        <div className="field">
          <label htmlFor="password">密码</label>
          <input id="password" name="password" type="password" required minLength={10} maxLength={128}
            autoComplete={register ? 'new-password' : 'current-password'} aria-describedby="password-hint" />
          <p className="field-hint" id="password-hint">10–128 个字符</p>
        </div>
        {register && <div className="field">
          <label htmlFor="invite_code">邀请码</label>
          <input id="invite_code" name="invite_code" required autoComplete="off" spellCheck={false}
            autoCapitalize="none" aria-describedby="invite-hint" />
          <p className="field-hint" id="invite-hint">目前采用邀请注册，请向管理员获取邀请码。</p>
        </div>}
      </fieldset>
      {error && <ErrorNotice message={error} />}
      <button className="primary submit" type="submit" disabled={busy}>
        {busy ? (register ? '正在创建账号…' : '正在登录…') : (register ? '创建账号并进入书房' : '进入书房')}
        {!busy && <span aria-hidden="true">→</span>}
      </button>
      <p className="auth-switch">
        {register ? '已经有账号？' : '还没有账号？'}
        <button className="text-button" type="button" onClick={switchMode} disabled={busy}>
          {register ? '返回登录' : '使用邀请码注册'}
        </button>
      </p>
      {!register && <div className="recovery-link"><button className="text-button" type="button" onClick={() => setRecovering(true)} disabled={busy}>忘记用户名或密码</button></div>}
    </form>
  </section>;
}

function RecoveryForm({ onBack }: { onBack: () => void }) {
  const [ticket, setTicket] = useState<{ username: string; reset_token: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [complete, setComplete] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [ticket, complete]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError('');
    try {
      if (ticket) {
        await request('/auth/recovery/reset', { reset_token: ticket.reset_token, password: String(data.get('password')) });
        setComplete(true);
      } else {
        setTicket(await request('/auth/recovery/verify', { code: String(data.get('code')).trim() }));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '账号找回失败，请重试。');
    } finally {
      setBusy(false);
    }
  }

  return <section className="auth-panel" aria-labelledby="recovery-title">
    <div className="panel-heading"><p className="eyebrow">回到你的书房</p><h2 id="recovery-title" ref={heading} tabIndex={-1}>找回账号</h2></div>
    {complete ? <div className="recovery-success"><p role="status">密码已重设。请使用用户名 <strong>{ticket?.username}</strong> 和新密码登录。</p><button className="primary" onClick={onBack}>返回登录</button></div>
      : <form onSubmit={submit} aria-busy={busy}>
        <p className="form-copy">{ticket ? '已验证你的 QQ 身份，请设置新密码。已有作品与经历会保留，所有设备需要重新登录。' : '请使用已绑定的 QQ，私聊 LISA 机器人发送“找回”，将收到的专用找回码填在下面。绑定码不能用于找回账号。'}</p>
        <fieldset disabled={busy}>
          {ticket ? <><p className="verified-account">关联账号：<strong>{ticket.username}</strong></p><div className="field"><label htmlFor="new-password">新密码</label><input id="new-password" name="password" type="password" autoComplete="new-password" required minLength={10} maxLength={128} aria-describedby="new-password-hint" /><p className="field-hint" id="new-password-hint">10–128 个字符</p></div></>
            : <div className="field"><label htmlFor="recovery-code">找回码</label><input id="recovery-code" name="code" required autoComplete="off" autoCapitalize="none" spellCheck={false} /></div>}
        </fieldset>
        {error && <ErrorNotice message={error} />}
        <button className="primary submit" type="submit" disabled={busy}>{busy ? '正在处理…' : ticket ? '设置新密码' : '验证找回码'}</button>
        {ticket && <button className="text-button" type="button" disabled={busy} onClick={() => { setTicket(null); setError(''); }}>重新领取并验证找回码</button>}
        <p className="field-hint manual-help">QQ 无法使用或需要更换 QQ？请联系邀请你的管理员核实处理，人工核实可能需要等待。</p>
        <button className="text-button" type="button" onClick={onBack} disabled={busy}>返回登录</button>
      </form>}
  </section>;
}
