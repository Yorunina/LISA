import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({ status: 401, json: { detail: '请先登录。' } }));
  await page.goto('/');
});

test('request deadline covers reading the body and never replays a write', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const modulePath = '/web/api.ts';
    const { request } = await import(/* @vite-ignore */ modulePath);
    const original = window.fetch;
    let calls = 0;
    window.fetch = async (_input, init) => {
      calls++;
      return {
        ok: true,
        status: 200,
        json: () => new Promise((_, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        }),
      } as Response;
    };
    try {
      await request('/probe', { text: 'synthetic-input' }, 'PUT', { timeoutMs: 30 });
      return { message: 'unexpected success', calls };
    } catch (cause) {
      return { message: (cause as Error).message, calls };
    } finally { window.fetch = original; }
  });
  expect(result.calls).toBe(1);
  expect(result.message).toContain('操作结果尚未确认');
});

test('caller cancellation stays distinguishable from a network failure', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const modulePath = '/web/api.ts';
    const { request } = await import(/* @vite-ignore */ modulePath);
    const controller = new AbortController();
    controller.abort();
    try { await request('/probe', undefined, undefined, { signal: controller.signal }); }
    catch (cause) { return (cause as Error).name; }
  });
  expect(result).toBe('AbortError');
});

test('expired session is explicit and does not silently repeat the operation', async ({ page }) => {
  let writes = 0;
  await page.route('**/api/probe', route => {
    writes++;
    return route.fulfill({ status: 401, json: { detail: '请先登录。' } });
  });
  const result = await page.evaluate(async () => {
    const modulePath = '/web/api.ts';
    const { request, SESSION_EXPIRED_EVENT } = await import(/* @vite-ignore */ modulePath);
    let expired = false;
    const listener = () => { expired = true; };
    window.addEventListener(SESSION_EXPIRED_EVENT, listener);
    try { await request('/probe', { text: 'synthetic-input' }); }
    catch (cause) { return { expired, message: (cause as Error).message }; }
    finally { window.removeEventListener(SESSION_EXPIRED_EVENT, listener); }
  });
  expect(writes).toBe(1);
  expect(result?.expired).toBe(true);
  expect(result?.message).toContain('当前未保存内容仍保留');
});
