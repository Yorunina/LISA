import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import { ErrorNotice } from './ui';

export type CharacterRetrievalConfig = { enabled: boolean; limit: number; max_chars: number };
export type CharacterDefaults = { retrieval_config: CharacterRetrievalConfig; indexing_enabled: boolean };
export type CharacterDraft = { name: string; prompt: string; known_information: string; current_scene: string; retrieval_config: CharacterRetrievalConfig; enabled: boolean };
export type Character = CharacterDraft & { id: string; project_id: string; revision: number; created_at: string };
export const newCharacter = (retrieval_config: CharacterRetrievalConfig): CharacterDraft => ({ name: '', prompt: '只扮演这个角色，根据已知信息提出自身的对白、行动与反应；未知信息保持未知，不创建其他角色或改写全局剧情。', known_information: '', current_scene: '', retrieval_config, enabled: false });
export const characterDraft = ({ name, prompt, known_information, current_scene, retrieval_config, enabled }: CharacterDraft): CharacterDraft => ({ name, prompt, known_information, current_scene, retrieval_config, enabled });

export function CharacterEditor({ items, defaults, selectedId, draft, busy, dirty, saved, onSelect, onChange, onSave, onDelete }: { items: Character[]; defaults: CharacterDefaults; selectedId: string | null; draft: CharacterDraft | null; busy: boolean; dirty: boolean; saved: boolean; onSelect: (character: Character | null) => void; onChange: (draft: CharacterDraft) => void; onSave: () => void; onDelete: () => Promise<boolean> }) {
  const [deleting, setDeleting] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { if (deleting) dialog.current?.showModal(); else dialog.current?.close(); }, [deleting]);
  return <section className="material-paper character-paper" aria-label="作品角色配置">
    <p className="eyebrow">人物自己的声音</p><h2>角色</h2>
    <p className="form-copy">这里配置作品角色，与 LISA 的性格分开。作者会按需选择最多一名已启用角色参与，不保证每次调用。</p>
    <p className="field-hint">角色只读取自身设定和你维护的已知信息；不会获得作者的创作要求、世界书或记忆。请只填入角色亲历、已知或当前能看见的事。</p>
    <div className="character-layout"><nav className="character-list" aria-label="作品角色列表"><button className="secondary" disabled={busy} onClick={() => onSelect(null)}>新建角色</button>{items.map(item => <button key={item.id} disabled={busy} aria-current={selectedId === item.id ? 'page' : undefined} onClick={() => { if (item.id !== selectedId) onSelect(item); }}><strong>{item.name}</strong><span>{item.enabled ? '可参与创作' : '未启用'}</span></button>)}{!items.length && <p className="field-hint">尚无角色配置。作者自动建立的角色也会列在这里，初始未启用，补充已知信息并保存后再启用。</p>}</nav>
      {draft ? <form onSubmit={event => { event.preventDefault(); onSave(); }}>
        <div className="field"><label htmlFor="character-name">角色名称</label><input id="character-name" required maxLength={60} value={draft.name} disabled={busy} onChange={event => onChange({ ...draft, name: event.target.value })} /></div>
        <div className="field"><label htmlFor="character-prompt">角色 prompt</label><textarea id="character-prompt" required rows={6} maxLength={3000} value={draft.prompt} disabled={busy} onChange={event => onChange({ ...draft, prompt: event.target.value })} /></div>
        <div className="field"><label htmlFor="character-known">角色已知信息</label><textarea id="character-known" rows={8} maxLength={6000} value={draft.known_information} disabled={busy} onChange={event => onChange({ ...draft, known_information: event.target.value })} /><p className="field-hint">记录角色亲历或已知的信息，最多 6000 字符。启用前必须填写；改稿后的关联内容仍需你维护。</p></div>
        <div className="field"><label htmlFor="character-scene">当前可见场景</label><textarea id="character-scene" rows={4} maxLength={1500} value={draft.current_scene} disabled={busy} onChange={event => onChange({ ...draft, current_scene: event.target.value })} /><p className="field-hint">只写角色眼前能感知的事，最多 1500 字符。每次参与都会提供这段场景，也用于查找相关已知信息。</p><p className="field-hint">角色参与时会从当前场景提取少量事实或推测，并保留原文依据；小说正文采用后追加到知识中。重复条目、依据不足、知识已满或期间配置有改动时跳过，正文照常保存。</p></div>
        <fieldset className="character-retrieval" disabled={busy}><legend>这个角色的知识检索</legend>
          <p className="field-hint">仅影响这个角色，不改变其他角色或作者的检索配置。</p>
          <label className="check-row"><input type="checkbox" checked={draft.retrieval_config.enabled} onChange={event => onChange({ ...draft, retrieval_config: { ...draft.retrieval_config, enabled: event.target.checked } })} />按当前场景检索角色已知信息</label>
          <p className="field-hint">关闭时提供全部已知信息。开启后只从这个角色的知识中召回片段；没有可用索引时，取已知信息开头，并遵守下面的文本预算。</p>
          {!defaults.indexing_enabled && <p className="contact-availability">服务尚未启用向量检索。开启此配置后暂取已知信息开头；当前可见场景始终保留。</p>}
          <div className="contact-fields"><div className="field"><label htmlFor="character-retrieval-limit">角色召回数量（条）</label><input id="character-retrieval-limit" type="number" min={1} max={8} step={1} required value={draft.retrieval_config.limit} onChange={event => onChange({ ...draft, retrieval_config: { ...draft.retrieval_config, limit: Number(event.target.value) } })} /><p className="field-hint">最多 1–8 条相关片段。</p></div><div className="field"><label htmlFor="character-retrieval-budget">角色知识文本预算（字符）</label><input id="character-retrieval-budget" type="number" min={300} max={3000} step={1} required value={draft.retrieval_config.max_chars} onChange={event => onChange({ ...draft, retrieval_config: { ...draft.retrieval_config, max_chars: Number(event.target.value) } })} /><p className="field-hint">知识片段最多 300–3000 字符，不包含当前可见场景。</p></div></div>
          <button className="secondary" type="button" onClick={() => onChange({ ...draft, retrieval_config: { ...defaults.retrieval_config } })}>恢复角色检索默认值</button><p className="field-hint">恢复默认只修改表单，保存角色后才会生效。</p>
        </fieldset>
        <label className="check-row"><input type="checkbox" checked={draft.enabled} disabled={busy} onChange={event => onChange({ ...draft, enabled: event.target.checked })} />允许作者按需调用这个角色</label>
        <div className="save-bar"><button className="primary" disabled={busy}>保存角色</button>{selectedId && <button type="button" className="danger" disabled={busy} onClick={() => setDeleting(true)}>删除角色</button>}<p role="status">{dirty ? '有未保存修改 · Ctrl+S 保存' : saved ? '已保存' : '已保存的配置用于后续创作'}</p></div>
      </form> : <p className="field-hint">选择一个角色，或新建角色配置。</p>}
    </div>
    <dialog ref={dialog} className="leave-dialog" aria-labelledby="delete-character-title" onCancel={event => { event.preventDefault(); if (!busy) setDeleting(false); }}><h2 id="delete-character-title">删除角色配置？</h2><p>删除配置和本次未保存修改，不改变已有正文。任务参与记录仍保留，角色姓名会显示为「角色已删除」。</p><div className="button-row"><button className="danger" disabled={busy} onClick={async () => { await onDelete(); setDeleting(false); }}>确认删除角色</button><button className="secondary" disabled={busy} autoFocus onClick={() => setDeleting(false)}>保留角色</button></div></dialog>
  </section>;
}

