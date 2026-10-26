import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';
import { useModalDialog, useSaveShortcut, useUnsavedWarning } from './editor-state';
import { itemOccupies, timeRange } from './life-plan-time';
import './life-plan.css';

type PlanStatus = 'tentative' | 'confirmed' | 'completed' | 'cancelled' | 'expired';
type PlanItem = {
  id: string; date: string; time: string; title: string; detail: string; status: PlanStatus;
  source: 'agent' | 'chat' | 'user'; source_message_id: string | null;
  source_deleted?: boolean; source_label?: string;
};
type Plan = { id: string; week_start: string; week_end: string; state: 'active' | 'cancelled'; revision: number; items: PlanItem[]; updated_at: string | null };
type PlanResponse = { current: Plan | null; plans: Plan[] };
export type LifePlanHandle = { requestLeave: (action: () => void) => void };

const weekdayNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const MAX_PLAN_ITEMS = 56;
const timeSlots = Array.from({ length: 12 }, (_, index) => {
  const start = index * 2;
  return { start, label: `${String(start).padStart(2, '0')}:00–${String(start + 2).padStart(2, '0')}:00` };
});
const weekLabel = (plan: Plan) => `${plan.week_start} 至 ${plan.week_end}`;
function addDays(value: string, offset: number) {
  const date = new Date(`${value}T12:00:00`);
  date.setDate(date.getDate() + offset);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}
function dayLabel(value: string, weekday: string) {
  const [, month, day] = value.split('-');
  return `${weekday} ${Number(month)}/${Number(day)}`;
}
function slotTime(start: number) {
  return `${String(start).padStart(2, '0')}:00`;
}
const newItem = (date: string, time: string): PlanItem => ({
  id: crypto.randomUUID(), date, time, title: '', detail: '', status: 'tentative', source: 'user', source_message_id: null, source_label: '用户填写',
});

