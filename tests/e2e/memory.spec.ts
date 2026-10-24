import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

type Memory = {
  id: string; content: string; kind: string; basis: string; project_id: string | null;
  allow_chat: boolean; allow_writing: boolean; source_message_id: string | null;
  source_deleted: boolean; created_at: string; updated_at: string;
  life_kind?: 'event' | 'dream' | null; occurred_at?: string | null; shared_at?: string | null;
  source_chapter_id?: string | null; source_version?: number | null;
}
type Chat = { id: string; role: string; content: string; sent_at: string; context_excluded: boolean }
const stamp = '2026-10-23T06:00:00Z'

async function stubMemory(page: Page) {
  const state = {
    memories: [{ id: 'memory-1', content: '喜欢在下雨的晚上读书。', kind: 'preference', basis: '', project_id: null, allow_chat: true, allow_writing: true, source_message_id: 'chat-1', source_deleted: false, created_at: stamp, updated_at: stamp }] as Memory[],
    chats: [{ id: 'chat-1', role: 'user', content: '雨天晚上我喜欢读散文，这只是我的个人喜好。', sent_at: stamp, context_excluded: false }, { id: 'chat-2', role: 'assistant', content: '下次可以聊聊你最近读到的段落。', sent_at: '2026-10-23T06:01:00Z', context_excluded: false }] as Chat[],
    saveFails: false,
    saves: [] as Record<string, unknown>[],
    clearBodies: [] as { start: string; end: string }[],
    queries: [] as string[],
  }
  function deleteChat(id: string) {
    state.chats = state.chats.filter(item => item.id !== id)
    state.memories = state.memories.map(item => item.source_message_id === id ? { ...item, source_deleted: true } : item)
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
    if (path === '/memories' && req.method() === 'POST') {
      if (state.saveFails) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请重试。' } })
      state.saves.push(body)
      const item = { ...body, id: `memory-${state.memories.length + 1}`, source_deleted: false, created_at: stamp, updated_at: stamp }
      state.memories.unshift(item)
      return route.fulfill({ status: 201, json: item })
    }
    const source = path.match(/^\/memories\/([^/]+)\/source$/)
    if (source) {
      const item = state.memories.find(item => item.id === source[1])!
      if (item.source_chapter_id) return route.fulfill({ json: { status: 'chapter', chapter: { id: item.source_chapter_id, title: '十月二十二日的梦', version: item.source_version, content: '昨夜做了一个梦，旧书店里长出了一棵银杏树。这是采用时的第二版。' } } })
      return route.fulfill({ json: { status: item.source_deleted ? 'deleted' : 'available', message: state.chats.find(message => message.id === item.source_message_id) ?? null } })
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
      state.chats = state.chats.map(message => message.id === item.source_message_id ? { ...message, context_excluded: true } : message)
      return route.fulfill({ json: item })
    }
    if (path === '/chat-messages') return route.fulfill({ json: state.chats.filter(item => item.content.includes(url.searchParams.get('query') || '')) })
    if (path === '/chat-messages/clear') {
      state.clearBodies.push(body)
      const removed = state.chats.filter(item => item.sent_at >= body.start && item.sent_at < body.end)
      for (const item of removed) deleteChat(item.id)
      return route.fulfill({ json: { deleted_count: removed.length } })
    }
    const chat = path.match(/^\/chat-messages\/([^/]+)$/)
    if (chat && req.method() === 'DELETE') {
      expect(req.headers()['x-lisa-request']).toBe('1')
      deleteChat(chat[1]); return route.fulfill({ status: 204 })
    }
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic route: ${path}` } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await expect(page.getByText('喜欢在下雨的晚上读书。', { exact: true })).toBeVisible()
  return state
}

test('memory correction and use restrictions require save, failed leave preserves input, and source remains viewable', async ({ page }, testInfo) => {
  const state = await stubMemory(page)
  await page.getByRole('button', { name: '查看来源' }).click()
  await expect(page.getByRole('dialog')).toContainText('雨天晚上我喜欢读散文')
  await page.getByRole('button', { name: '关闭来源' }).click()
  await page.screenshot({ path: testInfo.outputPath('desktop-memory-list.png'), fullPage: true })
  await page.getByRole('button', { name: '纠正与用途' }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('喜欢在雨天傍晚读散文，不喜欢晚上熬夜。')
  await page.getByLabel('作品创作', { exact: true }).uncheck()
  expect(state.memories[0].allow_writing).toBe(true)
  await page.getByRole('button', { name: '个人设置' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  state.saveFails = true
  await page.getByRole('button', { name: '原始聊天', exact: true }).click()
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
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-memory-editor.png'), fullPage: true })
  await page.getByRole('button', { name: '返回列表', exact: true }).click()
  await page.getByRole('button', { name: '查看来源' }).click()
  await expect(page.getByRole('dialog')).toContainText('这段原话仅供查看，不再作为模型依据')
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

test('source deletion and range clearing preserve independent memories and explain impact before deletion', async ({ page }, testInfo) => {
  const state = await stubMemory(page)
  await page.getByRole('button', { name: '原始聊天', exact: true }).click()
  await expect(page.getByText('雨天晚上我喜欢读散文，这只是我的个人喜好。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '删除原话', exact: true }).first().click()
  await expect(page.getByRole('dialog')).toContainText('独立记忆与已有作品保留')
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click()
  expect(state.chats).toHaveLength(2)
  await page.getByRole('button', { name: '删除原话', exact: true }).first().click()
  await page.getByRole('button', { name: '确认删除原话', exact: true }).click()
  await expect.poll(() => state.chats.length).toBe(1)
  await page.getByRole('button', { name: '记忆条目', exact: true }).click()
  await expect(page.getByText('原始聊天已删除', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '查看来源' })).toHaveCount(0)
  expect(state.memories).toHaveLength(1)
  await page.getByRole('button', { name: '原始聊天', exact: true }).click()
  await page.getByText('按时间范围清理聊天', { exact: true }).click()
  await page.getByLabel('开始时间（含）').fill('2026-10-23T00:00')
  await page.getByLabel('结束时间（不含）').fill('2026-10-24T00:00')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-chat-cleanup.png'), fullPage: true })
  await page.getByRole('button', { name: '清理此时间段', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('清理整个时间段，不受搜索条件限制')
  expect(state.clearBodies).toHaveLength(0)
  await page.getByRole('button', { name: '确认清理聊天', exact: true }).click()
  await expect(page.getByText('已清理 1 条聊天原文。独立记忆与已有作品保留。', { exact: true })).toBeVisible()
  expect(state.clearBodies[0].start).toMatch(/Z$/)
  expect(state.memories).toHaveLength(1)
  await page.getByRole('button', { name: '记忆条目', exact: true }).click()
  await page.getByRole('button', { name: '删除记忆', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('不会从原话自动恢复这条记忆')
  await page.getByRole('button', { name: '确认删除记忆', exact: true }).click()
  await expect(page.getByRole('heading', { name: '还没有留下记忆' })).toBeVisible()
})

test('manual inference needs its basis and unsaved memory can be discarded; chat can explicitly become a sourced memory', async ({ page }) => {
  const state = await stubMemory(page)
  await page.getByRole('button', { name: '添加记忆', exact: true }).click()
  await page.getByLabel('记忆内容', { exact: true }).fill('也许偏爱安静的叙事。')
  await page.getByLabel('记忆类型', { exact: true }).selectOption('inference')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  expect(state.saves).toHaveLength(0)
  await page.getByLabel('推测依据', { exact: true }).fill('多次选择节奏舒缓的散文，尚未明确确认。')
  await page.getByRole('button', { name: '保存记忆', exact: true }).click()
  await expect.poll(() => state.memories.length).toBe(2)
  expect(state.memories[0]).toMatchObject({ kind: 'inference', allow_writing: false, source_message_id: null })
  await page.getByLabel('记忆内容', { exact: true }).fill('不会保存的改动。')
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByRole('button', { name: '创建小说', exact: true })).toBeVisible()
  expect(state.memories[0].content).toBe('也许偏爱安静的叙事。')
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await page.getByRole('button', { name: '原始聊天', exact: true }).click()
  await page.getByRole('button', { name: '记为记忆', exact: true }).first().click()
  await expect(page.getByLabel('记忆内容', { exact: true })).toHaveValue('雨天晚上我喜欢读散文，这只是我的个人喜好。')
  await page.getByLabel('记忆内容', { exact: true }).fill('个人喜好：雨天读散文。')
  await page.getByRole('button', { name: '原始聊天', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => state.memories.length).toBe(3)
  expect(state.memories[0].source_message_id).toBe('chat-1')
  await expect(page.getByText('雨天晚上我喜欢读散文，这只是我的个人喜好。', { exact: true })).toBeVisible()
})

test('life timeline keeps dreams distinct, displays exact diary source version and saves corrected time without claiming prior sharing', async ({ page }, testInfo) => {
  const state = await stubMemory(page)
  state.memories.push(
    { ...state.memories[0], id: 'life-dream', kind: 'life', life_kind: 'dream', occurred_at: '2026-10-22T05:00:00Z', shared_at: null, content: '梦见旧书店里长出一棵银杏树。', source_message_id: null, source_chapter_id: 'diary-chapter', source_version: 2 },
    { ...state.memories[0], id: 'life-event', kind: 'life', life_kind: 'event', occurred_at: '2026-10-23T05:00:00Z', shared_at: stamp, content: '下午在旧书店买了一本诗集。', source_message_id: null },
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
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
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
