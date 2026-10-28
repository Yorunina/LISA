import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { itemOccupies, timeRange } from '../../web/life-plan-time'

type Item = { id: string; date: string; time: string; title: string; detail: string; status: string; source: string; source_message_id: null }
const item = (id: string, time: string, title: string): Item => ({ id, date: '2026-10-19', time, title, detail: '', status: 'tentative', source: 'agent', source_message_id: null })

async function stubPlan(page: Page, items = [item('morning', '09:30–10:30', '读一章小说'), item('night', '23:30–00:30', '夜间散步')]) {
  const state = {
    plan: { id: 'plan-1', week_start: '2026-10-19', week_end: '2026-10-25', state: 'active', revision: 1, updated_at: '2026-10-19T00:00:00Z', items },
    operations: [] as string[], failSave: false, failGenerate: false,
  }
  await page.route('**/api/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname.replace('/api', '')
    if (path === '/auth/me') return route.fulfill({ json: { id: 'plan-reader', username: '合成周表读者' } })
    if (path === '/setup') return route.fulfill({ json: { qq_id: '700000096', initialized: true, persona: { name: '测试小莎', prompt: '合成周表测试。', version: 1 }, presets: [] } })
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务已就绪。' } })
    if (path === '/projects' || path === '/memories') return route.fulfill({ json: [] })
    if (path === '/life-plan') return route.fulfill({ json: { current: state.plan, today: '2026-10-19' } })
    if (path === '/life-plan/plan-1' && req.method() === 'PUT') {
      state.operations.push('save')
      if (state.failSave) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请重试。' } })
      const body = req.postDataJSON()
      expect(body.revision).toBe(state.plan.revision)
      state.plan = { ...state.plan, ...body, revision: state.plan.revision + 1 }
      return route.fulfill({ json: state.plan })
    }
    if (path === '/life-plan/generate') {
      state.operations.push('generate')
      if (state.failGenerate) return route.fulfill({ status: 503, json: { detail: '合成生成失败。' } })
      // Keep user saves, mirroring the server's preservation of explicit edits.
      state.plan = { ...state.plan, revision: state.plan.revision + 1, items: [...state.plan.items.filter(item => item.source === 'user'), item('generated', '14:00–16:00', '整理书架')] }
      return route.fulfill({ json: state.plan })
    }
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic route: ${path}` } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '记忆', exact: true }).click()
  await page.getByRole('button', { name: '每周提案', exact: true }).click()
  await expect(page.getByRole('heading', { name: '每周虚拟生活提案' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: '选择计划日期' })).toBeVisible()
  return state
}

async function editMorning(page: Page, title: string) {
  await page.getByRole('button', { name: /09:30–10:30 读一章小说/ }).first().click()
  await page.getByLabel('提案', { exact: true }).fill(title)
  await page.getByRole('button', { name: '完成', exact: true }).click()
}

for (const width of [1440, 1024, 390]) {
  test(`memory tabs stay in place across short and long panels at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1200 });
    await stubPlan(page);
    const nav = page.getByRole('navigation', { name: '记忆页面' });
    const planBox = (await nav.boundingBox())!;
    await nav.getByRole('button', { name: '记忆条目', exact: true }).click();
    await expect(page.getByRole('heading', { name: '记忆与日常', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: '还没有留下记忆', exact: true })).toBeVisible();
    const memoryBox = (await nav.boundingBox())!;
    await page.screenshot({ path: info.outputPath(`memory-${width}.png`), fullPage: true });
    expect(Math.abs(memoryBox.y - planBox.y)).toBeLessThan(1);
    expect(Math.abs(memoryBox.x - planBox.x)).toBeLessThan(1);
    expect(Math.abs(memoryBox.width - planBox.width)).toBeLessThan(1);
    await nav.getByRole('button', { name: '每周提案', exact: true }).click();
    await expect(page.getByRole('heading', { name: '每周虚拟生活提案', exact: true })).toBeVisible();
    await expect(page.getByRole('navigation', { name: '选择计划日期' })).toBeVisible();
    const restoredBox = (await nav.boundingBox())!;
    await page.screenshot({ path: info.outputPath(`plan-${width}.png`), fullPage: true });
    expect(Math.abs(restoredBox.y - memoryBox.y)).toBeLessThan(1);
    expect(await page.locator('h1').count()).toBe(1);
    expect(await page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  });
}

