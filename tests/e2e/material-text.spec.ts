import { expect, test } from '@playwright/test'
import { personaFields, plotFields, readItems, readSections, writeItems, writeSections } from '../../web/material-text'

test('structured material text round trips headings, whitespace and legacy source without interpretation', () => {
  const original = ' 原有文字\n\n## 这仍是正文\n有些规则包含多行。\n'
  expect(readSections(original, plotFields)).toEqual({ legacy: original, values: plotFields.map(() => '') })
  expect(readItems(original, '规则')).toEqual({ legacy: original, values: [] })
  for (const content of ['第一行\n第二行\n\n', '## 当前时间\n这不是新的分类', '\\## 标题\n保留反斜杠', '## 规则 2\n这一行属于正文', '  保留空格  ', '']) {
    const parts = { legacy: original, values: [content, '另一条\n\n', '', '', '提醒'] }
    expect(readSections(writeSections(parts, plotFields), plotFields)).toEqual(parts)
    expect(readSections(writeSections(parts, plotFields).trim(), plotFields)).toEqual(parts)
    const items = { legacy: original, values: [content, '', '另一条'] }
    expect(readItems(writeItems(items, '规则'), '规则')).toEqual(items)
    expect(readItems(writeItems(items, '规则').trim(), '规则')).toEqual(items)
  }
  expect(readItems(writeItems({ legacy: '', values: [''] }, '约束'), '约束')).toEqual({ legacy: '', values: [''] })
  expect(writeSections({ legacy: original, values: personaFields.map(() => '') }, personaFields)).toBe(original)
  expect(writeItems({ legacy: original, values: [] }, '规则')).toBe(original)
  expect(readSections('## 当前时间\n只有一个手写标题', plotFields).legacy).toBe('## 当前时间\n只有一个手写标题')
})
