import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const recordedAt = '2026-10-24T05:00:00Z';
const group = () => ({ id: 'group-1', bot_id: '700000010', qq_group_id: '700000020', name: '合成读书群', enabled: false, message_count: 52, first_at: recordedAt, last_at: recordedAt });
async function groupsPage(page: Page, options: { admin?: boolean; empty?: boolean; failLoad?: boolean } = {}) {
  const state = {
    admin: options.admin ?? false, groups: options.empty ? [] : [group()], grants: [] as { username: string; user_id: string }[],
    failLoad: options.failLoad ?? false, failGrant: false, failSearch: false, failClear: false, failCorpus: false,
    searches: [] as URLSearchParams[], mutations: [] as { path: string; method: string; body: Record<string, unknown> }[],
    profiles: [] as Record<string, unknown>[], starts: [] as Record<string, unknown>[], corpusRequests: [] as Record<string, unknown>[],
    task: null as { id: string; kind: string; status: string; result: unknown; cleanup_state: string } | null,
  };
  await page.route('**/api/**', async route => {
    const req = route.request(); const url = new URL(req.url()); const path = url.pathname.replace('/api', ''); const method = req.method();
    const body = method === 'GET' || method === 'DELETE' ? {} : req.postDataJSON();
    if (method !== 'GET') state.mutations.push({ path, method, body });
    if (path === '/auth/me') return route.fulfill({ json: { id: 'group-reader', username: '合成群记录读者' } });
    if (path === '/setup') return route.fulfill({ json: { qq_id: '700000099', initialized: true, persona: { name: '原有人格', prompt: '不被群消息自动替换。', version: 1 }, presets: [] } });
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务' } });
    if (path === '/projects') return route.fulfill({ json: [] });
    if (path === '/style/profiles') return route.fulfill({ json: { profiles: state.profiles, applications: { personality: null, writing: null } } });
    if (path === '/style/tasks' && method === 'GET') return route.fulfill({ json: [] });
    if (path === '/style/tasks' && method === 'POST') {
      state.starts.push(body); state.task = { id: 'style-group-task', kind: 'style_distill', status: 'pending', result: null, cleanup_state: 'processing' };
      return route.fulfill({ status: 201, json: state.task });
    }
    if (path === '/style/tasks/style-group-task') {
      state.task = { ...state.task!, status: 'completed', cleanup_state: 'cleaned', result: { kind: 'personality', prompt: '合成群风格：自然接话，保留事实边界。', summary: '合成交流画像。', report: { coverage: { group_source: { total_target_messages: 52, sampled_target_messages: 8, selected_messages: 12 }, sampling: { analyzed_target_messages: 6, total_target_messages: 8, context_messages: 4, sampled_windows: 2, excluded_messages: 2 } } } } };
      return route.fulfill({ json: state.task });
    }
    if (path === '/groups' && method === 'GET') return route.fulfill(state.failLoad ? { status: 503, json: { detail: '合成群列表暂不可用，请重试。' } } : { json: { is_admin: state.admin, groups: state.groups } });
    if (path === '/groups' && method === 'POST') { state.groups.push({ ...group(), ...body }); return route.fulfill({ status: 201, json: state.groups.at(-1) }); }
    if (path === '/groups/group-1' && method === 'PATCH') { Object.assign(state.groups[0], body); return route.fulfill({ json: state.groups[0] }); }
    if (path === '/groups/group-1/members') return route.fulfill({ json: [{ qq_id: '700000031', name: '小雨', message_count: 30 }, { qq_id: '700000032', name: '小林', message_count: 22 }] });
    if (path === '/groups/group-1/messages' && method === 'GET') {
      state.searches.push(url.searchParams);
      if (state.failSearch) return route.fulfill({ status: 503, json: { detail: '合成检索失败，条件仍可编辑。' } });
      const offset = Number(url.searchParams.get('offset') ?? 0); const total = 52;
      return route.fulfill({ json: { total, offset, limit: 50, messages: Array.from({ length: offset ? 2 : 50 }, (_, index) => ({ id: `m-${offset + index}`, qq_id: url.searchParams.get('qq_id') || '700000031', name: '小雨', content: `合成群消息 ${offset + index + 1}：关于阅读的一句交流。`, sent_at: recordedAt })) } });
    }
    if (path === '/groups/group-1/messages' && method === 'DELETE') {
      if (state.failClear) return route.fulfill({ status: 503, json: { detail: '合成清理失败，记录未清理。' } });
      state.groups[0].message_count = 0; return route.fulfill({ status: 204 });
    }
    if (path === '/groups/group-1/grants' && method === 'GET') return route.fulfill({ json: { grants: state.grants } });
    if (path === '/groups/group-1/grants' && method === 'PUT') {
      if (state.failGrant) return route.fulfill({ status: 404, json: { detail: '合成用户名不存在，请检查。' } });
      state.grants = [{ user_id: 'granted-reader', username: String(body.username) }]; return route.fulfill({ json: { grants: state.grants } });
    }
    if (path === '/groups/group-1/grants/granted-reader' && method === 'DELETE') { state.grants = []; return route.fulfill({ status: 204 }); }
    if (path === '/groups/group-1/corpus') {
      state.corpusRequests.push(body);
      if (state.failCorpus) return route.fulfill({ status: 422, json: { detail: '所选范围超过限制，请缩小时间或关键字范围。' } });
      return route.fulfill({ json: { target_speakers: ['0:p0'], corpus: { id: 'group-corpus-1', kind: 'personality', state: 'ready', cleanup_state: 'pending', task_id: null, preview: { total_chars: 120, files: [{ name: '群消息临时副本', chars: 120, encoding: 'UTF-8', segments: 2, sections: [] }], participants: [{ id: '0:p0', label: '小雨', messages: 2 }], warnings: ['仅提取目标人物与邻近上下文。'] } } } });
    }
    if (path === '/style/corpora/group-corpus-1' && method === 'DELETE') return route.fulfill({ status: 204 });
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic group route: ${path}` } });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '性格与风格', exact: true }).click();
  await page.getByRole('button', { name: '群消息', exact: true }).click();
  await expect(page.getByRole('region', { name: '群消息记录', exact: true })).toBeVisible();
  return state;
}

test('granted members browse paginated records and distill another member through the existing task workflow', async ({ page }, info) => {
  const state = await groupsPage(page);
  await expect(page.getByText('管理员：采集与访问权限', { exact: true })).toHaveCount(0);
  await page.getByLabel('已记录的群成员').selectOption('700000032');
  await expect(page.getByLabel('目标成员 QQ 号', { exact: true })).toHaveValue('700000032');
  await page.getByLabel('消息关键字', { exact: true }).fill('阅读');
  await page.getByLabel('开始时间（本地）', { exact: true }).fill('2026-10-24T09:00');
  await page.getByLabel('结束时间（本地）', { exact: true }).fill('2026-10-24T18:00');
  await expect(page.getByRole('button', { name: '从选定群消息提炼性格', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '检索群消息', exact: true }).click();
  const results = page.getByLabel('群消息检索结果');
  await expect(results).toContainText('共找到 52 条消息');
  expect(state.searches[0].get('qq_id')).toBe('700000032'); expect(state.searches[0].get('keyword')).toBe('阅读');
  expect(state.searches[0].get('start')).toMatch(/Z$/); expect(state.searches[0].get('end')).toMatch(/Z$/);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(results).toContainText('显示第 51–52 条');
  expect(state.searches[1].get('offset')).toBe('50');
  await page.screenshot({ path: info.outputPath('desktop-group-records.png'), fullPage: true });
  await page.getByRole('button', { name: '从选定群消息提炼性格', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('合成群风格：自然接话，保留事实边界。');
  expect(state.corpusRequests[0]).toMatchObject({ qq_id: '700000032', keyword: '阅读' });
  expect(state.starts).toEqual([{ corpus_id: 'group-corpus-1', target_speakers: ['0:p0'], selection: [] }]);
  await page.getByText('覆盖、证据与验证说明', { exact: true }).click();
  await expect(page.getByText(/群来源覆盖：30 天可用记录中，当前检索命中 52 条目标消息/)).toBeVisible();
  await expect(page.getByText(/最终实际分析 6 条目标发言/)).toBeVisible();
  expect(state.profiles).toHaveLength(0);
  expect(state.mutations.some(item => /applications|onebot|memory|chapters|projects/.test(item.path))).toBe(false);
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('原始材料已清理');
});

test('no grant is an explanatory empty state and unavailable lists can be retried', async ({ page }) => {
  const state = await groupsPage(page, { empty: true, failLoad: true });
  await expect(page.getByRole('alert')).toContainText('合成群列表暂不可用');
  state.failLoad = false;
  await page.getByRole('button', { name: '刷新可访问的群', exact: true }).click();
  await expect(page.getByText('暂无可访问的群记录。', { exact: true })).toBeVisible();
  await expect(page.getByLabel('目标成员 QQ 号', { exact: true })).toHaveCount(0);
  await expect(page.getByText('管理员：采集与访问权限', { exact: true })).toHaveCount(0);
  expect(state.mutations).toHaveLength(0);
});

test('admin registration, collection, grants and explicit cleanup keep failures actionable on mobile', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await groupsPage(page, { admin: true, empty: true });
  await page.getByText('管理员：采集与访问权限', { exact: true }).click();
  await page.getByLabel('机器人 QQ', { exact: true }).fill('700000010');
  await page.getByLabel('QQ 群号', { exact: true }).fill('700000020');
  await page.getByLabel('群名称', { exact: true }).fill('合成读书群');
  await page.getByRole('button', { name: '登记群', exact: true }).click();
  await expect(page.getByRole('button', { name: '启用此群采集', exact: true })).toBeVisible();
  expect(state.mutations.find(item => item.path === '/groups')?.body).toEqual({ bot_id: '700000010', qq_group_id: '700000020', name: '合成读书群', enabled: false });
  await page.getByRole('button', { name: '启用此群采集', exact: true }).click();
  await expect(page.getByRole('button', { name: '关闭此群采集', exact: true })).toBeVisible();
  expect(state.mutations.find(item => item.method === 'PATCH')?.body).toEqual({ enabled: true });
  await page.getByLabel('授权的网页用户名', { exact: true }).fill('synthetic_reader');
  state.failGrant = true;
  await page.getByRole('button', { name: '授权访问此群', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成用户名不存在');
  await expect(page.getByLabel('授权的网页用户名', { exact: true })).toHaveValue('synthetic_reader');
  state.failGrant = false;
  await page.getByRole('button', { name: '授权访问此群', exact: true }).click();
  await page.getByRole('button', { name: '撤回 synthetic_reader 的授权', exact: true }).click();
  await expect(page.getByRole('button', { name: '撤回 synthetic_reader 的授权', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '清理此群全部记录', exact: true }).click();
  const confirmation = page.getByRole('group', { name: '确认清理群记录', exact: true });
  await expect(confirmation).toContainText('此操作不可恢复');
  expect(state.mutations.filter(item => item.path.endsWith('/messages') && item.method === 'DELETE')).toHaveLength(0);
  await confirmation.getByRole('button', { name: '暂不清理', exact: true }).click();
  await page.getByRole('button', { name: '清理此群全部记录', exact: true }).click();
  state.failClear = true;
  await confirmation.getByRole('button', { name: '确认清理群记录', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成清理失败');
  await expect(confirmation).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-group-admin.png'), fullPage: true });
  state.failClear = false;
  await confirmation.getByRole('button', { name: '确认清理群记录', exact: true }).click();
  await expect(page.getByText('本群当前记录已清理，未完成的相关蒸馏将取消；已保存方案保留。', { exact: true })).toBeVisible();
  expect(state.groups[0].message_count).toBe(0);
});

test('manual targets and filter changes require a fresh search, with range and extraction errors retaining input', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await groupsPage(page);
  await page.getByLabel('目标成员 QQ 号', { exact: true }).fill('700000040');
  await page.getByLabel('开始时间（本地）', { exact: true }).fill('2026-10-24T18:00');
  await page.getByLabel('结束时间（本地）', { exact: true }).fill('2026-10-24T09:00');
  await page.getByRole('button', { name: '检索群消息', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('开始时间不能晚于结束时间');
  expect(state.searches).toHaveLength(0);
  await page.getByLabel('开始时间（本地）', { exact: true }).fill('2026-10-24T08:00');
  state.failSearch = true;
  await page.getByRole('button', { name: '检索群消息', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成检索失败');
  await expect(page.getByLabel('目标成员 QQ 号', { exact: true })).toHaveValue('700000040');
  state.failSearch = false;
  await page.getByRole('button', { name: '检索群消息', exact: true }).click();
  await expect(page.getByLabel('群消息检索结果')).toContainText('共找到 52 条');
  await page.getByLabel('消息关键字', { exact: true }).fill('新范围');
  await expect(page.getByRole('button', { name: '从选定群消息提炼性格', exact: true })).toBeDisabled();
  await expect(page.getByLabel('群消息检索结果')).toContainText('检索条件已修改');
  await page.getByRole('button', { name: '检索群消息', exact: true }).click();
  state.failCorpus = true;
  await page.getByRole('button', { name: '从选定群消息提炼性格', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('所选范围超过限制');
  await expect(page.getByLabel('消息关键字', { exact: true })).toHaveValue('新范围');
  expect(state.starts).toHaveLength(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-group-records.png'), fullPage: true });
});

test('real isolated API grants, retrieves and cancels a group distillation before explicit cleanup', async ({ page }, info) => {
  const failures: string[] = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.goto('/');
  await page.getByLabel('用户名', { exact: true }).fill('styles_reader');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-2026');
  await page.getByRole('button', { name: '进入书房', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '性格与风格', exact: true }).click();
  await page.getByRole('button', { name: '群消息', exact: true }).click();
  await page.getByLabel('选择获授权的群', { exact: true }).selectOption({ label: '接口验收群 · 群 700000021' });
  await page.getByLabel('目标成员 QQ 号', { exact: true }).fill('700000031');
  await page.getByRole('button', { name: '检索群消息', exact: true }).click();
  await expect(page.getByLabel('群消息检索结果')).toContainText('共找到 5 条消息');
  const corpusResponse = page.waitForResponse(response => /\/api\/groups\/[^/]+\/corpus$/.test(response.url()) && response.request().method() === 'POST');
  const taskResponse = page.waitForResponse(response => response.url().endsWith('/api/style/tasks') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '从选定群消息提炼性格', exact: true }).click();
  const corpus = await (await corpusResponse).json();
  const task = await (await taskResponse).json();
  expect(task.status).toBe('pending');
  expect(corpus.target_speakers).toHaveLength(1);
  await expect(page.getByRole('button', { name: '取消蒸馏任务', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '取消蒸馏任务', exact: true }).click();
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('已取消');
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('原始材料已清理');
  const cleaned = await (await page.request.get(`/api/style/corpora/${corpus.corpus.id}`)).json();
  expect(cleaned.cleanup_state).toBe('cleaned');
  await page.getByText('管理员：采集与访问权限', { exact: true }).click();
  await page.getByLabel('授权的网页用户名', { exact: true }).fill('recovery_reader');
  await page.getByRole('button', { name: '授权访问此群', exact: true }).click();
  await expect(page.getByRole('button', { name: '撤回 recovery_reader 的授权', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '撤回 recovery_reader 的授权', exact: true }).click();
  await expect(page.getByRole('button', { name: '撤回 recovery_reader 的授权', exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('real-api-group-records.png'), fullPage: true });
  await page.getByRole('button', { name: '清理此群全部记录', exact: true }).click();
  const confirmation = page.getByRole('group', { name: '确认清理群记录', exact: true });
  await expect(confirmation).toContainText('接口验收群');
  await confirmation.getByRole('button', { name: '确认清理群记录', exact: true }).click();
  await expect(page.getByText('本群当前记录已清理，未完成的相关蒸馏将取消；已保存方案保留。', { exact: true })).toBeVisible();
  const state = await (await page.request.get('/api/groups')).json();
  expect(state.groups.find((item: { qq_group_id: string }) => item.qq_group_id === '700000021').message_count).toBe(0);
  expect(failures).toEqual([]);
});
