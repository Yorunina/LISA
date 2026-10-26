import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { itemOccupies, timeRange } from '../../web/life-plan-time'

type Item = { id: string; date: string; time: string; title: string; detail: string; status: string; source: string; source_message_id: null }
const item = (id: string, time: string, title: string): Item => ({ id, date: '2026-10-19', time, title, detail: '', status: 'tentative', source: 'agent', source_message_id: null })

async function stubPlan(page: Page) {
  const state = {
    plan: { id: 'plan-1', week_start: '2026-10-19', week_end: '2026-10-25', state: 'active', revision: 1, updated_at: '2026-10-19T00:00:00Z', items: [item('morning', '09:30–10:30', '读一章小说'), item('night', '23:30–00:30', '夜间散步')] },
    operations: [] as string[], failSave: false, failGenerate: false,
  }
  await page.route('**/api/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname.replace('/api', '')
    if (path === '/auth/me') return route.fulfill({ json: { id: 'plan-reader', username: '合成周表读者' } })
    if (path === '/setup') return route.fulfill({ json: { qq_id: '700000096', initialized: true, persona: { name: '测试小莎', prompt: '合成周表测试。', version: 1 }, presets: [] } })
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务已就绪。' } })
    if (path === '/projects' || path === '/memories') return route.fulfill({ json: [] })
    if (path === '/life-plan') return route.fulfill({ json: { current: state.plan, plans: [] } })
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
  return state
}

async function editMorning(page: Page, title: string) {
  await page.getByRole('button', { name: /09:30–10:30 读一章小说/ }).first().click()
  await page.getByLabel('提案', { exact: true }).fill(title)
  await page.getByRole('button', { name: '完成', exact: true }).click()
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
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await expect(page.getByLabel('按日查看提案')).toBeVisible()
  await page.getByLabel('查看日期', { exact: true }).selectOption('2026-10-20')
  const overnight = page.getByRole('button', { name: /前一日延续.*夜间散步/ })
  await expect(overnight).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('life-plan-mobile.png'), fullPage: true })
  await overnight.click()
  await expect(page.getByLabel('时间段', { exact: true })).toHaveValue('23:30–00:30')
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('life-plan-mobile-dialog.png'), fullPage: true })
  await page.keyboard.press('Escape')
  await expect(overnight).toBeFocused()
})
