import { useEffect, useImperativeHandle, useState } from 'react';
import type { Ref } from 'react';
import { request } from './api';
import type { StyleLibrary, WritingStyle } from './api';
import { ErrorNotice } from './ui';

export function GuidedField({ id, label, hint, placeholder, example, value, busy, onChange, rows = 5 }: { id: string; label: string; hint: string; placeholder: string; example: string; value: string; busy: boolean; onChange: (value: string) => void; rows?: number }) {
  return <div className="field guided-field"><label htmlFor={id}>{label}</label><p className="field-hint" id={`${id}-hint`}>{hint}</p><textarea id={id} aria-describedby={`${id}-hint`} rows={rows} maxLength={12000} value={value} placeholder={placeholder} disabled={busy} onChange={event => onChange(event.target.value)} /><details className="field-example"><summary>看一个填写示例</summary><p>{example}</p><button className="text-button" type="button" disabled={busy || !!value.trim()} onClick={() => onChange(example)}>填入此示例</button>{!!value.trim() && <span className="field-hint">已有内容时仅供参考。</span>}</details></div>;
}

export function RequirementsForm({ direction, constraints, busy, onDirection, onConstraints }: { direction: string; constraints: string; busy: boolean; onDirection: (value: string) => void; onConstraints: (value: string) => void }) {
  return <div className="guided-materials"><p className="material-intro">先说清故事要往哪里走。只填写已经确定的内容，其余可以留空；文字风格在「写作风格」单独设置。</p>
    <GuidedField id="work-direction" label="作品方向（已确认）" hint="写什么故事、围绕谁、主要矛盾是什么？保存后视为已确认的创作方向。" placeholder="例如：现实悬疑，围绕一封迟来的信展开。主角寻找寄信人，逐渐发现旧友隐瞒的往事。" example="写一部现实悬疑小说。主角收到一封迟来的信，循着线索寻找寄信人，逐渐发现旧友隐瞒的往事。" value={direction} busy={busy} onChange={onDirection} />
    <GuidedField id="work-constraints" label="必须遵守（可选）" hint="只写不可违背的要求或禁区，建议每行一条。一般喜好放在「讨论记录」。" placeholder={'不得出现超自然力量。\n必须保留主角与旧友的核心关系。'} example={'不得出现超自然力量。\n主角不能靠偶然获得关键答案。'} value={constraints} busy={busy} onChange={onConstraints} rows={4} />
  </div>;
}

export function DiscussionForm({ preferences, tentative, busy, onPreferences, onTentative }: { preferences: string; tentative: string; busy: boolean; onPreferences: (value: string) => void; onTentative: (value: string) => void }) {
  return <div className="guided-materials"><p className="material-intro">留住交流中的倾向和未定想法。这里的内容不会自动成为已确认要求；确定后，请整理到「创作要求」。</p>
    <details className="discussion-section"><summary><strong>偏好倾向</strong><span className="material-label">仅供参考</span><span>{preferences.trim() ? '已有记录' : '可留空'}</span></summary><p className="discussion-preview">{preferences.trim() ? preferences.slice(0, 100) + (preferences.length > 100 ? '…' : '') : '例如：似乎更喜欢克制的对白，但还没有确定。'}</p><GuidedField id="work-preferences" label="偏好倾向内容" hint="可以写喜欢或不喜欢什么，也可纠正 LISA 的推测。注明是否只是当前倾向。" placeholder="我目前更喜欢缓慢展开、对白克制的写法；需要时可以调整。" example="目前更偏好缓慢展开、对白克制的故事，但不要求每一场都如此。" value={preferences} busy={busy} onChange={onPreferences} rows={4} /></details>
    <details className="discussion-section"><summary><strong>待确认想法</strong><span className="material-label">尚未决定</span><span>{tentative.trim() ? '已有记录' : '可留空'}</span></summary><p className="discussion-preview">{tentative.trim() ? tentative.slice(0, 100) + (tentative.length > 100 ? '…' : '') : '例如：寄信人是否设为旧友，留待后续商量。'}</p><GuidedField id="work-tentative" label="待确认想法内容" hint="写清尚未决定的问题或备选方案。不要将已发生的剧情放在这里。" placeholder="寄信人可能是旧友，也可能是旧友的家人，尚未决定。" example="寄信人是否设为主角的旧友？目前保留这一可能，等线索确定后再决定。" value={tentative} busy={busy} onChange={onTentative} rows={4} /></details>
  </div>;
}

