import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

type Memory = {
  id: string; content: string; kind: string; basis: string; project_id: string | null;
  allow_chat: boolean; allow_writing: boolean; source_message_id: string | null; source_task_id: string | null;
  source_deleted: boolean; created_at: string; updated_at: string;
  life_kind?: 'event' | 'dream' | null; occurred_at?: string | null; shared_at?: string | null;
  source_chapter_id?: string | null; source_version?: number | null;
}
const stamp = '2026-10-23T06:00:00Z'

async function stubMemory(page: Page) {
  const state = {
    memories: [{ id: 'memory-1', content: '喜欢在下雨的晚上读书。', kind: 'preference', basis: '', project_id: null, allow_chat: true, allow_writing: true, source_message_id: null, source_task_id: 'chat-task-1', source_deleted: false, created_at: stamp, updated_at: stamp }] as Memory[],
    saveFails: false,
    saves: [] as Record<string, unknown>[],
    queries: [] as string[],
    deprecatedRequests: [] as string[],
  }
  await page.route('**/api/**', async route => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname.replace('/api', '')
    const body = req.postData() ? req.postDataJSON() : {}
    if (path === '/auth/me') return route.fulfill({ json: { id: 'synthetic-reader', username: '合成读者' } })
    if (path === '/setup') return route.fulfill({ json: { qq_id: '700000099', initialized: true, persona: { name: '测试小莎', prompt: '只用于合成浏览器测试。', version: 1 }, presets: [] } })
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务已就绪。' } })
    if (path === '/projects') return route.fulfill({ json: [] })
    if (path === '/memories' && req.method() === 'GET') {
      state.queries.push(url.search)
      const items = state.memories.filter(item => item.content.includes(url.searchParams.get('query') || '') && (!url.searchParams.get('kind') || item.kind === url.searchParams.get('kind')))
      if (url.searchParams.get('kind') === 'life') items.sort((a, b) => (b.occurred_at ?? '').localeCompare(a.occurred_at ?? ''))
      const offset = Number(url.searchParams.get('offset') || 0)
      return route.fulfill({ json: items.slice(offset, offset + Number(url.searchParams.get('limit') || 100)) })
    }
    if (path === '/memory-map' && req.method() === 'GET') {
      const items = state.memories.filter(item => item.content.includes(url.searchParams.get('query') || '') && (!url.searchParams.get('kind') || item.kind === url.searchParams.get('kind')))
      return route.fulfill({ json: { nodes: items, edges: items.length > 1 ? [{ source: items[0].id, target: items[1].id, type: 'semantic' }] : [], semantic_enabled: true } })
    }
    if (path === '/memories' && req.method() === 'POST') {
      if (state.saveFails) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请重试。' } })
      state.saves.push(body)
      const item = { ...body, id: `memory-${state.memories.length + 1}`, source_message_id: null, source_task_id: null, source_deleted: false, created_at: stamp, updated_at: stamp }
      state.memories.unshift(item)
      return route.fulfill({ status: 201, json: item })
    }
    const source = path.match(/^\/memories\/([^/]+)\/source$/)
    if (source) {
      const item = state.memories.find(item => item.id === source[1])!
      if (item.source_chapter_id) return route.fulfill({ json: { status: 'chapter', chapter: { id: item.source_chapter_id, title: '十月二十二日的梦', version: item.source_version, content: '昨夜做了一个梦，旧书店里长出了一棵银杏树。这是采用时的第二版。' } } })
      return route.fulfill({ json: { status: item.source_task_id ? 'agent' : 'manual', message: null } })
    }
    const memory = path.match(/^\/memories\/([^/]+)$/)
    if (memory) {
      if (state.saveFails) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请重试。' } })
      if (req.method() === 'DELETE') {
        expect(req.headers()['x-lisa-request']).toBe('1')
        state.memories = state.memories.filter(item => item.id !== memory[1])
        return route.fulfill({ status: 204 })
      }
      state.saves.push(body)
      const previous = state.memories.find(item => item.id === memory[1])!
      const item = { ...previous, ...body, shared_at: previous.kind === 'life' && (body.content !== previous.content || body.life_kind !== previous.life_kind || body.occurred_at !== previous.occurred_at) ? null : previous.shared_at }
      state.memories = state.memories.map(current => current.id === item.id ? item : current)
      return route.fulfill({ json: item })
    }
    if (path.startsWith('/chat-messages')) {
      state.deprecatedRequests.push(path)
      return route.fulfill({ status: 410, json: { detail: 'LISA 不再保存或提供原始聊天记录。' } })
    }
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic route: ${path}` } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page.getByText('喜欢在下雨的晚上读书。', { exact: true })).toBeVisible()
  return state
}

for (const width of [1440, 1024, 390]) {
  test(`memory list, map and empty search keep the same navigation width at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1200 });
    await stubMemory(page);
    const tabs = page.getByRole('tablist', { name: '记忆呈现方式' });
    const original = (await tabs.boundingBox())!;
    await tabs.getByRole('tab', { name: '记忆地图' }).click();
    await expect(page.getByRole('heading', { name: '记忆地图', exact: true })).toBeVisible();
    expect(Math.abs((await tabs.boundingBox())!.width - original.width)).toBeLessThan(1);
    expect(Math.abs((await tabs.boundingBox())!.y - original.y)).toBeLessThan(1);
    await page.getByLabel('搜索记忆', { exact: true }).fill('没有这条记忆');
    await page.getByRole('button', { name: '搜索', exact: true }).click();
    await expect(page.getByRole('heading', { name: '没有符合条件的记忆', exact: true })).toBeVisible();
    expect(Math.abs((await tabs.boundingBox())!.width - original.width)).toBeLessThan(1);
    await tabs.getByRole('tab', { name: '列表', exact: true }).click();
    await expect(page.getByRole('heading', { name: '没有符合条件的记录', exact: true })).toBeVisible();
    expect(Math.abs((await tabs.boundingBox())!.y - original.y)).toBeLessThan(1);
    expect(await page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  });
}

