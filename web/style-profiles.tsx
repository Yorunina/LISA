import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { abandonStyleCorpusOnClose, request, styleFile } from './api';
import type { StyleApplications, StyleKind, StyleLibrary, StyleProfile, StyleReport, StyleVersion, WritingStyle } from './api';
import { ErrorNotice } from './ui';
import { GroupRecords } from './group-records';

type Segment = { id: string; start: number; end: number; chars: number; speaker?: string | null };
export type Corpus = {
  id: string; kind: StyleKind; state: string; cleanup_state: string; task_id?: string | null;
  preview: { files: { name: string; chars: number; encoding: string; segments: number; unrecognized_lines?: number; sections: Segment[] }[]; participants: { id: string; label: string; messages: number }[]; warnings: string[]; total_chars: number; group_sampling?: Record<string, unknown> };
};
type StyleResult = { kind: StyleKind; prompt: string; summary: string; report: StyleReport };
type StyleTask = { id: string; kind: string; style_kind?: StyleKind; status: string; error?: string | null; result?: StyleResult | { text: string; isolated: boolean } | null; cleanup_state?: string; can_cancel?: boolean; corpus_id?: string | null };
type Draft = { name: string; prompt: string; summary: string; report: StyleReport };
type Project = { id: string; title: string };
export type StyleProfilesHandle = { requestLeave: (action: () => void) => void };
const blank = (): Draft => ({ name: '', prompt: '', summary: '', report: {} });
const kindName = (kind: StyleKind) => kind === 'personality' ? '交流性格' : '写作风格';
const activeTask = (task: StyleTask | null) => !!task && ['queued', 'pending', 'running', 'cancel_requested'].includes(task.status);
const statuses: Record<string, string> = { queued: '等待执行', pending: '等待执行', running: '正在分析', cancel_requested: '正在取消', cancelled: '已取消', succeeded: '已完成', completed: '已完成', failed: '执行失败', outcome_unknown: '结果尚未确认', blocked: '暂时暂停' };
const taskErrors: Record<string, string> = { budget_exhausted: '今日或本任务额度不足，已停止后续调用。原文清理后重试需要重新提供材料。', model_failed: '模型调用未完成，请稍后重试。', model_unknown: '本次调用结果尚未确认，已停止后续调用。', invalid_style_result: '结果未通过检查。', user_cancelled: '你已取消本次任务。', input_too_large: '所选范围超过当前配置上限，请缩小范围。' };
Object.assign(taskErrors, { model_busy: '模型服务暂时繁忙，请稍后重试。', corpus_unavailable: '材料已过期、已清理或不可用，请重新上传。', source_copy_detected: '候选可能包含来源原句，未通过检查，未保存或应用。', source_identifier_detected: '候选可能包含来源人物身份，未通过检查，未保存或应用。' });
const validationNames: Record<string, string> = { not_executed: '尚未执行独立验证', model_reviewed: '已进行模型评审', edited_not_evaluated: '内容已编辑，当前版本尚未验证', not_performed: '尚未进行', generated_and_model_reviewed: '已生成留出样例并经模型评审' };
function cleanLabel(state?: string, group = false) { return state === 'cleaned' ? group ? '本次蒸馏原始材料已清理；群采集记录仍按 30 天规则管理，可重新检索提取。' : '原始材料已清理；重新分析需再次上传。' : state === 'failed' ? '原文清理尚未完成，系统将继续清理。' : '原始材料清理状态尚未确认。'; }
function savedDraft(profile: StyleProfile, version: StyleVersion): Draft { return { name: profile.name, prompt: version.prompt, summary: version.summary, report: version.report }; }

