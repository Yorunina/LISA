import { useEffect, useImperativeHandle, useRef, useState } from 'react';
import type { Ref } from 'react';
import { EditorContent, useEditor } from '@tiptap/react';
import type { JSONContent } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import { downloadBody, request } from './api';
import { Versions } from './versions';
import type { Chapter } from './versions';
import { RetrievalEditor, TaskReferences } from './retrieval';
import type { RetrievalView } from './retrieval';
import { ErrorNotice } from './ui';
import { ShareWork } from './sharing';
import { CharacterEditor, TaskCharacters, characterDraft, newCharacter } from './characters';
import type { Character, CharacterDefaults, CharacterDraft } from './characters';

const materials = [
  ['agreement', '已确认共识', '保存即确认这些创作要求。也可以先在 QQ 中与 LISA 商量，再把确定的内容写在这里。'],
  ['worldbook', '世界书', '故事的世界、背景与基本规则。'],
  ['constraints', '限制条件', '创作必须遵守的限制与禁区。'],
  ['characters', '人物设定', '作品中的人物、动机与关系。'],
  ['author_prompt', '作者 prompt', '作者身份、叙述方式与文风要求。'],
  ['summary', '作品摘要', '已经发生的主要故事。'],
  ['plot_state', '剧情状态', '当前进展、悬念与待处理线索。'],
  ['preferences', '创作偏好', '尚未成为硬性要求的创作倾向。'],
  ['tentative', '待定事项', '仍需商量的想法，不作为已确认共识。'],
] as const;
type WorkTab = 'body' | 'materials' | 'characters' | 'retrieval' | 'versions' | 'tasks' | 'library';
const workTabs: [WorkTab, string][] = [['body', '正文'], ['materials', '创作资料'], ['characters', '角色'], ['retrieval', '检索配置'], ['versions', '版本记录'], ['tasks', '创作任务'], ['library', '书架']];

type MaterialKey = typeof materials[number][0];
type Project = Record<MaterialKey, string> & { id: string; title: string; kind: string; is_current: boolean; completed: boolean };
type Task = { id: string; kind: string; goal: string; status: string; error: string | null; can_cancel: boolean; can_retry: boolean; input_tokens: number; output_tokens: number; charged_usd: string | number; reserved_usd: string | number; adopted_at: string | null };
type FinishingTask = Task & { project_id: string; project_title: string };
type Schedule = { enabled: boolean; status: 'no_plan' | 'scheduled' | 'paused' | 'waiting' | 'disabled'; pause_reason: string | null; due_at: string | null; goal: string | null; project_id: string | null; remaining_scenes: number };
export type WorksHandle = { requestLeave: (action: () => void) => void };
const emptyDoc: JSONContent = { type: 'doc', content: [{ type: 'paragraph' }] };
const statusNames: Record<string, string> = { queued: '等待执行', pending: '等待执行', running: '正在创作', succeeded: '已完成', completed: '已完成', adopted: '已采用', failed: '执行失败', cancelled: '已取消', cancel_requested: '正在取消', outcome_unknown: '结果尚未确认', blocked: '暂时暂停' };
const taskErrors: Record<string, string> = {
  account_unavailable: '账号或作品暂时不可用。',
  unsupported_project_kind: '当前作品类型暂不支持此任务。',
  agreement_required: '请先保存已确认共识，再开始写作。',
  autonomous_paused: '自主创作已暂停，当前任务未开始。',
  target_changed: '原文片段已改变，请重新选择后发起重写。',
  context_changed: '记忆或聊天记录已更新，这次结果未保存；可用最新资料重试。',
  writing_scope_exceeded: '生成内容超出本次范围，原文保留。',
  model_busy: '创作服务暂时繁忙，请稍后重试。',
  budget_exhausted: '可用额度已用尽，本次创作已停止。',
  model_unknown: '调用结果尚未确认，已停止重试，原文保留。',
  input_too_large: '本次创作资料过长，请缩减相关内容后重试。',
  model_failed: '模型调用失败，原文保留，可稍后重试。',
  invalid_writing_result: '生成结果未通过检查，原文保留。',
  character_unavailable: '选中的角色已停用、删除或缺少已知信息，原文保留；可检查角色配置后重试。',
  user_cancelled: '你已取消这次任务。',
  project_changed: '当前作品状态已改变，这项尚未开始的自主任务已取消。',
  project_completed: '作品已完结，这项尚未开始的续写已取消。',
  new_user_message: '收到新消息，原自主安排已取消，待交流后重新安排。',
};
const money = (value: string | number) => `¥${Number(value || 0).toFixed(6)}`;
const scheduleNames: Record<Schedule['status'], string> = { no_plan: '暂无后续计划', scheduled: '已有后续计划', paused: '自主续写已暂停', waiting: '等待当前任务处理', disabled: '自主续写尚未开启' };
const pauseReasons: Record<string, string> = {
  inactive: '连续 24 小时未在 QQ 回复，自主续写已暂停。新的 QQ 回复后再按条件安排。',
  no_project: '尚未选定可继续的当前作品。',
  no_agreement: '当前作品还没有已确认的创作共识。',
  completed: '当前作品已完结，不再自主续写。',
  limit_reached: '这一轮不再安排新的自主续写，等待新的交流。',
  account_unavailable: '账号当前不可用于自主续写。',
};

