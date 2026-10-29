import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

async function login(page: Page, username: string) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录书房', exact: true })).toBeVisible();
  await page.getByLabel('用户名', { exact: true }).fill(username);
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-2026');
  await page.getByRole('button', { name: '进入书房', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
}

test('real operations API and UI honor the deployment maintainer allowlist', async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  // This test deliberately uses real cookies, FastAPI and the disposable test database.
  expect((await page.request.get('/api/operations/access')).status()).toBe(401);
  expect((await page.request.get('/api/operations/dashboard')).status()).toBe(401);
  await login(page, 'operations_e2e');
  await expect(page.getByRole('button', { name: '运行仪表盘', exact: true })).toBeVisible();
  const response = page.waitForResponse(value => new URL(value.url()).pathname === '/api/operations/dashboard');
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect(page.getByRole('heading', { name: '组件状态', exact: true })).toBeVisible();
  await expect(page.getByText('统计时区：Asia/Shanghai', { exact: false })).toBeVisible();
  await expect(page.getByText('组件、队列和索引覆盖展示当前全局快照', { exact: false })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('operations-live-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: 'RAG', exact: true }).click();
  await expect(page.getByRole('heading', { name: '固定评测召回率', exact: true })).toBeVisible();
  await expect(page.getByText('尚无固定评测结果。线上非空结果率不能代替召回率。')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('operations-live-rag-390.png'), fullPage: true });

  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '登录书房', exact: true })).toBeVisible();
  await login(page, 'operations_reader');
  const access = await page.request.get('/api/operations/access');
  expect(access.status()).toBe(200);
  expect(await access.json()).toEqual({ allowed: false });
  await expect(page.getByRole('button', { name: '运行仪表盘', exact: true })).toHaveCount(0);
  const denied = await page.request.get('/api/operations/dashboard?range=7d');
  expect(denied.status()).toBe(403);
  const body = JSON.stringify(await denied.json());
  expect(body).not.toContain('components');
  expect(body).not.toContain('known_total');
  expect(errors).toEqual([]);
});