test('minute ranges include partial slots and cross midnight without a DST offset', () => {
  expect(timeRange('09:30–10:30')).toEqual({ start: 570, end: 630 })
  expect(itemOccupies(item('a', '09:30–10:30', ''), '2026-10-19', 5)).toBe(true)
  expect(itemOccupies(item('a', '23:30–00:30', ''), '2026-10-20', 0)).toBe(true)
  expect(itemOccupies(item('a', '23:00', ''), '2026-10-20', 0)).toBe(true)
  expect(itemOccupies(item('a', '22:00–24:00', ''), '2026-10-20', 0)).toBe(false)
  expect(timeRange('稍后再定')).toBeNull()
})

test('regeneration protects edits, stops on failed save, and saves before generating', async ({ page }) => {
  const state = await stubPlan(page)
  await editMorning(page, '先读完正在看的小说')
  await page.getByRole('button', { name: '重新生成', exact: true }).click()
  const guard = page.getByRole('dialog', { name: '有未保存的周表修改' })
  await expect(guard).toBeVisible()
  expect(state.operations).toEqual([])
  await guard.getByRole('button', { name: '继续编辑' }).click()
  await expect(page.getByRole('button', { name: /先读完正在看的小说/ }).first()).toBeVisible()
  state.failSave = true
  await page.getByRole('button', { name: '重新生成', exact: true }).click()
  await guard.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  expect(state.operations).toEqual(['save'])
  await expect(page.getByRole('button', { name: /先读完正在看的小说/ }).first()).toBeVisible()
  state.failSave = false
  await page.getByRole('button', { name: '重新生成', exact: true }).click()
  await guard.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByText('已重新生成 LISA 的暂定提案。', { exact: true })).toBeVisible()
  expect(state.operations).toEqual(['save', 'save', 'generate'])
  expect(state.plan.items.some(item => item.title === '先读完正在看的小说')).toBe(true)
})

test('explicit discard allows regeneration and navigation still protects pending edits', async ({ page }) => {
  const state = await stubPlan(page)
  await editMorning(page, '尚未保存的提案')
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  await page.getByRole('dialog', { name: '有未保存的周表修改' }).getByRole('button', { name: '继续编辑' }).click()
  await page.getByRole('button', { name: '重新生成', exact: true }).click()
  await page.getByRole('dialog', { name: '有未保存的周表修改' }).getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByText('已重新生成 LISA 的暂定提案。', { exact: true })).toBeVisible()
  expect(state.operations).toEqual(['generate'])
  expect(state.plan.items.some(item => item.title === '尚未保存的提案')).toBe(false)
})

