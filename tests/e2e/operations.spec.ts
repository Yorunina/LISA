import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { OperationsData } from '../../web/operations';

function fixture(): OperationsData {
  const now = new Date().toISOString();
  const distribution = { median_ms: 620, p95_ms: 2300, samples: 12 };
  return {
    generated_at: now, timezone: 'Asia/Shanghai', refresh_seconds: 30,
    range: { start: now, end: now, label: '今日' },
    coverage: { started_at: now, total_calls: 16, classified_calls: 12, unknown_dispatch: 2, history_note: '早期记录缺少发送阶段，用途未知单列。' },
    components: [
      { component: 'api', label: '网页 API', status: 'ok', observed_at: now, age_seconds: 0, details: {} },
      { component: 'napcat', label: 'NapCat', status: 'ok', observed_at: now, age_seconds: 0, details: { process_running: true, webui_reachable: true, webui_url: 'http://127.0.0.1:6099/webui', open_url: '/api/operations/napcat/open' } },
      { component: 'database', label: '数据库', status: 'ok', observed_at: now, age_seconds: 0, details: {} },
      { component: 'worker', label: '普通 worker', status: 'stale', observed_at: now, age_seconds: 180, details: {} },
      { component: 'image_worker', label: '图片 worker', status: 'unknown', observed_at: null, age_seconds: null, details: {} },
      { component: 'gateway', label: 'QQ 网关', status: 'offline', observed_at: now, age_seconds: 2, details: { connected: false, logged_in: false } },
    ],
    queue: { pending: 3, running: 1, debouncing: 2, scheduled: 5, oldest_wait_seconds: 25 },
    tasks: {
      created: 20, finished: 16, states: [{ name: 'completed', count: 12 }, { name: 'failed', count: 2 }, { name: 'cancelled', count: 2 }], success_rate: 0.75,
      wait: distribution, execution: distribution, end_to_end: distribution,
      delivery: { pending: 1, succeeded: 12, failed: 1, unknown: 1, partial_tasks: 1 },
      recent: [{ id: 'task-synthetic-01', kind: 'scene', status: 'failed', created_at: now, started_at: now, finished_at: now, error: 'http_502', adopted: false }],
    },
    calls: {
      total: 16, confirmed_sent: 12, not_sent: 2, unknown_dispatch: 2, inflight: 1, success_rate: 0.8,
      latency: distribution, tokens: { input: 8500, output: null, unknown: 3 },
      by_purpose: [
        { purpose: 'chat', count: 10, succeeded: 8, failed: 1, unknown: 1, input_tokens: 8000, output_tokens: null, unknown_tokens: 2, known_amount: 1.25, unknown_costs: 1, median_ms: 620, p95_ms: 2300, latency_samples: 8 },
        { purpose: 'image_generation', count: 2, succeeded: 2, failed: 0, unknown: 0, input_tokens: null, output_tokens: null, unknown_tokens: 2, known_amount: 0, unknown_costs: 2, median_ms: 26000, p95_ms: 35000, latency_samples: 2 },
      ],
      daily: [{ day: '2026-10-29', count: 12, known_amount: 1.25, unknown_count: 3 }],
      recent: [{ id: 'call-synthetic-01', task_id: 'task-synthetic-01', task_kind: 'image', purpose: 'image_generation', model: 'synthetic-image-model', state: 'succeeded', dispatch_state: 'sent', cost_source: 'unknown', amount: null, input_tokens: null, output_tokens: null, started_at: now, finished_at: now, error: 'price_unavailable' }],
    },
    costs: { currency: 'CNY', known_total: 1.25, provider: 0, estimate: 1.1, conservative: 0.15, unknown_count: 3, inflight_reserved: 0.1, incomplete: true, per_task_average: null, ledger_note: '账单未接入。估算与实际不重复计入。' },
    rag: {
      observations: 9, executed_count: 9, empty_count: 1, empty_result_rate: 1 / 9, states: [{ name: 'executed', count: 8 }, { name: 'empty', count: 1 }], routes: [{ name: 'semantic', count: 7 }, { name: 'recent', count: 2 }],
      latencies: { embedding: { median_ms: null, p95_ms: null, samples: 0 }, retrieval: distribution, assembly: distribution }, public_counts: { candidates: 4, assembled: 2, final_count: 1, final_chars: 100 }, counts: { candidates: 36, assembled: 22, final_count: 18, final_chars: 6200 }, omitted: [{ name: 'char_budget', count: 4 }], latency: distribution,
      coverage: [{ purpose: 'chat', total: 11, complete: 7, stale: 1, partial: 1, missing: 1, failed: 1 }],
      recent: [{ id: 'retrieval-synthetic-01', purpose: 'chat', created_at: now, metrics: { state: 'executed', route: 'semantic', candidates: 5, assembled: 3, final_count: 2 } }], evaluation: null,
    },
    alerts: [{ code: 'worker_stale', severity: 'warning', message: '普通 worker 心跳已过期，请核对执行状态。', observed_at: now }],
    filters: { purposes: ['chat', 'image_generation'], models: ['synthetic-text-model', 'synthetic-image-model'], task_kinds: ['chat', 'scene', 'image'], states: ['succeeded', 'failed', 'unknown'] },
    pagination: { page: 1, page_size: 25, has_more_calls: true, has_more_tasks: false, has_more_retrieval: false },
  };
}