function ScheduleNote({ schedule, failed, projectId, compact = false }: { schedule: Schedule | null; failed: boolean; projectId: string; compact?: boolean }) {
  const elsewhere = schedule?.enabled && schedule.project_id && schedule.project_id !== projectId;
  const status = schedule && !schedule.enabled ? 'disabled' : schedule?.status;
  if (compact) return <p className="schedule-summary" role="status" aria-label="自主续写状态">自主续写 · {failed ? '状态暂不可用' : !status ? '正在读取计划…' : elsewhere ? '计划属于另一部作品' : status === 'paused' ? pauseReasons[schedule?.pause_reason ?? ''] ?? '自主续写已暂停' : scheduleNames[status]}</p>;
  return <div className="schedule-note" role="status" aria-label="自主续写状态">
    <div className="task-heading"><strong>自主续写</strong><span>{failed ? '状态暂不可用' : !status ? '正在读取计划…' : elsewhere ? '计划属于另一部作品' : scheduleNames[status]}</span></div>
    {failed ? <p>暂时无法读取后续计划，稍后自动重试。正文编辑与手动创作仍可使用。</p>
      : elsewhere ? <p>这项计划不会写入当前查看的作品。</p>
        : status === 'paused' ? <><p>{pauseReasons[schedule?.pause_reason ?? ''] ?? '当前不满足自主续写条件。'}</p><p className="field-hint">自主续写暂停不影响网页编辑与单次手动创作；模型调用仍受可用额度限制。</p></>
          : status === 'scheduled' || status === 'waiting' ? <><p>{schedule?.goal}</p>{status === 'scheduled' && schedule?.due_at && <p className="field-hint">计划时间：<time dateTime={schedule.due_at}>{new Date(schedule.due_at).toLocaleString('zh-CN')}</time></p>}<p className="field-hint">计划不代表已经开始；执行前仍会检查共识、活跃状态与可用额度。</p></>
            : status === 'no_plan' ? <p>目前没有排定下一段自主创作。可以继续在 QQ 商量，或在网页发起一次创作。</p>
              : status === 'disabled' ? <p>可以在网页发起一次创作，或继续编辑已有正文。</p> : null}
  </div>;
}

