import { expect, test } from '@playwright/test';

test('public work reads saved chapters without a login on desktop and phone', async ({ page }, testInfo) => {
  await page.goto(`/read/${'r'.repeat(43)}`);
  await expect(page.getByRole('heading', { name: '雨夜来信', exact: true })).toBeVisible();
  await expect(page.getByRole('article')).toContainText('雨停在黄昏之前');
  await expect(page.getByText('不公开的设定资料')).toHaveCount(0);
  await expect(page.getByRole('textbox')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('public-work-desktop.png'), fullPage: true });
  await page.getByRole('link', { name: '第二章 · 信' }).click();
  await expect(page.getByRole('article')).toContainText('<script>只是正文，不执行。</script>');
  await page.reload();
  await expect(page.getByRole('article')).toContainText('她展开了那封信');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('public-work-phone.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.goto(`/read/${'x'.repeat(43)}`);
  await expect(page.getByRole('heading', { name: '链接不可用' })).toBeVisible();
});
