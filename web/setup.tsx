import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { request } from './api';
import type { User } from './api';
import { ErrorNotice } from './ui';
import { AccountDeletion } from './account-deletion';
import type { WorksHandle } from './works';
import type { MemoryHandle } from './memory';
import type { ContactSettingsHandle } from './contact-settings';
const Works = lazy(() => import('./works').then(module => ({ default: module.Works })));
const Memory = lazy(() => import('./memory').then(module => ({ default: module.Memory })));
const ContactSettingsPanel = lazy(() => import('./contact-settings').then(module => ({ default: module.ContactSettingsPanel })));

type Setup = {
  qq_id: string | null;
  initialized: boolean;
  persona: { name: string; prompt: string; version: number } | null;
  presets: unknown[];
};
type Binding = { qq_id: string; expires_at: string };
type LeaveAction = 'logout' | 'overview' | 'works' | 'memory' | 'account' | 'contact';
type ServiceStatus = {
  status: 'ready' | 'unavailable' | 'budget_exhausted';
  message: string;
};

function ServiceNotice() {
  const [service, setService] = useState<ServiceStatus | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const state = await request<ServiceStatus>('/service-status');
        if (active) { setService(state); setError(''); }
      } catch {
        if (active) { setService(null); setError('暂时无法确认交流服务状态，稍后会自动重试。书房设置仍可编辑。'); }
      } finally {
        if (active) timer = setTimeout(refresh, 15_000);
      }
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, []);

  return <div className="setup-note ready-note" data-service-status={service?.status}>
    <strong>初始化已完成</strong>
    <p role="status" aria-label="交流服务状态">{error || service?.message || '正在确认交流服务状态…'}</p>
    <p>初始化前的消息不会自动补答，完成设置也不会自动开始创作。</p>
  </div>;
}