test('details trap focus, Escape restores focus, and Ctrl+S retains failed edits', async ({ page }, testInfo) => {
  const state = await stubPlan(page)
  const trigger = page.getByRole('button', { name: /09:30–10:30 读一章小说/ }).first()
  await trigger.click()
  const dialog = page.getByRole('dialog', { name: '提案详情' })
  await expect(page.getByLabel('日期', { exact: true })).toBeFocused()
  await dialog.getByRole('button', { name: '完成', exact: true }).focus()
  await page.keyboard.press('Tab')
  await expect(dialog.getByRole('button', { name: '关闭', exact: true })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(dialog.getByRole('button', { name: '完成', exact: true })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(trigger).toBeFocused()
  await trigger.click()
  await page.getByLabel('提案', { exact: true }).fill('用快捷键保存提案')
  state.failSave = true
  await page.getByLabel('提案', { exact: true }).press('Control+s')
  await expect(dialog.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByLabel('提案', { exact: true })).toHaveValue('用快捷键保存提案')
  state.failSave = false
  await page.getByLabel('提案', { exact: true }).press('Control+s')
  await expect(page.getByText('每周提案已保存。', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: /09:30–10:30 用快捷键保存提案/ }).first()).toBeFocused()
  await page.screenshot({ path: testInfo.outputPath('life-plan-desktop.png'), fullPage: true })
})

test('390px daily view keeps cross-day items and editor reachable without horizontal scrolling', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await stubPlan(page)
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(page.getByLabel('按日查看提案')).toBeVisible()
  await page.getByRole('navigation', { name: '选择计划日期' }).getByRole('button', { name: '周二 10/20', exact: true }).click()
  const overnight = page.getByRole('button', { name: /前一日延续.*夜间散步/ })
  await expect(overnight).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('life-plan-mobile.png'), fullPage: true })
  await overnight.click()
  await expect(page.getByLabel('时间段', { exact: true })).toHaveValue('23:30–00:30')
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('life-plan-mobile-dialog.png'), fullPage: true })
  await page.keyboard.press('Escape')
  await expect(overnight).toBeFocused()
})

test('single day shows each proposal once in time order and keeps overnight and flexible times', async ({ page }) => {
  await stubPlan(page, [item('night', '23:30–00:30', '夜间散步'), item('flexible', '稍后再定', '挑一部电影'), item('long', '08:00–18:00', '在图书馆度过一天'), item('early', '07:15–07:45', '早餐')])
  const monday = page.getByRole('region', { name: '按日查看提案', exact: true })
  await expect(monday.getByRole('listitem')).toHaveCount(4)
  await expect(monday.getByRole('listitem')).toHaveText(['07:15–07:45早餐›', '08:00–18:00在图书馆度过一天›', '23:30–00:30夜间散步›', '稍后再定挑一部电影›'])
  await expect(monday.getByRole('button', { name: /在图书馆度过一天/ })).toHaveCount(1)
  await page.getByRole('navigation', { name: '选择计划日期' }).getByRole('button', { name: '周二 10/20', exact: true }).click()
  const tuesday = monday
  await tuesday.getByRole('button', { name: /前一日延续.*夜间散步/ }).click()
  await expect(page.getByLabel('日期', { exact: true })).toHaveValue('2026-10-19')
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('navigation', { name: '选择计划日期' }).getByRole('button', { name: '周一 10/19', exact: true }).click()
  await expect(page.getByLabel('按日查看提案').getByRole('listitem')).toHaveCount(4)
  await page.getByRole('button', { name: /稍后再定.*挑一部电影/ }).click()
  await expect(page.getByLabel('时间段', { exact: true })).toHaveValue('稍后再定')
})

test('keyboard add uses its day, moving and deleting a card preserves explicit saving', async ({ page }) => {
  const state = await stubPlan(page)
  await page.getByRole('navigation', { name: '选择计划日期' }).getByRole('button', { name: '周三 10/21', exact: true }).click()
  const wednesday = page.getByRole('region', { name: '按日查看提案', exact: true })
  await wednesday.getByRole('button', { name: /添加提案/ }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByLabel('日期', { exact: true })).toHaveValue('2026-10-21')
  await page.getByLabel('提案', { exact: true }).fill('和朋友喝茶')
  await page.getByLabel('时间段', { exact: true }).fill('15:00–17:00')
  await page.getByRole('button', { name: '完成', exact: true }).click()
  await expect(wednesday.getByRole('button', { name: /和朋友喝茶/ })).toBeVisible()
  expect(state.operations).toEqual([])
  await wednesday.getByRole('button', { name: /和朋友喝茶/ }).click()
  await page.getByLabel('日期', { exact: true }).fill('2026-10-22')
  await page.getByRole('button', { name: '完成', exact: true }).click()
  await expect(wednesday.getByRole('listitem')).toHaveCount(0)
  await page.getByRole('navigation', { name: '选择计划日期' }).getByRole('button', { name: '周四 10/22', exact: true }).click()
  const thursday = wednesday
  await thursday.getByRole('button', { name: /和朋友喝茶/ }).click()
  await page.getByRole('button', { name: '删除提案', exact: true }).click()
  await expect(thursday.getByRole('listitem')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '保存周表', exact: true })).toBeDisabled()
  expect(state.operations).toEqual([])
})