type MockOptions = { allowed?: boolean; forbidden?: boolean; fail?: boolean; empty?: boolean; evaluation?: boolean; urls: string[] };
async function mockStudy(page: Page, options: MockOptions) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace('/api', '');
    if (path === '/auth/me') return route.fulfill({ json: { id: 'synthetic-maintainer', username: '维护测试' } });
    if (path === '/setup') return route.fulfill({ json: { qq_id: '700000096', initialized: true, persona: { name: '测试小莎', prompt: '合成设置', version: 1 }, presets: [] } });
    if (path === '/projects') return route.fulfill({ json: [] });
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务可用' } });
    if (path === '/shared/preferences') return route.fulfill({ json: { meme_mode: 'normal' } });
    if (path === '/operations/access') return route.fulfill({ json: { allowed: options.allowed ?? true } });
    if (path === '/operations/dashboard') {
      options.urls.push(url.toString());
      if (options.forbidden) return route.fulfill({ status: 403, json: { detail: '此入口仅向维护者开放。' } });
      if (options.fail) return route.fulfill({ status: 503, json: { detail: '合成采集暂不可用。' } });
      const data = fixture();
      data.pagination.page = Number(url.searchParams.get('page') ?? 1);
      if (options.evaluation) data.rag.evaluation = {
        id: 'synthetic-evaluation', created_at: data.generated_at,
        configuration: { k: 5, max_chars: 1500, code_version: 'synthetic-revision', embedding_space: 'synthetic-vector-space' },
        results: {
          dataset_version: 'synthetic-rag-v1', evaluation_kind: 'deterministic_pipeline', sample_count: 36, answerable_count: 30,
          by_purpose: [{ purpose: 'chat', sample_count: 12, answerable_count: 10, recall_at_k: 0.9, precision_at_k: 0.8, context_coverage: 0.7 }],
          cases: [{ id: 'synthetic-long-memory', purpose: 'chat', scenario: '长记忆整条省略', recall_at_k: 1, precision_at_k: 1, context_coverage: 0, passed: false }],
        },
      };
      if (options.empty) {
        data.calls.recent = []; data.calls.by_purpose = []; data.calls.daily = [];
        data.tasks.recent = []; data.tasks.states = [];
        data.rag.recent = []; data.rag.states = []; data.rag.routes = []; data.rag.omitted = []; data.rag.coverage = [];
      }
      return route.fulfill({ json: data });
    }
    return route.fulfill({ status: 404, json: { detail: '未配置的合成接口' } });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
}

test('ordinary readers have no operations entry or monitoring request', async ({ page }) => {
  const options = { allowed: false, urls: [] as string[] };
  await mockStudy(page, options);
  await expect(page.getByRole('button', { name: '运行仪表盘', exact: true })).toHaveCount(0);
  expect(options.urls).toEqual([]);
});

test('maintainer dashboard shows separate service, money and RAG states at every viewport', async ({ page }, testInfo) => {
  const options = { urls: [] as string[] };
  await mockStudy(page, options);
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  await expect(page.getByRole('heading', { name: '运行仪表盘', exact: true })).toBeFocused();
  await expect(page.getByText('统计时区：Asia/Shanghai', { exact: false })).toBeVisible();
  await expect(page.getByText('费用不完整 · 3 次无法计价', { exact: true })).toBeVisible();
  await expect(page.getByText('普通 worker 心跳已过期，请核对执行状态。')).toBeVisible();
  const api = page.locator('.operations-components article').filter({ hasText: '网页 API' });
  await expect(api).toContainText('正常');
  const napcat = page.locator('.operations-components article').filter({ has: page.getByRole('heading', { name: 'NapCat', exact: true }) });
  await expect(napcat).toContainText('本机进程：已启动');
  await expect(napcat).toContainText('管理页：可访问');
  const consoleLink = napcat.getByRole('link', { name: '打开 NapCat 管理台', exact: false });
  await expect(consoleLink).toHaveAttribute('href', '/api/operations/napcat/open');
  await expect(consoleLink).toHaveAttribute('target', '_blank');
  await expect(consoleLink).toHaveAttribute('rel', 'noopener noreferrer');
  await expect(consoleLink).toHaveAttribute('referrerpolicy', 'no-referrer');
  const worker = page.locator('.operations-components article').filter({ hasText: '普通 worker' });
  await expect(worker).toContainText('已过期');
  for (const width of [1440, 1024, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`operations-overview-${width}.png`), fullPage: true });
  }
  await page.getByRole('button', { name: '调用与费用', exact: true }).click();
  await expect(page.getByRole('heading', { name: '按用途统计', exact: true })).toBeVisible();
  await expect(page.getByText('账单未接入。估算与实际不重复计入。')).toBeVisible();
  const image = page.getByRole('table').filter({ hasText: '仅显示运维元数据' });
  await expect(image).toContainText('未知');
  await expect(page.getByText('价格或用量未知不等于免费。', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.getByText('明细第 2 页', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'RAG', exact: true }).click();
  await expect(page.getByRole('heading', { name: '固定评测召回率', exact: true })).toBeVisible();
  await expect(page.getByText('尚无固定评测结果。线上非空结果率不能代替召回率。')).toBeVisible();
  await expect(page.getByText('字符预算', { exact: true })).toBeVisible();
  await expect(page.getByText('空结果率 11.1% · 实际执行 9 次 · 空结果 1 次', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '共有表情检索', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '查询向量耗时', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('operations-rag-390.png'), fullPage: true });
});

