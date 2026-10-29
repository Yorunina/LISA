import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, request } from './api';
import { pollWhileVisible } from './polling';
import { ErrorNotice } from './ui';
import './operations.css';

type Distribution = { median_ms: number | null; p95_ms: number | null; samples: number };
type Count = { name: string; count: number };
type Query = { range: string; start: string; end: string; purpose: string; model: string; task_kind: string; state: string; page: number };
type Evaluation = { id: string; created_at: string; configuration: Record<string, unknown>; results: Record<string, unknown> };
export type OperationsData = {
  generated_at: string; timezone: string; refresh_seconds: number;
  range: { start: string; end: string; label: string };
  coverage: { started_at: string | null; total_calls: number; classified_calls: number; unknown_dispatch: number; history_note: string };
  components: { component: string; label: string; status: string; observed_at: string | null; age_seconds: number | null; details: Record<string, unknown> }[];
  queue: { pending: number; running: number; debouncing: number; scheduled: number; oldest_wait_seconds: number | null };
  tasks: {
    created: number; finished: number; states: Count[]; success_rate: number | null; wait: Distribution; execution: Distribution; end_to_end: Distribution;
    delivery: { pending: number; succeeded: number; failed: number; unknown: number; partial_tasks: number };
    recent: { id: string; kind: string; status: string; created_at: string; started_at: string | null; finished_at: string | null; error: string | null; adopted: boolean }[];
  };
  calls: {
    total: number; confirmed_sent: number; not_sent: number; unknown_dispatch: number; inflight: number; success_rate: number | null; latency: Distribution;
    tokens: { input: number | null; output: number | null; unknown: number };
    by_purpose: { purpose: string; count: number; succeeded: number; failed: number; unknown: number; input_tokens: number | null; output_tokens: number | null; unknown_tokens: number; known_amount: number; unknown_costs: number; median_ms: number | null; p95_ms: number | null; latency_samples: number }[];
    daily: { day: string; count: number; known_amount: number; unknown_count: number }[];
    recent: { id: string; task_id: string; task_kind: string; purpose: string; model: string; state: string; dispatch_state: string; cost_source: string; amount: number | null; input_tokens: number | null; output_tokens: number | null; started_at: string; finished_at: string | null; error: string | null }[];
  };
  costs: { currency: string; known_total: number; provider: number; estimate: number; conservative: number; unknown_count: number; inflight_reserved: number; incomplete: boolean; per_task_average: number | null; ledger_note: string };
  rag: {
    observations: number; states: Count[]; routes: Count[];
    executed_count?: number; empty_count?: number; empty_result_rate?: number | null;
    counts: { candidates: number; assembled: number; final_count: number; final_chars: number };
    omitted: Count[]; latency: Distribution;
    latencies?: { embedding: Distribution; retrieval: Distribution; assembly: Distribution };
    public_counts?: { candidates: number; assembled: number; final_count: number; final_chars: number };
    coverage: { purpose: string; total: number; complete: number; stale: number; partial: number; missing: number; failed?: number }[];
    recent: { id: string; purpose: string; created_at: string; metrics: Record<string, unknown> }[];
    evaluation: Evaluation | null;
  };
  alerts: { code: string; severity: string; message: string; observed_at: string | null }[];
  filters: { purposes: string[]; models: string[]; task_kinds: string[]; states: string[] };
  pagination: { page: number; page_size: number; has_more_calls: boolean; has_more_tasks: boolean; has_more_retrieval: boolean };
};

