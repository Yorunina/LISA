import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';
import { useModalDialog, useSaveShortcut, useUnsavedWarning } from './editor-state';

type ContactSettings = {
  enabled: boolean; timezone: string; quiet_start: number | null; quiet_end: number | null;
  min_interval_minutes: number; max_per_day: number; revision: number; available: boolean;
  observations?: {
    sample_count: number; active_days: number; timezone: string;
    preferred_hours: { start: number; end: number; active_days: number } | null;
    text_length: { sample_count: number; median: number | null; trend: 'longer' | 'shorter' | null };
  };
};
type Draft = {
  enabled: boolean; timezone: string; quiet_enabled: boolean; quiet_start: string; quiet_end: string;
  min_interval_minutes: string; max_per_day: string;
};
export type ContactSettingsHandle = { requestLeave: (action: () => void) => void };

function InteractionNotes({ observations }: { observations: NonNullable<ContactSettings['observations']> }) {
  const hours = observations.preferred_hours;
  return <div className="contact-group" aria-label="交流记录中的线索">
    <h2>交流记录中的线索</h2>
    <p className="field-hint">LISA 不保存聊天历史，也不从消息记录推测交流习惯。你的联系设置始终优先。</p>
    {observations.sample_count ? <>
      <p>目前参考了 {observations.sample_count} 条消息，分布在 {observations.active_days} 天。</p>
      <p>{hours ? `暂时观察到，你较常在 ${String(hours.start).padStart(2, '0')}:00–${String(hours.end).padStart(2, '0')}:00 交流，有 ${hours.active_days} 天的记录作为依据（${observations.timezone}）。` : '目前还没有足够、稳定的跨日记录来推测常见交流时段。'}</p>
      {observations.text_length.median !== null && <p>文字消息长度的中位数约为 {observations.text_length.median} 字。{observations.text_length.trend ? `与前一周相比，最近一周的文字回复${observations.text_length.trend === 'longer' ? '更长' : '更短'}一些。` : ''}这不代表你要求 LISA 回复同样的长度。</p>}
    </> : <p>还没有可参考的交流记录，先按你明确保存的设置安排联系。</p>}
  </div>;
}