export function Study({ user, onLogout, onDeleted }: { user: User; onLogout: () => Promise<void>; onDeleted: () => void }) {
  const [section, setSection] = useState<'account' | 'works' | 'memory' | 'contact'>('account');
  const works = useRef<WorksHandle>(null);
  const memory = useRef<MemoryHandle>(null);
  const contact = useRef<ContactSettingsHandle>(null);
  const [setup, setSetup] = useState<Setup | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [bindingCode, setBindingCode] = useState('');
  const [binding, setBinding] = useState<Binding | null>(null);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState('');
  const [saved, setSaved] = useState(false);
  const [leave, setLeave] = useState<LeaveAction | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const personaForm = useRef<HTMLFormElement>(null);
  const dirty = section === 'account' && editing && (name !== (setup?.persona?.name ?? '') || prompt !== (setup?.persona?.prompt ?? ''));

  async function load() {
    setLoading(true);
    setError('');
    try {
      const state = await request<Setup>('/setup');
      setSetup(state);
      setName(state.persona?.name ?? '');
      setPrompt(state.persona?.prompt ?? '');
      setEditing(!state.persona);
      setSection(state.initialized ? 'works' : 'account');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法读取初始化状态，请重试。');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);
  useEffect(() => { heading.current?.focus(); }, [editing, setup?.qq_id]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    if (section !== 'account' || !editing || !setup?.qq_id || deleteOpen) return;
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        if (!busy && !leave) personaForm.current?.requestSubmit();
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, [section, editing, setup?.qq_id, busy, leave, deleteOpen]);
  useEffect(() => {
    if (leave) dialog.current?.showModal();
    else dialog.current?.close();
  }, [leave]);

  function showFailure(cause: unknown) {
    setError(cause instanceof Error ? cause.message : '操作失败，请重试。');
  }

  async function leavePage(action: LeaveAction, discard = false) {
    setError('');
    if (action === 'works' || action === 'memory' || action === 'account' || action === 'contact') {
      if (discard) { setName(setup?.persona?.name ?? ''); setPrompt(setup?.persona?.prompt ?? ''); }
      setSection(action);
      return;
    }
    if (action === 'overview') {
      if (discard) {
        setName(setup?.persona?.name ?? '');
        setPrompt(setup?.persona?.prompt ?? '');
      }
      setEditing(false);
      setSaved(false);
      return;
    }
    setBusy(true);
    try { await onLogout(); } catch (cause) { showFailure(cause); } finally { setBusy(false); }
  }

  function requestLeave(action: LeaveAction) {
    if (section === 'works' && works.current) works.current.requestLeave(() => { void leavePage(action); });
    else if (section === 'memory' && memory.current) memory.current.requestLeave(() => { void leavePage(action); });
    else if (section === 'contact' && contact.current) contact.current.requestLeave(() => { void leavePage(action); });
    else if (dirty) setLeave(action);
    else void leavePage(action);
  }

  async function previewBinding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try { setBinding(await request<Binding>('/setup/binding/preview', { code: bindingCode.trim() })); }
    catch (cause) { showFailure(cause); }
    finally { setBusy(false); }
  }

  async function confirmBinding() {
    setBusy(true);
    setError('');
    try {
      const state = await request<Setup>('/setup/binding/confirm', { code: bindingCode.trim(), qq_id: binding!.qq_id });
      setSetup(state);
      setBinding(null);
      setBindingCode('');
    } catch (cause) { showFailure(cause); }
    finally { setBusy(false); }
  }

  async function savePersona(): Promise<boolean> {
    if (!personaForm.current?.reportValidity()) return false;
    setBusy(true);
    setError('');
    setSaved(false);
    try {
      const state = await request<Setup>('/setup/persona', { name, prompt });
      setSetup(state);
      setName(state.persona!.name);
      setPrompt(state.persona!.prompt);
      setSaved(true);
      return true;
    } catch (cause) { showFailure(cause); return false; }
    finally { setBusy(false); }
  }

  async function resolveLeave(save: boolean) {
    const action = leave!;
    dialog.current?.close();
    setLeave(null);
    if (save && !(await savePersona())) return;
    await leavePage(action, !save);
  }

  if (loading) return <main id="main" className="study-page" role="status">正在打开书房…</main>;

  return <>
    <header className="topbar"><nav className="study-nav" aria-label="书房导航"><button className="text-button" aria-current={section === 'works' ? 'page' : undefined} onClick={() => { if (section !== 'works') requestLeave('works'); }} disabled={busy}>我的作品</button><button className="text-button" aria-current={section === 'memory' ? 'page' : undefined} onClick={() => { if (section !== 'memory') requestLeave('memory'); }} disabled={busy}>记忆</button><button className="text-button" aria-current={section === 'account' || section === 'contact' ? 'page' : undefined} onClick={() => { if (section !== 'account') requestLeave('account'); }} disabled={busy}>账号与 LISA 性格</button></nav><button className="text-button" onClick={() => requestLeave('logout')} disabled={busy}>退出登录</button></header>
    {section === 'works' ? <Suspense fallback={<main id="main" className="study-page" role="status">正在打开作品…</main>}><Works ref={works} /></Suspense> : section === 'memory' ? <Suspense fallback={<main id="main" className="study-page" role="status">正在打开记忆…</main>}><Memory ref={memory} /></Suspense> : section === 'contact' ? <Suspense fallback={<main id="main" className="study-page" role="status">正在打开联系偏好…</main>}><ContactSettingsPanel ref={contact} onBack={() => requestLeave('account')} /></Suspense> : <main id="main" tabIndex={-1}>
      <div className="study-page">
        <p className="eyebrow">我的书房</p><h1 ref={heading} tabIndex={-1}>欢迎，{user.username}。</h1>
        <p className="page-subtitle">{setup?.initialized ? '绑定与性格设置已保存。' : '完成 QQ 绑定与性格设置，准备与你的 LISA 相识。'}</p>
        {error && <ErrorNotice message={error} />}
        {!setup ? <button className="primary" onClick={load}>重新读取设置</button>
          : <>
            <ol className="setup-steps" aria-label="初始化进度"><li><span aria-hidden="true">01</span><strong>QQ 绑定</strong><small>{setup.qq_id ? '已完成' : '待完成'}</small></li><li><span aria-hidden="true">02</span><strong>LISA 性格</strong><small>{setup.persona ? '已完成' : '待完成'}</small></li></ol>
            {!setup.qq_id ? <section className="setup-panel" aria-labelledby="binding-title">
              <p className="eyebrow">第一步</p><h2 id="binding-title">绑定你的 QQ</h2>
              {!binding ? <form onSubmit={previewBinding} aria-busy={busy}>
                <p className="form-copy">用本人 QQ 私聊 LISA 机器人，发送“绑定”，将收到的一次性绑定码填在下面。下一步核对 QQ 号后，才会完成绑定。</p>
                <div className="field"><label htmlFor="binding-code">绑定码</label><input id="binding-code" value={bindingCode} onChange={event => setBindingCode(event.target.value)} required autoComplete="off" autoCapitalize="none" spellCheck={false} disabled={busy} /></div>
                <button className="primary" disabled={busy}>{busy ? '正在核对…' : '核对 QQ 号'}</button>
              </form> : <div className="binding-preview"><p>请确认这是你本人的 QQ：</p><strong className="qq-number">{binding.qq_id}</strong><p className="field-hint">有效至 {new Date(binding.expires_at).toLocaleString('zh-CN')}。确认后将与你当前的网页账号绑定。</p><div className="button-row"><button className="primary" onClick={confirmBinding} disabled={busy}>{busy ? '正在绑定…' : '确认绑定'}</button><button className="secondary" onClick={() => setBinding(null)} disabled={busy}>返回修改绑定码</button></div></div>}
              <p className="field-hint manual-help">一个 QQ 只能绑定一个账号。如果已经绑定，请登录或找回原账号。无法使用 QQ 时，请联系邀请你的管理员核实处理。</p>
            </section> : <>
              <p className="bound-account">已绑定 QQ：<strong>{setup.qq_id}</strong><span>如需更换，请联系邀请你的管理员核实处理。</span></p>
              {setup.initialized && <ServiceNotice />}
              {editing ? <section className="setup-panel" aria-labelledby="persona-title">
                <div className="panel-toolbar"><div><p className="eyebrow">{setup.persona ? '性格设计' : '第二步'}</p><h2 id="persona-title">LISA 性格</h2></div>{setup.persona && <button className="text-button" onClick={() => requestLeave('overview')} disabled={busy}>返回书房</button>}</div>
                <p className="form-copy">使用自定义 prompt 描述 LISA 的性格、背景与相处方式。正式性格预设尚在设计中。这是陪伴你的 LISA，与作品中的角色设置不同。</p>
                <form ref={personaForm} onSubmit={event => { event.preventDefault(); void savePersona(); }} aria-busy={busy}>
                  <fieldset disabled={busy}><div className="field"><label htmlFor="persona-name">性格名称</label><input id="persona-name" value={name} onChange={event => { setName(event.target.value); setSaved(false); }} required maxLength={80} /></div><div className="field"><label htmlFor="persona-prompt">LISA 性格 prompt</label><textarea id="persona-prompt" value={prompt} onChange={event => { setPrompt(event.target.value); setSaved(false); }} required maxLength={12000} rows={10} aria-describedby="persona-hint" /><p id="persona-hint" className="field-hint">最多 12,000 个字符。修改保存后只影响后续交流与创作，已有记忆、关系经历和作品保留。</p></div></fieldset>
                  <div className="save-bar"><button className="primary" disabled={busy}>{busy ? '正在保存…' : '保存性格'}</button><p role="status">{busy ? '请稍候' : dirty ? '有未保存修改 · Ctrl+S 保存' : saved ? '已保存' : setup.persona ? `当前版本 ${setup.persona.version}` : '保存后完成性格初始化'}</p></div>
                </form>
              </section> : <section className="welcome-paper"><span className="paper-flower" aria-hidden="true">✳</span><h2>{setup.persona?.name}</h2><p>性格设置已保存。你们的经历，会从这里慢慢积累。</p><button className="secondary" onClick={() => { setEditing(true); setSaved(false); }}>调整 LISA 性格</button></section>}
              {setup.initialized && <section className="account-contact-entry" aria-labelledby="contact-entry-title"><div><h2 id="contact-entry-title">联系偏好</h2><p>主动联系默认关闭。设置是否允许联系、免打扰时段与联系频率。</p></div><button className="secondary" onClick={() => requestLeave('contact')} disabled={busy}>设置联系偏好</button></section>}
            </>}
          </>}
        <AccountDeletion open={deleteOpen} busy={busy} onOpen={() => setDeleteOpen(true)} onClose={() => setDeleteOpen(false)} onBusy={setBusy} onExport={() => requestLeave('works')} onDeleted={onDeleted} />
      </div>
    </main>}
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="leave-title" onCancel={event => { event.preventDefault(); setLeave(null); }}>
      <h2 id="leave-title">有未保存的性格修改</h2><p>保存后再离开，或放弃本次修改。未保存内容不会用于后续交流与创作。</p><div className="button-row"><button className="primary" onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" onClick={() => setLeave(null)} autoFocus>继续编辑</button></div>
    </dialog>
  </>;
}