const initialQuery: Query = { range: 'today', start: '', end: '', purpose: '', model: '', task_kind: '', state: '', page: 1 };
const labels: Record<string, string> = {
  ok: '正常', stale: '已过期', offline: '离线', unknown: '未知', disabled: '未启用',
  pending: '等待中', running: '执行中', completed: '已完成', failed: '失败', succeeded: '成功',
  cancelled: '已取消', cancel_requested: '正在取消', outcome_unknown: '结果未知', reserved: '在途',
  sent: '确认发送', confirmed: '确认发送', not_sent: '未发送', sending: '发送待确认',
  dispatched: '确认发送', dispatching: '发送待确认', unknown_dispatch: '发送待确认',
  chat: '普通聊天', writing: '作者', role: '角色', scene: '场景创作', rewrite: '局部重写',
  image: '图片任务', image_generation: '图片生成', image_description: '绘图描述', vision: '视觉理解',
  embedding: '向量调用', embedding_query: '查询向量', embedding_index: '资料索引',
  query_embedding: '查询向量', index_embedding: '资料索引', style_distill: '风格蒸馏', style_trial: '风格试用',
  proactive: '主动话题', diary: '日记', life_plan: '每周提案', shared_analyze: '共有表情分析', shared_index: '共有索引',
  semantic: '语义检索', title: '标题匹配', recent: '近期记忆兜底', empty: '空结果',
  no_sources: '无可检索资料', unavailable: '不可用', fallback: '降级', executed: '已执行',
  provider: '供应商已确认', estimate: '应用估算', conservative: '保守待确认',
  budget: '字符预算', char_budget: '字符预算', input_budget: '请求容量', count_limit: '数量上限', revision: '版本变化',
};
function label(value: string) { return labels[value] ?? value; }
function number(value: number | null | undefined) { return value == null ? '未知' : value.toLocaleString('zh-CN'); }
function money(value: number | null | undefined) { return value == null ? '未知' : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`; }
function percent(value: number | null | undefined) { return value == null ? '无样本' : `${(value * 100).toFixed(1)}%`; }
function duration(value: number | null | undefined) { return value == null ? '无样本' : value < 1000 ? `${value.toFixed(0)} 毫秒` : `${(value / 1000).toFixed(1)} 秒`; }
function time(value: string | null | undefined, timezone: string) {
  if (!value) return '尚无记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { timeZone: timezone, hour12: false });
}
function short(value: string) { return value.length > 13 ? `${value.slice(0, 8)}…` : value; }
function DistributionView({ title, value }: { title: string; value: Distribution }) {
  return <div className="operations-distribution"><h3>{title}</h3><p>中位数 <strong>{duration(value.median_ms)}</strong><span>P95 <strong>{duration(value.p95_ms)}</strong></span></p><small>{number(value.samples)} 个样本</small></div>;
}
function Counts({ values, empty = '暂无记录' }: { values: Count[]; empty?: string }) {
  return values.length ? <dl className="operations-counts">{values.map(item => <div key={item.name}><dt>{label(item.name)}</dt><dd>{number(item.count)}</dd></div>)}</dl> : <p className="operations-empty">{empty}</p>;
}
function Stat({ title, value, note }: { title: string; value: string; note?: string }) {
  return <div className="operations-stat"><dt>{title}</dt><dd>{value}</dd>{note && <p>{note}</p>}</div>;
}
function SafeMetric({ value }: { value: unknown }) {
  if (typeof value === 'number') return <>{number(value)}</>;
  if (typeof value === 'string') return <>{label(value)}</>;
  return <>未知</>;
}

function EvaluationView({ evaluation, timezone }: { evaluation: Evaluation | null; timezone: string }) {
  if (!evaluation || !Object.keys(evaluation.results).length) return <p className="operations-empty">尚无固定评测结果。线上非空结果率不能代替召回率。</p>;
  const result = evaluation.results;
  const rawGroups = result.by_purpose ?? result.purposes;
  const groups: Record<string, unknown>[] = Array.isArray(rawGroups) ? rawGroups.filter(item => item && typeof item === 'object') : [];
  const cases: Record<string, unknown>[] = Array.isArray(result.cases) ? result.cases.filter(item => item && typeof item === 'object' && item.passed === false) : [];
  return <>
    <p className="field-hint">评测时间：{time(evaluation.created_at, timezone)} · 结果对应当次配置与资料快照。</p>
    <p className="operations-evaluation-note">{result.evaluation_kind === 'deterministic_pipeline' ? '工程链路评测 · 确定性向量' : '固定样本评测'}。此结果不代表真实语义召回效果已验收。</p>
    <dl className="operations-counts"><div><dt>评测集版本</dt><dd><SafeMetric value={result.dataset_version ?? evaluation.configuration.dataset_version ?? evaluation.configuration.dataset} /></dd></div><div><dt>K</dt><dd><SafeMetric value={evaluation.configuration.k} /></dd></div><div><dt>样本量</dt><dd><SafeMetric value={result.sample_count ?? result.samples ?? result.total ?? result.case_count} /></dd></div><div><dt>有目标题</dt><dd><SafeMetric value={result.answerable_count} /></dd></div></dl>
    <dl className="operations-counts"><div><dt>代码版本</dt><dd><SafeMetric value={evaluation.configuration.code_version ?? evaluation.configuration.code_revision} /></dd></div><div><dt>向量空间</dt><dd><SafeMetric value={evaluation.configuration.embedding_space} /></dd></div><div><dt>字符预算</dt><dd><SafeMetric value={evaluation.configuration.max_chars ?? evaluation.configuration.char_budget} /></dd></div></dl>
    {groups.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>固定评测按用途统计</caption><thead><tr><th scope="col">用途</th><th scope="col">题数</th><th scope="col">Recall@K</th><th scope="col">相关结果占比</th><th scope="col">最终覆盖</th></tr></thead><tbody>{groups.map((item, index) => <tr key={String(item.purpose ?? index)}><th scope="row">{label(String(item.purpose ?? 'unknown'))}</th><td><SafeMetric value={item.sample_count ?? item.samples ?? item.count} /></td><td>{percent(typeof item.recall === 'number' ? item.recall : typeof item.recall_at_k === 'number' ? item.recall_at_k : null)}</td><td>{percent(typeof item.precision === 'number' ? item.precision : typeof item.precision_at_k === 'number' ? item.precision_at_k : null)}</td><td>{percent(typeof item.final_coverage === 'number' ? item.final_coverage : typeof item.context_coverage === 'number' ? item.context_coverage : null)}</td></tr>)}</tbody></table></div> : <p className="field-hint">已保存评测报告，当前报告未提供按用途汇总。</p>}
    {cases.length > 0 && <details className="operations-failed-cases"><summary>查看未通过题（{cases.length}）</summary><ul>{cases.map((item, index) => <li key={String(item.id ?? index)}><strong>{String(item.id ?? `题目 ${index + 1}`)}</strong> · {typeof item.scenario === 'string' ? item.scenario : '未标注场景'}<p>用途：{label(String(item.purpose ?? 'unknown'))} · Recall@K：{percent(typeof item.recall_at_k === 'number' ? item.recall_at_k : null)} · 最终覆盖：{percent(typeof item.context_coverage === 'number' ? item.context_coverage : null)}</p></li>)}</ul></details>}
    <p className="field-hint">固定小样本用于比较同配置回归；无答案题单列，不以 100% 召回率参与均值。结果不代表所有真实请求，也不证明模型已采用召回内容。</p>
  </>;
}

export function OperationsDashboard() {
  const [draft, setDraft] = useState<Query>(initialQuery);
  const [query, setQuery] = useState<Query>(initialQuery);
  const [tab, setTab] = useState<'overview' | 'tasks' | 'calls' | 'rag'>('overview');
  const [data, setData] = useState<OperationsData | null>(null);
  const [error, setError] = useState('');
  const [denied, setDenied] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [automatic, setAutomatic] = useState(true);
  const [intervalSeconds, setIntervalSeconds] = useState(30);
  const [refresh, setRefresh] = useState(0);
  const [now, setNow] = useState(Date.now());
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => { heading.current?.focus(); }, []);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let active = true;
    const parameters = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) if (value !== '') parameters.set(key, String(value));
    async function load(signal: AbortSignal) {
      if (active) setRefreshing(true);
      try {
        const result = await request<OperationsData>(`/operations/dashboard?${parameters}`, undefined, undefined, { signal });
        if (active) {
          setData(result); setError(''); setDenied(false); setNow(Date.now());
          setIntervalSeconds(Math.max(5, result.refresh_seconds || 30));
        }
      } catch (cause) {
        if (!active || signal.aborted) return;
        if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) { setData(null); setDenied(true); }
        setError(cause instanceof Error ? cause.message : '暂时无法读取运行状态，请重试。');
      } finally { if (active) setRefreshing(false); }
    }
    const cancel = automatic ? pollWhileVisible(load, intervalSeconds * 1000) : (() => {
      const controller = new AbortController(); void load(controller.signal); return () => controller.abort();
    })();
    return () => { active = false; cancel(); };
  }, [query, refresh, automatic, intervalSeconds]);

  function apply(event: FormEvent) {
    event.preventDefault();
    setData(null); setError(''); setQuery({ ...draft, page: 1 });
  }
  function changePage(page: number) { setQuery(value => ({ ...value, page })); }
  const stale = !!data && (now - new Date(data.generated_at).getTime() > Math.max(60, data.refresh_seconds * 2) * 1000 || !!error);
  const more = data && (tab === 'tasks' ? data.pagination.has_more_tasks : tab === 'calls' ? data.pagination.has_more_calls : data.pagination.has_more_retrieval);

  return <main id="main" className="operations-page" tabIndex={-1}>
    <header className="operations-heading"><div><p className="eyebrow">维护者 · 只读运行记录</p><h1 ref={heading} tabIndex={-1}>运行仪表盘</h1><p className="page-subtitle">查看服务、任务、调用消耗与检索表现。</p></div><div className="operations-refresh"><button className="secondary" disabled={refreshing} onClick={() => setRefresh(value => value + 1)}>{refreshing ? '正在刷新…' : '刷新数据'}</button><label><input type="checkbox" checked={automatic} onChange={event => setAutomatic(event.target.checked)} />每 {intervalSeconds} 秒自动刷新</label></div></header>
    {error && <ErrorNotice message={error} />}
    {denied ? <section className="operations-panel"><h2>无法访问运行数据</h2><p>此入口仅向部署指定的维护账号开放。请确认当前登录账号。</p></section> : <>
      <form className="operations-filters" onSubmit={apply} aria-label="运行数据筛选">
        <div className="field"><label htmlFor="operations-range">统计范围</label><select id="operations-range" value={draft.range} onChange={event => setDraft(value => ({ ...value, range: event.target.value }))}><option value="today">今日</option><option value="7d">近 7 日</option><option value="30d">近 30 日</option><option value="custom">自定义日期</option></select></div>
        {draft.range === 'custom' && <><div className="field"><label htmlFor="operations-start">开始日期</label><input id="operations-start" type="date" required value={draft.start} max={draft.end || undefined} onChange={event => setDraft(value => ({ ...value, start: event.target.value }))} /></div><div className="field"><label htmlFor="operations-end">结束日期（含当日）</label><input id="operations-end" type="date" required min={draft.start || undefined} value={draft.end} onChange={event => setDraft(value => ({ ...value, end: event.target.value }))} /></div></>}
        {(['purpose', 'model', 'task_kind', 'state'] as const).map(key => {
          const source = key === 'purpose' ? data?.filters.purposes : key === 'model' ? data?.filters.models : key === 'task_kind' ? data?.filters.task_kinds : data?.filters.states;
          const options = [...new Set([...(source ?? []).filter(value => typeof value === 'string' && value.length > 0), ...(draft[key] ? [draft[key]] : [])])];
          const title = { purpose: '调用用途', model: '模型', task_kind: '任务类型', state: '调用状态' }[key];
          return <div className="field" key={key}><label htmlFor={`operations-${key}`}>{title}</label><select id={`operations-${key}`} value={draft[key]} onChange={event => setDraft(value => ({ ...value, [key]: event.target.value }))}><option value="">全部</option>{options.map(value => <option key={value} value={value}>{key === 'model' ? value : label(value)}</option>)}</select></div>;
        })}
        <button className="primary" disabled={refreshing}>应用筛选</button>
      </form>
      {!data ? <p className="operations-empty" role="status">{refreshing ? '正在读取运行记录…' : '尚未读取到运行记录，可刷新重试。'}</p> : <>
        <div className="operations-freshness" role="status"><p>{stale ? '数据已过期，当前保留上次成功结果。' : '数据已更新。'}<span>刷新时间：{time(data.generated_at, data.timezone)}</span></p><p>统计时区：{data.timezone} · {({ today: '今日', '7d': '近 7 日', '30d': '近 30 日', custom: '自定义日期' } as Record<string, string>)[data.range.label] ?? data.range.label}<span>采集起始：{time(data.coverage.started_at, data.timezone)}</span></p></div>
        <p className="operations-scope-note">组件、队列和索引覆盖展示当前全局快照；筛选分别作用于对应的历史任务、模型调用和检索记录。模型或用途筛选不会改变当前全局状态。</p><nav className="operations-tabs" aria-label="运行数据分区">{([['overview', '总览'], ['tasks', '任务'], ['calls', '调用与费用'], ['rag', 'RAG']] as const).map(([key, title]) => <button className="text-button" key={key} aria-current={tab === key ? 'page' : undefined} onClick={() => setTab(key)}>{title}</button>)}</nav>
        {tab === 'overview' && <>
          <dl className="operations-stats"><Stat title="可执行等待" value={number(data.queue.pending)} note={`最老等待 ${duration(data.queue.oldest_wait_seconds == null ? null : data.queue.oldest_wait_seconds * 1000)}`} /><Stat title="模型请求" value={number(data.calls.confirmed_sent)} note={`${number(data.calls.unknown_dispatch)} 次发送待确认`} /><Stat title="当前可计价合计" value={money(data.costs.known_total)} note={data.costs.incomplete ? `费用不完整 · ${number(data.costs.unknown_count)} 次无法计价` : '人民币 · 计价来源见费用明细'} /><Stat title="RAG 运行记录" value={number(data.rag.observations)} note="线上记录与固定评测分开" /></dl>
          <section className="operations-panel" aria-labelledby="operations-services"><h2 id="operations-services">组件状态</h2><div className="operations-components">{data.components.map(item => <article key={item.component}><div><h3>{item.label}</h3><span className="operations-state" data-state={item.status}>{label(item.status)}</span></div><p>最近观测：{time(item.observed_at, data.timezone)}</p>{item.component === 'napcat' && <><p>本机进程：{item.details.process_running === true ? '已启动' : item.details.process_running === false ? '未检测到' : '无法确认'}</p><p>管理页：{item.details.webui_reachable === true ? '可访问' : item.details.webui_reachable === false ? '不可达' : '无法确认'}</p>{typeof item.details.webui_url === 'string' && <a className="operations-console-link" href={typeof item.details.open_url === 'string' ? item.details.open_url : item.details.webui_url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">打开 NapCat 管理台<span className="sr-only">（新窗口）</span></a>}<p className="field-hint">在管理台中扫码登录、重新登录或重启。NapCat 已停止时，先用本机启动入口启动。</p></>}{typeof item.details.connected === 'boolean' && <p>连接：{item.details.connected ? '已连接' : '已断开'}</p>}{typeof item.details.logged_in === 'boolean' && <p>QQ 登录：{item.details.logged_in ? '已登录' : '未登录'}</p>}{typeof item.details.last_received_at === 'string' && <p>最近入站：{time(item.details.last_received_at, data.timezone)}</p>}{typeof item.details.last_sent_at === 'string' && <p>最近发送：{time(item.details.last_sent_at, data.timezone)}</p>}</article>)}</div><p className="field-hint">NapCat 进程、管理页、OneBot 网关与 QQ 登录分别观测；没有新消息不等于离线。</p></section>
          <section className="operations-panel" aria-labelledby="operations-alerts"><h2 id="operations-alerts">需要留意</h2>{data.alerts.length ? <ul className="operations-alerts">{data.alerts.map(item => <li key={item.code} data-severity={item.severity}><p>{item.message}</p><small>{time(item.observed_at, data.timezone)}</small></li>)}</ul> : <p className="operations-empty">当前没有已记录的异常提示。</p>}</section>
          <section className="operations-panel"><h2>记录覆盖</h2><p>{data.coverage.history_note}</p><dl className="operations-counts"><div><dt>调用记录</dt><dd>{number(data.coverage.total_calls)}</dd></div><div><dt>用途明确</dt><dd>{number(data.coverage.classified_calls)}</dd></div><div><dt>发送待确认</dt><dd>{number(data.coverage.unknown_dispatch)}</dd></div></dl></section>
        </>}
        {tab === 'tasks' && <>
          <section className="operations-panel"><h2>当前任务队列</h2><dl className="operations-stats"><Stat title="可执行等待" value={number(data.queue.pending)} /><Stat title="执行中" value={number(data.queue.running)} /><Stat title="静默合批等待" value={number(data.queue.debouncing)} /><Stat title="未来计划" value={number(data.queue.scheduled)} /></dl><p className="field-hint">当前快照。静默合批和未来计划不算异常积压，历史区间不会还原过去的队列。</p></section>
          <section className="operations-panel"><h2>期间任务结果</h2><Counts values={data.tasks.states} /><p>创建 {number(data.tasks.created)} · 已结束 {number(data.tasks.finished)} · 已结束任务成功率 {percent(data.tasks.success_rate)}</p><div className="operations-distributions"><DistributionView title="排队耗时" value={data.tasks.wait} /><DistributionView title="执行耗时" value={data.tasks.execution} /><DistributionView title="聊天端到端耗时" value={data.tasks.end_to_end} /></div><p className="field-hint">端到端耗时包含合批、排队、生成及最终送达，仅统计完整确认送达的聊天；部分或未知投递单列。</p><h3 className="operations-subheading">QQ 投递</h3><Counts values={[{ name: 'pending', count: data.tasks.delivery.pending }, { name: 'succeeded', count: data.tasks.delivery.succeeded }, { name: 'failed', count: data.tasks.delivery.failed }, { name: 'unknown', count: data.tasks.delivery.unknown }]} /><p className="field-hint">部分投递任务 {number(data.tasks.delivery.partial_tasks)} 项；模型成功、正式采用和实际送达分别记录。</p></section>
          <section className="operations-panel"><h2>脱敏任务记录</h2>{data.tasks.recent.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>任务明细，按创建时间归属统计范围</caption><thead><tr><th scope="col">任务</th><th scope="col">类型与状态</th><th scope="col">正式采用</th><th scope="col">创建时间</th><th scope="col">安全错误码</th></tr></thead><tbody>{data.tasks.recent.map(item => <tr key={item.id}><th scope="row" title={item.id}>{short(item.id)}</th><td>{label(item.kind)} · {label(item.status)}</td><td>{item.adopted ? '已采用' : '未采用'}</td><td>{time(item.created_at, data.timezone)}</td><td>{item.error ?? '—'}</td></tr>)}</tbody></table></div> : <p className="operations-empty">所选范围暂无任务记录。</p>}</section>
        </>}
        {tab === 'calls' && <>
          <section className="operations-panel"><h2>调用与计价</h2><dl className="operations-stats"><Stat title="当前可计价合计" value={money(data.costs.known_total)} note={data.costs.incomplete ? '总体费用不完整' : '人民币'} /><Stat title="供应商已确认" value={money(data.costs.provider)} /><Stat title="应用估算" value={money(data.costs.estimate)} /><Stat title="保守待确认" value={money(data.costs.conservative)} /></dl><p className="operations-cost-note">无法计价 {number(data.costs.unknown_count)} 次 · 在途预留 {money(data.costs.inflight_reserved)}（另列，不重复加总） · 每任务平均 {money(data.costs.per_task_average)}</p><p className="field-hint">{data.costs.ledger_note}</p><p className="field-hint">费用统计和提示不阻断文本生成。价格或用量未知不等于免费。</p><Counts values={[{ name: '确认发送', count: data.calls.confirmed_sent }, { name: '未发送', count: data.calls.not_sent }, { name: '发送待确认', count: data.calls.unknown_dispatch }, { name: '在途', count: data.calls.inflight }]} /><p>已结束调用成功率 {percent(data.calls.success_rate)} · 输入 token {number(data.calls.tokens.input)} / 输出 {number(data.calls.tokens.output)} · 用量未知 {number(data.calls.tokens.unknown)} 次</p><div className="operations-distributions"><DistributionView title="调用耗时" value={data.calls.latency} /></div></section>
          <section className="operations-panel"><h2>按用途统计</h2>{data.calls.by_purpose.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>用途按每次调用实际行为分类</caption><thead><tr><th scope="col">用途</th><th scope="col">次数</th><th scope="col">成功 / 失败 / 未知</th><th scope="col">输入 / 输出 token</th><th scope="col">计价合计 / 未计价次数</th><th scope="col">耗时中位数 / P95</th></tr></thead><tbody>{data.calls.by_purpose.map(item => <tr key={item.purpose}><th scope="row">{label(item.purpose)}</th><td>{number(item.count)}</td><td>{number(item.succeeded)} / {number(item.failed)} / {number(item.unknown)}</td><td>{number(item.input_tokens)} / {number(item.output_tokens)}<small>用量未知 {number(item.unknown_tokens)} 次</small></td><td>{money(item.known_amount)} / {number(item.unknown_costs)}</td><td>{duration(item.median_ms)} / {duration(item.p95_ms)}<small>{number(item.latency_samples)} 个耗时样本</small></td></tr>)}</tbody></table></div> : <p className="operations-empty">所选范围暂无调用记录。</p>}</section>
          <section className="operations-panel"><h2>每日调用与费用趋势</h2>{data.calls.daily.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>按发起时间及 {data.timezone} 自然日统计</caption><thead><tr><th scope="col">日期</th><th scope="col">调用次数</th><th scope="col">可计价合计</th><th scope="col">无法计价</th></tr></thead><tbody>{data.calls.daily.map(item => <tr key={item.day}><th scope="row">{item.day}</th><td>{number(item.count)}</td><td>{money(item.known_amount)}</td><td>{number(item.unknown_count)} 次</td></tr>)}</tbody></table></div> : <p className="operations-empty">暂无可展示的每日趋势。</p>}</section>
          <section className="operations-panel"><h2>脱敏调用记录</h2>{data.calls.recent.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>仅显示运维元数据，不包含请求或回复正文</caption><thead><tr><th scope="col">调用 / 任务</th><th scope="col">用途 / 模型</th><th scope="col">结果 / 发送</th><th scope="col">金额 / 来源</th><th scope="col">发起时间 / 错误码</th></tr></thead><tbody>{data.calls.recent.map(item => <tr key={item.id}><th scope="row" title={item.id}>{short(item.id)}<small title={item.task_id}>{short(item.task_id)}</small></th><td>{label(item.purpose)}<small>{item.model}</small></td><td>{label(item.state)}<small>{label(item.dispatch_state)}</small></td><td>{money(item.amount)}<small>{label(item.cost_source)}</small></td><td>{time(item.started_at, data.timezone)}<small>{item.error ?? '—'}</small></td></tr>)}</tbody></table></div> : <p className="operations-empty">暂无调用明细。</p>}</section>
        </>}
        {tab === 'rag' && <>
          <section className="operations-panel"><h2>线上检索状态</h2><Counts values={data.rag.states} /><p>空结果率 {data.rag.empty_result_rate === undefined ? '未知' : percent(data.rag.empty_result_rate)} · 实际执行 {number(data.rag.executed_count)} 次 · 空结果 {number(data.rag.empty_count)} 次</p><p className="field-hint">分母仅包含实际执行、空结果及降级检索；未启用、无资料和失败不纳入。</p><h3 className="operations-subheading">检索路径</h3><Counts values={data.rag.routes} /><h3 className="operations-subheading">个人资料召回</h3><dl className="operations-stats"><Stat title="候选条目" value={number(data.rag.counts.candidates)} /><Stat title="组装条目" value={number(data.rag.counts.assembled)} /><Stat title="最终装入条目" value={number(data.rag.counts.final_count)} /><Stat title="最终装入字符" value={number(data.rag.counts.final_chars)} /></dl><div className="operations-distributions"><DistributionView title="检索总耗时" value={data.rag.latency} />{data.rag.latencies?.embedding && <DistributionView title="查询向量耗时" value={data.rag.latencies.embedding} />}{data.rag.latencies?.retrieval && <DistributionView title="数据库检索耗时" value={data.rag.latencies.retrieval} />}{data.rag.latencies?.assembly && <DistributionView title="资料组装耗时" value={data.rag.latencies.assembly} />}</div><h3 className="operations-subheading">共有表情检索</h3>{data.rag.public_counts ? <dl className="operations-stats"><Stat title="共有候选" value={number(data.rag.public_counts.candidates)} /><Stat title="共有组装条目" value={number(data.rag.public_counts.assembled)} /><Stat title="共有最终装入" value={number(data.rag.public_counts.final_count)} /><Stat title="共有装入字符" value={number(data.rag.public_counts.final_chars)} /></dl> : <p className="operations-empty">共有表情检索尚无分项记录。</p>}<h3 className="operations-subheading">省略原因</h3><Counts values={data.rag.omitted} empty="暂无已记录的省略原因。" /><p className="field-hint">候选与最终上下文分开；近期兜底成功不等于语义召回成功，引用记录不等于模型实际采用。</p></section>
          <section className="operations-panel"><h2>有效索引覆盖</h2>{data.rag.coverage.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>当前有效且允许对应用途的资料，当前快照</caption><thead><tr><th scope="col">用途</th><th scope="col">完整 / 应索引</th><th scope="col">覆盖率</th><th scope="col">过期 / 部分 / 未建 / 失败</th></tr></thead><tbody>{data.rag.coverage.map(item => <tr key={item.purpose}><th scope="row">{label(item.purpose)}</th><td>{number(item.complete)} / {number(item.total)}</td><td>{percent(item.total ? item.complete / item.total : null)}</td><td>{number(item.stale)} / {number(item.partial)} / {number(item.missing)} / {number(item.failed ?? 0)}</td></tr>)}</tbody></table></div> : <p className="operations-empty">尚无有效索引覆盖记录。</p>}</section>
          <section className="operations-panel"><h2>固定评测召回率</h2><EvaluationView evaluation={data.rag.evaluation} timezone={data.timezone} /></section>
          <section className="operations-panel"><h2>脱敏检索记录</h2>{data.rag.recent.length ? <div className="operations-table-wrap" tabIndex={0} aria-label="可横向滚动的数据表"><table><caption>不保存检索问题和资料原文</caption><thead><tr><th scope="col">记录</th><th scope="col">用途</th><th scope="col">状态 / 路径</th><th scope="col">候选 / 组装 / 最终</th><th scope="col">时间</th></tr></thead><tbody>{data.rag.recent.map(item => <tr key={item.id}><th scope="row" title={item.id}>{short(item.id)}</th><td>{label(item.purpose)}</td><td><SafeMetric value={item.metrics.state} /> / <SafeMetric value={item.metrics.route} /></td><td><SafeMetric value={item.metrics.candidates} /> / <SafeMetric value={item.metrics.assembled} /> / <SafeMetric value={item.metrics.final_count} /></td><td>{time(item.created_at, data.timezone)}</td></tr>)}</tbody></table></div> : <p className="operations-empty">所选范围暂无检索运行记录。</p>}</section>
        </>}
        {tab !== 'overview' && <nav className="operations-pagination" aria-label="运行记录分页"><button className="secondary" disabled={data.pagination.page <= 1 || refreshing} onClick={() => changePage(Math.max(1, data.pagination.page - 1))}>上一页</button><p>明细第 {data.pagination.page} 页 · 每页最多 {data.pagination.page_size} 条</p><button className="secondary" disabled={!more || refreshing} onClick={() => changePage(data.pagination.page + 1)}>下一页</button></nav>}
      </>}
    </>}
  </main>;
}