type Participation = { position: number; character_id: string | null; name: string | null; revision: number; state: 'started' | 'completed' | 'failed' | 'cancelled'; created_at: string };
const participationStates = { started: '正在参与', completed: '参与完成', failed: '参与失败', cancelled: '已取消' };
export function TaskCharacters({ taskId, status }: { taskId: string; status: string }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<Participation[] | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setItems(null); setError('');
    request<{ participations: Participation[] }>(`/tasks/${taskId}/characters`).then(result => { if (active) setItems(result.participations); }).catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [taskId, status, open, retry]);
  return <div className="task-characters"><button className="text-button" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? '收起角色参与' : '查看角色参与'}</button>{open && <div><p className="field-hint">记录角色调用的状态，不代表建议已经用于正式正文。</p>{error ? <><ErrorNotice message={error} /><button className="text-button" onClick={() => setRetry(value => value + 1)}>重新读取角色参与</button></> : !items ? <p role="status">正在读取角色参与…</p> : !items.length ? <p className="field-hint">这项任务暂无角色参与。</p> : <ol className="reference-list">{items.map(item => <li key={item.position}><p>{item.name ?? '角色已删除'} · {participationStates[item.state]}</p><p className="field-hint">配置版本 {item.revision} · <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString('zh-CN')}</time></p></li>)}</ol>}</div>}</div>;
}
