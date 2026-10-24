import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import type { Corpus } from './style-profiles';
import { ErrorNotice } from './ui';

type Group = { id: string; bot_id: string; qq_group_id: string; name: string; enabled: boolean; message_count: number; first_at: string | null; last_at: string | null };
type Member = { qq_id: string; name: string; message_count: number };
type Grant = { username: string; user_id: string };
type Message = { id: string; qq_id: string; name: string; content: string; sent_at: string };
type Filters = { qq_id: string; keyword: string; start: string; end: string };
type Search = { total: number; messages: Message[]; offset: number; limit: number; filters: Filters };
const emptyFilters = (): Filters => ({ qq_id: '', keyword: '', start: '', end: '' });
const time = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '暂无记录';

export function GroupRecords({ disabled, canDistill = true, onCorpus }: { disabled: boolean; canDistill?: boolean; onCorpus: (corpus: Corpus, targetSpeakers: string[]) => Promise<void> | void }) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [groupId, setGroupId] = useState('');
  const [members, setMembers] = useState<Member[]>([]);
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [search, setSearch] = useState<Search | null>(null);
  const [grants, setGrants] = useState<Grant[]>([]);
  const [adminOpen, setAdminOpen] = useState(false);
  const [botId, setBotId] = useState('');
  const [qqGroupId, setQQGroupId] = useState('');
  const [groupName, setGroupName] = useState('');
  const [collectNew, setCollectNew] = useState(false);
  const [username, setUsername] = useState('');
  const [cleaning, setCleaning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [membersError, setMembersError] = useState('');
  const [membersRetry, setMembersRetry] = useState(0);
  const [grantsError, setGrantsError] = useState('');
  const [grantsRetry, setGrantsRetry] = useState(0);
  const mounted = useRef(true);
  const loadGeneration = useRef(0);
  const selectedGroup = useRef(groupId);
  selectedGroup.current = groupId;
  const locked = disabled || busy || loading;
  const group = groups.find(item => item.id === groupId);
  const currentSearch = search && JSON.stringify(search.filters) === JSON.stringify(filters);
  function failure(cause: unknown) { setError(cause instanceof Error ? cause.message : '群消息操作暂时未能完成，请重试。'); }

  async function load() {
    const generation = ++loadGeneration.current;
    setLoading(true); setError('');
    try {
      const state = await request<{ is_admin: boolean; groups: Group[] }>('/groups');
      if (mounted.current && generation === loadGeneration.current) { setGroups(state.groups); setIsAdmin(state.is_admin); setGroupId(previous => state.groups.some(item => item.id === previous) ? previous : state.groups[0]?.id ?? ''); }
    } catch (cause) { if (mounted.current && generation === loadGeneration.current) failure(cause); }
    finally { if (mounted.current && generation === loadGeneration.current) setLoading(false); }
  }
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; loadGeneration.current += 1; }; }, []);
  useEffect(() => { setFilters(emptyFilters()); setSearch(null); setCleaning(false); }, [groupId]);
  useEffect(() => {
    if (!groupId) return;
    let active = true; setMembers([]); setMembersError('');
    void request<Member[]>(`/groups/${groupId}/members`).then(items => { if (active) setMembers(items); }).catch(cause => { if (active) setMembersError(cause instanceof Error ? cause.message : '暂时无法读取群成员。'); });
    return () => { active = false; };
  }, [groupId, membersRetry]);
  useEffect(() => {
    if (!groupId || !isAdmin || !adminOpen) return;
    let active = true; setGrants([]); setGrantsError('');
    void request<{ grants: Grant[] }>(`/groups/${groupId}/grants`).then(state => { if (active) setGrants(state.grants); }).catch(cause => { if (active) setGrantsError(cause instanceof Error ? cause.message : '暂时无法读取授权。'); });
    return () => { active = false; };
  }, [groupId, isAdmin, adminOpen, grantsRetry]);

  function selectGroup(id: string) { setGroupId(id); setFilters(emptyFilters()); setSearch(null); setCleaning(false); setError(''); setNotice(''); }
  function params(value: Filters) {
    if (value.start && value.end && new Date(value.start) > new Date(value.end)) throw new Error('开始时间不能晚于结束时间。');
    return { ...(value.qq_id.trim() ? { qq_id: value.qq_id.trim() } : {}), ...(value.keyword.trim() ? { keyword: value.keyword.trim() } : {}), ...(value.start ? { start: new Date(value.start).toISOString() } : {}), ...(value.end ? { end: new Date(value.end).toISOString() } : {}) };
  }
  async function find(offset = 0, value = filters) {
    if (!groupId || locked) return;
    const searchedGroup = groupId;
    setBusy(true); setError(''); setNotice('');
    try {
      const query = new URLSearchParams({ ...params(value), offset: String(offset) });
      const state = await request<Omit<Search, 'filters'>>(`/groups/${groupId}/messages?${query}`);
      if (mounted.current && selectedGroup.current === searchedGroup) setSearch({ ...state, filters: { ...value } });
    } catch (cause) { if (mounted.current && selectedGroup.current === searchedGroup) failure(cause); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function distill() {
    if (!groupId || !filters.qq_id.trim() || !currentSearch || !search.total || !canDistill || locked) return;
    const searchedGroup = groupId;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await request<{ corpus: Corpus; target_speakers: string[] }>(`/groups/${groupId}/corpus`, { ...params(filters), qq_id: filters.qq_id.trim() });
      if (mounted.current && selectedGroup.current === searchedGroup) await onCorpus(result.corpus, result.target_speakers);
      else void request(`/style/corpora/${result.corpus.id}`, undefined, 'DELETE').catch(() => {});
    } catch (cause) { if (mounted.current && selectedGroup.current === searchedGroup) failure(cause); }
    finally { if (mounted.current) setBusy(false); }
  }
  async function adminAction(action: () => Promise<unknown>, message: string) {
    if (locked) return false;
    setBusy(true); setError(''); setNotice('');
    try { await action(); if (mounted.current) { setNotice(message); await load(); } return true; }
    catch (cause) { if (mounted.current) failure(cause); return false; }
    finally { if (mounted.current) setBusy(false); }
  }
  async function addGroup() {
    const ok = await adminAction(() => request('/groups', { bot_id: botId.trim(), qq_group_id: qqGroupId.trim(), name: groupName.trim(), enabled: collectNew }), '群已登记。只有启用采集后收到的新消息才会进入记录。');
    if (ok && mounted.current) { setBotId(''); setQQGroupId(''); setGroupName(''); setCollectNew(false); }
  }
  async function grant() {
    const ok = await adminAction(() => request(`/groups/${groupId}/grants`, { username: username.trim() }, 'PUT'), '已授予此账号访问本群记录的权限。');
    if (ok && mounted.current) { setUsername(''); setGrantsRetry(value => value + 1); }
  }
  async function revoke(userId: string) {
    const ok = await adminAction(() => request(`/groups/${groupId}/grants/${userId}`, undefined, 'DELETE'), '授权已撤回；该账号未完成的相关蒸馏将取消，已保存方案保留。');
    if (ok && mounted.current) setGrantsRetry(value => value + 1);
  }
  async function clear() {
    const ok = await adminAction(() => request(`/groups/${groupId}/messages`, undefined, 'DELETE'), '本群当前记录已清理，未完成的相关蒸馏将取消；已保存方案保留。');
    if (ok && mounted.current) { setSearch(null); setMembersRetry(value => value + 1); setCleaning(false); }
  }

  return <section className="group-records" aria-label="群消息记录">
    <p className="form-copy">从获授权群中选择成员，按时间或关键字检索，确认范围后提炼交流性格。机器人只接收启用后收到的新消息，按 30 天滚动保留，不保证完整历史，群内不会回复。</p>
    <p className="field-hint">蒸馏只抽取目标人物及邻近上下文，不将群聊写为 LISA 经历。临时提取副本在任务结束后清理，群记录仍按 30 天规则管理。</p>
    {error && <ErrorNotice message={error} />}{notice && <p role="status" className="style-notice">{notice}</p>}
    {loading ? <p role="status">正在读取可访问的群…</p> : <>
      {!groups.length ? <div className="group-empty"><p>暂无可访问的群记录。</p><p className="field-hint">请管理员登记并启用群采集，再按你的网页用户名授予访问权限。</p></div> : <>
        <div className="field"><label htmlFor="record-group">选择获授权的群</label><select id="record-group" value={groupId} disabled={locked} onChange={event => selectGroup(event.target.value)}>{groups.map(item => <option key={item.id} value={item.id}>{item.name || item.qq_group_id} · 群 {item.qq_group_id}</option>)}</select></div>
        {group && <p className="group-meta">{group.enabled ? '正在采集新消息' : '采集已关闭'} · 当前 {group.message_count} 条记录<br />可用范围：{time(group.first_at)} 至 {time(group.last_at)}</p>}
        {membersError && <><ErrorNotice message={membersError} /><button className="text-button" disabled={locked} onClick={() => setMembersRetry(value => value + 1)}>重新读取成员</button></>}
        <form className="group-search" onSubmit={event => { event.preventDefault(); void find(); }}>
          <fieldset disabled={locked}>
            <div className="contact-fields"><div className="field"><label htmlFor="record-member">已记录的群成员</label><select id="record-member" value={members.some(item => item.qq_id === filters.qq_id) ? filters.qq_id : ''} onChange={event => setFilters({ ...filters, qq_id: event.target.value })}><option value="">按 QQ 号手动指定</option>{members.map(member => <option key={member.qq_id} value={member.qq_id}>{member.name || member.qq_id} · {member.qq_id} · {member.message_count} 条</option>)}</select></div><div className="field"><label htmlFor="record-qq">目标成员 QQ 号</label><input id="record-qq" value={filters.qq_id} onChange={event => setFilters({ ...filters, qq_id: event.target.value })} pattern="[0-9]+" maxLength={32} inputMode="numeric" /><p className="field-hint">可选择其他成员，也可手动填写。留空时仅浏览，不发起蒸馏。</p></div></div>
            <div className="contact-fields"><div className="field"><label htmlFor="record-start">开始时间（本地）</label><input id="record-start" type="datetime-local" value={filters.start} onChange={event => setFilters({ ...filters, start: event.target.value })} /></div><div className="field"><label htmlFor="record-end">结束时间（本地）</label><input id="record-end" type="datetime-local" value={filters.end} onChange={event => setFilters({ ...filters, end: event.target.value })} /></div></div>
            <div className="field"><label htmlFor="record-keyword">消息关键字</label><input id="record-keyword" value={filters.keyword} maxLength={200} onChange={event => setFilters({ ...filters, keyword: event.target.value })} /></div>
            <div className="button-row"><button className="secondary">检索群消息</button><button type="button" className="primary" disabled={!canDistill || !currentSearch || !search.total || !filters.qq_id.trim()} onClick={() => void distill()}>从选定群消息提炼性格</button></div>
          {!canDistill && <p className="field-hint">候选已有编辑内容；保存后请新建交流性格方案，再发起新的提炼。群记录检索和管理仍可使用。</p>}</fieldset>
        </form>
        {search && <div className="group-results" aria-label="群消息检索结果"><p role="status">{currentSearch ? `共找到 ${search.total} 条消息` : '检索条件已修改，请重新检索后再提炼。'}{search.total > 0 && ` · 显示第 ${search.offset + 1}–${search.offset + search.messages.length} 条`}</p><p className="field-hint">这里展示匹配记录；蒸馏会另行加入有界邻近上下文。关键字和时间范围不代表完整话题，超限请缩小范围。</p>{!search.messages.length ? <p>当前范围没有匹配消息，请调整成员、时间或关键字。</p> : <ol>{search.messages.map(message => <li key={message.id}><div><strong>{message.name || message.qq_id}</strong><span>QQ {message.qq_id}</span><time dateTime={message.sent_at}>{time(message.sent_at)}</time></div><p>{message.content}</p></li>)}</ol>}<div className="button-row"><button className="text-button" disabled={locked || !currentSearch || search.offset === 0} onClick={() => void find(Math.max(0, search.offset - search.limit), search.filters)}>上一页</button><button className="text-button" disabled={locked || !currentSearch || search.offset + search.messages.length >= search.total} onClick={() => void find(search.offset + search.limit, search.filters)}>下一页</button></div></div>}
      </>}
      <button className="text-button" disabled={locked} onClick={() => void load()}>刷新可访问的群</button>
    </>}
    {isAdmin && <details className="group-admin" open={adminOpen} onToggle={event => setAdminOpen(event.currentTarget.open)}><summary>管理员：采集与访问权限</summary><p className="field-hint">用机器人 QQ 与群号登记来源。关闭采集不删除已有记录；撤权和清理会取消尚未完成的相关蒸馏，不删除已保存方案。</p>
      <form onSubmit={event => { event.preventDefault(); void addGroup(); }}><fieldset disabled={locked}><h3>登记一个群</h3><div className="contact-fields"><div className="field"><label htmlFor="admin-bot">机器人 QQ</label><input id="admin-bot" required pattern="[0-9]+" maxLength={32} inputMode="numeric" value={botId} onChange={event => setBotId(event.target.value)} /></div><div className="field"><label htmlFor="admin-group">QQ 群号</label><input id="admin-group" required pattern="[0-9]+" maxLength={32} inputMode="numeric" value={qqGroupId} onChange={event => setQQGroupId(event.target.value)} /></div></div><div className="field"><label htmlFor="admin-group-name">群名称</label><input id="admin-group-name" required maxLength={120} value={groupName} onChange={event => setGroupName(event.target.value)} /></div><label className="contact-choice compact-choice"><input type="checkbox" checked={collectNew} onChange={event => setCollectNew(event.target.checked)} /><span>登记后启用新消息采集</span></label><button className="secondary">登记群</button></fieldset></form>
      {group && <div className="group-access"><h3>管理《{group.name || group.qq_group_id}》</h3><button className="secondary" disabled={locked} onClick={() => void adminAction(() => request(`/groups/${groupId}`, { enabled: !group.enabled }, 'PATCH'), group.enabled ? '已关闭新消息采集，已有记录保留至到期。' : '已启用新消息采集，不会补拉此前历史。')}>{group.enabled ? '关闭此群采集' : '启用此群采集'}</button>
        <form onSubmit={event => { event.preventDefault(); void grant(); }}><fieldset disabled={locked}><div className="field"><label htmlFor="admin-grant-user">授权的网页用户名</label><input id="admin-grant-user" required minLength={3} maxLength={32} value={username} onChange={event => setUsername(event.target.value)} /></div><button className="secondary">授权访问此群</button></fieldset></form>
        {grantsError ? <><ErrorNotice message={grantsError} /><button className="text-button" disabled={locked} onClick={() => setGrantsRetry(value => value + 1)}>重新读取授权</button></> : <ul className="group-grants">{grants.map(item => <li key={item.user_id}><span>{item.username}</span><button className="text-button" disabled={locked} onClick={() => void revoke(item.user_id)}>撤回 {item.username} 的授权</button></li>)}</ul>}
        <div className="group-cleanup">{!cleaning ? <button className="text-button danger" disabled={locked} onClick={() => setCleaning(true)}>清理此群全部记录</button> : <div role="group" aria-label="确认清理群记录"><p>确认清理《{group.name || group.qq_group_id}》现有的全部群消息？此操作不可恢复，相关未完成蒸馏将取消，已保存的风格方案保留。若采集仍开启，之后的新消息还会记录。</p><div className="button-row"><button className="secondary danger" disabled={locked} onClick={() => void clear()}>确认清理群记录</button><button className="text-button" disabled={locked} onClick={() => setCleaning(false)}>暂不清理</button></div></div>}</div>
      </div>}
    </details>}
  </section>;
}