function Report({ report }: { report: StyleReport }) {
  const coverage = report.coverage as { group_source?: { total_target_messages?: number; matched_target_messages?: number; sampled_target_messages?: number; selected_messages?: number }; sampling?: Record<string, number>; total_segments?: number; selected_segments?: number; analyzed_segments?: number; selected_chars?: number; analyzed_chars?: number; files?: { name: string; selected_segments: number; analyzed_segments: number }[] } | undefined;
  const sampling = coverage?.sampling;
  const groupSource = coverage?.group_source;
  const validation = report.validation as { status?: string; development?: unknown; holdout?: unknown; user_evaluation?: string } | undefined;
  const evidence = report.evidence as { feature: string; condition?: string; source_ids?: string[]; counterexample?: string }[] | undefined;
  const limitations = report.limitations as string[] | undefined;
  const model = report.model as { name?: string } | undefined;
  return <details className="style-report"><summary>覆盖、证据与验证说明</summary>
    {coverage && <><p>所选 {coverage.selected_segments ?? 0} / 共 {coverage.total_segments ?? 0} 个片段；实际分析 {coverage.analyzed_segments ?? 0} 个片段、{coverage.analyzed_chars ?? 0} / 所选 {coverage.selected_chars ?? 0} 字。</p>{coverage.files?.map((file, index) => <p key={index}>{file.name}：分析 {file.analyzed_segments} / 所选 {file.selected_segments} 个片段</p>)}</>}
    {groupSource && <p>群来源覆盖：30 天可用记录中，当前检索命中 {groupSource.total_target_messages ?? groupSource.matched_target_messages ?? 0} 条目标消息，其中 {groupSource.sampled_target_messages ?? 0} 条匹配消息进入临时快照；快照含邻近上下文共 {groupSource.selected_messages ?? 0} 条。最终实际分析 {sampling?.analyzed_target_messages ?? 0} 条目标发言，可能包含邻近语境中同一人的其他发言；未入选消息不视为已分析。</p>}
    {sampling && <p>{groupSource ? '本次临时快照目标消息' : '目标消息'}：分析 {sampling.analyzed_target_messages ?? 0} / {groupSource ? '快照内' : '共'} {sampling.total_target_messages ?? 0} 条；邻近上下文 {sampling.context_messages ?? 0} 条，取样 {sampling.sampled_windows ?? 0} 个窗口，其余 {sampling.excluded_messages ?? 0} 条未进入模型。{sampling.oversized_targets ? `其中 ${sampling.oversized_targets} 条目标消息超过单窗口预算，未截断使用。` : ''}</p>}
    {evidence?.map((item, index) => <div className="style-evidence" key={index}><strong>{item.feature}</strong>{item.condition && <p>适用条件：{item.condition}</p>}{item.counterexample && <p>反例与边界：{item.counterexample}</p>}{item.source_ids?.length ? <p className="field-hint">来源定位：{item.source_ids.join('、')}</p> : null}</div>)}
    {limitations?.length ? <ul>{limitations.map((item, index) => <li key={index}>{item}</li>)}</ul> : null}
    <p>验证：{validationNames[validation?.status ?? ''] || '未提供可确认的独立验证记录'}。用户评价：{validationNames[validation?.user_evaluation ?? ''] || '尚未进行'}。模型评审不能保证自然度或相似度。</p>
    <p className="field-hint">{report.source_state === 'cleaned' ? groupSource ? '本次蒸馏临时副本已清理；群采集记录仍按 30 天规则管理。结果仅保留概括与来源定位。' : '原始材料已清理，仅保留概括与来源定位。' : '已保存结果不提供原文展开；重新分析需要再次提供材料。'}</p>
    <p className="field-hint">方法：{String(report.method_version ?? '未提供')} · 模型：{model?.name || '见任务模型记录'}</p>
  </details>;
}

