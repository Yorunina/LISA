import { expect, test } from '@playwright/test';

test('expired session keeps dirty editor and resumes only for the original account', async ({ page }) => {
  let currentUser = 'original-reader';
  let expired = false;
  let saves = 0;
  let persona = { name: '合成性格', prompt: '原来已保存的性格', version: 1 };
  const setup = () => ({ qq_id: '700000096', initialized: true, persona, presets: [] });
  await page.route('**/api/**', route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace('/api', '');
    if (path === '/auth/me') return route.fulfill({ json: { id: currentUser, username: currentUser } });
    if (path === '/projects') return route.fulfill({ json: [] });
    if (path === '/setup') return route.fulfill({ json: setup() });
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务' } });
    if (path === '/setup/persona') {
      saves++;
      if (expired) return route.fulfill({ status: 401, json: { detail: '请先登录。' } });
      persona = { ...request.postDataJSON(), version: persona.version + 1 };
      return route.fulfill({ json: setup() });
    }
    if (path === '/shared/preferences') return route.fulfill({ json: { meme_mode: 'normal' } });
    return route.fulfill({ status: 404, json: { detail: '未配置的合成接口' } });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '调整 LISA 性格', exact: true }).click();
  const prompt = page.getByLabel('LISA 性格 prompt', { exact: true });
  await prompt.fill('不能因为登录失效而丢失的合成修改');
  expired = true;
  await page.getByRole('button', { name: '保存性格', exact: true }).click();
  const status = page.getByRole('status', { name: '登录状态' });
  await expect(status).toBeVisible();
  await expect(prompt).toHaveValue('不能因为登录失效而丢失的合成修改');
  await expect(status.getByRole('link', { name: '在新标签页登录' })).toHaveAttribute('target', '_blank');

  currentUser = 'another-reader';
  expired = false;
  await status.getByRole('button', { name: '我已重新登录' }).click();
  await expect(status).toContainText('请在新标签页登录原账号');
  await page.getByRole('button', { name: '保存性格', exact: true }).click();
  expect(saves).toBe(1);
  await expect(prompt).toHaveValue('不能因为登录失效而丢失的合成修改');

  currentUser = 'original-reader';
  await status.getByRole('button', { name: '我已重新登录' }).click();
  await expect(status).not.toBeVisible();
  await prompt.press('Control+s');
  await expect(page.getByRole('status').filter({ hasText: /^已保存$/ })).toBeVisible();
  expect(saves).toBe(2);
  expect(persona.prompt).toBe('不能因为登录失效而丢失的合成修改');
});