test('filters apply with local date boundaries and retain no-data states', async ({ page }) => {
  const options = { empty: true, urls: [] as string[] };
  await mockStudy(page, options);
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  await page.getByLabel('统计范围', { exact: true }).selectOption('custom');
  await page.getByLabel('开始日期', { exact: true }).fill('2026-10-27');
  await page.getByLabel('结束日期（含当日）', { exact: true }).fill('2026-10-29');
  await page.getByLabel('调用用途', { exact: true }).selectOption('chat');
  await page.getByLabel('模型', { exact: true }).selectOption('synthetic-text-model');
  await page.getByRole('button', { name: '应用筛选', exact: true }).click();
  await expect.poll(() => options.urls.some(value => {
    const parameters = new URL(value).searchParams;
    return parameters.get('range') === 'custom' && parameters.get('start') === '2026-10-27' && parameters.get('end') === '2026-10-29' && parameters.get('purpose') === 'chat' && parameters.get('model') === 'synthetic-text-model';
  })).toBeTruthy();
  await page.getByRole('button', { name: '调用与费用', exact: true }).click();
  await expect(page.getByText('所选范围暂无调用记录。')).toBeVisible();
  await page.getByRole('button', { name: '任务', exact: true }).click();
  await expect(page.getByText('所选范围暂无任务记录。')).toBeVisible();
});

test('failed refresh marks retained data stale and leaving cancels monitoring', async ({ page }) => {
  await page.clock.install();
  const options = { fail: false, urls: [] as string[] };
  await mockStudy(page, options);
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  await expect(page.getByRole('heading', { name: '组件状态' })).toBeVisible();
  options.fail = true;
  await page.clock.fastForward(31_000);
  await expect(page.getByRole('alert')).toContainText('合成采集暂不可用。');
  await expect(page.getByText('数据已过期，当前保留上次成功结果。', { exact: false })).toBeVisible();
  await expect(page.getByRole('heading', { name: '组件状态' })).toBeVisible();
  await page.getByRole('button', { name: '我的作品', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
  const before = options.urls.length;
  await page.clock.fastForward(120_000);
  expect(options.urls.length).toBe(before);
});

test('server denial removes monitoring data even when the navigation was visible', async ({ page }) => {
  const options = { forbidden: false, urls: [] as string[] };
  await mockStudy(page, options);
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  await expect(page.getByRole('heading', { name: '组件状态' })).toBeVisible();
  options.forbidden = true;
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect(page.getByRole('heading', { name: '无法访问运行数据' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '组件状态' })).toHaveCount(0);
  await expect(page.getByRole('form', { name: '运行数据筛选' })).toHaveCount(0);
});

test('entering operations respects the existing unsaved persona guard', async ({ page }) => {
  const options = { urls: [] as string[] };
  await mockStudy(page, options);
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '调整 LISA 性格', exact: true }).click();
  await page.getByLabel('LISA 性格 prompt', { exact: true }).fill('尚未保存的合成性格');
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(options.urls).toEqual([]);
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.getByRole('heading', { name: '运行仪表盘', exact: true })).toBeVisible();
});

test('fixed RAG results name engineering evidence, snapshot and omitted cases', async ({ page }) => {
  await mockStudy(page, { evaluation: true, urls: [] });
  await page.getByRole('button', { name: '运行仪表盘', exact: true }).click();
  await page.getByRole('button', { name: 'RAG', exact: true }).click();
  await expect(page.getByText('工程链路评测 · 确定性向量', { exact: false })).toBeVisible();
  await expect(page.getByText('此结果不代表真实语义召回效果已验收。', { exact: false })).toBeVisible();
  await expect(page.getByText('synthetic-rag-v1', { exact: true })).toBeVisible();
  await expect(page.getByText('synthetic-revision', { exact: true })).toBeVisible();
  const evaluation = page.getByRole('table').filter({ hasText: '固定评测按用途统计' });
  await expect(evaluation).toContainText('90.0%');
  await expect(evaluation).toContainText('70.0%');
  await page.getByText('查看未通过题（1）', { exact: true }).click();
  await expect(page.getByText('长记忆整条省略', { exact: false })).toBeVisible();
  await evaluation.locator('..').focus();
  await expect(evaluation.locator('..')).toBeFocused();
});