export const StyleProfiles = forwardRef<StyleProfilesHandle, { onBack: () => void; backLabel?: string; initialKind?: StyleKind; initialProjectId?: string | null; onApplied: () => void; initialized: boolean }>(function StyleProfiles({ onBack, backLabel = '返回账号设置', initialKind = 'personality', initialProjectId, onApplied, initialized }, ref) {
  const [library, setLibrary] = useState<StyleLibrary>({ profiles: [], applications: { personality: null, writing: null } });
  const [projects, setProjects] = useState<Project[]>([]);
  const [kind, setKind] = useState<StyleKind>(initialProjectId ? 'writing' : initialKind);
  const [profileId, setProfileId] = useState<string | null>(null);
  const [versionId, setVersionId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(blank);
  const [baseline, setBaseline] = useState(JSON.stringify(blank()));
  const [sourceTask, setSourceTask] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [filesChanged, setFilesChanged] = useState(false);
  const [source, setSource] = useState<'upload' | 'group'>('upload');
  const [corpus, setCorpus] = useState<Corpus | null>(null);
  const [speakers, setSpeakers] = useState<string[]>([]);
  const [selection, setSelection] = useState<string[]>([]);
  const [task, setTask] = useState<StyleTask | null>(null);
  const [trial, setTrial] = useState<StyleTask | null>(null);
  const [trialInput, setTrialInput] = useState('');
  const [projectId, setProjectId] = useState(initialProjectId ?? '');
  const [writingStyle, setWritingStyle] = useState<WritingStyle | null>(null);
  const [writingError, setWritingError] = useState('');
  const [writingRetry, setWritingRetry] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pollError, setPollError] = useState('');
  const [notice, setNotice] = useState('');
  const [leaving, setLeaving] = useState(false);
  const pending = useRef<(() => void) | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const receivedTask = useRef<string | null>(null);
  const finishedTask = useRef<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const currentCorpus = useRef<{ id: string; running: boolean } | null>(null);
  const latestDraft = useRef(draft);
  latestDraft.current = draft;
  const dirty = JSON.stringify(draft) !== baseline;
  const selectedProfile = library.profiles.find(item => item.id === profileId);
  const selectedVersion = selectedProfile?.versions.find(item => item.id === versionId);
  const appliedVersion = library.applications[kind];
  currentCorpus.current = corpus ? { id: corpus.id, running: activeTask(task) || !!corpus.task_id } : null;

  function failure(cause: unknown) { setError(cause instanceof Error ? cause.message : '暂时无法完成操作，请重试。'); }
  function editPrompt(prompt: string) {
    const report = Object.keys(draft.report).length ? { ...draft.report, validation: { ...(draft.report.validation as object ?? {}), status: 'edited_not_evaluated', user_evaluation: 'not_performed' } } : draft.report;
    setDraft({ ...draft, prompt, report }); if (!activeTask(trial)) setTrial(null);
  }
  async function refreshLibrary() { const state = await request<StyleLibrary>('/style/profiles'); setLibrary(state); return state; }
  async function load(restoreTask = false) {
    setLoading(true); setError('');
    try {
      const saved = await refreshLibrary(); if (initialized) setProjects(await request<Project[]>('/projects'));
      const tasks = await request<StyleTask[]>('/style/tasks');
      if (restoreTask) { const restoredTrial = tasks.find(item => item.kind === 'style_trial' && activeTask(item)) ?? null; setTrial(restoredTrial); if (restoredTrial?.style_kind) setKind(restoredTrial.style_kind); }
      const savedTasks = new Set(saved.profiles.flatMap(profile => profile.versions.map(version => version.source_task_id)));
      const recent = tasks.find(item => item.kind === 'style_distill' && activeTask(item)) ?? tasks.find(item => item.kind === 'style_distill' && item.result && !savedTasks.has(item.id));
      if (recent && restoreTask) { setTask(recent); if (recent.corpus_id) { const restored = await request<Corpus>(`/style/corpora/${recent.corpus_id}`); setCorpus(restored); setKind(restored.kind); if (restored.preview.group_sampling) setSource('group'); } }
    }
    catch (cause) { failure(cause); } finally { setLoading(false); }
  }
  useEffect(() => { void load(true); heading.current?.focus(); return () => { const abandoned = currentCorpus.current; if (abandoned && !abandoned.running) void request(`/style/corpora/${abandoned.id}`, undefined, 'DELETE').catch(() => {}); }; }, []);
  useEffect(() => {
    const close = () => { const abandoned = currentCorpus.current; if (abandoned && !abandoned.running) abandonStyleCorpusOnClose(abandoned.id); };
    window.addEventListener('pagehide', close); return () => window.removeEventListener('pagehide', close);
  }, []);
  useEffect(() => {
    if (!projectId) { setWritingStyle(null); return; }
    let active = true; setWritingStyle(null); setWritingError('');
    void request<WritingStyle>(`/projects/${projectId}/writing-style`).then(value => { if (active) setWritingStyle(value); }).catch(cause => { if (active) setWritingError(cause instanceof Error ? cause.message : '暂时无法读取此作品的风格。'); });
    return () => { active = false; };
  }, [projectId, writingRetry]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!busy && !leaving) form.current?.requestSubmit(); } };
    window.addEventListener('keydown', shortcut); return () => window.removeEventListener('keydown', shortcut);
  }, [busy, leaving]);
  useEffect(() => { if (leaving) dialog.current?.showModal(); else dialog.current?.close(); }, [leaving]);
  useEffect(() => {
    const checkingCleanup = !!task && task.cleanup_state === 'pending';
    if (!activeTask(task) && !checkingCleanup && !activeTask(trial)) return;
    let active = true; let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const results = await Promise.all([activeTask(task) || checkingCleanup ? request<StyleTask>(`/style/tasks/${task!.id}`) : Promise.resolve(task), activeTask(trial) ? request<StyleTask>(`/style/tasks/${trial!.id}`) : Promise.resolve(trial)]);
        if (active) { setTask(results[0]); setTrial(results[1]); setPollError(''); }
      } catch { if (active) setPollError('暂时无法更新任务状态，稍后自动重试；不会将未知结果显示为完成。'); }
      finally { if (active) timer = setTimeout(poll, 1500); }
    }
    timer = setTimeout(poll, 500); return () => { active = false; clearTimeout(timer); };
  }, [task?.id, task?.status, task?.cleanup_state, trial?.id, trial?.status]);
  useEffect(() => {
    if (!task || activeTask(task)) return;
    if (finishedTask.current !== task.id) { finishedTask.current = task.id; setFiles([]); setFilesChanged(false); if (fileInput.current) fileInput.current.value = ''; }
    if (task.result && 'prompt' in task.result && receivedTask.current !== task.id) {
      receivedTask.current = task.id;
      const result = task.result;
      setDraft(previous => ({ name: previous.name, prompt: result.prompt, summary: result.summary, report: result.report }));
      setProfileId(null); setVersionId(null); setSourceTask(task.id); setNotice('已生成候选，尚未保存或应用。');
    }
  }, [task]);

  async function abandon() {
    if (corpus && !activeTask(task) && !corpus.task_id && corpus.cleanup_state !== 'cleaned') await request(`/style/corpora/${corpus.id}`, undefined, 'DELETE');
    setCorpus(null); currentCorpus.current = null; setFiles([]); setFilesChanged(false); if (fileInput.current) fileInput.current.value = '';
  }
  async function leaveNow(action: () => void) {
    setBusy(true); setError('');
    try { await abandon(); action(); } catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  function requestLeave(action: () => void) { if (busy) return; if (dirty) { pending.current = action; setLeaving(true); } else void leaveNow(action); }
  useImperativeHandle(ref, () => ({ requestLeave }));
  function open(profile: StyleProfile, version: StyleVersion) {
    setKind(profile.kind); setProfileId(profile.id); setVersionId(version.id); const next = savedDraft(profile, version); setDraft(next); setBaseline(JSON.stringify(next)); setSourceTask(null); setTrial(null); setNotice(''); setError('');
  }
  function newDraft(nextKind: StyleKind) { setKind(nextKind); setSource('upload'); setProfileId(null); setVersionId(null); setDraft(blank()); setBaseline(JSON.stringify(blank())); setSourceTask(null); setTask(null); setTrial(null); receivedTask.current = null; setNotice(''); }
  async function upload() {
    if (!files.length) { setError('请先选择要分析的文件。'); return; }
    setBusy(true); setError(''); setNotice(''); setFilesChanged(true);
    try {
      const previousFiles = files;
      if (corpus) await request(`/style/corpora/${corpus.id}`, undefined, 'DELETE');
      const uploaded = await request<Corpus>('/style/corpora', { kind, files: await Promise.all(previousFiles.map(styleFile)) });
      setCorpus(uploaded); setFilesChanged(false); setSpeakers([]); setSelection([]); setTask(null); receivedTask.current = null;
    } catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function start() {
    if (filesChanged) { setError('所选文件已更换，请先重新上传并预览。上次解析不能用于本次分析。'); return; }
    if (!corpus || kind === 'personality' && !speakers.length) { setError('请明确选择目标发言者，其他人仅作为上下文。'); return; }
    if (draft.prompt || draft.summary) { setError('候选中已有编辑内容，请先保存或通过“新建方案”放弃修改，再上传分析，避免覆盖输入。'); return; }
    setBusy(true); setError('');
    try { const created = await request<StyleTask>('/style/tasks', { corpus_id: corpus.id, target_speakers: speakers, selection }); setTask(created); setCorpus({ ...corpus, task_id: created.id }); }
    catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function fromGroup(nextCorpus: Corpus, targets: string[]) {
    setBusy(true); setError(''); setNotice('');
    try {
      if (latestDraft.current.prompt || latestDraft.current.summary) {
        await request(`/style/corpora/${nextCorpus.id}`, undefined, 'DELETE');
        setError('候选已有编辑内容，未启动新的分析。请先保存或新建方案后再提炼群消息。');
        return;
      }
      await abandon();
      setCorpus(nextCorpus); setSpeakers(targets); setSelection([]); setFilesChanged(false); setTask(null); receivedTask.current = null;
      const created = await request<StyleTask>('/style/tasks', { corpus_id: nextCorpus.id, target_speakers: targets, selection: [] });
      setTask(created); setCorpus({ ...nextCorpus, task_id: created.id });
    } catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function save(): Promise<boolean> {
    if (!form.current?.reportValidity()) return false;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = selectedProfile
        ? await request<StyleProfile>(`/style/profiles/${selectedProfile.id}/versions`, { prompt: draft.prompt, summary: draft.summary, report: draft.report })
        : await request<StyleProfile>('/style/profiles', { kind, ...draft, ...(sourceTask ? { source_task_id: sourceTask } : {}) });
      const latest = saved.versions.reduce((left, right) => left.version > right.version ? left : right);
      await refreshLibrary(); open(saved, latest); setNotice('方案已保存，尚未自动应用。'); return true;
    } catch (cause) { failure(cause); return false; } finally { setBusy(false); }
  }
  async function resolveLeave(shouldSave: boolean) { setLeaving(false); if (shouldSave && !(await save())) return; const action = pending.current; pending.current = null; if (action) void leaveNow(action); }
  async function apply(toProject: boolean) {
    if (!selectedVersion || dirty) return;
    setBusy(true); setError('');
    try {
      if (toProject) setWritingStyle(await request<WritingStyle>(`/projects/${projectId}/writing-style`, { version_id: selectedVersion.id }, 'PUT'));
      else {
        const applications = await request<StyleApplications>(`/style/applications/${kind}`, { version_id: selectedVersion.id }, 'PUT');
        setLibrary(value => ({ ...value, applications })); onApplied();
        if (kind === 'writing' && projectId) { setWritingStyle(null); setWritingRetry(value => value + 1); }
      }
      setNotice('已明确应用此版本，从后续任务生效。已有经历与正文保留。');
    } catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function followDefault() {
    setBusy(true); setError('');
    try { setWritingStyle(await request<WritingStyle>(`/projects/${projectId}/writing-style`, { version_id: null }, 'PUT')); setNotice('此作品已恢复跟随 LISA 默认写作风格。'); }
    catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function restore() {
    if (!selectedProfile || !selectedVersion) return;
    setBusy(true); setError('');
    try { const saved = await request<StyleProfile>(`/style/profiles/${selectedProfile.id}/restore`, { version_id: selectedVersion.id }); await refreshLibrary(); open(saved, saved.versions.reduce((left, right) => left.version > right.version ? left : right)); setNotice('已恢复为新保存版本，当前应用版本保持原样。'); }
    catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function tryStyle() {
    if (!draft.prompt.trim() || !trialInput.trim()) return;
    setBusy(true); setError('');
    try { setTrial(await request<StyleTask>('/style/trials', { kind, prompt: draft.prompt, input: trialInput })); }
    catch (cause) { failure(cause); } finally { setBusy(false); }
  }
  async function cancel(target: StyleTask) {
    setBusy(true); setError('');
    try { const next = await request<StyleTask>(`/style/tasks/${target.id}/cancel`, {}); if (target.id === task?.id) setTask(next); else setTrial(next); }
    catch (cause) { failure(cause); } finally { setBusy(false); }
  }

  return <main id="main" className="study-page style-page" tabIndex={-1}>
    <div className="panel-toolbar"><div><p className="eyebrow">把表达留在书房</p><h1 ref={heading} tabIndex={-1}>性格与写作风格</h1></div><button className="text-button" onClick={onBack} disabled={busy}>{backLabel}</button></div>
    <p className="page-subtitle">QQ 记录提炼交流性格，作品提炼写作风格。保存多个方案，再选择想使用的版本。</p>
    {error && <ErrorNotice message={error} />}{notice && <p className="style-notice" role="status">{notice}</p>}{pollError && <p role="status" className="work-warning">{pollError}</p>}
    {loading ? <p role="status">正在读取风格方案…</p> : <>
      <div className="style-kind-choice" aria-label="方案类型">{(['personality', 'writing'] as const).map(value => <button className="secondary" key={value} disabled={busy || activeTask(task) || activeTask(trial)} aria-pressed={kind === value} onClick={() => { if (kind !== value) requestLeave(() => newDraft(value)); }}>{kindName(value)}</button>)}</div>
      <div className="style-layout"><aside className="style-shelf" aria-label="已保存的方案"><h2>方案书签</h2><button className="text-button" disabled={busy || activeTask(task) || activeTask(trial)} onClick={() => requestLeave(() => newDraft(kind))}>新建{kindName(kind)}方案</button>
        {!library.profiles.some(item => item.kind === kind) && <p className="field-hint">还没有保存的{kindName(kind)}方案。</p>}
        {library.profiles.filter(item => item.kind === kind).map(profile => <button className="style-bookmark" key={profile.id} aria-current={profile.id === profileId ? 'page' : undefined} disabled={busy || activeTask(task) || activeTask(trial)} onClick={() => requestLeave(() => open(profile, profile.versions.reduce((left, right) => left.version > right.version ? left : right)))}><strong>{profile.name}</strong><span>已保存 · {profile.versions.length} 个版本</span>{profile.versions.some(version => version.id === appliedVersion) && <span>正在用于 LISA · 版本 {profile.versions.find(version => version.id === appliedVersion)?.version}</span>}</button>)}
        <p className="field-hint">{appliedVersion ? '当前 LISA 使用已明确应用的版本。保存和恢复不会自动切换。' : `LISA 尚未应用蒸馏${kindName(kind)}，继续使用已有配置。`}</p>
        <button className="text-button" disabled={busy} onClick={() => void load()}>刷新方案列表</button>
      </aside><div className="style-content">
        {!profileId && <section className="setup-panel style-upload" aria-label="上传语料与解析预览"><h2>{kind === 'personality' ? '从 QQ 记录提炼性格' : '从作品提炼写作风格'}</h2>{kind === 'personality' && <div className="style-kind-choice group-source-choice" aria-label="聊天语料来源"><button className="secondary" disabled={busy || activeTask(task) || activeTask(trial)} aria-pressed={source === 'upload'} onClick={() => { if (source !== 'upload') void leaveNow(() => setSource('upload')); }}>上传 TXT</button><button className="secondary" disabled={busy || activeTask(task) || activeTask(trial)} aria-pressed={source === 'group'} onClick={() => { if (source !== 'group') void leaveNow(() => setSource('group')); }}>群消息</button></div>}{kind === 'personality' && source === 'group' ? <GroupRecords disabled={busy || activeTask(task) || activeTask(trial)} canDistill={!draft.prompt && !draft.summary} onCorpus={fromGroup} /> : <><p className="form-copy">{kind === 'personality' ? '上传可读的 QQ TXT，选择目标发言者。系统只抽取目标消息及同会话邻近消息，按时段有限取样；无关群聊不进入模型。' : '上传一份或多份 TXT、Markdown，支持长篇。请确认分析范围；作品角色性格提取留待以后。'}</p><p className="style-source-note">材料将交给平台配置的模型分析，计入统一额度。任务成功、最终失败或取消后自动清理原文，放弃未分析上传也会清理。仅保留风格结果，不导入聊天、记忆或作品剧情。</p>
          <div className="field"><label htmlFor="style-files">选择{kind === 'personality' ? ' QQ TXT' : ' TXT / Markdown'}文件（可多选）</label><input ref={fileInput} id="style-files" type="file" accept={kind === 'personality' ? '.txt' : '.txt,.md,.markdown'} multiple disabled={busy || activeTask(task)} onChange={event => { setFiles(Array.from(event.target.files ?? [])); setFilesChanged(true); setError(''); }} /></div>
          <div className="button-row"><button className="secondary" disabled={busy || activeTask(task) || !files.length} onClick={() => void upload()}>上传并预览解析</button>{corpus && !activeTask(task) && !corpus.task_id && <button className="text-button" disabled={busy} onClick={() => void leaveNow(() => setNotice('已放弃上传并清理原文。'))}>放弃本次上传</button>}</div></>}
          {corpus && <div className="style-preview"><h3>解析与分析范围</h3>{filesChanged && <p className="work-warning" role="status">上次解析已失效。当前所选文件尚未成功上传，请重新上传并预览；不能开始分析。</p>}<p>识别 {corpus.preview.total_chars} 字。以下为文件统计与来源定位，不保留原文预览。</p><ul>{corpus.preview.files.map((file, index) => <li key={index}><strong>{file.name}</strong> · {file.chars} 字 · {file.encoding} · {file.segments} 个片段{file.unrecognized_lines ? ` · ${file.unrecognized_lines} 行未识别` : ''}</li>)}</ul>{corpus.preview.warnings?.map((warning, index) => <p className="work-warning" key={index}>{warning}</p>)}
            {!corpus.task_id && <fieldset disabled={busy}><legend>{kind === 'personality' ? '明确目标发言者' : '作品选区'}</legend>{kind === 'personality' ? <><p className="field-hint">同名或改昵称不自动合并；仅在你确认属于同一人的情况下多选。先由程序索引，再提取邻近窗口；超出预算时分层取样，不分析全量群聊。</p>{corpus.preview.participants.map(speaker => <label className="contact-choice compact-choice" key={speaker.id}><input type="checkbox" checked={speakers.includes(speaker.id)} onChange={event => setSpeakers(event.target.checked ? [...speakers, speaker.id] : speakers.filter(id => id !== speaker.id))} /><span>{speaker.label} · {speaker.messages} 条消息 · 文件 {Number(speaker.id.split(':')[0]) + 1}（{corpus.preview.files[Number(speaker.id.split(':')[0])]?.name ?? '来源文件'}）</span></label>)}</> : <><p className="field-hint">不勾选时分析全部已识别片段；勾选后仅分析选区。混合作者或译文会影响结果。</p>{corpus.preview.files.flatMap(file => file.sections.map((segment, index) => ({ ...segment, file_name: file.name, number: index + 1 }))).map(segment => <label className="contact-choice compact-choice" key={segment.id}><input type="checkbox" checked={selection.includes(segment.id)} onChange={event => setSelection(event.target.checked ? [...selection, segment.id] : selection.filter(id => id !== segment.id))} /><span>{segment.file_name ? `${segment.file_name} · ` : ''}片段 {segment.number}（字符 {segment.start + 1}–{segment.end}）{segment.chars ? ` · ${segment.chars} 字` : ''}</span></label>)}</>}
              <button className="primary" disabled={filesChanged || kind === 'personality' && !speakers.length} onClick={() => void start()}>开始有限蒸馏</button></fieldset>}
          </div>}
          {task && <div className="style-task" aria-label="蒸馏任务状态"><strong>{statuses[task.status] ?? task.status}</strong>{task.error && <p>{taskErrors[task.error] ?? '本次蒸馏未能完成，请检查范围或重新上传后再试。'}</p>}<p>{activeTask(task) ? '正在处理所选材料；取消后停止后续调用，迟到结果不会写入。离开页面后任务继续。' : cleanLabel(task.cleanup_state, source === 'group' || !!corpus?.preview.group_sampling)}</p>{activeTask(task) && <button className="text-button" disabled={busy || task.status === 'cancel_requested'} onClick={() => void cancel(task)}>取消蒸馏任务</button>}{!activeTask(task) && task.cleanup_state !== 'cleaned' && <button className="text-button" disabled={busy} onClick={() => { void request<StyleTask>(`/style/tasks/${task.id}`).then(setTask).catch(failure); }}>核对清理状态</button>}</div>}
        </section>}
        <section className="setup-panel style-editor" aria-label="风格方案编辑"><div className="panel-toolbar"><h2>{selectedProfile ? selectedProfile.name : '编辑候选方案'}</h2><span className="work-tag">{selectedVersion ? selectedVersion.id === appliedVersion ? `正在使用 · 版本 ${selectedVersion.version}` : `已保存 · 版本 ${selectedVersion.version}` : draft.prompt ? '已生成 · 待保存' : '尚未生成'}</span></div>
          {selectedProfile && <div className="field"><label htmlFor="style-version">查看已保存版本</label><select id="style-version" value={versionId ?? ''} disabled={busy} onChange={event => { const version = selectedProfile.versions.find(item => item.id === event.target.value)!; requestLeave(() => open(selectedProfile, version)); }}>{selectedProfile.versions.map(version => <option key={version.id} value={version.id}>版本 {version.version}{version.id === appliedVersion ? ' · LISA 正在使用' : ''} · {new Date(version.created_at).toLocaleString('zh-CN')}</option>)}</select></div>}
          <form ref={form} onSubmit={event => { event.preventDefault(); void save(); }}><fieldset disabled={busy || activeTask(task)}><div className="field"><label htmlFor="style-name">方案名称</label><input id="style-name" required maxLength={80} readOnly={!!selectedProfile} value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} /></div><div className="field"><label htmlFor="style-summary">精简画像与适用说明</label><textarea id="style-summary" rows={3} maxLength={2400} value={draft.summary} onChange={event => setDraft({ ...draft, summary: event.target.value })} /></div><div className="field"><label htmlFor="style-prompt">{kind === 'personality' ? '交流性格 prompt' : '写作风格指引'}</label><textarea id="style-prompt" required rows={10} maxLength={12000} value={draft.prompt} onChange={event => editPrompt(event.target.value)} /><p className="field-hint">只保留可迁移的表达方式与适用条件，不将样本身份、关系经历或原作剧情写为 LISA 事实。</p></div></fieldset><div className="save-bar"><button className="primary" disabled={busy || activeTask(task)}>{profileId ? '保存为新版本' : '保存方案'}</button><p role="status" aria-label="风格保存状态">{dirty ? '有未保存修改 · Ctrl+S 保存' : selectedVersion ? `已保存版本 ${selectedVersion.version}；应用需单独操作` : '生成、保存与应用是不同动作'}</p></div></form>
          {!!Object.keys(draft.report).length && <Report report={draft.report} />}
          {selectedVersion && <div className="style-application"><p>仅应用已保存版本。当前编辑内容{dirty ? '尚未保存，请先保存后再应用。' : '已保存。应用从后续交流或创作生效，保留已有经历与正文。'}</p><div className="button-row"><button className="secondary" disabled={busy || dirty || selectedVersion.id === appliedVersion} onClick={() => void apply(false)}>{kind === 'personality' ? '应用为 LISA 性格' : '应用为 LISA 默认写作风格'}</button><button className="text-button" disabled={busy || dirty || selectedVersion.version === selectedProfile?.latest_version} onClick={() => void restore()}>恢复此版本为新保存版本</button></div></div>}
          <div className="style-trial"><h3>隔离{kind === 'personality' ? '试聊' : '试写'}</h3><p className="field-hint">使用点击开始时的编辑内容进行一次临时试用，编辑后需再次试用，计入统一额度；不发 QQ，不写关系记忆、作品正文或自主任务。</p><div className="field"><label htmlFor="style-trial-input">{kind === 'personality' ? '试聊情境与消息' : '不同内容的试写要求'}</label><textarea id="style-trial-input" rows={3} maxLength={3000} value={trialInput} disabled={busy || activeTask(trial)} onChange={event => setTrialInput(event.target.value)} /></div><button className="secondary" disabled={busy || activeTask(trial) || !draft.prompt.trim() || !trialInput.trim()} onClick={() => void tryStyle()}>开始{kind === 'personality' ? '试聊' : '试写'}</button>{trial && <div className="style-trial-result" aria-label="隔离试用结果"><p>{statuses[trial.status] ?? trial.status}</p>{trial.error && <p>{taskErrors[trial.error] ?? '试用未完成，输入仍保留。'}</p>}{trial.result && 'text' in trial.result && <p className="style-sample">{trial.result.text}</p>}{activeTask(trial) && <button className="text-button" disabled={busy || trial.status === 'cancel_requested'} onClick={() => void cancel(trial)}>取消试用</button>}</div>}</div>
        </section>
        {kind === 'writing' && <section className="setup-panel" aria-label="单作品写作风格"><h2>各作品的写作风格</h2><div className="field"><label htmlFor="style-project">选择作品</label><select id="style-project" value={projectId} disabled={busy} onChange={event => setProjectId(event.target.value)}><option value="">选择一部作品</option>{projects.map(project => <option key={project.id} value={project.id}>{project.title}</option>)}</select></div>{projectId && (writingStyle ? <><p role="status">{writingStyle.source === 'override' ? '此作品单独覆盖' : writingStyle.source === 'default' ? '跟随 LISA 默认' : '跟随 LISA 默认，尚无蒸馏风格'}{writingStyle.effective_profile_name ? ` · ${writingStyle.effective_profile_name}` : ''}{writingStyle.effective_version_id ? ` · ${library.profiles.flatMap(profile => profile.versions).find(version => version.id === writingStyle.effective_version_id)?.version ?? '已应用'} 版` : ''}</p><div className="button-row"><button className="secondary" disabled={busy || dirty || !selectedVersion || writingStyle.override_version_id === selectedVersion.id} onClick={() => void apply(true)}>将所选版本用于此作品</button><button className="text-button" disabled={busy || writingStyle.source !== 'override'} onClick={() => void followDefault()}>恢复跟随 LISA 默认</button></div></> : writingError ? <><ErrorNotice message={writingError} /><button className="text-button" disabled={busy} onClick={() => setWritingRetry(value => value + 1)}>重新读取作品风格</button></> : <p role="status">正在读取作品风格…</p>)}</section>}
      </div></div>
    </>}
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="style-leave-title" onCancel={event => { event.preventDefault(); setLeaving(false); }}><h2 id="style-leave-title">有未保存的风格修改</h2><p>保存方案后再离开，或放弃修改。保存不会自动应用；未启动分析的上传将清理。</p><div className="button-row"><button className="primary" onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" autoFocus onClick={() => setLeaving(false)}>继续编辑</button></div></dialog>
  </main>;
});
