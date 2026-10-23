import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

async function stubContact(page: Page) {
  const state = {
    settings: { enabled: false, timezone: 'Asia/Shanghai', quiet_start: null as number | null, quiet_end: null as number | null, min_interval_minutes: 240, max_per_day: 2, revision: 1, available: false, observations: { sample_count: 12, active_days: 4, timezone: "Asia/Shanghai", preferred_hours: { start: 18, end: 24, active_days: 4 } as { start: number; end: number; active_days: number } | null, text_length: { sample_count: 12, median: 36 as number | null, trend: null as "longer" | "shorter" | null } } },
    persona: { name: '窗边的小莎', prompt: '合成性格设置，不向真实服务发送。', version: 1 },
    posts: [] as Record<string, unknown>[],
    personaPosts: [] as Record<string, unknown>[],
    failRead: false,
    failSave: false,
    loggedIn: true,
  }
  await page.route('**/api/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname.replace('/api', '')
    if (path === '/auth/me') return route.fulfill(state.loggedIn ? { json: { id: 'contact-reader', username: '合成联系读者' } } : { status: 401, json: { detail: '请先登录。' } })
    if (path === '/auth/logout') { state.loggedIn = false; return route.fulfill({ status: 204 }) }
    const setup = () => ({ qq_id: '700000098', initialized: true, persona: state.persona, presets: [] })
    if (path === '/setup') return route.fulfill({ json: setup() })
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成交流服务已就绪。' } })
    if (path === '/setup/persona') {
      const body = req.postDataJSON()
      state.personaPosts.push(body)
      state.persona = { ...body, version: state.persona.version + 1 }
      return route.fulfill({ json: setup() })
    }
    if (path === '/contact-settings' && req.method() === 'GET') return route.fulfill(state.failRead ? { status: 503, json: { detail: '合成联系设置暂时无法读取。' } } : { json: state.settings })
    if (path === '/contact-settings' && req.method() === 'POST') {
      const body = req.postDataJSON()
      state.posts.push(body)
      if (state.failSave) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请保留修改后重试。' } })
      if (body.timezone === 'Unknown/Zone') return route.fulfill({ status: 422, json: { detail: '请填写有效的 IANA 时区。' } })
      state.settings = { ...state.settings, ...body, timezone: body.timezone.trim(), revision: state.settings.revision + 1 }
      return route.fulfill({ json: state.settings })
    }
    if (path === '/projects') return route.fulfill({ json: [] })
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic route: ${path}` } })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '账号与 LISA 性格', exact: true }).click()
  await expect(page.getByRole('button', { name: '设置联系偏好', exact: true })).toBeVisible()
  return state
}

test('contact preferences opt in explicitly, persist overnight quiet hours and isolate persona shortcuts', async ({ page }, testInfo) => {
  const state = await stubContact(page)
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await expect(page.getByRole('heading', { name: '联系偏好', exact: true })).toBeVisible()
  await expect(page.getByLabel('允许 LISA 主动联系我')).not.toBeChecked()
  await expect(page.getByText('主动联系服务暂未开启。偏好仍可保存，开启前不会自动发出消息。')).toBeVisible()
  await page.getByLabel('允许 LISA 主动联系我').check()
  await page.getByLabel('启用免打扰时段').check()
  await page.getByLabel('免打扰开始', { exact: true }).fill('22:30')
  await page.getByLabel('免打扰结束', { exact: true }).fill('07:15')
  await page.getByLabel('所在时区', { exact: true }).fill(' Asia/Shanghai ')
  await page.getByLabel('两次主动联系至少间隔（分钟）').fill('480')
  await page.getByLabel('任意 24 小时最多主动联系（次）').fill('1')
  expect(state.posts).toHaveLength(0)
  await page.getByLabel('所在时区', { exact: true }).press('Control+s')
  await expect(page.getByRole('status', { name: '联系偏好保存状态' })).toHaveText('联系偏好已保存。')
  expect(state.posts).toEqual([{ enabled: true, timezone: 'Asia/Shanghai', quiet_start: 1350, quiet_end: 435, min_interval_minutes: 480, max_per_day: 1 }])
  expect(state.personaPosts).toHaveLength(0)
  await expect(page.getByLabel('所在时区', { exact: true })).toHaveValue('Asia/Shanghai')
  await page.screenshot({ path: testInfo.outputPath('desktop-contact-settings.png'), fullPage: true })
  await page.reload()
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '账号与 LISA 性格', exact: true }).click()
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await expect(page.getByLabel('允许 LISA 主动联系我')).toBeChecked()
  await expect(page.getByLabel('免打扰开始', { exact: true })).toHaveValue('22:30')
  await expect(page.getByLabel('免打扰结束', { exact: true })).toHaveValue('07:15')
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click()
  await page.getByRole('button', { name: '调整 LISA 性格' }).click()
  await page.getByLabel('LISA 性格 prompt', { exact: true }).fill('仍在编辑的合成人格。')
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await page.getByRole('dialog', { name: '有未保存的性格修改' }).getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('heading', { name: '联系偏好', exact: true })).toBeVisible()
  expect(state.personaPosts).toHaveLength(1)
  expect(state.posts).toHaveLength(1)
  await page.getByLabel('启用免打扰时段').uncheck()
  await page.getByRole('button', { name: '保存联系偏好', exact: true }).click()
  await expect(page.getByRole('status', { name: '联系偏好保存状态' })).toHaveText('联系偏好已保存。')
  expect(state.settings.quiet_start).toBeNull()
  expect(state.settings.quiet_end).toBeNull()
})

test('contact changes survive failed save, validate times, and require a leave decision on mobile', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const state = await stubContact(page)
  state.settings.available = true
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await page.getByLabel('允许 LISA 主动联系我').check()
  await page.getByLabel('启用免打扰时段').check()
  await page.getByLabel('免打扰开始', { exact: true }).fill('08:00')
  await page.getByLabel('免打扰结束', { exact: true }).fill('08:00')
  await page.getByRole('button', { name: '保存联系偏好', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('开始和结束时间不能相同')
  expect(state.posts).toHaveLength(0)
  await page.getByLabel('免打扰开始', { exact: true }).fill('23:00')
  await page.getByLabel('所在时区', { exact: true }).fill('Unknown/Zone')
  await page.getByRole('button', { name: '保存联系偏好', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('有效的 IANA 时区')
  await expect(page.getByLabel('所在时区', { exact: true })).toHaveValue('Unknown/Zone')
  await page.getByLabel('所在时区', { exact: true }).fill('Europe/London')
  await page.getByLabel('两次主动联系至少间隔（分钟）').fill('720')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-contact-settings.png'), fullPage: true })
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '有未保存的联系偏好' })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: '继续编辑', exact: true }).click()
  await expect(page.getByLabel('所在时区', { exact: true })).toHaveValue('Europe/London')
  state.failSave = true
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByRole('heading', { name: '联系偏好', exact: true })).toBeVisible()
  await expect(page.getByLabel('两次主动联系至少间隔（分钟）')).toHaveValue('720')
  state.failSave = false
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click()
  await dialog.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('heading', { name: '欢迎，合成联系读者。' })).toBeVisible()
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await expect(page.getByLabel('所在时区', { exact: true })).toHaveValue('Europe/London')
  await page.getByLabel('允许 LISA 主动联系我').uncheck()
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath('mobile-contact-unsaved.png') })
  await dialog.getByRole('button', { name: '放弃修改', exact: true }).click()
  await expect(page.getByRole('heading', { name: '登录书房', exact: true })).toBeVisible()
  expect(state.settings.enabled).toBe(true)
})

test('contact settings load failure offers retry and preserves the default off choice', async ({ page }) => {
  const state = await stubContact(page)
  state.failRead = true
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('暂时无法读取')
  await expect(page.getByRole('button', { name: '保存联系偏好', exact: true })).toHaveCount(0)
  state.failRead = false
  await page.getByRole('button', { name: '重新读取联系偏好', exact: true }).click()
  await expect(page.getByLabel('允许 LISA 主动联系我')).not.toBeChecked()
  expect(state.posts).toHaveLength(0)
})


test('interaction observations remain read only and update after sources are removed', async ({ page }) => {
  const state = await stubContact(page)
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  const notes = page.getByLabel('交流记录中的线索')
  await expect(notes).toContainText('12 条消息')
  await expect(notes).toContainText('18:00–24:00')
  await expect(notes).toContainText('4 天的记录作为依据')
  await expect(page.getByText('有未保存修改 · Ctrl+S 保存', { exact: true })).toHaveCount(0)
  expect(state.posts).toHaveLength(0)
  state.settings.observations = { ...state.settings.observations, sample_count: 0, active_days: 0, preferred_hours: null, text_length: { sample_count: 0, median: null, trend: null } }
  await page.reload()
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '账号与 LISA 性格', exact: true }).click()
  await page.getByRole('button', { name: '设置联系偏好', exact: true }).click()
  await expect(page.getByLabel('交流记录中的线索')).toContainText('还没有可参考的交流记录')
  await expect(page.getByLabel('交流记录中的线索')).not.toContainText('18:00–24:00')
  expect(state.posts).toHaveLength(0)
})
