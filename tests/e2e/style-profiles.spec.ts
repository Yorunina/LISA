import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

type Version = { id: string; version: number; prompt: string; summary: string; report: Record<string, unknown>; created_at: string; source_task_id?: string | null };
type Profile = { id: string; name: string; kind: string; latest_version: number; versions: Version[] };
type Task = { id: string; kind: string; mode?: string; status: string; error: string | null; model_http_status?: number | null; result: Record<string, unknown> | null; candidate_discarded?: boolean; cleanup_state: string; corpus_id: string | null };
const timestamp = '2026-10-23T04:00:00Z';

async function study(page: Page, initialized = true) {
  const state = {
    initialized, kind: 'personality', failSave: false, failUpload: false, failDiscard: false, holdTask: false,
    uploads: [] as Record<string, unknown>[], starts: [] as Record<string, unknown>[], trials: [] as Record<string, unknown>[],
    profiles: [] as Profile[], applications: { personality: null as string | null, writing: null as string | null },
    override: null as string | null, cleaned: [] as string[], mutations: [] as string[],
    tasks: [] as Task[], corpusId: 0,
  };
  const corpus = () => ({ id: `corpus-${state.corpusId}`, kind: state.kind, state: 'uploaded', cleanup_state: 'pending', task_id: null, preview: {
    files: [{ name: state.kind === 'writing' ? '第一卷.md' : 'QQ.txt', chars: 240, encoding: 'UTF-8', segments: 2, unrecognized_lines: 1, sections: [{ id: '0:0', start: 0, end: 120, chars: 120, speaker: '0:小雨' }, { id: '0:1', start: 120, end: 240, chars: 120, speaker: '0:小林' }] }, ...(state.kind === 'writing' ? [{ name: '第二卷.txt', chars: 120, encoding: 'GB18030', segments: 1, unrecognized_lines: 0, sections: [{ id: '1:0', start: 0, end: 120, chars: 120, speaker: null }] }] : [])],
    participants: [{ id: '0:小雨', label: '小雨', messages: 4 }, { id: '0:小林', label: '小林', messages: 3 }], warnings: ['存在未识别行，请检查导出格式；未静默纳入分析。'], total_chars: state.kind === 'writing' ? 360 : 240,
  } });
  const result = () => ({ kind: state.kind, prompt: '合成风格：短句接话，分歧时先确认对方的意思；不迁移样本人物事实。', summary: '温和而简洁，适合日常交流。', report: { method_version: 'lisa-style-v1', source_state: 'cleaned', coverage: { total_segments: 3, selected_segments: 2, analyzed_segments: 2, total_chars: 360, selected_chars: 240, analyzed_chars: 240, files: [{ name: '第一卷.md', selected_segments: 1, analyzed_segments: 1 }, { name: '第二卷.txt', selected_segments: 1, analyzed_segments: 1 }] }, evidence: [{ feature: '简洁接话', condition: '日常闲聊', source_ids: ['0:0', '1:0'], counterexample: '严肃分歧不套用玩笑' }], limitations: ['合成样本，不代表真实自然度。'], validation: { status: 'not_executed', development: { status: 'not_executed' }, holdout: { status: 'not_executed' }, user_evaluation: 'not_performed' }, model: { name: 'synthetic-no-network' } } });
  const projectStyle = () => {
    const id = state.override ?? state.applications.writing;
    const profile = state.profiles.find(item => item.versions.some(version => version.id === id));
    return { override_version_id: state.override, effective_version_id: id, effective_profile_name: profile?.name ?? null, prompt: '', source: state.override ? 'override' : id ? 'default' : 'none' };
  };
  await page.route('**/api/**', async route => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace('/api', '');
    const method = req.method();
    if (method !== 'GET') state.mutations.push(path);
    const data = () => req.postDataJSON();
    if (path === '/auth/me') return route.fulfill({ json: { id: 'synthetic-styles-reader', username: '风格读者' } });
    if (path === '/setup') return route.fulfill({ json: { initialized: state.initialized, qq_id: '700000099', persona: state.initialized ? { name: '现有性格', prompt: '现有人格保持。', version: 1 } : null, presets: [] } });
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务，不连接真实 QQ 或模型。' } });
    if (path === '/projects') return route.fulfill({ json: [] });
    if (path === '/style/profiles' && method === 'GET') return route.fulfill({ json: { profiles: state.profiles, applications: state.applications } });
    if (path === '/style/profiles' && method === 'POST') {
      if (state.failSave) return route.fulfill({ status: 503, json: { detail: '合成保存失败，输入保留。' } });
      const body = data(); const id = `profile-${state.profiles.length + 1}`;
      const profile = { id, name: body.name, kind: body.kind, latest_version: 1, versions: [{ id: `${id}-v1`, version: 1, prompt: body.prompt, summary: body.summary, report: body.report, source_task_id: body.source_task_id, created_at: timestamp }] };
      state.profiles.push(profile); return route.fulfill({ json: profile });
    }
    const removed = path.match(/^\/style\/profiles\/([^/]+)$/);
    if (removed && method === 'DELETE') {
      const profile = state.profiles.find(item => item.id === removed[1])!;
      if (profile.versions.some(version => Object.values(state.applications).includes(version.id) || state.override === version.id)) return route.fulfill({ status: 409, json: { detail: '该方案仍在使用，请先切换。' } });
      for (const task of state.tasks) if (profile.versions.some(version => version.source_task_id === task.id)) { task.candidate_discarded = true; task.result = null; }
      state.profiles = state.profiles.filter(item => item.id !== profile.id);
      return route.fulfill({ status: 204 });
    }
    const update = path.match(/^\/style\/profiles\/([^/]+)\/(versions|restore)$/);
    if (update) {
      const profile = state.profiles.find(item => item.id === update[1])!; const body = data();
      const source = update[2] === 'restore' ? profile.versions.find(item => item.id === body.version_id)! : body;
      profile.latest_version += 1; profile.versions.push({ ...source, id: `${profile.id}-v${profile.latest_version}`, version: profile.latest_version, created_at: timestamp });
      return route.fulfill({ json: profile });
    }
    if (path.startsWith('/style/applications/')) {
      const kind = path.endsWith('personality') ? 'personality' : 'writing'; state.applications[kind] = data().version_id;
      if (kind === 'personality') state.initialized = true;
      return route.fulfill({ json: state.applications });
    }
    if (path === '/style/corpora' && method === 'POST') {
      state.uploads.push(data());
      if (state.failUpload) return route.fulfill({ status: 422, json: { detail: '编码无法识别，请转为 UTF-8 TXT 后重新选择。' } });
      state.kind = data().kind; state.corpusId += 1; return route.fulfill({ json: corpus() });
    }
    if (path.startsWith('/style/corpora/')) {
      if (method === 'DELETE') { state.cleaned.push(path.split('/').at(-1)!); return route.fulfill({ status: 204 }); }
      return route.fulfill({ json: corpus() });
    }
    if (path === '/style/tasks' && method === 'GET') return route.fulfill({ json: state.tasks });
    if (path === '/style/tasks' && method === 'POST') {
      state.starts.push(data()); const task = { id: `task-${state.tasks.length + 1}`, kind: 'style_distill', status: 'pending', error: null, result: null, cleanup_state: 'pending', corpus_id: `corpus-${state.corpusId}` };
      state.tasks.unshift(task); return route.fulfill({ json: task });
    }
    if (path === '/style/trials') {
      state.trials.push(data()); const task = { id: `trial-${state.tasks.length + 1}`, kind: 'style_trial', mode: data().mode ?? 'reply', status: 'pending', error: null, result: null, cleanup_state: 'not_applicable', corpus_id: null };
      state.tasks.unshift(task); return route.fulfill({ json: task });
    }
    const taskMatch = path.match(/^\/style\/tasks\/([^/]+)(\/cancel|\/discard)?$/);
    if (taskMatch) {
      const task = state.tasks.find(item => item.id === taskMatch[1])!;
      if (taskMatch[2] === '/discard') {
        if (state.failDiscard) return route.fulfill({ status: 503, json: { detail: '丢弃失败，请重试；当前输入保留。' } });
        task.candidate_discarded = true; task.result = null;
      }
      else if (taskMatch[2]) { task.status = 'cancelled'; task.cleanup_state = 'cleaned'; }
      else if (!state.holdTask && task.status === 'pending') { task.status = 'succeeded'; task.cleanup_state = task.kind === 'style_distill' ? 'cleaned' : 'not_applicable'; task.result = task.kind === 'style_distill' ? result() : task.mode === 'refine' ? { ...result(), prompt: '修订候选：按当前话头回应，说完就停；求助时再展开。' } : { text: '这是一段隔离合成试用文字。', isolated: true }; }
      return route.fulfill({ json: task });
    }
    if (/^\/projects\/[^/]+\/writing-style$/.test(path)) {
      if (method === 'PUT') state.override = data().version_id;
      return route.fulfill({ json: projectStyle() });
    }
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic route: ${path}` } });
  });
  await page.goto('/');
  if (initialized) { await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible(); await page.getByRole('button', { name: '个人设置', exact: true }).click(); }
  await page.getByRole('button', { name: '管理风格方案', exact: true }).click();
  await expect(page.getByRole('heading', { name: '性格与写作风格', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '群消息', exact: true })).toHaveCount(0);
  await expect(page.getByLabel('聊天语料来源')).toHaveCount(0);
  return state;
}

async function uploadQQ(page: Page) {
  await page.getByLabel('选择 QQ TXT文件（可多选）').setInputFiles({ name: 'QQ.txt', mimeType: 'text/plain', buffer: Buffer.from('2026-10-23 10:00:00 小雨\n你好，合成消息。', 'utf8') });
  await page.getByRole('button', { name: '上传并预览解析', exact: true }).click();
  await expect(page.getByRole('heading', { name: '解析与分析范围' })).toBeVisible();
}

test('feedback revises a candidate without applying and preserves version provenance', async ({ page }, info) => {
  const state = await study(page);
  await page.getByLabel('方案名称', { exact: true }).fill('自然接话');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('平时简洁，有自己的判断。');
  await page.getByLabel('试聊情境与消息').fill('我先去吃饭。');
  await page.getByRole('button', { name: '开始试聊', exact: true }).click();
  await expect(page.getByLabel('隔离试用结果')).toContainText('这是一段隔离合成试用文字');
  await page.getByLabel('哪里不像，或者希望怎样回应').fill('收尾时不要继续追问。');
  await page.getByRole('button', { name: '生成修订候选', exact: true }).click();
  const revision = page.getByRole('region', { name: '调整交流表达' });
  await expect(revision).toContainText('修订候选：按当前话头回应');
  expect(state.trials[1].mode).toBe('refine');
  expect(state.trials[1].wrong).toBe('这是一段隔离合成试用文字。');
  expect(state.profiles).toHaveLength(0);
  expect(state.applications.personality).toBeNull();
  await page.screenshot({ path: info.outputPath('desktop-chat-refinement-candidate.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-chat-refinement-candidate.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '载入修订候选', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/修订候选/);
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 1');
  expect(state.profiles[0].versions[0].source_task_id).toBe('trial-2');
  expect(state.applications.personality).toBeNull();
  await page.screenshot({ path: info.outputPath('desktop-chat-refinement.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-chat-refinement.png'), fullPage: true });
});

test('QQ selection, isolated trial, save and apply are separate and preserve explicit version choice', async ({ page }, info) => {
  const state = await study(page);
  await uploadQQ(page);
  expect(state.uploads[0].files).toEqual([{ name: 'QQ.txt', content_base64: Buffer.from('2026-10-23 10:00:00 小雨\n你好，合成消息。').toString('base64') }]);
  await expect(page.getByRole('button', { name: '开始有限蒸馏', exact: true })).toBeDisabled();
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await page.getByRole('button', { name: '开始有限蒸馏', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/合成风格/);
  expect(state.starts).toEqual([{ corpus_id: 'corpus-1', target_speakers: ['0:小雨'], selection: [] }]);
  expect(state.profiles).toHaveLength(0); expect(state.applications.personality).toBeNull();
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('原始材料已清理');
  await page.getByLabel('方案名称', { exact: true }).fill('雨声性格');
  await page.getByLabel('试聊情境与消息').fill('朋友说今天有点累，接一句话。');
  await page.getByRole('button', { name: '开始试聊', exact: true }).click();
  await expect(page.getByLabel('隔离试用结果')).toContainText('这是一段隔离合成试用文字');
  expect(state.trials).toHaveLength(1); expect(state.trials[0].prompt).toContain('合成风格');
  expect(state.mutations.some(path => /onebot|chat|memory|chapters|autonomy/.test(path))).toBe(false);
  await page.getByLabel('方案名称', { exact: true }).press('Control+s');
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 1');
  expect(state.cleaned).toContain('corpus-1');
  await expect(page.getByLabel('蒸馏任务状态')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '解析与分析范围', exact: true })).toHaveCount(0);
  const savedBefore = await page.evaluate(() => {
    const rect = document.querySelector('.style-editor')?.getBoundingClientRect();
    const upload = document.querySelector('.style-upload')?.getBoundingClientRect();
    return { documentY: (rect?.top ?? 0) + window.scrollY, uploadHeight: upload?.height, minHeight: getComputedStyle(document.querySelector('.style-upload')!).minHeight, scrollY: window.scrollY };
  });
  await page.getByRole('button', { name: /雨声性格/ }).click();
  const savedAfter = await page.evaluate(() => {
    const rect = document.querySelector('.style-editor')?.getBoundingClientRect();
    const upload = document.querySelector('.style-upload')?.getBoundingClientRect();
    return { documentY: (rect?.top ?? 0) + window.scrollY, uploadHeight: upload?.height, minHeight: getComputedStyle(document.querySelector('.style-upload')!).minHeight, scrollY: window.scrollY };
  });
  expect(Math.abs(savedAfter.documentY - savedBefore.documentY)).toBeLessThanOrEqual(1);
  expect(state.applications.personality).toBeNull();
  await page.getByRole('button', { name: '应用为 LISA 性格', exact: true }).click();
  await expect(page.getByText('正在使用 · 版本 1', { exact: true })).toBeVisible();
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('新版本采用更舒缓的节奏。');
  await page.getByRole('button', { name: '保存为新版本', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 2');
  expect(state.applications.personality).toBe('profile-1-v1');
  await page.getByLabel('查看已保存版本').selectOption('profile-1-v1');
  await page.getByRole('button', { name: '恢复此版本为新保存版本' }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 3');
  expect(state.applications.personality).toBe('profile-1-v1');
  await page.screenshot({ path: info.outputPath('desktop-style-profile.png'), fullPage: true });
});

test('saved bookmark keeps the upload region stable and releases an unstarted corpus', async ({ page }) => {
  const state = await study(page);
  await page.getByLabel('方案名称', { exact: true }).fill('稳定书签');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('保存后可切换的合成性格。');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByLabel('上传语料与解析预览')).toBeVisible();

  await page.getByRole('button', { name: '新建交流性格方案', exact: true }).first().click();
  await uploadQQ(page);
  const before = await page.evaluate(() => {
    const upload = document.querySelector('.style-upload')?.getBoundingClientRect();
    const editor = document.querySelector('.style-editor')?.getBoundingClientRect();
    return { uploadTop: upload?.top, uploadHeight: upload?.height, editorTop: editor?.top };
  });
  await page.getByRole('button', { name: /稳定书签/ }).click();

  await expect(page.getByLabel('上传语料与解析预览')).toBeVisible();
  const after = await page.evaluate(() => {
    const upload = document.querySelector('.style-upload')?.getBoundingClientRect();
    const editor = document.querySelector('.style-editor')?.getBoundingClientRect();
    return { uploadTop: upload?.top, uploadHeight: upload?.height, editorTop: editor?.top };
  });
  expect(Math.abs((after.uploadTop ?? 0) - (before.uploadTop ?? 0))).toBeLessThanOrEqual(1);
  expect(Math.abs((after.editorTop ?? 0) - (before.editorTop ?? 0))).toBeLessThanOrEqual(1);
  expect(state.cleaned).toEqual(['corpus-1']);
});

test('multi-file writing range, LISA default and a work override remain independent', async ({ page }, info) => {
  const state = await study(page);
  await page.getByRole('button', { name: '写作风格', exact: true }).click();
  await page.getByLabel('选择 TXT / Markdown文件（可多选）').setInputFiles([{ name: '第一卷.md', mimeType: 'text/markdown', buffer: Buffer.from('# 第一章\n合成作品一') }, { name: '第二卷.txt', mimeType: 'text/plain', buffer: Buffer.from('合成作品二') }]);
  await page.getByRole('button', { name: '上传并预览解析' }).click();
  await page.getByLabel(/第一卷.md · 片段 1/).check();
  await page.getByLabel(/第二卷.txt · 片段 1/).check();
  await page.getByRole('button', { name: '开始有限蒸馏' }).click();
  await expect(page.getByLabel('写作风格指引', { exact: true })).toHaveValue(/合成风格/);
  expect(state.starts[0]).toEqual({ corpus_id: 'corpus-1', target_speakers: [], selection: ['0:0', '1:0'] });
  await page.getByLabel('方案名称', { exact: true }).fill('雨夜笔法');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '应用为 LISA 默认写作风格', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '应用为 LISA 默认写作风格', exact: true }).click();
  expect(state.applications.writing).toBe('profile-1-v1');
  await page.getByLabel('写作风格指引', { exact: true }).fill('仅此作品使用的合成笔法。');
  await page.getByRole('button', { name: '保存为新版本', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 2');
  // Inject only a project list response; the writing-style operations still use the real UI contract.
  await page.route('**/api/projects', route => route.fulfill({ json: [{ id: 'book-1', title: '合成山海' }] }));
  await page.getByRole('button', { name: '刷新方案列表', exact: true }).click();
  await page.getByLabel('选择作品', { exact: true }).selectOption('book-1');
  await expect(page.getByLabel('单作品写作风格').getByRole('status')).toContainText('跟随 LISA 默认');
  await page.getByRole('button', { name: '将所选版本用于此作品', exact: true }).click();
  await expect(page.getByLabel('单作品写作风格')).toContainText('此作品单独覆盖');
  expect(state.override).toBe('profile-1-v2'); expect(state.applications.writing).toBe('profile-1-v1');
  await page.getByRole('button', { name: '恢复跟随 LISA 默认', exact: true }).click();
  await expect(page.getByLabel('单作品写作风格').getByRole('status')).toContainText('跟随 LISA 默认');
  expect(state.override).toBeNull();
  await page.getByRole('button', { name: '应用为 LISA 默认写作风格', exact: true }).click();
  await expect(page.getByLabel('单作品写作风格')).toContainText('2 版');
  expect(state.applications.writing).toBe('profile-1-v2');
  await page.getByText('覆盖、证据与验证说明', { exact: true }).click();
  await expect(page.getByText('来源定位：0:0、1:0', { exact: true })).toBeVisible();
  await page.screenshot({ path: info.outputPath('desktop-writing-style.png'), fullPage: true });
});

test('mobile save failure retains edits, leave has three choices, and abandon cleans unstarted uploads', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await study(page);
  await uploadQQ(page);
  await page.getByLabel('方案名称', { exact: true }).fill('未保存方案');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('手写合成候选，保留输入。');
  await page.getByRole('button', { name: '我的作品', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '有未保存的风格修改' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '继续编辑', exact: true }).click();
  state.failSave = true;
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click();
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('合成保存失败');
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('手写合成候选，保留输入。');
  expect(state.cleaned).toHaveLength(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-style-editor.png'), fullPage: true });
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click();
  await dialog.getByRole('button', { name: '丢弃候选', exact: true }).click();
  await expect(page.getByRole('button', { name: '管理风格方案', exact: true })).toBeVisible();
  expect(state.cleaned).toContain('corpus-1'); expect(state.profiles).toHaveLength(0);
});

test('discard survives reentry and reload, and failures retain the candidate', async ({ page }) => {
  const state = await study(page);
  await uploadQQ(page);
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await page.getByRole('button', { name: '开始有限蒸馏', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/合成风格/);
  state.failDiscard = true;
  await page.getByRole('button', { name: '丢弃候选', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '丢弃候选', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('丢弃失败');
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/合成风格/);
  state.failDiscard = false;
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '丢弃候选', exact: true }).click();
  await page.getByRole('button', { name: '管理风格方案', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('');
  await page.reload();
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '管理风格方案', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('');
  await page.getByText('恢复历史候选', { exact: true }).click();
  await expect(page.getByRole('button', { name: /^恢复交流性格候选/ })).toHaveCount(0);
  expect(state.tasks[0].candidate_discarded).toBe(true);
  expect(state.profiles).toHaveLength(0);
});

test('history requires explicit recovery, saved edits undo independently, and deletion is confirmed', async ({ page }, info) => {
  const state = await study(page);
  await uploadQQ(page);
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await page.getByRole('button', { name: '开始有限蒸馏', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/合成风格/);
  // Reopening the browser loses local edits but leaves the server result available.
  page.once('dialog', dialog => dialog.accept());
  await page.reload();
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '管理风格方案', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('');
  await page.getByLabel('方案名称', { exact: true }).fill('临时手写');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('当前编辑不能被刷新覆盖');
  await page.getByRole('button', { name: '刷新方案列表', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('当前编辑不能被刷新覆盖');
  await page.getByText('恢复历史候选', { exact: true }).click();
  await page.getByRole('button', { name: /^恢复交流性格候选/ }).click();
  await page.getByRole('dialog').getByRole('button', { name: '丢弃候选', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/合成风格/);
  await page.getByLabel('方案名称', { exact: true }).fill('可删除性格');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 1');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('尚未保存的新修改');
  await page.getByRole('button', { name: '撤销本次修改', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '撤销本次修改', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue(/合成风格/);
  expect(state.profiles[0].versions).toHaveLength(1);
  await page.getByRole('button', { name: '删除方案', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '删除方案', exact: true });
  await expect(confirmation.getByRole('button', { name: '保留方案', exact: true })).toBeFocused();
  await page.keyboard.press('Control+s');
  expect(state.profiles[0].versions).toHaveLength(1);
  await confirmation.getByRole('button', { name: '保留方案', exact: true }).click();
  expect(state.profiles).toHaveLength(1);
  await page.getByRole('button', { name: '删除方案', exact: true }).click();
  await page.screenshot({ path: info.outputPath('desktop-style-delete.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-style-delete.png'), fullPage: true });
  await confirmation.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('');
  await expect(page.getByRole('button', { name: /可删除性格/ })).toHaveCount(0);
  expect(state.profiles).toHaveLength(0);
  await page.getByText('恢复历史候选', { exact: true }).click();
  await expect(page.getByRole('button', { name: /^恢复交流性格候选/ })).toHaveCount(0);
});

test('deleting a profile in use keeps the library, application and unsaved input', async ({ page }) => {
  const state = await study(page);
  await page.getByLabel('方案名称', { exact: true }).fill('正在使用的性格');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('已保存性格');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await page.getByRole('button', { name: '应用为 LISA 性格', exact: true }).click();
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('删除失败要保留的输入');
  await page.getByRole('button', { name: '删除方案', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('仍在使用');
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('删除失败要保留的输入');
  expect(state.profiles).toHaveLength(1);
  expect(state.applications.personality).toBe('profile-1-v1');
});

test('cancellation never invents a result and refresh recovers in-progress tasks', async ({ page }) => {
  const state = await study(page);
  state.holdTask = true;
  await uploadQQ(page);
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await page.getByRole('button', { name: '开始有限蒸馏' }).click();
  await expect(page.getByRole('button', { name: '取消蒸馏任务', exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '管理风格方案', exact: true }).click();
  await expect(page.getByRole('button', { name: '取消蒸馏任务', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '取消蒸馏任务', exact: true }).click();
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('已取消');
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('原始材料已清理');
  await expect(page.getByLabel('交流性格 prompt', { exact: true })).toHaveValue('');
  expect(state.profiles).toHaveLength(0); expect(state.applications.personality).toBeNull();
});

test('upstream 502 explains the model failure while preserving cleanup and no automatic retry', async ({ page }) => {
  const state = await study(page);
  state.holdTask = true;
  await uploadQQ(page);
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await page.getByRole('button', { name: '开始有限蒸馏', exact: true }).click();
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('等待执行');
  Object.assign(state.tasks[0], { status: 'failed', error: 'model_unknown', model_http_status: 502, cleanup_state: 'cleaned' });
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('模型服务返回 HTTP 502');
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('调用结果及计费尚未确认');
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('原始材料已清理');
  expect(state.starts).toHaveLength(1);
  await expect(page.getByRole('button', { name: '取消蒸馏任务' })).toHaveCount(0);
});

test('an invalid encoding preserves the selected files for a corrected retry', async ({ page }) => {
  const state = await study(page);
  state.failUpload = true;
  await page.getByLabel('选择 QQ TXT文件（可多选）').setInputFiles({ name: 'QQ.txt', mimeType: 'text/plain', buffer: Buffer.from([0xff, 0xfe, 0x00]) });
  await page.getByRole('button', { name: '上传并预览解析' }).click();
  await expect(page.getByRole('alert')).toContainText('编码无法识别');
  expect(await page.getByLabel('选择 QQ TXT文件（可多选）').evaluate((element: HTMLInputElement) => element.files?.length)).toBe(1);
  expect(state.starts).toHaveLength(0);
  state.failUpload = false;
  await page.getByRole('button', { name: '上传并预览解析' }).click();
  await expect(page.getByRole('heading', { name: '解析与分析范围' })).toBeVisible();
  expect(state.uploads).toHaveLength(2);
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await expect(page.getByRole('button', { name: '开始有限蒸馏', exact: true })).toBeEnabled();
  await page.getByLabel('选择 QQ TXT文件（可多选）').setInputFiles({ name: 'QQ-corrected.txt', mimeType: 'text/plain', buffer: Buffer.from('重新选择的合成文件。', 'utf8') });
  await expect(page.getByText('上次解析已失效。当前所选文件尚未成功上传，请重新上传并预览；不能开始分析。', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '开始有限蒸馏', exact: true })).toBeDisabled();
  state.failUpload = true;
  await page.getByRole('button', { name: '上传并预览解析', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('编码无法识别');
  await expect(page.getByRole('button', { name: '开始有限蒸馏', exact: true })).toBeDisabled();
  expect(await page.getByLabel('选择 QQ TXT文件（可多选）').evaluate((element: HTMLInputElement) => element.files?.[0]?.name)).toBe('QQ-corrected.txt');
  expect(state.starts).toHaveLength(0);
  state.failUpload = false;
  await page.getByRole('button', { name: '上传并预览解析', exact: true }).click();
  await expect(page.getByText('上次解析已失效。当前所选文件尚未成功上传，请重新上传并预览；不能开始分析。', { exact: true })).toHaveCount(0);
  await page.getByLabel('小雨 · 4 条消息 · 文件 1（QQ.txt）').check();
  await expect(page.getByRole('button', { name: '开始有限蒸馏', exact: true })).toBeEnabled();
});

test('a bound account can initialize LISA by explicitly applying a saved personality', async ({ page }) => {
  const state = await study(page, false);
  await page.getByLabel('方案名称', { exact: true }).fill('初始化候选');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('合成初始化性格，不调用模型。');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 1');
  expect(state.initialized).toBe(false);
  await page.getByRole('button', { name: '应用为 LISA 性格', exact: true }).click();
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click();
  await expect(page.getByText('初始化已完成', { exact: true })).toBeVisible();
  expect(state.initialized).toBe(true);
});

test('real isolated API parses QQ bytes, cancels and cleans, then persists explicit personality and work style versions', async ({ page }, info) => {
  const failures: string[] = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.goto('/');
  await page.getByLabel('用户名', { exact: true }).fill('styles_reader');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-password-2026');
  await page.getByRole('button', { name: '进入书房', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
  await page.getByLabel('作品名称', { exact: true }).fill('隔离风格测试作品');
  await page.getByRole('button', { name: '创建小说', exact: true }).click();
  await expect(page.getByRole('heading', { name: '隔离风格测试作品', exact: true })).toBeVisible();
  const projects = await (await page.request.get('/api/projects')).json();
  const projectId = projects.find((item: { title: string }) => item.title === '隔离风格测试作品').id;
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '管理风格方案', exact: true }).click();
  const recommendation = page.getByRole('link', { name: 'QQChatExporter V5', exact: true });
  await expect(recommendation).toHaveAttribute('href', 'https://github.com/shuakami/qq-chat-exporter');
  await expect(page.getByText('上传预览由程序解析参与者，不调用模型', { exact: false })).toBeVisible();
  const source = readFileSync(new URL('../fixtures/qq_exporter_v5.txt', import.meta.url), 'utf8').replace('结尾的停顿', '结尾\u0000的停顿');
  await page.getByLabel('选择 QQ TXT文件（可多选）').setInputFiles({ name: 'QQ-真实解析合成.txt', mimeType: 'text/plain', buffer: Buffer.from(source, 'utf8') });
  await page.getByRole('button', { name: '上传并预览解析', exact: true }).click();
  await expect(page.getByRole('heading', { name: '解析与分析范围' })).toBeVisible();
  await expect(page.getByText('文件 1 含 1 个空字符，已用 � 标记；原文件未修改。', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('checkbox')).toHaveCount(2);
  await page.screenshot({ path: info.outputPath('desktop-v5-upload.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(recommendation).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('mobile-v5-upload.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByLabel(/^小雨\(700000095\) · 2 条消息/).check();
  const startedResponse = page.waitForResponse(response => response.url().endsWith('/api/style/tasks') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始有限蒸馏', exact: true }).click();
  const started = await (await startedResponse).json();
  expect(started.status).toBe('pending');
  await page.getByRole('button', { name: '取消蒸馏任务', exact: true }).click();
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('已取消');
  await expect(page.getByLabel('蒸馏任务状态')).toContainText('原始材料已清理');
  const cleaned = await (await page.request.get(`/api/style/corpora/${started.corpus_id}`)).json();
  expect(cleaned.cleanup_state).toBe('cleaned');
  expect(cleaned.preview.participants).toEqual([]);
  await page.getByLabel('方案名称', { exact: true }).fill('明确采用的交流性格');
  await page.getByLabel('交流性格 prompt', { exact: true }).fill('合成性格：温和简洁，不把语料中的经历视为自己的经历。');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 1');
  let library = await (await page.request.get('/api/style/profiles')).json();
  expect(library.applications.personality).toBeNull();
  await page.getByRole('button', { name: '应用为 LISA 性格', exact: true }).click();
  await expect(page.getByText('正在使用 · 版本 1', { exact: true })).toBeVisible();
  const setup = await (await page.request.get('/api/setup')).json();
  expect(setup.persona.prompt).toContain('合成性格：温和简洁');
  await page.getByRole('button', { name: '写作风格', exact: true }).click();
  await page.getByLabel('方案名称', { exact: true }).fill('明确采用的写作风格');
  await page.getByLabel('写作风格指引', { exact: true }).fill('合成笔法：以动作呈现心情，句式克制，不复制原作剧情。');
  await page.getByRole('button', { name: '保存方案', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 1');
  await page.getByRole('button', { name: '应用为 LISA 默认写作风格', exact: true }).click();
  await expect(page.getByText('正在使用 · 版本 1', { exact: true })).toBeVisible();
  library = await (await page.request.get('/api/style/profiles')).json();
  const appliedV1 = library.applications.writing;
  await page.getByLabel('写作风格指引', { exact: true }).fill('合成笔法第二版：更舒展的段落，仍保持原作事实隔离。');
  await page.getByRole('button', { name: '保存为新版本', exact: true }).click();
  await expect(page.getByRole('status', { name: '风格保存状态' })).toContainText('已保存版本 2');
  library = await (await page.request.get('/api/style/profiles')).json();
  expect(library.applications.writing).toBe(appliedV1);
  await page.getByLabel('选择作品', { exact: true }).selectOption(projectId);
  await expect(page.getByLabel('单作品写作风格').getByRole('status')).toContainText('跟随 LISA 默认');
  await page.getByRole('button', { name: '将所选版本用于此作品', exact: true }).click();
  await expect(page.getByLabel('单作品写作风格')).toContainText('此作品单独覆盖');
  const override = await (await page.request.get(`/api/projects/${projectId}/writing-style`)).json();
  expect(override.override_version_id).not.toBe(appliedV1);
  expect(override.source).toBe('override');
  await page.screenshot({ path: info.outputPath('real-api-writing-style.png'), fullPage: true });
  await page.getByRole('button', { name: '恢复跟随 LISA 默认', exact: true }).click();
  await expect(page.getByLabel('单作品写作风格').getByRole('status')).toContainText('跟随 LISA 默认');
  const following = await (await page.request.get(`/api/projects/${projectId}/writing-style`)).json();
  expect(following.override_version_id).toBeNull(); expect(following.effective_version_id).toBe(appliedV1);
  await page.getByRole('button', { name: '我的作品', exact: true }).click();
  await page.getByRole('button', { name: '创作资料', exact: true }).click();
  await page.getByRole('button', { name: '写作风格', exact: true }).click();
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('跟随 LISA 默认');
  const legacyNotes = page.getByLabel('本作品补充写法（可选）');
  if (!(await legacyNotes.isVisible())) await page.locator('.material-legacy').filter({ has: legacyNotes }).locator('summary').click();
  await legacyNotes.fill('本作品用第三人称限知视角，保留已选风格的句式。');
  await page.getByLabel('风格方案', { exact: true }).selectOption(override.override_version_id);
  await page.getByRole('button', { name: '保存写作设置' }).click();
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('本作品单独使用');
  const workSettings = await (await page.request.get(`/api/projects/${projectId}`)).json();
  expect(workSettings.author_prompt).toBe('本作品用第三人称限知视角，保留已选风格的句式。');
  const selectedStyle = await (await page.request.get(`/api/projects/${projectId}/writing-style`)).json();
  expect(selectedStyle.override_version_id).toBe(override.override_version_id);
  await page.getByLabel('风格方案', { exact: true }).selectOption('');
  await page.getByRole('button', { name: '保存写作设置' }).click();
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('跟随 LISA 默认');
  await page.getByRole('button', { name: '管理或提炼写作风格', exact: true }).click();
  await expect(page.getByRole('button', { name: '写作风格', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('选择作品', { exact: true })).toHaveValue(projectId);
  await page.getByRole('button', { name: '返回我的作品', exact: true }).click();
  await expect(page.getByRole('heading', { name: '隔离风格测试作品', exact: true })).toBeVisible();
  expect(failures).toEqual([]);
});


test('persistent style navigation and the persona editor shortcut both protect unsaved changes', async ({ page }, info) => {
  await study(page);
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click();
  await page.getByRole('button', { name: '我的作品', exact: true }).click();
  const entry = page.getByRole('navigation', { name: '书房导航' }).getByRole('button', { name: '性格与风格', exact: true });
  await entry.click();
  await expect(entry).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('button', { name: '交流性格', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: '返回我的作品', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的作品', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '个人设置', exact: true }).click();
  await page.getByRole('button', { name: '调整 LISA 性格', exact: true }).click();
  await page.getByLabel('LISA 性格 prompt', { exact: true }).fill('尚未保存的交流性格。');
  await page.getByRole('button', { name: '从 QQ 记录提炼性格', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(page.getByLabel('LISA 性格 prompt', { exact: true })).toHaveValue('尚未保存的交流性格。');
  await entry.click();
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(page.getByLabel('选择 QQ TXT文件（可多选）')).toBeVisible();
  await page.getByRole('button', { name: '返回账号设置', exact: true }).click();
  await page.getByRole('button', { name: '从 QQ 记录提炼性格', exact: true }).click();
  await expect(page.getByRole('button', { name: '交流性格', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.screenshot({ path: info.outputPath('desktop-style-navigation.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390);
  await expect(entry).toBeVisible();
  await page.screenshot({ path: info.outputPath('mobile-style-navigation.png'), fullPage: true });
});
