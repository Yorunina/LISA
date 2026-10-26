import { useId, useState } from 'react';

export type TextField = { title: string; hint: string; placeholder: string };
type TextParts = { legacy: string; values: string[] };
const legacyTitle = '原有文字';
// Store ordinary, model-readable Markdown through the existing text APIs.
// Only our exact, reversible layout is recognized; other text stays untouched.
const escapeText = (text: string) => text.replace(/^(\\*## )/gm, '\\$1');
const unescapeText = (text: string) => text.replace(/^\\(\\*## )/gm, '$1');

function encode(titles: string[], parts: TextParts, keepEmpty = false): string {
  if (!keepEmpty && !parts.values.some(value => value.length)) return parts.legacy;
  return [...(parts.legacy ? [[legacyTitle, parts.legacy]] : []), ...titles.map((title, i) => [title, parts.values[i] ?? ''])]
    .map(([title, text]) => `## ${title}\n${escapeText(text)}`).join('\n\n') + '\n\n---';
}

function decode(text: string, titles: string[], keepEmpty = false): TextParts {
  const fallback = { legacy: text, values: titles.map(() => '') };
  if (!text.endsWith('\n\n---')) return fallback;
  const source = text.slice(0, -5);
  const headings = [...source.matchAll(/^## (.+)\n/gm)];
  if (!headings.length || headings[0].index !== 0) return fallback;
  const hasLegacy = headings[0][1] === legacyTitle;
  const expected = hasLegacy ? [legacyTitle, ...titles] : titles;
  if (headings.length !== expected.length || headings.some((item, i) => item[1] !== expected[i])) return fallback;
  const values = headings.map((item, i) => unescapeText(source.slice(item.index! + item[0].length, i + 1 < headings.length ? headings[i + 1].index! - 2 : undefined)));
  const parts = { legacy: hasLegacy ? values[0] : '', values: hasLegacy ? values.slice(1) : values };
  return encode(titles, parts, keepEmpty) === text ? parts : fallback;
}

export const readSections = (text: string, fields: TextField[]) => decode(text, fields.map(field => field.title));
export const writeSections = (parts: TextParts, fields: TextField[]) => encode(fields.map(field => field.title), parts);

export function readItems(text: string, singular: string): TextParts {
  const headings = [...text.matchAll(/^## (.+)\n/gm)];
  const count = headings.length - (headings[0]?.[1] === legacyTitle ? 1 : 0);
  const parts = count > 0 ? decode(text, Array.from({ length: count }, (_, i) => `${singular} ${i + 1}`), true) : null;
  return parts && parts.legacy !== text ? parts : { legacy: text, values: [] };
}
export const writeItems = (parts: TextParts, singular: string) => parts.values.length
  ? encode(parts.values.map((_, i) => `${singular} ${i + 1}`), parts, true) : parts.legacy;

function LegacyText({ text, label, busy, onChange }: { text: string; label: string; busy: boolean; onChange: (text: string) => void }) {
  const [open, setOpen] = useState(!!text);
  const id = useId();
  return <details className="material-legacy" open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>原有文字与补充说明</summary><p className="field-hint">原文完整保留。可在这里编辑，再按需要手动整理到上面的字段；不会自动拆分、删除或提升为事实。</p><label htmlFor={id}>{label}</label><textarea id={id} rows={5} value={text} disabled={busy} onChange={event => onChange(event.target.value)} /></details>;
}

export function SectionTextEditor({ id, value, fields, rawLabel, busy, onChange, maxLength = 12000 }: { id: string; value: string; fields: TextField[]; rawLabel: string; busy: boolean; onChange: (text: string) => void; maxLength?: number }) {
  const parts = readSections(value, fields);
  const change = (next: TextParts) => onChange(writeSections(next, fields));
  return <div className="section-text-editor">{fields.map((field, index) => <div className="field" key={field.title}><label htmlFor={`${id}-${index}`}>{field.title}</label><p className="field-hint" id={`${id}-${index}-hint`}>{field.hint}</p><textarea id={`${id}-${index}`} aria-describedby={`${id}-${index}-hint`} rows={3} maxLength={maxLength} placeholder={field.placeholder} disabled={busy} value={parts.values[index]} onChange={event => change({ ...parts, values: parts.values.map((text, i) => i === index ? event.target.value : text) })} /></div>)}<LegacyText text={parts.legacy} label={rawLabel} busy={busy} onChange={legacy => change({ ...parts, legacy })} /><p className="field-hint">{value.length} / {maxLength} 字符（含原有文字与分类标题）。统一保存后生效。</p></div>;
}

export function ItemTextEditor({ value, title, singular, hint, placeholder, rawLabel, busy, onChange, maxLength = 12000 }: { value: string; title: string; singular: string; hint: string; placeholder: string; rawLabel: string; busy: boolean; onChange: (text: string) => void; maxLength?: number }) {
  const parts = readItems(value, singular);
  const id = useId();
  const change = (next: TextParts) => onChange(writeItems(next, singular));
  return <section className="item-text-editor" aria-label={`${singular}列表`}><div className="item-text-heading"><h3>{title}</h3><button type="button" className="secondary" disabled={busy || parts.values.length >= 80} onClick={() => change({ ...parts, values: [...parts.values, ''] })}>新增{singular}</button></div><p className="field-hint">{hint}</p>{!parts.values.length && <p className="field-hint">尚未添加独立条目，可从一条{singular}开始。</p>}{parts.values.map((text, index) => <div className="material-text-item" key={index}><label htmlFor={`${id}-${index}`}>{singular} {index + 1}</label><textarea id={`${id}-${index}`} rows={3} maxLength={maxLength} placeholder={placeholder} value={text} disabled={busy} onChange={event => change({ ...parts, values: parts.values.map((content, i) => i === index ? event.target.value : content) })} /><button type="button" className="text-button danger-text" disabled={busy} onClick={() => change({ ...parts, values: parts.values.filter((_, i) => i !== index) })}>移除{singular} {index + 1}</button></div>)}<LegacyText text={parts.legacy} label={rawLabel} busy={busy} onChange={legacy => change({ ...parts, legacy })} /><p className="field-hint">{value.length} / {maxLength} 字符。移除只修改当前表单，统一保存后生效。</p></section>;
}

export const plotFields: TextField[] = [
  { title: '当前时间', hint: '故事此刻的年代、日期；倒叙时标明叙事位置。', placeholder: '例如：旧历 319 年，初冬傍晚。' },
  { title: '当前地点', hint: '正在发生这一幕的地点。', placeholder: '例如：北境车站，候车厅。' },
  { title: '正在发生的事', hint: '只记录目前已发生或正在进行的行动。', placeholder: '例如：主角刚收到信，尚未拆开。' },
  { title: '未解线索与悬念', hint: '写清仍未知的问题，猜测要标明不确定。', placeholder: '例如：寄信人身份未知；邮戳似乎来自旧城区。' },
  { title: '人物知情提醒', hint: '只提醒作者注意知识边界；角色实际已知信息仍在角色页维护。', placeholder: '例如：沈舟还不知道主角已收到信。' },
];
export const writingFields: TextField[] = [
  { title: '叙述视角', hint: '补充所选风格方案没有说明的视角要求。', placeholder: '例如：第三人称限知，只写主角能感知的事。' },
  { title: '语言与节奏', hint: '描述句式、描写密度与推进速度。', placeholder: '例如：句子简洁，让冲突在短场景里慢慢浮现。' },
  { title: '对白写法', hint: '人物怎么说话、如何表达情绪。', placeholder: '例如：少解释情绪，用停顿与动作表现。' },
  { title: '避免的写法', hint: '写法限制放在这里；剧情硬性约束放在创作要求。', placeholder: '例如：避免用总结句反复解释同一段情绪。' },
];
export const personaFields: TextField[] = [
  { title: '核心性格与价值倾向', hint: '她稳定的性格、看重的事和面对分歧的态度。', placeholder: '例如：好奇，有自己的判断，愿意认真讨论不同意见。' },
  { title: '虚拟身份与生活背景', hint: '这是虚拟设定，不会自动写入已发生的生活经历。', placeholder: '例如：喜欢旧书与散步，生活在临河的小城。' },
  { title: '交流与相处方式', hint: '称呼、语气、表达长短与幽默习惯。', placeholder: '例如：自然地接话，少用固定夸赞，不每轮都追问。' },
  { title: '相处边界', hint: '希望她尊重的个人边界与表达习惯。', placeholder: '例如：不通过催促或施压要求回复。' },
];