const clockTime = (minutes: number) => `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
const minutesOf = (time: string) => { const [hours, minutes] = time.split(':').map(Number); return hours * 60 + minutes; };
const draftOf = (settings: ContactSettings): Draft => ({
  enabled: settings.enabled, timezone: settings.timezone, quiet_enabled: settings.quiet_start !== null,
  quiet_start: clockTime(settings.quiet_start ?? 23 * 60), quiet_end: clockTime(settings.quiet_end ?? 8 * 60),
  min_interval_minutes: String(settings.min_interval_minutes), max_per_day: String(settings.max_per_day),
});

export const ContactSettingsPanel = forwardRef<ContactSettingsHandle, { onBack: () => void }>(function ContactSettingsPanel({ onBack }, ref) {
  const [settings, setSettings] = useState<ContactSettings | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [leaving, setLeaving] = useState(false);
  const [reload, setReload] = useState(0);
  const pending = useRef<(() => void) | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const dirty = !!settings && !!draft && JSON.stringify(draft) !== JSON.stringify(draftOf(settings));

  function failure(cause: unknown) { setError(cause instanceof Error ? cause.message : '联系偏好暂时无法保存，请重试。'); }
  function change(next: Draft) { setDraft(next); setNotice(''); setError(''); }
  function requestLeave(action: () => void) {
    if (busy) return;
    if (dirty) { pending.current = action; setLeaving(true); }
    else action();
  }
  useImperativeHandle(ref, () => ({ requestLeave }));

  useEffect(() => {
    let active = true;
    setLoading(true); setError('');
    void request<ContactSettings>('/contact-settings').then(state => {
      if (active) { setSettings(state); setDraft(draftOf(state)); }
    }).catch(cause => { if (active) failure(cause); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reload]);
  useEffect(() => { heading.current?.focus(); }, []);
  useUnsavedWarning(dirty);
  useSaveShortcut(!!draft, busy || leaving, () => form.current?.requestSubmit());
  useModalDialog(dialog, leaving);

  async function save() {
    if (!draft || !form.current?.reportValidity()) return false;
    if (draft.quiet_enabled && draft.quiet_start === draft.quiet_end) {
      setError('免打扰的开始和结束时间不能相同。');
      return false;
    }
    setBusy(true); setError(''); setNotice('');
    try {
      const state = await request<ContactSettings>('/contact-settings', {
        enabled: draft.enabled, timezone: draft.timezone.trim(),
        quiet_start: draft.quiet_enabled ? minutesOf(draft.quiet_start) : null,
        quiet_end: draft.quiet_enabled ? minutesOf(draft.quiet_end) : null,
        min_interval_minutes: Number(draft.min_interval_minutes), max_per_day: Number(draft.max_per_day),
      });
      setSettings(state); setDraft(draftOf(state)); setNotice('联系偏好已保存。');
      return true;
    } catch (cause) { failure(cause); return false; }
    finally { setBusy(false); }
  }
  async function resolveLeave(shouldSave: boolean) {
    setLeaving(false);
    if (shouldSave && !(await save())) return;
    pending.current?.(); pending.current = null;
  }

  return <main id="main" className="study-page contact-page" tabIndex={-1}>
    <div className="panel-toolbar"><div><p className="eyebrow">留一点安静的时间</p><h1 ref={heading} tabIndex={-1}>联系偏好</h1></div><button className="text-button" onClick={onBack} disabled={busy}>返回账号设置</button></div>
    <p className="page-subtitle">决定 LISA 是否可以主动来找你，以及适合联系的时间。</p>
    {error && <ErrorNotice message={error} />}
    {loading ? <p className="loading-note" role="status">正在读取联系偏好…</p>
      : !draft || !settings ? <button className="primary" onClick={() => setReload(value => value + 1)}>重新读取联系偏好</button>
      : <section className="setup-panel contact-panel" aria-label="主动联系设置">
        {!settings.available && <p className="contact-availability" role="status">主动联系服务暂未开启。偏好仍可保存，开启前不会自动发出消息。</p>}
        <form ref={form} onSubmit={event => { event.preventDefault(); void save(); }} aria-busy={busy}>
          <fieldset disabled={busy}>
            <label className="contact-choice"><input type="checkbox" checked={draft.enabled} onChange={event => change({ ...draft, enabled: event.target.checked })} /><span><strong>允许 LISA 主动联系我</strong><small>关闭时，LISA 不会主动发起新的 QQ 消息。</small></span></label>
            <p className="contact-rules">等待你的回复时，最多主动联系一次；超过 24 小时未收到你的 QQ 消息，会自动暂停。普通聊天回复与明确委托的结果通知不受这里的开关、免打扰和频率设置影响。</p>
            <div className="contact-group">
              <h2>免打扰时间</h2>
              <div className="field"><label htmlFor="contact-timezone">所在时区</label><input id="contact-timezone" value={draft.timezone} onChange={event => change({ ...draft, timezone: event.target.value })} required maxLength={64} autoComplete="off" spellCheck={false} list="contact-timezones" aria-describedby="contact-timezone-hint" /><datalist id="contact-timezones"><option value="Asia/Shanghai" /><option value="Asia/Tokyo" /><option value="Europe/London" /><option value="America/New_York" /><option value="America/Los_Angeles" /><option value="UTC" /></datalist><p className="field-hint" id="contact-timezone-hint">使用 IANA 时区名称，例如中国大陆填写 Asia/Shanghai。免打扰时段按此时区计算。</p></div>
              <label className="contact-choice compact-choice"><input type="checkbox" checked={draft.quiet_enabled} onChange={event => change({ ...draft, quiet_enabled: event.target.checked })} /><span>启用免打扰时段</span></label>
              {draft.quiet_enabled && <><div className="contact-fields"><div className="field"><label htmlFor="contact-quiet-start">免打扰开始</label><input id="contact-quiet-start" type="time" required value={draft.quiet_start} onChange={event => change({ ...draft, quiet_start: event.target.value })} /></div><div className="field"><label htmlFor="contact-quiet-end">免打扰结束</label><input id="contact-quiet-end" type="time" required value={draft.quiet_end} onChange={event => change({ ...draft, quiet_end: event.target.value })} /></div></div><p className="field-hint">可以跨越午夜，例如 23:00 至次日 08:00；开始和结束时间不能相同。</p></>}
            </div>
            <div className="contact-group">
              <h2>联系频率</h2><p className="field-hint">调长间隔或降低 24 小时上限，让主动联系更少一些。这些是发送上限，不代表一定会发送。</p>
              <div className="contact-fields"><div className="field"><label htmlFor="contact-interval">两次主动联系至少间隔（分钟）</label><input id="contact-interval" type="number" required min={60} max={1440} step={1} value={draft.min_interval_minutes} onChange={event => change({ ...draft, min_interval_minutes: event.target.value })} /><p className="field-hint">60–1,440 分钟；240 分钟为 4 小时。收到你的新消息后，也会留出这段间隔。</p></div><div className="field"><label htmlFor="contact-daily">任意 24 小时最多主动联系（次）</label><input id="contact-daily" type="number" required min={1} max={5} step={1} value={draft.max_per_day} onChange={event => change({ ...draft, max_per_day: event.target.value })} /><p className="field-hint">按滚动 24 小时统计，最多 1–5 次；等待回复时仍最多 1 次。</p></div></div>
            </div>
            {settings.observations && <InteractionNotes observations={settings.observations} />}
          </fieldset>
          <div className="save-bar"><button className="primary" disabled={busy}>{busy ? '正在保存…' : '保存联系偏好'}</button><p role="status" aria-label="联系偏好保存状态">{busy ? '请稍候' : dirty ? '有未保存修改 · Ctrl+S 保存' : notice || '按已保存的偏好执行'}</p></div>
        </form>
      </section>}
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="contact-leave-title" onCancel={event => { event.preventDefault(); setLeaving(false); }}>
      <h2 id="contact-leave-title">有未保存的联系偏好</h2><p>保存后再离开，或放弃本次修改。未保存的偏好不会改变联系安排。</p><div className="button-row"><button className="primary" onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" onClick={() => setLeaving(false)} data-modal-autofocus>继续编辑</button></div>
    </dialog>
  </main>;
});