test('single-day navigation makes all 56 proposals reachable without crowding or history', async ({ page }, testInfo) => {
  const times = ['00:00–07:30', '07:30–09:00', '09:00–12:00', '12:00–14:00', '14:00–17:30', '17:30–19:00', '19:00–22:30', '22:30–24:00']
  const titles = ['睡眠与休息', '做早餐，读几页书', '在窗边整理昨天没有写完的小说手稿', '午饭和午休', '去附近的图书馆找一本想读的书', '散步，买菜做晚饭', '读书、看电影或和朋友聊天', '收拾房间，准备休息']
  const items = Array.from({ length: 56 }, (_, index) => ({ ...item(`dense-${index}`, times[index % 8], titles[index % 8]), date: `2026-10-${19 + Math.floor(index / 8)}`, detail: '留一点从容的时间，按自己的节奏来。' }))
  await stubPlan(page, items.reverse())
  const daily = page.getByRole('region', { name: '按日查看提案', exact: true })
  const dates = page.getByRole('navigation', { name: '选择计划日期' })
  await expect(dates.getByRole('button')).toHaveCount(7)
  await expect(page.getByRole('button', { name: /添加提案/ })).toBeDisabled()
  for (let index = 0; index < 7; index++) {
    await dates.getByRole('button').nth(index).click()
    await expect(daily.getByRole('listitem')).toHaveCount(8)
    await expect(daily.getByRole('heading')).toHaveText(`周${['一', '二', '三', '四', '五', '六', '日'][index]} 10/${19 + index}`)
    await expect(dates.getByRole('button').nth(index)).toHaveAttribute('aria-current', 'date')
  }
  await dates.getByRole('button').first().click()
  const card = await daily.getByRole('button', { name: /在窗边整理/ }).boundingBox()
  expect(card!.width).toBeGreaterThan(700)
  await expect(page.getByRole('heading', { name: '记忆与日常', exact: true })).toHaveCount(0)
  await expect(page.getByText(/查看已有周表/)).toHaveCount(0)
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('life-plan-single-day-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 1024, height: 900 })
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('life-plan-single-day-small-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.body.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(daily.getByRole('listitem')).toHaveCount(8)
  await page.screenshot({ path: testInfo.outputPath('life-plan-single-day-mobile.png'), fullPage: true })
})

test('keyboard date navigation and previous/next days preserve unsaved edits', async ({ page }) => {
  const state = await stubPlan(page)
  await editMorning(page, '切换后仍保留的安排')
  const dates = page.getByRole('navigation', { name: '选择计划日期' })
  await dates.getByRole('button').first().focus()
  await page.keyboard.press('ArrowRight')
  await expect(dates.getByRole('button').nth(1)).toBeFocused()
  await expect(dates.getByRole('button').nth(1)).toHaveAttribute('aria-current', 'date')
  await page.keyboard.press('End')
  await expect(page.getByRole('button', { name: /后一天/ })).toBeDisabled()
  await page.keyboard.press('Home')
  await expect(page.getByRole('button', { name: /前一天/ })).toBeDisabled()
  await expect(page.getByRole('button', { name: /切换后仍保留的安排/ })).toBeVisible()
  expect(state.operations).toEqual([])
  await page.getByRole('button', { name: /后一天/ }).click()
  await page.getByRole('button', { name: /前一天/ }).click()
  await expect(page.getByRole('button', { name: /切换后仍保留的安排/ })).toBeVisible()
  await page.getByRole('button', { name: '保存周表', exact: true }).click()
  expect(state.operations).toEqual(['save'])
  expect(state.plan.items.some(item => item.title === '切换后仍保留的安排')).toBe(true)
})
