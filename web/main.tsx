import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, request, resumeSession, SESSION_EXPIRED_EVENT } from './api';
import type { User } from './api';
import { AuthForm } from './auth';
import { Study } from './setup';
import { ErrorNotice } from './ui';
import './styles.css';

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessionError, setSessionError] = useState('');
  const [accountDeleted, setAccountDeleted] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [renewing, setRenewing] = useState(false);
  const [renewError, setRenewError] = useState('');

  async function checkSession() {
    setChecking(true);
    setSessionError('');
    try {
      setUser(await request<User>('/auth/me'));
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) setUser(null);
      else setSessionError(cause instanceof Error ? cause.message : '无法确认登录状态，请重试。');
    } finally {
      setChecking(false);
    }
  }

  useEffect(() => { void checkSession(); }, []);
  useEffect(() => {
    const expired = () => setSessionExpired(true);
    window.addEventListener(SESSION_EXPIRED_EVENT, expired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, expired);
  }, []);

  async function confirmSession() {
    setRenewing(true); setRenewError('');
    try {
      const current = await request<User>('/auth/me');
      if (current.id !== user?.id) { setRenewError('请在新标签页登录原账号，再返回这里确认。'); return; }
      resumeSession();
      setSessionExpired(false);
    } catch { setRenewError('尚未恢复登录，请在新标签页登录原账号后再试。'); }
    finally { setRenewing(false); }
  }

  async function logout() {
    try {
      await request('/auth/logout', {});
    } catch (cause) {
      if (!(cause instanceof ApiError && cause.status === 401)) throw cause;
    }
    setUser(null);
    resumeSession();
    setSessionExpired(false);
  }

  return <div className="shell">
    <a className="skip-link" href="#main">跳到主要内容</a>
    <aside className="sidebar" aria-label="LISA 私人书房">
      <div className="brand"><span className="brand-mark" aria-hidden="true">l.</span><span>LISA<small>文学创作伙伴</small></span></div>
      <div className="shelf-note"><span className="tiny-star" aria-hidden="true">✳</span><p>写下故事，<br />也留住日常。</p></div>
      <p className="sidebar-footer">你的私人书房</p>
    </aside>
    <div className="workspace">
      {user && sessionExpired && <section className="setup-note" aria-label="登录状态" role="status">
        <p>登录已失效，当前未保存内容仍保留。请在新标签页登录原账号，然后返回继续。</p>
        <div className="button-row"><a className="text-button" href="/" target="_blank" rel="noopener noreferrer">在新标签页登录</a><button className="secondary" disabled={renewing} onClick={() => void confirmSession()}>{renewing ? '正在确认…' : '我已重新登录'}</button></div>
        {renewError && <p>{renewError}</p>}
      </section>}
      {user && !checking && !sessionError ? <Study key={user.id} user={user} onLogout={logout} onDeleted={() => { setUser(null); setAccountDeleted(true); }} /> : <>
      <header className="topbar">
        <span>私人书房 <span className="breadcrumb-divider" aria-hidden="true">/</span> 欢迎</span>
      </header>
      <main id="main" tabIndex={-1}>
        {checking ? <div className="connection-state" role="status"><h1>正在打开书房…</h1><p>正在确认登录状态，请稍候。</p></div>
          : sessionError ? <section className="connection-state"><h1>书房暂时未能打开</h1><ErrorNotice message={sessionError} /><button className="primary" onClick={checkSession}>重新连接</button></section>
          : <div className="entry-page">
            <section className="entry-intro" aria-labelledby="entry-title">
              <p className="eyebrow">LITERARY INTERACTIVE STORY ASSISTANT</p>
              <h1 id="entry-title">故事，慢慢发生。</h1>
              <p className="intro-copy">给想象一张纸，给日常一个落脚的地方。<br />从这里，走进你与 LISA 的书房。</p>
              <div className="book-art" aria-hidden="true"><div className="book-cover"><span>故事的起点</span><div className="book-window"><i className="sun" /><i className="hill" /><i className="hill front" /></div><small>LISA</small></div><div className="book-caption">一页未写完的故事，<br />一种缓缓生长的陪伴。</div></div>
              <p className="invitation-note">邀请制开放 · 为每一段故事留出空间</p>
            </section>
            <AuthForm notice={accountDeleted ? '账号及全部关联个人数据已删除。' : ''} onLogin={value => { resumeSession(); setSessionExpired(false); setUser(value); setAccountDeleted(false); }} />
          </div>}
      </main>
      </>}
      <footer className="page-footer">LISA <span aria-hidden="true">·</span> 故事与日常，在这里相遇。</footer>
    </div>
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