test('memory map is a read-only visual view and opens the existing editor', async ({ page }) => {
  await stubMemory(page)
  await page.getByRole('tab', { name: '记忆地图' }).click()
  await expect(page.getByRole('heading', { name: '记忆地图' })).toBeVisible()
  await expect(page.getByText('1 条记忆', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: /喜欢在下雨的晚上读书/ }).click()
  await expect(page.getByLabel('记忆内容', { exact: true })).toHaveValue('喜欢在下雨的晚上读书。')
})

test('memory correction and use restrictions require save, failed leave preserves input, and source remains viewable', async ({ page }, testInfo) => {
  const state = await stubMemory(page)
  await page.getByRole('button', { name: '查看来源' }).click()
  await expect(page.getByRole('dialog')).toContainText('聊天 Agent 根据当轮内容主动提升')
  await expect(page.getByRole('dialog')).toContainText('原始聊天不会被保存')
  await page.getByRole('button', { name: '关闭来源' }).click()
  await page.screenshot({ path: testInfo.outputPath('desktop-memory-list.png'), fullPage: true })
  await page.getByRole('button', { name: '纠正与用途' }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('喜欢在雨天傍晚读散文，不喜欢晚上熬夜。')
  await page.getByLabel('作品创作', { exact: true }).uncheck()
  expect(state.memories[0].allow_writing).toBe(true)
  await page.getByRole('button', { name: '个人设置' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  state.saveFails = true
  await page.getByRole('button', { name: '返回列表', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByLabel('记忆内容', { exact: true })).toHaveValue('喜欢在雨天傍晚读散文，不喜欢晚上熬夜。')
  await expect(page.getByLabel('作品创作', { exact: true })).not.toBeChecked()
  state.saveFails = false
  await page.getByLabel('记忆内容', { exact: true }).press('Control+s')
  await expect.poll(() => state.memories[0].allow_writing).toBe(false)
  await expect(page.getByText('已保存', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('desktop-memory-editor.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-memory-editor.png'), fullPage: true })
  await page.getByRole('button', { name: '返回列表', exact: true }).click()
  await page.getByRole('button', { name: '查看来源' }).click()
  await expect(page.getByRole('dialog')).toContainText('原始聊天不会被保存')
  expect(state.memories[0].source_task_id).toBe('chat-task-1')
  expect(state.memories[0].source_message_id).toBeNull()
  await page.getByRole('button', { name: '关闭来源' }).click()
  await page.getByLabel('搜索记忆').fill('不存在的条目')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.getByRole('heading', { name: '没有符合条件的记录' })).toBeVisible()
  await page.getByLabel('搜索记忆').fill('雨天')
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  await expect(page.getByText('喜欢在雨天傍晚读散文，不喜欢晚上熬夜。', { exact: true })).toBeVisible()
  await page.getByLabel('类型筛选').selectOption('fact')
  await expect(page.getByRole('heading', { name: '没有符合条件的记录' })).toBeVisible()
  expect(state.queries.some(query => query.includes('kind=fact'))).toBe(true)
})

test('memory deletion confirms scope, preserves other memories, and never offers raw chat history', async ({ page }, testInfo) => {
  const state = await stubMemory(page)
  state.memories.push({ ...state.memories[0], id: 'memory-independent', content: '手动留下的另一条记忆。', source_task_id: null })
  await page.getByRole('button', { name: '搜索', exact: true }).click()
  const entry = page.locator('.memory-entry').filter({ hasText: '喜欢在下雨的晚上读书。' })
  await expect(page.getByRole('button', { name: '原始聊天', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '记为记忆', exact: true })).toHaveCount(0)
  await expect(page.getByText('按时间范围清理聊天', { exact: true })).toHaveCount(0)
  await entry.getByRole('button', { name: '删除记忆', exact: true }).click()
  const confirmation = page.getByRole('dialog', { name: '删除这条记忆？' })
  await expect(confirmation).toContainText('不会从聊天记录重新提取它')
  await expect(confirmation).toContainText('已有作品需另行修改')
  await confirmation.getByRole('button', { name: '取消', exact: true }).click()
  expect(state.memories).toHaveLength(2)
  await entry.getByRole('button', { name: '删除记忆', exact: true }).click()
  state.saveFails = true
  await confirmation.getByRole('button', { name: '确认删除记忆', exact: true }).click()
  await expect(confirmation.getByRole('alert')).toContainText('合成保存失败')
  expect(state.memories).toHaveLength(2)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-memory-deletion.png'), fullPage: true })
  state.saveFails = false
  await confirmation.getByRole('button', { name: '确认删除记忆', exact: true }).click()
  await expect(entry).toHaveCount(0)
  const independent = page.locator('.memory-entry').filter({ hasText: '手动留下的另一条记忆。' })
  await expect(independent).toContainText('手动添加')
  await expect(independent.getByRole('button', { name: '查看来源' })).toHaveCount(0)
  expect(state.memories).toHaveLength(1)
  await independent.getByRole('button', { name: '删除记忆', exact: true }).click()
  await confirmation.getByRole('button', { name: '确认删除记忆', exact: true }).click()
  await expect(page.getByRole('heading', { name: '还没有留下记忆' })).toBeVisible()
  expect(state.deprecatedRequests).toEqual([])
})

test('manual inference needs its basis, unsaved edits can be discarded, and new memories have manual provenance', async ({ page }) => {
  const state = await stubMemory(page)
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('也许偏爱安静的叙事。')
  await page.getByLabel('记忆类型', { exact: true }).selectOption('inference')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  expect(state.saves).toHaveLength(0)
  await page.getByLabel('推测依据', { exact: true }).fill('多次选择节奏舒缓的散文，尚未明确确认。')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await expect.poll(() => state.memories.length).toBe(2)
  expect(state.memories[0]).toMatchObject({ kind: 'inference', allow_writing: false, source_message_id: null, source_task_id: null })
  await page.getByLabel('记忆内容', { exact: true }).fill('不会保存的改动。')
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByRole('button', { name: '创建小说', exact: true })).toBeVisible()
  expect(state.memories[0].content).toBe('也许偏爱安静的叙事。')
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  const manual = page.locator('.memory-entry').filter({ hasText: '也许偏爱安静的叙事。' })
  await expect(manual).toContainText('手动添加')
  await expect(manual.getByRole('button', { name: '查看来源' })).toHaveCount(0)
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('个人喜好：雨天读散文。')
  await page.getByRole('button', { name: '返回列表', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => state.memories.length).toBe(3)
  expect(state.memories[0]).toMatchObject({ source_message_id: null, source_task_id: null })
  await expect(page.getByText('个人喜好：雨天读散文。', { exact: true })).toBeVisible()
  expect(state.deprecatedRequests).toEqual([])
})

test('life timeline keeps dreams distinct, displays exact diary source version and saves corrected time without claiming prior sharing', async ({ page }, testInfo) => {
  const state = await stubMemory(page)
  state.memories.push(
    { ...state.memories[0], id: 'life-dream', kind: 'life', life_kind: 'dream', occurred_at: '2026-10-22T05:00:00Z', shared_at: null, content: '梦见旧书店里长出一棵银杏树。', source_message_id: null, source_task_id: null, source_chapter_id: 'diary-chapter', source_version: 2 },
    { ...state.memories[0], id: 'life-event', kind: 'life', life_kind: 'event', occurred_at: '2026-10-23T05:00:00Z', shared_at: stamp, content: '下午在旧书店买了一本诗集。', source_message_id: null, source_task_id: null },
  )
  await page.getByLabel('类型筛选').selectOption('life')
  await expect(page.getByText('虚拟生活时间线', { exact: false })).toContainText('按发生时间由近到远排列')
  const entries = page.locator('.memory-entry')
  await expect(entries).toHaveCount(2)
  await expect(entries.first()).toContainText('下午在旧书店买了一本诗集。')
  await expect(entries.first()).toContainText('已分享')
  const dream = entries.filter({ hasText: '梦见旧书店里长出一棵银杏树。' })
  await expect(dream).toContainText('做过的梦')
  await expect(dream).toContainText('尚无成功分享记录')
  await expect(dream).toContainText('来自日记 · 版本 2')
  await page.screenshot({ path: testInfo.outputPath('desktop-life-timeline.png'), fullPage: true })
  await dream.getByRole('button', { name: '查看来源', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('十月二十二日的梦 · 版本 2')
  await expect(page.getByRole('dialog')).toContainText('这是采用时的第二版')
  await expect(page.getByRole('dialog')).toContainText('之后的改稿不会自动改写这条记忆')
  await page.getByRole('button', { name: '关闭来源', exact: true }).click()
  await dream.getByRole('button', { name: '纠正与用途', exact: true }).click()
  await expect(page.getByLabel('作用范围', { exact: true })).toBeDisabled()
  await expect(page.getByLabel('生活性质', { exact: true })).toHaveValue('dream')
  await page.getByLabel('发生时间', { exact: true }).fill('2026-10-21T23:30')
  await page.getByLabel('作品创作', { exact: true }).uncheck()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-life-editor.png'), fullPage: true })
  await page.getByRole('button', { name: '返回列表', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(dream).toBeVisible()
  const updated = state.memories.find(item => item.id === 'life-dream')!
  expect(updated).toMatchObject({ life_kind: 'dream', project_id: null, allow_writing: false, source_version: 2, shared_at: null })
  expect(updated.occurred_at).toMatch(/Z$/)
  expect(updated.occurred_at).not.toBe('2026-10-22T05:00:00Z')
  await entries.filter({ hasText: '下午在旧书店买了一本诗集。' }).getByRole('button', { name: '纠正与用途', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('下午在旧书店买了一本散文集。')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await expect(page.locator('.life-sharing')).toContainText('尚无成功分享记录')
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('在窗边整理刚买的书。')
  await page.getByLabel('记忆类型', { exact: true }).selectOption('life')
  const saves = state.saves.length
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  expect(state.saves).toHaveLength(saves)
  await page.getByLabel('发生时间', { exact: true }).fill('2026-10-23T17:00')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await expect.poll(() => state.saves.length).toBe(saves + 1)
  expect(state.saves.at(-1)).toMatchObject({ kind: 'life', life_kind: 'event', project_id: null })
})