export type WorkStyleHandle = { saveSelection: () => Promise<void> };
export function WorkStylePicker({ ref, projectId, busy, onDirty, onManage }: { ref: Ref<WorkStyleHandle>; projectId: string; busy: boolean; onDirty: (value: boolean) => void; onManage: () => void }) {
  const [view, setView] = useState<WritingStyle | null>(null);
  const [library, setLibrary] = useState<StyleLibrary | null>(null);
  const [selection, setSelection] = useState('');
  const [baseline, setBaseline] = useState('');
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setError(''); setView(null); setLibrary(null);
    Promise.all([request<WritingStyle>(`/projects/${projectId}/writing-style`), request<StyleLibrary>('/style/profiles')]).then(([style, saved]) => {
      if (active) { setView(style); setLibrary(saved); setSelection(style.override_version_id ?? ''); setBaseline(style.override_version_id ?? ''); }
    }).catch(cause => { if (active) setError(cause.message); });
    return () => { active = false; };
  }, [projectId, retry]);
  useEffect(() => { onDirty(selection !== baseline); }, [selection, baseline, onDirty]);
  useImperativeHandle(ref, () => ({ saveSelection: async () => {
    if (!view || selection === baseline) return;
    const next = await request<WritingStyle>(`/projects/${projectId}/writing-style`, { version_id: selection || null }, 'PUT');
    setView(next); setBaseline(next.override_version_id ?? ''); setSelection(next.override_version_id ?? ''); onDirty(false);
  } }));
  const profiles = library?.profiles.filter(profile => profile.kind === 'writing') ?? [];
  const selectedId = selection || library?.applications.writing;
  const selectedProfile = profiles.find(profile => profile.versions.some(version => version.id === selectedId));
  const selectedVersion = selectedProfile?.versions.find(version => version.id === selectedId);
  const currentVersion = profiles.flatMap(profile => profile.versions).find(version => version.id === view?.effective_version_id);
  return <section className="work-style-picker" aria-label="本作品风格方案"><h3>使用哪种写作风格</h3>
    {error ? <><ErrorNotice message={error} /><p className="field-hint">仍可填写并保存下方补充写法；方案尚未读取，不会更改当前应用。</p><button className="text-button" disabled={busy} onClick={() => setRetry(value => value + 1)}>重新读取风格方案</button></> : !view || !library ? <p role="status">正在读取风格方案…</p> : <>
      <p role="status" aria-label="当前作品风格">当前：{view.source === 'override' ? '本作品单独使用' : '跟随 LISA 默认'}{view.effective_profile_name ? ` · ${view.effective_profile_name}${currentVersion ? ` · 版本 ${currentVersion.version}` : ''}` : ' · 尚未应用风格方案'}</p>
      <div className="field"><label htmlFor="work-style-selection">风格方案</label><select id="work-style-selection" value={selection} disabled={busy} onChange={event => setSelection(event.target.value)}><option value="">跟随 LISA 默认风格</option>{profiles.map(profile => <optgroup label={profile.name} key={profile.id}>{[...profile.versions].sort((a, b) => b.version - a.version).map(version => <option key={version.id} value={version.id}>{profile.name} · 版本 {version.version}</option>)}</optgroup>)}</select><p className="field-hint">点击下方「保存写作设置」后应用。跟随默认会使用 LISA 后续选定的默认方案；选择具体版本仅影响本作品。</p></div>
      {selection !== baseline && <p className="material-pending" role="status">待应用：{selection ? `${selectedProfile?.name ?? '所选方案'} · 版本 ${selectedVersion?.version ?? ''}` : '跟随 LISA 默认风格'}</p>}
      {selectedVersion ? <details className="style-instructions"><summary>查看所选方案的写作指引</summary>{selectedVersion.summary && <p>{selectedVersion.summary}</p>}<p className="reference-text">{selectedVersion.prompt}</p></details> : <p className="field-hint">还没有可用方案时，可以直接填写补充写法，也可以从作品提炼。</p>}
    </>}
    <button className="secondary" disabled={busy} onClick={onManage}>管理或提炼写作风格</button>
  </section>;
}