export function Works({ ref, onStyles, initialProjectId }: { ref: Ref<WorksHandle>; onStyles: (projectId: string) => void; initialProjectId?: string | null }) {
  const [project, setProject] = useState<Project | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [finishingTasks, setFinishingTasks] = useState<FinishingTask[]>([]);
  const currentProject = useRef<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [newKind, setNewKind] = useState<'novel' | 'diary'>('novel');
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [chapter, setChapter] = useState<Chapter | null>(null);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState<JSONContent>(emptyDoc);
  const [tab, setTab] = useState<WorkTab>('body');
  const versionsOpen = tab === 'versions';
  const retrievalOpen = tab === 'retrieval';
  const charactersOpen = tab === 'characters';
  const [retrieval, setRetrieval] = useState<RetrievalView | null>(null);
  const [retrievalBaseline, setRetrievalBaseline] = useState('');
  const [retrievalError, setRetrievalError] = useState('');
  const [retrievalRetry, setRetrievalRetry] = useState(0);
  const [characters, setCharacters] = useState<Character[] | null>(null);
  const [characterDefaults, setCharacterDefaults] = useState<CharacterDefaults | null>(null);
  const [characterId, setCharacterId] = useState<string | null>(null);
  const [roleDraft, setRoleDraft] = useState<CharacterDraft | null>(null);
  const [characterBaseline, setCharacterBaseline] = useState('');
  const [characterError, setCharacterError] = useState('');
  const [characterRetry, setCharacterRetry] = useState(0);
  const [exported, setExported] = useState(false);
  const [material, setMaterial] = useState<MaterialKey | null>(null);
  const [draft, setDraft] = useState('');
  const [materialBaseline, setMaterialBaseline] = useState('');
  const [saved, setSaved] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [pollError, setPollError] = useState('');
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [scheduleError, setScheduleError] = useState(false);
  const [kind, setKind] = useState<'scene' | 'rewrite'>('scene');
  const [goal, setGoal] = useState('');
  const [targetText, setTargetText] = useState('');
  const [leave, setLeave] = useState(false);
  const pending = useRef<(() => void) | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const dirty = charactersOpen ? !!roleDraft && JSON.stringify(roleDraft) !== characterBaseline : retrievalOpen ? !!retrieval && JSON.stringify(retrieval.config) !== retrievalBaseline : (tab === 'body' || tab === 'materials') && (material ? draft !== materialBaseline : !!chapter && (title !== chapter.title || JSON.stringify(content) !== JSON.stringify(chapter.content)));
  const latest = useRef({ dirty, chapter, material });
  latest.current = { dirty, chapter, material };
  const editor = useEditor({ extensions: [StarterKit], content: emptyDoc, editorProps: { attributes: { 'aria-label': '章节正文', role: 'textbox', 'aria-multiline': 'true' } }, onUpdate: ({ editor: current }) => { setContent(current.getJSON()); setSaved(false); } });
  useEffect(() => { editor?.setEditable(!busy, false); }, [editor, busy]);

  function failure(cause: unknown) { setError(cause instanceof Error ? cause.message : '操作失败，请重试。'); }
  function updateProject(next: Project | null) { currentProject.current = next; setProject(next); }
  function openProject(next: Project | null) {
    setTab('body'); setCharacters(null); setRoleDraft(null); setRetrieval(null); setExported(false);
    updateProject(next); setChapter(null); setChapters([]); setTasks([]); setMaterial(null);
    setTitle(''); setContent(emptyDoc); setDraft(''); setMaterialBaseline(''); setSaved(false);
    setGoal(''); setTargetText(''); setKind(next?.completed ? 'rewrite' : 'scene'); setError(''); setPollError('');
  }
  function openMaterial(key: MaterialKey) {
    const text = currentProject.current![key];
    setTab('materials'); setMaterial(key); setDraft(text); setMaterialBaseline(text); setSaved(false);
  }
  function openCharacter(next: Character | null) {
    const value = next ? characterDraft(next) : newCharacter({ ...characterDefaults!.retrieval_config });
    setCharacterId(next?.id ?? null); setRoleDraft(value); setCharacterBaseline(JSON.stringify(value)); setSaved(false); setError('');
  }
  function openChapter(next: Chapter) {
    setChapter(next); setTitle(next.title); setContent(next.content); setMaterial(null); setSaved(false); setTargetText('');
    editor?.commands.setContent(next.content, { emitUpdate: false });
  }
  function requestLeave(action: () => void) {
    if (busy) return;
    if (dirty) { pending.current = action; setLeave(true); }
    else action();
  }
  useImperativeHandle(ref, () => ({ requestLeave }));

  function showTab(next: WorkTab) {
    if (next === tab) return;
    requestLeave(() => {
      setError(''); setSaved(false);
      if (next === 'materials') { openMaterial('agreement'); return; }
      setMaterial(null);
      if (next === 'body') {
        const savedChapter = chapters.find(item => item.id === chapter?.id) ?? chapters[0];
        if (savedChapter) openChapter(savedChapter);
      }
      setTab(next);
    });
  }

  async function load() {
    setLoading(true); setError('');
    try {
      const list = await request<Project[]>('/projects');
      setProjects(list); openProject(list.find(item => item.id === initialProjectId) ?? list.find(item => item.is_current) ?? list[0] ?? null);
    } catch (cause) { failure(cause); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (!charactersOpen || !project) return;
    let active = true;
    setCharacters(null); setCharacterDefaults(null); setRoleDraft(null); setCharacterError('');
    Promise.all([request<Character[]>(`/projects/${project.id}/characters`), request<CharacterDefaults>('/characters/defaults')]).then(([items, defaults]) => {
      if (active) { setCharacters(items); setCharacterDefaults(defaults); if (items.length) openCharacter(items[0]); }
    }).catch(cause => { if (active) setCharacterError(cause.message); });
    return () => { active = false; };
  }, [charactersOpen, project?.id, characterRetry]);
  useEffect(() => {
    if (!retrievalOpen || !project) return;
    let active = true;
    setRetrieval(null); setRetrievalError(''); setSaved(false);
    request<RetrievalView>(`/projects/${project.id}/retrieval`).then(next => {
      if (active) { setRetrieval(next); setRetrievalBaseline(JSON.stringify(next.config)); }
    }).catch(cause => { if (active) setRetrievalError(cause.message); });
    return () => { active = false; };
  }, [retrievalOpen, project?.id, retrievalRetry]);
  useEffect(() => {
    if (!project) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const [nextProjects, nextChapters, nextTasks, handoff] = await Promise.all([
          request<Project[]>('/projects'),
          request<Chapter[]>(`/projects/${project!.id}/chapters`), request<Task[]>(`/projects/${project!.id}/tasks`),
          request<{ finishing_tasks: FinishingTask[] }>('/projects/handoff'),
        ]);
        if (!active || currentProject.current?.id !== project!.id) return;
        const nextProject = nextProjects.find(item => item.id === project!.id)!;
        setProjects(nextProjects); setFinishingTasks(handoff.finishing_tasks);
        updateProject(nextProject); setChapters(nextChapters); setTasks(nextTasks); setPollError('');
        const current = latest.current;
        if (current.material && !current.dirty) {
          setDraft(nextProject[current.material]);
          setMaterialBaseline(nextProject[current.material]);
        }
        const updated = nextChapters.find(item => item.id === current.chapter?.id);
        if (!current.dirty && !current.material && (!current.chapter || (updated && updated.version !== current.chapter.version))) {
          const next = updated ?? nextChapters[0];
          if (next) openChapter(next);
        }
      } catch { if (active) setPollError('暂时无法刷新作品资料、章节与任务状态，稍后自动重试。当前编辑内容仍保留。'); }
      finally { if (active) timer = setTimeout(refresh, 5_000); }
    }
    void refresh();
    return () => { active = false; clearTimeout(timer); };
  }, [project?.id, editor]);
  useEffect(() => {
    if (!project) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function refreshSchedule() {
      try {
        const next = await request<Schedule>('/schedule');
        if (active) { setSchedule(next); setScheduleError(false); }
      } catch { if (active) setScheduleError(true); }
      finally { if (active) timer = setTimeout(refreshSchedule, 5_000); }
    }
    setSchedule(null); setScheduleError(false);
    void refreshSchedule();
    return () => { active = false; clearTimeout(timer); };
  }, [project?.id]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault(); if (!busy && !leave && tab !== 'tasks' && tab !== 'library' && !versionsOpen && (charactersOpen || retrievalOpen || chapter || material)) void save();
      }
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  });
  useEffect(() => { if (leave) dialog.current?.showModal(); else dialog.current?.close(); }, [leave]);

  async function save() {
    if (!project || tab === 'tasks' || tab === 'library' || (charactersOpen ? !roleDraft : retrievalOpen ? !retrieval : !material && !chapter)) return false;
    if (retrievalOpen && retrieval && (!Number.isInteger(retrieval.config.limit) || retrieval.config.limit < 1 || retrieval.config.limit > 12 || !Number.isInteger(retrieval.config.max_chars) || retrieval.config.max_chars < 300 || retrieval.config.max_chars > 6000)) { setError('召回数量须为 1–12 的整数，文本预算须为 300–6000 的整数。'); return false; }
    if (charactersOpen && roleDraft && (!roleDraft.name.trim() || !roleDraft.prompt.trim() || (roleDraft.enabled && !roleDraft.known_information.trim()))) { setError('请填写角色名称与 prompt；启用角色前必须填写已知信息。'); return false; }
    if (charactersOpen && roleDraft) {
      const config = roleDraft.retrieval_config;
      if (!Number.isInteger(config.limit) || config.limit < 1 || config.limit > 8 || !Number.isInteger(config.max_chars) || config.max_chars < 300 || config.max_chars > 3000) { setError('角色召回数量须为 1–8 的整数，知识文本预算须为 300–3000 的整数。'); return false; }
    }
    if (!charactersOpen && !retrievalOpen && !material && !title.trim()) { setError('请填写章节标题。'); return false; }
    setBusy(true); setError('');
    try {
      if (charactersOpen) {
        const next = await request<Character>(characterId ? `/characters/${characterId}` : `/projects/${project.id}/characters`, roleDraft!, characterId ? 'PUT' : 'POST');
        setCharacters(items => characterId ? items!.map(item => item.id === characterId ? next : item) : [...items!, next]);
        openCharacter(next);
      } else if (retrievalOpen) {
        const next = await request<RetrievalView>(`/projects/${project.id}/retrieval`, retrieval!.config, 'PUT');
        setRetrieval(next); setRetrievalBaseline(JSON.stringify(next.config));
      } else if (material) {
        const next = await request<Project>(`/projects/${project.id}`, { [material]: draft }, 'PUT');
        updateProject(next); setDraft(next[material]); setMaterialBaseline(next[material]);
      } else {
        const next = await request<Chapter>(`/chapters/${chapter!.id}`, { title, content }, 'PUT');
        setChapter(next); setChapters(items => items.map(item => item.id === next.id ? next : item));
      }
      setSaved(true); return true;
    } catch (cause) { failure(cause); return false; }
    finally { setBusy(false); }
  }
  async function deleteCharacter() {
    if (!characterId) return false;
    setBusy(true); setError('');
    try {
      await request(`/characters/${characterId}`, undefined, 'DELETE');
      setCharacters(items => items!.filter(item => item.id !== characterId));
      setCharacterId(null); setRoleDraft(null); setSaved(false); return true;
    } catch (cause) { failure(cause); return false; }
    finally { setBusy(false); }
  }
  async function resolveLeave(shouldSave: boolean) {
    dialog.current?.close(); setLeave(false);
    if (shouldSave && !(await save())) return;
    pending.current?.(); pending.current = null;
  }
  async function createProject() {
    setBusy(true); setError('');
    try {
      const next = await request<Project>('/projects', { title: newTitle, kind: newKind });
      setProjects(items => [...items.map(item => ({ ...item, is_current: false })), next]);
      openProject(next); setNewTitle('');
    }
    catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  async function exportBody() {
    if (!project) return;
    setBusy(true); setError(''); setExported(false);
    try { await downloadBody(project.id); setExported(true); }
    catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  function restoredChapter(next: Chapter) {
    setChapters(items => items.map(item => item.id === next.id ? next : item));
    openChapter(next); setTab('body'); setSaved(true);
  }
  async function changeProjectState(action: 'activate' | 'complete') {
    if (!project) return;
    setBusy(true); setError('');
    try {
      const next = await request<Project>(`/projects/${project.id}/${action}`, {});
      updateProject(next);
      setProjects(items => items.map(item => item.id === next.id ? next : action === 'activate' ? { ...item, is_current: false } : item));
      if (next.completed) setKind('rewrite');
      const handoff = await request<{ finishing_tasks: FinishingTask[] }>('/projects/handoff');
      setFinishingTasks(handoff.finishing_tasks);
    } catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  async function cancelFinishingTask(task: FinishingTask) {
    setBusy(true); setError('');
    try {
      const next = await request<Task>(`/tasks/${task.id}/cancel`, {});
      setFinishingTasks(items => items.filter(item => item.id !== next.id));
      setTasks(items => items.map(item => item.id === next.id ? next : item));
    } catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  async function startTask() {
    if (!project) return;
    setBusy(true); setError('');
    try {
      const next = await request<Task>(`/projects/${project.id}/tasks`, { kind, goal, ...(kind === 'rewrite' ? { chapter_id: chapter?.id, target_text: targetText } : {}) });
      setTasks(items => [next, ...items]); setGoal('');
    } catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  async function taskAction(task: Task, action: 'cancel' | 'retry') {
    setBusy(true); setError('');
    try {
      const next = await request<Task>(`/tasks/${task.id}/${action}`, {});
      setTasks(items => next.id === task.id ? items.map(item => item.id === task.id ? next : item) : [next, ...items]);
    } catch (cause) { failure(cause); }
    finally { setBusy(false); }
  }
  const activeTasks = tasks.some(task => task.can_cancel);
  const materialInfo = materials.find(item => item[0] === material);
  const remoteChanged = chapter && chapters.some(item => item.id === chapter.id && item.version !== chapter.version);
  const isDiary = project?.kind === 'diary';

  return <main id="main" tabIndex={-1} className="works-page">
    <div className="work-heading"><div><p className="eyebrow">{isDiary ? '把日常写在纸上' : '故事正在生长'}</p><h1>{project?.title || '我的作品'}</h1></div><div className="work-heading-actions">{project && <span className="work-tag">{isDiary ? '日记' : '小说'}</span>}{!!projects.length && <button className="secondary" disabled={busy} onClick={() => requestLeave(() => openProject(null))}>开启新作品</button>}{!project && !!projects.length && <button className="text-button" disabled={busy} onClick={() => openProject(projects.find(item => item.is_current) ?? projects[0])}>返回书架中的作品</button>}</div></div>
    {error && <ErrorNotice message={error} />}
    {loading ? <p className="loading-note" role="status">正在打开作品…</p> : !project ? <section className="setup-panel first-work"><h2>{projects.length ? '开启新作品' : '从第一部作品开始'}</h2><p className="form-copy">先选一种写作方式，给作品起个名字。新作会成为当前作品，资料从空白开始；保存已确认的创作共识后，再请 LISA 一小段一小段地写下去。</p><form onSubmit={event => { event.preventDefault(); void createProject(); }}><div className="field"><label htmlFor="work-kind">作品类型</label><select id="work-kind" value={newKind} onChange={event => setNewKind(event.target.value as 'novel' | 'diary')} disabled={busy}><option value="novel">小说</option><option value="diary">LISA 的日记</option></select>{newKind === 'diary' && <p className="field-hint">记录 LISA 的视角与虚拟生活，不是你的现实日记。创建后仍需先确认写作方向。</p>}</div><div className="field"><label htmlFor="work-title">作品名称</label><input id="work-title" required maxLength={160} value={newTitle} onChange={event => setNewTitle(event.target.value)} disabled={busy} /></div><button className="primary" disabled={busy}>创建{newKind === 'diary' ? '日记' : '小说'}</button></form>{error && <button className="text-button" onClick={load}>重新读取作品</button>}</section> : <>
      <section className="work-lifecycle" aria-label="作品状态">
        <p>{project.is_current ? '当前作品' : '正在查看旧作'} · {project.completed ? '已完结，仍可阅读和局部修订。' : '未完结'}</p>
        <div className="button-row">
          {(!project.is_current || project.completed) && <button className="secondary" disabled={busy} onClick={() => void changeProjectState('activate')}>{project.completed ? '继续创作' : '设为当前作品'}</button>}
          <button className="text-button" disabled={busy || !chapters.length} onClick={() => void exportBody()}>导出正文</button>
          <ShareWork key={project.id} projectId={project.id} chapterId={chapter?.id} />
          {!project.completed && <button className="text-button" disabled={busy} onClick={() => void changeProjectState('complete')}>标记完结</button>}
        </div>
        {exported && <p className="field-hint" role="status">正文文件已交给浏览器下载，仅含已保存的正文。</p>}
      </section>
      {!!finishingTasks.length && <aside className="handoff-note" aria-label="旧作品任务">
        {finishingTasks.map(task => <div key={task.id}><p>《{task.project_title}》的当前任务仍在完成，结果只保存到该作品。新作自主生成会等待它结束。</p><div className="button-row"><button className="text-button" disabled={busy} onClick={() => requestLeave(() => openProject(projects.find(item => item.id === task.project_id)!))}>查看《{task.project_title}》</button>{task.can_cancel && <button className="text-button" disabled={busy} onClick={() => void cancelFinishingTask(task)}>取消《{task.project_title}》的任务</button>}</div></div>)}
      </aside>}
      <nav className="work-tabs" aria-label="作品页面">{workTabs.map(([value, label]) => <button key={value} disabled={busy || (value === 'versions' && !chapter)} aria-current={tab === value ? 'page' : undefined} onClick={() => showTab(value)}>{label}{value === 'tasks' && activeTasks && <span className="tab-indicator" aria-hidden="true" title="有进行中的任务" />}</button>)}</nav>
    {tab === 'library' && !!projects.length && <section className="work-library" aria-label="作品列表">
      <div className="library-heading"><h2>书架</h2></div>
      <p className="field-hint">同时只自主推进一部当前作品。打开旧作阅读或修订，不会切换。</p>
      <ul>{projects.map(item => <li key={item.id}>
        <button className="library-title" aria-label={`打开《${item.title}》`} aria-current={project?.id === item.id ? 'page' : undefined} disabled={busy} onClick={() => { if (project?.id === item.id) showTab('body'); else requestLeave(() => openProject(item)); }}>{item.title}</button>
        <div className="library-labels"><span>{item.kind === 'diary' ? '日记' : '小说'}</span><span>{item.completed ? '已完结' : '未完结'}</span>{item.is_current && <span className="current-work-label">当前作品</span>}</div>
      </li>)}</ul>
    </section>}
      {pollError && <p className="work-warning" role="status">{pollError}</p>}
      {isDiary && <p className="work-warning">这是 LISA 视角的虚拟生活日记。已采用的明确事件可进入生活记忆，梦境保留梦境性质；手动修改正文不会自动更新生活记忆，请在「记忆」中另行维护。</p>}
      {tab !== 'tasks' && tab !== 'library' && <div className="work-task-summary" aria-label="创作任务摘要"><div><p role="status">{activeTasks ? 'LISA 正在处理创作任务。你仍可编辑；任务读取已保存内容。' : tasks[0] ? `最近任务：${tasks[0].adopted_at ? '已采用' : statusNames[tasks[0].status] ?? tasks[0].status}` : '在 QQ 商量下一段，或在这里发起一次创作。'}</p><ScheduleNote schedule={schedule} failed={scheduleError} projectId={project.id} compact /></div><div className="inline-actions"><button className="secondary" disabled={busy} onClick={() => showTab('tasks')}>{activeTasks ? '查看创作任务' : '发起创作'}</button>{tasks.find(task => task.can_cancel) && <button className="text-button" disabled={busy} onClick={() => void taskAction(tasks.find(task => task.can_cancel)!, 'cancel')}>取消当前任务</button>}</div></div>}

      {remoteChanged && dirty && <p className="work-warning" role="status">这一章已有新的正式内容。当前未保存修改仍在这里；保存将以你的编辑为准，放弃修改后可查看新内容。</p>}
      {tab === 'tasks' ? <div className="task-workspace">        <section className="generation-panel" aria-labelledby="generation-title"><p className="eyebrow">与 LISA 一起写</p><h2 id="generation-title">{isDiary ? '下一段日记' : '下一段故事'}</h2><form onSubmit={event => { event.preventDefault(); void startTask(); }}><div className="field"><label htmlFor="task-kind">创作方式</label><select disabled={busy} id="task-kind" value={kind} onChange={event => setKind(event.target.value as 'scene' | 'rewrite')}><option value="scene" disabled={project.completed}>{isDiary ? '写一段日记' : '写一个场景'}</option><option value="rewrite" disabled={!chapter}>局部重写</option></select></div>{project.completed && <p className="field-hint">作品已完结，可执行一次局部重写；如需新增正文，请先点击「继续创作」。</p>}{kind === 'scene' && !project.agreement.trim() && <p className="work-warning">先在「创作资料 → 已确认共识」保存确定的创作要求；也可以先去 QQ 商量。</p>}{kind === 'rewrite' && <div className="field"><label htmlFor="rewrite-target">要重写的原文</label><textarea disabled={busy} id="rewrite-target" required rows={4} maxLength={4000} value={targetText} onChange={event => setTargetText(event.target.value)} /><p className="field-hint">从当前章节已保存的正文中复制一段原文，且该段只出现一次。仅替换这段文字。</p></div>}<div className="field"><label htmlFor="task-goal">这次的创作要求</label><textarea disabled={busy} id="task-goal" required rows={3} maxLength={4000} value={goal} onChange={event => setGoal(event.target.value)} placeholder={isDiary ? '例如：记录今天整理旧书的一段经历，写到合上最后一本书时结束。' : '例如：写两人在雨夜车站重逢的一个场景，停在他们认出彼此时。'} /></div>{dirty && <p className="field-hint">当前有未保存修改。任务使用已保存的内容，需要时请先保存。</p>}<button className="primary" disabled={busy || (kind === 'scene' && (project.completed || !project.agreement.trim())) || (kind === 'rewrite' && !chapter)}>开始{kind === 'scene' ? '写作' : '局部重写'}</button><p className="field-hint">网页发起的任务不会另发 QQ 通知。结果采用后直接成为正式正文。</p></form></section>
        <section className="tasks-panel" aria-labelledby="tasks-title"><h2 id="tasks-title">创作任务</h2><ScheduleNote schedule={schedule} failed={scheduleError} projectId={project.id} />{!tasks.length ? <p className="field-hint">还没有创作任务。</p> : <ol className="task-list">{tasks.map(task => <li key={task.id}><div className="task-heading"><strong>{task.kind === 'rewrite' ? '局部重写' : isDiary ? '日记写作' : '场景写作'}</strong><span>{task.adopted_at ? '已采用' : statusNames[task.status] ?? task.status}</span></div><p>{task.goal}</p>{task.error && <p className="task-error">{taskErrors[task.error] ?? '这次创作未完成，原文保留。请稍后重试。'}</p>}{task.status === 'cancelled' && <p className="field-hint">已保存的正文与版本保留；本次未采用内容不再写入。</p>}<p className="field-hint">输入 {task.input_tokens ?? 0} / 输出 {task.output_tokens ?? 0} tokens · 开发估算 {money(task.charged_usd)}{Number(task.reserved_usd) > 0 ? ` · 预留估算 ${money(task.reserved_usd)}` : ''}</p>{task.can_cancel && <button className="text-button" disabled={busy} onClick={() => void taskAction(task, 'cancel')}>取消任务</button>}{task.can_retry && <><button className="text-button" disabled={busy} onClick={() => void taskAction(task, 'retry')}>使用最新保存内容重试</button><p className="field-hint">重试创建新任务；原任务估算费用保留。</p></>}<TaskReferences taskId={task.id} status={task.status} /><TaskCharacters taskId={task.id} status={task.status} /></li>)}</ol>}</section>
      </div> : tab === 'library' ? null : <div className="work-layout"><section className="writing-column">
        {charactersOpen ? characters && characterDefaults ? <CharacterEditor items={characters} defaults={characterDefaults} selectedId={characterId} draft={roleDraft} busy={busy} dirty={dirty} saved={saved} onSelect={next => requestLeave(() => openCharacter(next))} onChange={next => { setRoleDraft(next); setSaved(false); }} onSave={() => void save()} onDelete={deleteCharacter} /> : <section className="material-paper">{characterError ? <><ErrorNotice message={characterError} /><button className="text-button" onClick={() => setCharacterRetry(value => value + 1)}>重新读取角色</button></> : <p role="status">正在读取角色…</p>}</section> : retrievalOpen ? retrieval ? <RetrievalEditor view={retrieval} busy={busy} dirty={dirty} saved={saved} onChange={config => { setRetrieval({ ...retrieval, config }); setSaved(false); }} onSave={() => void save()} /> : <section className="material-paper">{retrievalError ? <><ErrorNotice message={retrievalError} /><button className="text-button" onClick={() => setRetrievalRetry(value => value + 1)}>重新读取检索配置</button></> : <p role="status">正在读取检索配置…</p>}</section> : versionsOpen && chapter ? <Versions key={chapter.id} chapter={chapter} busy={busy} setBusy={setBusy} onRestored={restoredChapter} /> : material ? <section className="material-paper"><p className="eyebrow">创作资料</p><h2>{materialInfo?.[1]}</h2><p className="form-copy">{materialInfo?.[2]}</p><div className="material-style-entry"><span>这部作品的表达方式</span><button className="secondary" disabled={busy} onClick={() => requestLeave(() => onStyles(project.id))}>作品写作风格</button></div><div className="field"><label htmlFor="material-content">{materialInfo?.[1]}内容</label><textarea id="material-content" maxLength={material === 'worldbook' || material === 'characters' ? 30000 : 12000} rows={16} value={draft} onChange={event => { setDraft(event.target.value); setSaved(false); }} disabled={busy} /></div><div className="save-bar"><button className="primary" onClick={() => void save()} disabled={busy}>保存资料</button><p role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : saved ? '已保存' : '已保存的内容用于后续创作'}</p></div></section>
          : chapter ? <section className="chapter-paper"><fieldset className="editor-toolbar" aria-label="正文格式" disabled={busy}><button className={editor?.isActive('bold') ? 'selected' : ''} aria-label="加粗" onClick={() => editor?.chain().focus().toggleBold().run()}>B</button><button aria-label="斜体" onClick={() => editor?.chain().focus().toggleItalic().run()}><i>I</i></button><button onClick={() => editor?.chain().focus().setParagraph().run()}>段落</button><button onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()}>小标题</button></fieldset><label className="sr-only" htmlFor="chapter-title">章节标题</label><input id="chapter-title" className="chapter-title" value={title} maxLength={160} onChange={event => { setTitle(event.target.value); setSaved(false); }} disabled={busy} /><EditorContent editor={editor} /><div className="save-bar"><button className="primary" onClick={() => void save()} disabled={busy}>保存正文</button><p role="status">{busy ? '正在处理…' : dirty ? '有未保存修改 · Ctrl+S 保存' : `已保存 · 版本 ${chapter.version}`}</p></div></section>
            : <section className="empty-paper"><span className="paper-flower" aria-hidden="true">✳</span><h2>{isDiary ? '第一段日记，等待落笔。' : '第一段故事，等待落笔。'}</h2><p>{isDiary ? '先在「创作资料」保存共识，再到「创作任务」描述这一段日记要记录的片段。任务完成后，正式正文会出现在这里。' : '先在「创作资料」保存共识，再到「创作任务」描述一个场景。任务完成后，正式正文会出现在这里。'}</p></section>}
      </section><aside className="work-rail" aria-label={material ? '资料目录' : '章节目录'}><p className="eyebrow">{material ? '故事的底稿' : '章节目录'}</p>{material ? materials.map(([key, label]) => <button key={key} aria-current={material === key ? 'page' : undefined} onClick={() => { if (key !== material) requestLeave(() => openMaterial(key)); }}>{label}</button>) : chapters.length ? chapters.map(item => <button key={item.id} aria-current={chapter?.id === item.id ? 'page' : undefined} onClick={() => { if (chapter?.id !== item.id) requestLeave(() => { setTab('body'); openChapter(item); }); }}>{item.title}</button>) : <p className="field-hint">{isDiary ? '首段日记采用后会出现在目录中。' : '首个场景采用后会创建章节。'}</p>}<div className="rail-note">每一次保存，<br />都让故事更近一点。</div></aside></div>}
    </>}
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="work-leave-title" onCancel={event => { event.preventDefault(); setLeave(false); }}><h2 id="work-leave-title">有未保存的修改</h2><p>保存后再离开，或放弃本次修改。未保存的内容不会用于后续创作。</p><div className="button-row"><button className="primary" onClick={() => void resolveLeave(true)}>保存</button><button className="secondary" onClick={() => void resolveLeave(false)}>放弃修改</button><button className="text-button" onClick={() => setLeave(false)} autoFocus>继续编辑</button></div></dialog>
  </main>;
}