export const LifePlan = forwardRef<LifePlanHandle>(function LifePlan(_, ref) {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [history, setHistory] = useState<Plan[]>([]);
  const [draft, setDraft] = useState<PlanItem[]>([]);
  const [baseline, setBaseline] = useState<PlanItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [modal, setModal] = useState<'leave' | 'regenerate' | 'delete' | null>(null);
  const [expandedItem, setExpandedItem] = useState<string | null>(null);
  const pending = useRef<(() => void) | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const detailsDialog = useRef<HTMLDialogElement>(null);
  const saveButton = useRef<HTMLButtonElement>(null);
  const [selectedDate, setSelectedDate] = useState('');
  const dirty = !!plan && JSON.stringify(draft) !== JSON.stringify(baseline);

  async function load() {
    setLoading(true); setError('');
    try {
      const result = await request<PlanResponse>('/life-plan?history=true');
      setPlan(result.current); setHistory(result.plans); setDraft(result.current?.items ?? []); setBaseline(result.current?.items ?? []);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取每周提案。'); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  useModalDialog(dialog, modal !== null);
  useModalDialog(detailsDialog, expandedItem !== null, saveButton);
  useUnsavedWarning(dirty);
  useSaveShortcut(!!plan, busy || modal !== null, () => dirty && save());
  function requestAction(action: () => void, reason: 'leave' | 'regenerate' = 'leave') {
    if (busy || loading) return;
    if (dirty) { pending.current = action; setModal(reason); } else action();
  }
  useImperativeHandle(ref, () => ({ requestLeave: requestAction }));

  function change(index: number, value: Partial<PlanItem>) {
    setDraft(items => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...value, source: 'user', source_message_id: null, source_label: '用户填写' } : item));
    setNotice('');
  }
  function addItemAt(date: string, start: number) {
    if (!plan || draft.length >= MAX_PLAN_ITEMS) return;
    const item = newItem(date, slotTime(start));
    setDraft(items => [...items, item]); setExpandedItem(item.id); setNotice('');
  }
  function removeItem(index: number) { setDraft(items => items.filter((_, itemIndex) => itemIndex !== index)); setExpandedItem(null); setNotice(''); }

  async function generate(force = false) {
    setBusy(true); setError(''); setNotice('');
    try {
      // Generation is synchronous; leave room for the configured 180-second model timeout.
      const next = await request<Plan>('/life-plan/generate', { force }, 'POST', { timeoutMs: 210_000 });
      setPlan(next); setDraft(next.items); setBaseline(next.items); setNotice(force ? '已重新生成 LISA 的暂定提案。' : '已生成本周的 LISA 暂定提案。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '暂时无法生成周表。'); }
    finally { setBusy(false); }
  }
  async function save() {
    if (!plan || draft.some(item => !item.title.trim() || !item.time.trim())) { setError('每条提案都需要填写内容和时间段。'); return false; }
    setBusy(true); setError(''); setNotice('');
    try {
      const items = draft.map(({ source_deleted: _sourceDeleted, source_label: _sourceLabel, ...item }) => item);
      const next = await request<Plan>(`/life-plan/${plan.id}`, { revision: plan.revision, state: plan.state, items }, 'PUT');
      setPlan(next); setDraft(next.items); setBaseline(next.items); setNotice('每周提案已保存。'); return true;
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存周表失败，请重新读取。'); return false; }
    finally { setBusy(false); }
  }
  async function saveState(state: Plan['state']) {
    if (!plan || dirty) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const items = draft.map(({ source_deleted: _sourceDeleted, source_label: _sourceLabel, ...item }) => item);
      const next = await request<Plan>(`/life-plan/${plan.id}`, { revision: plan.revision, state, items }, 'PUT');
      setPlan(next); setDraft(next.items); setBaseline(next.items);
      setNotice(state === 'cancelled' ? '本周提案已取消。' : '本周提案已恢复。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '更新周表状态失败，请重新读取。'); }
    finally { setBusy(false); }
  }
  async function deletePlan() {
    if (!plan) return;
    setBusy(true); setError('');
    try { await request(`/life-plan/${plan.id}`, undefined, 'DELETE'); setPlan(null); setDraft([]); setBaseline([]); setModal(null); setNotice('本周提案已删除。'); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '删除周表失败。'); }
    finally { setBusy(false); }
  }
  async function resolveLeave(saveChanges: boolean) {
    const action = pending.current; pending.current = null; setModal(null);
    if (saveChanges && !(await save())) return;
    if (!saveChanges) setDraft(baseline);
    action?.();
  }

  const calendarDays = plan
    ? weekdayNames.map((weekday, offset) => ({ date: addDays(plan.week_start, offset), weekday }))
    : [];
  const calendarItems = new Map<string, Array<{ item: PlanItem; index: number }>>();
  draft.forEach((item, index) => {
    const items = calendarItems.get(item.date) ?? [];
    items.push({ item, index });
    calendarItems.set(item.date, items);
  });
  const selectedIndex = expandedItem ? draft.findIndex(item => item.id === expandedItem) : -1;
  const selectedItem = selectedIndex >= 0 ? draft[selectedIndex] : null;
  const selectedDay = selectedItem && calendarDays.find(day => day.date === selectedItem.date);
  const visibleDate = calendarDays.some(day => day.date === selectedDate) ? selectedDate : plan?.week_start ?? '';
  const dailyItems = draft.filter(item => item.date === visibleDate || timeSlots.some(slot => itemOccupies(item, visibleDate, slot.start / 2)));
  const unscheduledItems = draft.filter(item => timeRange(item.time) === null);
  const renderCell = (date: string, slot: number, label: string) => {
    const entries = draft.flatMap((item, index) => itemOccupies(item, date, slot) ? [{ item, index }] : []);
    return <div
      className={`life-plan-slot${entries.length ? ' has-items' : ''}`}
      role={entries.length ? undefined : 'button'}
      tabIndex={entries.length ? -1 : busy ? -1 : 0}
      aria-label={`${dayLabel(date, calendarDays.find(day => day.date === date)?.weekday ?? '')} ${label}${entries.length ? `，${entries.length} 项提案` : '，自由时间'}`}
      onClick={() => { if (!busy && !entries.length) addItemAt(date, slot * 2); }}
      onKeyDown={event => { if ((event.key === 'Enter' || event.key === ' ') && !busy && !entries.length) { event.preventDefault(); addItemAt(date, slot * 2); } }}
    >
      {entries.length ? entries.map(({ item }) => <button type="button" className={`life-plan-event${expandedItem === item.id ? ' is-selected' : ''}`} key={item.id} onClick={event => { event.stopPropagation(); if (!busy) setExpandedItem(expandedItem === item.id ? null : item.id); }}>
        <span className="life-plan-event-time">{item.time || label}</span>
        <strong>{item.title || '未命名提案'}</strong>
      </button>) : <span className="life-plan-free-time">自由时间</span>}
    </div>;
  };

  return <section className="life-plan-page">
    <div className="memory-heading"><div><p className="eyebrow">LISA 的这一周</p><h1>每周虚拟生活提案</h1><p className="page-subtitle">这里是可修改的暂定安排，不是已经发生的生活事实。</p></div><div className="life-plan-actions"><button className="secondary" disabled={busy || loading} onClick={() => requestAction(() => { void generate(!!plan); }, 'regenerate')}>{plan ? '重新生成' : '生成本周提案'}</button>{plan && <><button className="text-button" disabled={busy || dirty} onClick={() => void saveState(plan.state === 'cancelled' ? 'active' : 'cancelled')}>{plan.state === 'cancelled' ? '恢复本周' : '取消本周'}</button><button className="text-button danger" disabled={busy} onClick={() => setModal('delete')}>删除本周</button></>}</div></div>
    {error && <ErrorNotice message={error} />}
    {notice && <p className="memory-notice" role="status">{notice}</p>}
    {loading ? <p className="loading-note" role="status">正在读取每周提案…</p> : !plan ? <section className="empty-paper life-plan-empty"><h2>还没有本周提案</h2><p>生成后，LISA 会先放下一份有连续作息的本周安排。你可以在这里修改、确认或删除它们。</p><button className="primary" disabled={busy} onClick={() => void generate()}>生成本周提案</button></section> : <>
      <section className="life-plan-summary" aria-label="周表范围"><strong>{weekLabel(plan)}</strong><span>{plan.state === 'cancelled' ? '本周已取消' : '计划不会自动变成已发生事实'}</span></section>
      <section className="life-plan-daily" aria-label="按日查看提案">
        <div className="life-plan-daily-toolbar"><div className="field"><label htmlFor="life-plan-selected-day">查看日期</label><select id="life-plan-selected-day" value={visibleDate} onChange={event => setSelectedDate(event.target.value)}>{calendarDays.map(({ date, weekday }) => <option key={date} value={date}>{dayLabel(date, weekday)}</option>)}</select></div><button className="secondary" disabled={busy || draft.length >= MAX_PLAN_ITEMS} onClick={() => addItemAt(visibleDate, 12)}>添加提案</button></div>
        {dailyItems.length ? dailyItems.map(item => <button className="life-plan-event" key={item.id} disabled={busy} onClick={() => setExpandedItem(item.id)}><span className="life-plan-event-time">{item.date !== visibleDate ? '前一日延续 · ' : ''}{item.time}</span><strong>{item.title || '未命名提案'}</strong></button>) : <p className="life-plan-day-empty">这一天暂无提案，可以留些自由时间。</p>}
      </section>
      <section className="life-plan-schedule" aria-label="每周虚拟生活提案日程">
        <div className="life-plan-schedule-grid">
          <div className="life-plan-schedule-head"><span className="life-plan-time-axis">时间</span>{calendarDays.map(({ date, weekday }) => <span className="life-plan-day-head" key={date}><strong>{dayLabel(date, weekday)}</strong><small>{calendarItems.get(date)?.length ?? 0} 项</small></span>)}</div>
          <div className="life-plan-schedule-body">
            {timeSlots.map(slot => <div className="life-plan-schedule-row" key={slot.start}><span className="life-plan-time-axis">{slot.label}</span>{calendarDays.map(({ date }) => <div key={date}>{renderCell(date, slot.start / 2, slot.label)}</div>)}</div>)}
          </div>
        </div>
      </section>
      {!!unscheduledItems.length && <section className="life-plan-unplaced" aria-label="未定位时间的提案"><p>以下时间尚未定位到日程格，仍保留在周表中：</p>{unscheduledItems.map(item => <button className="secondary" key={item.id} onClick={() => setExpandedItem(item.id)}>{item.date} · {item.time || '未填写时间'} · {item.title || '未命名提案'}</button>)}</section>}
      <dialog ref={detailsDialog} className="life-plan-popover" aria-labelledby="life-plan-popover-title" onCancel={event => { event.preventDefault(); if (!busy) setExpandedItem(null); }}>
        {selectedItem && selectedIndex >= 0 && <>
          <div className="life-plan-popover-head"><div><p className="eyebrow">{selectedDay ? dayLabel(selectedDay.date, selectedDay.weekday) : selectedItem.date}</p><h2 id="life-plan-popover-title">提案详情</h2></div><button type="button" className="text-button" disabled={busy} onClick={() => setExpandedItem(null)}>关闭</button></div>
          {error && <ErrorNotice message={error} />}
          <div className="life-plan-fields"><div className="field"><label htmlFor={`life-plan-date-${selectedItem.id}`}>日期</label><input id={`life-plan-date-${selectedItem.id}`} type="date" data-modal-autofocus value={selectedItem.date} disabled={busy} onChange={event => change(selectedIndex, { date: event.target.value })} /></div><div className="field"><label htmlFor={`life-plan-time-${selectedItem.id}`}>时间段</label><input id={`life-plan-time-${selectedItem.id}`} value={selectedItem.time} maxLength={80} disabled={busy} onChange={event => change(selectedIndex, { time: event.target.value })} placeholder="例如：18:00 或 18:00–20:00" required /></div></div>
          <div className="field"><label htmlFor={`life-plan-title-${selectedItem.id}`}>提案</label><input id={`life-plan-title-${selectedItem.id}`} value={selectedItem.title} maxLength={160} disabled={busy} onChange={event => change(selectedIndex, { title: event.target.value })} placeholder="这一周想做的一件小事" /></div>
          <div className="field"><label htmlFor={`life-plan-detail-text-${selectedItem.id}`}>补充说明（可选）</label><textarea id={`life-plan-detail-text-${selectedItem.id}`} rows={4} maxLength={500} disabled={busy} value={selectedItem.detail} onChange={event => change(selectedIndex, { detail: event.target.value })} /></div>
          {selectedItem.source !== 'agent' && <p className="life-plan-meta">来源：{selectedItem.source_label}（仅保留结构化来源，不保存原文） · 日期到达不会自动完成。</p>}
          <div className="life-plan-popover-actions"><button type="button" className="text-button danger" disabled={busy} onClick={() => removeItem(selectedIndex)}>删除提案</button><button type="button" className="primary" disabled={busy} onClick={() => setExpandedItem(null)}>完成</button></div>
        </>}
      </dialog>
      <div className="life-plan-footer"><div className="save-bar"><button ref={saveButton} className="primary" disabled={busy || !dirty} onClick={() => void save()}>{busy ? '正在处理…' : '保存周表'}</button><p role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : '已保存'}</p></div></div>
      {!!history.length && <details className="life-plan-history"><summary>查看已有周表（{history.length}）</summary>{history.map(item => <div key={item.id} className="life-plan-history-row"><strong>{weekLabel(item)}</strong><span>{item.items.length} 项 · {item.state === 'cancelled' ? '已取消' : '已保存'}</span></div>)}</details>}
    </>}
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="life-plan-dialog-title" onCancel={event => { event.preventDefault(); if (!busy) setModal(null); }}>
      {modal === 'leave' || modal === 'regenerate' ? <><h2 id="life-plan-dialog-title">有未保存的周表修改</h2><p>{modal === 'regenerate' ? '重新生成前，请先保存或放弃本次修改。' : '保存后再离开，或放弃本次修改。'}</p><div className="button-row"><button className="primary" disabled={busy} onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" disabled={busy} onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" disabled={busy} data-modal-autofocus onClick={() => setModal(null)}>继续编辑</button></div></> : <><h2 id="life-plan-dialog-title">删除本周提案？</h2><p>删除后本周提案不再显示；已经形成的生活事实不会被删除。</p><div className="button-row"><button className="secondary danger" disabled={busy} onClick={() => void deletePlan()}>确认删除</button><button className="text-button" disabled={busy} onClick={() => setModal(null)}>取消</button></div></>}
    </dialog>
  </section>;
});
