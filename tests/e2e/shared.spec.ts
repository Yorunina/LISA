import { expect, test } from '@playwright/test';

const headers = { 'X-LISA-Request': '1' };

test('all users maintain shared entries, revisions and personal preferences through the real API', async ({ page, browser }, info) => {
  const login = await page.request.post('/api/auth/login', { headers, data: { username: 'styles_reader', password: 'synthetic-password-2026' } });
  expect(login.ok()).toBeTruthy();
  await page.goto('/');
  await page.getByRole('button', { name: '共有记忆', exact: true }).click();
  await expect(page.getByRole('heading', { name: '表情图库', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '新增表情', exact: true }).click();
  await page.getByLabel('标题', { exact: true }).fill('隔离共建表情');
  await page.getByLabel('内容、含义与适用语境').fill('表示轻松开心，不适合严肃倾诉；这是合成测试资料。');
  await page.getByLabel('贡献表情图片（最多 4 MiB）').setInputFiles({ name: 'shared.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII=', 'base64') });
  await page.getByLabel('我确认内容和图片可供本部署所有用户查看、维护及由 LISA 使用。', { exact: true }).check();
  await page.getByRole('button', { name: '保存共有条目', exact: true }).click();
  await expect(page.getByText('已保存，所有用户均可查看和维护。', { exact: true })).toBeVisible();
  const original = (await (await page.request.get('/api/shared')).json()).find((item: { title: string }) => item.title === '隔离共建表情');
  expect(original.revision).toBe(1);
  const second = await browser.newContext({ baseURL: test.info().project.use.baseURL });
  const friend = await second.newPage();
  const otherLogin = await friend.request.post('/api/auth/register', { headers, data: { username: 'shared_friend', password: 'synthetic-password-2026', invite_code: process.env.LISA_TEST_INVITES!.split(',')[6] } });
  expect(otherLogin.ok()).toBeTruthy();
  await friend.goto('/');
  await friend.getByRole('button', { name: '共有记忆', exact: true }).click();
  const friendCard = friend.locator('article').filter({ has: friend.getByRole('heading', { name: '隔离共建表情', exact: true }) });
  await friendCard.getByRole('button', { name: '编辑', exact: true }).click();
  await friend.getByLabel('内容、含义与适用语境').fill('另一位用户直接修正：只用于轻松庆祝，语气温和。');
  await friend.getByRole('button', { name: '保存共有条目', exact: true }).click();
  await expect(friend.getByText('已保存，所有用户均可查看和维护。', { exact: true })).toBeVisible();

  // First user still has revision 1 open: a real stale write must retain input.
  await page.getByLabel('内容、含义与适用语境').fill('这个未保存输入不能覆盖朋友的新版本。');
  await page.getByRole('button', { name: '保存共有条目', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('条目已被更新');
  await expect(page.getByLabel('内容、含义与适用语境')).toHaveValue('这个未保存输入不能覆盖朋友的新版本。');
  await page.getByRole('button', { name: '返回列表', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click();
  await page.getByRole('button', { name: '共有记忆', exact: true }).click();
  await page.reload();
  await page.getByRole('button', { name: '共有记忆', exact: true }).click();
  const card = page.locator('article').filter({ has: page.getByRole('heading', { name: '隔离共建表情', exact: true }) });
  await expect(card).toContainText('另一位用户直接修正');
  await card.getByRole('button', { name: '版本记录', exact: true }).click();
  await expect(card.getByText(/版本 1 ·/)).toBeVisible();
  await expect(card.getByRole('img', { name: '隔离共建表情' })).toBeVisible();
  await expect.poll(() => card.locator('img').evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBeTruthy();
  await expect(page.getByText('网络热词', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('我的 LISA 使用表情')).toHaveCount(0);
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByLabel('我的 LISA 使用表情').selectOption('off');
  // Leaving the settings page requires an explicit save, and affects only this account.
  await page.getByRole('button', { name: '共有记忆', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get('/api/shared/preferences')).json()).meme_mode).toBe('off');
  expect((await (await friend.request.get('/api/shared/preferences')).json()).meme_mode).toBe('normal');
  await page.screenshot({ path: info.outputPath('desktop-shared.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card.getByRole('button', { name: '编辑', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  await page.screenshot({ path: info.outputPath('mobile-shared.png'), fullPage: true });
  await card.getByRole('button', { name: '撤下', exact: true }).click();
  await expect(page.getByText('已撤下，后续不再检索或发送。', { exact: true })).toBeVisible();
  await page.getByLabel('筛选共有状态').selectOption('withdrawn');
  await card.getByRole('button', { name: '恢复使用', exact: true }).click();
  await page.getByLabel('筛选共有状态').selectOption('active');
  await expect(card).toBeVisible();
  await second.close();
});
