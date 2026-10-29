import { expect, test } from '@playwright/test'

test('service status polling preserves edits and stops on logout', async ({ page }, testInfo) => {
  await page.clock.install()
  let status = 'ready'
  let fail = false
  let requests = 0
  await page.route('**/api/service-status', async route => {
    requests++
    if (fail) await route.abort()
    else await route.fulfill({ json: { status, message: `合成服务状态：${status}` } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '使用邀请码注册' }).click()
  await page.getByLabel('用户名', { exact: true }).fill('status_reader')
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-2026')
  await page.getByLabel('邀请码', { exact: true }).fill(process.env.LISA_TEST_INVITES!.split(',')[4])
  await page.getByRole('button', { name: '创建账号并进入书房' }).click()
  await expect(page.getByLabel('绑定码', { exact: true })).toBeVisible()
  await page.clock.fastForward(16_000)
  expect(requests).toBe(0)
  const headers = { 'X-LISA-Request': '1' }
  expect((await page.request.post('/api/setup/binding/confirm', {
    headers, data: { code: process.env.LISA_TEST_STATUS_BINDING_CODE, qq_id: '700000004' },
  })).status()).toBe(200)
  expect((await page.request.post('/api/setup/persona', {
    headers, data: { name: '状态测试小莎', prompt: '合成测试：服务状态与性格编辑互不干扰。' },
  })).status()).toBe(200)
  await page.reload()
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '个人设置', exact: true }).click()
  const notice = page.getByRole('status', { name: '交流服务状态' })
  await expect(notice).toHaveText('合成服务状态：ready')
  await page.getByRole('button', { name: '调整 LISA 性格' }).click()
  await page.getByLabel('LISA 性格 prompt', { exact: true }).fill('尚未保存的合成人格内容')
  for (const nextStatus of ['unavailable', 'ready']) {
    status = nextStatus
    await page.clock.fastForward(16_000)
    await expect(notice).toHaveText(`合成服务状态：${nextStatus}`)
    await expect(page.locator('.ready-note')).toHaveAttribute('data-service-status', nextStatus)
    await expect(page.getByLabel('LISA 性格 prompt', { exact: true })).toHaveValue('尚未保存的合成人格内容')
  }
  fail = true
  await page.clock.fastForward(16_000)
  await expect(notice).toContainText('暂时无法确认交流服务状态')
  await expect(page.getByLabel('LISA 性格 prompt', { exact: true })).toHaveValue('尚未保存的合成人格内容')
  await page.screenshot({ path: testInfo.outputPath('desktop-service-status.png'), fullPage: true })
  await page.getByRole('button', { name: '退出登录' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByRole('heading', { name: '登录书房' })).toBeVisible()
  const before = requests
  await page.clock.fastForward(60_000)
  expect(requests).toBe(before)
})
