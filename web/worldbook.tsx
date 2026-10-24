import { useState } from 'react';

export type WorldbookEntry = { id: string; title: string; category: string; content: string; scope: string; related_ids: string[] };
export const settingCategories = [['rule', '规则'], ['place', '地点'], ['faction', '组织'], ['history', '历史'], ['other', '其他']] as const;

export function WorldbookEditor({ core, entries, busy, onCore, onChange }: { core: string; entries: WorldbookEntry[]; busy: boolean; onCore: (value: string) => void; onChange: (value: WorldbookEntry[]) => void }) {
  const [section, setSection] = useState<'core' | 'entries'>('core');
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('all');
  const [deleting, setDeleting] = useState(false);
  const item = entries.find(entry => entry.id === selected);
  const filtered = entries.filter(entry => (category === 'all' || entry.category === category) && `${entry.title}\n${entry.content}\n${entry.scope}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  function change(patch: Partial<WorldbookEntry>) { onChange(entries.map(entry => entry.id === selected ? { ...entry, ...patch } : entry)); }
  function add() {
    const entry = { id: crypto.randomUUID(), title: '', category: 'other', content: '', scope: '', related_ids: [] };
    onChange([...entries, entry]); setSelected(entry.id); setSection('entries'); setQuery(''); setCategory('all'); setDeleting(false);
  }
  return <div className="worldbook-editor">
    <div className="setting-overview"><p><strong>核心设定</strong><span>始终交给作者的基本规则。</span></p><p><strong>设定条目</strong><span>按场景查找的地点、组织、历史与细则。</span></p><p><strong>当前剧情</strong><span>此刻的时间、地点和未解线索，在「剧情状态」维护。</span></p></div>
    <nav className="setting-tabs" aria-label="世界书分区"><button aria-current={section === 'core' ? 'page' : undefined} disabled={busy} onClick={() => setSection('core')}>核心设定</button><button aria-current={section === 'entries' ? 'page' : undefined} disabled={busy} onClick={() => setSection('entries')}>设定条目 · {entries.length}</button></nav>
    {section === 'core' ? <><div className="field"><label htmlFor="material-content">世界书内容</label><p className="field-hint" id="core-help">保留每次创作都要遵守的规则，尽量精简。原世界书原文保留在这里，可以自行整理到下方条目。</p><textarea id="material-content" aria-describedby="core-help" maxLength={30000} rows={12} value={core} disabled={busy} onChange={event => onCore(event.target.value)} /></div><button className="text-button" disabled={busy} onClick={() => setSection('entries')}>整理详细设定 →</button></> : <>
      <p className="field-hint">条目按相关性提供给作者，直接关联一同参考。未启用语义检索或索引未就绪时，只能按任务与近期正文中的完整标题匹配。单条尽量简短；条目与直接关联需共同容纳在检索预算内。</p>
      <div className="setting-tools"><div className="field"><label htmlFor="setting-search">搜索设定</label><input id="setting-search" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="标题、内容或适用条件" /></div><div className="field"><label htmlFor="setting-filter">分类筛选</label><select id="setting-filter" value={category} onChange={event => setCategory(event.target.value)}><option value="all">全部分类</option>{settingCategories.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><button className="secondary" disabled={busy || entries.length >= 200} onClick={add}>新增设定</button></div>
      <div className="setting-layout"><nav className="setting-list" aria-label="设定条目列表"><p className="field-hint">{filtered.length} / {entries.length} 条设定</p>{filtered.length ? filtered.map(entry => <button key={entry.id} aria-current={selected === entry.id ? 'page' : undefined} disabled={busy} onClick={() => { setSelected(entry.id); setDeleting(false); }}><strong>{entry.title || '未命名设定'}</strong><span>{settingCategories.find(([key]) => key === entry.category)?.[1]}</span></button>) : <p className="field-hint">{entries.length ? '没有匹配的设定。可清空搜索或切换分类。' : '还没有设定条目。从一个地点或一条规则开始。'}</p>}</nav>
        <div className="setting-detail">{item ? <fieldset disabled={busy} className="setting-fields"><legend className="sr-only">编辑设定条目</legend><div className="field"><label htmlFor="setting-title">设定标题</label><input id="setting-title" value={item.title} maxLength={100} onChange={event => change({ title: event.target.value })} placeholder="例如：北境火术禁令" /></div><div className="field"><label htmlFor="setting-category">设定分类</label><select id="setting-category" value={item.category} onChange={event => change({ category: event.target.value })}>{settingCategories.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div><div className="field"><label htmlFor="setting-content">设定正文</label><textarea id="setting-content" rows={8} maxLength={4000} value={item.content} onChange={event => change({ content: event.target.value })} placeholder="写清规则、例外与必要背景。" /></div><div className="field"><label htmlFor="setting-scope">适用条件（可选）</label><input id="setting-scope" value={item.scope} maxLength={200} onChange={event => change({ scope: event.target.value })} placeholder="例如：旧历 320 年后，仅在北境生效" /><p className="field-hint">随正文一并交给作者判断，不自动计算时间或修改剧情。</p></div><details className="setting-relations"><summary>关联设定 · {item.related_ids.length}</summary><p className="field-hint">选择理解此条必需的通用规则或例外，最多 8 条；只补直接关联。</p>{entries.length < 2 ? <p className="field-hint">新增其他条目后，可在这里关联。</p> : entries.filter(entry => entry.id !== item.id).map(entry => <label key={entry.id}><input type="checkbox" checked={item.related_ids.includes(entry.id)} disabled={!item.related_ids.includes(entry.id) && item.related_ids.length >= 8} onChange={event => change({ related_ids: event.target.checked ? [...item.related_ids, entry.id] : item.related_ids.filter(id => id !== entry.id) })} />{entry.title || '未命名设定'}</label>)}</details>
        {deleting ? <div className="setting-delete" role="group" aria-label="删除设定确认"><p>删除后会移除其他条目对它的关联。保存资料后生效。</p><button className="danger" onClick={() => { onChange(entries.filter(entry => entry.id !== item.id).map(entry => ({ ...entry, related_ids: entry.related_ids.filter(id => id !== item.id) }))); setSelected(null); setDeleting(false); }}>确认删除设定</button><button className="text-button" onClick={() => setDeleting(false)}>保留设定</button></div> : <button className="text-button danger-text" onClick={() => setDeleting(true)}>删除此设定</button>}</fieldset> : <div className="setting-empty"><h3>选择一条设定</h3><p>在列表中打开条目，或新增一条。所有修改由下方「保存资料」统一保存。</p></div>}</div>
      </div>
    </>}
  </div>;
}

export function PlotStateGuide() {
  return <aside className="plot-state-guide" aria-label="当前剧情填写提示"><h3>记录故事进行到此刻的状态</h3><dl><div><dt>时间与地点</dt><dd>目前处于哪个年代、哪一天、哪个地点；倒叙时写清叙事位置。</dd></div><div><dt>已发生与待解决</dt><dd>当前行动、悬念与未解线索。计划、猜测和已发生事实分开写。</dd></div><div><dt>人物知道什么</dt><dd>标明尚未揭晓的真相；角色自身的已知信息在「角色」单独维护。</dd></div></dl><p className="field-hint">长期成立的规则放在世界书；已发生的主要故事放在作品摘要。改稿后，请同步检查这里是否仍然准确。</p></aside>;
}
