import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { Character } from '../../web/characters'
import type { WorldbookEntry } from '../../web/worldbook'
import type { StyleLibrary, WritingStyle } from '../../web/api'
import { readFile } from 'node:fs/promises'
import { plotFields, writingFields, readSections, readItems } from '../../web/material-text'

const doc = (text: string) => ({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] })
const work = () => ({ id: 'work-1', title: '雨夜来信', kind: 'novel', is_current: true, completed: false, worldbook: '', worldbook_entries: [] as WorldbookEntry[], constraints: '', characters: '', author_prompt: '', summary: '', plot_state: '', agreement: '', preferences: '', tentative: '' })
const chapter = () => ({ id: 'chapter-1', title: '第一章 重逢', position: 1, content: doc('她站在雨里的车站。'), text: '她站在雨里的车站。', version: 1 })
const task = () => ({ id: 'task-1', kind: 'scene', goal: '描写雨夜重逢的一个场景。', status: 'pending', error: null as string | null, can_cancel: true, can_retry: false, input_tokens: 0, output_tokens: 0, charged_usd: '0', reserved_usd: '0.02', adopted_at: null as string | null })
const schedule = () => ({ enabled: true, status: 'no_plan', pause_reason: null as string | null, due_at: null as string | null, goal: null as string | null, project_id: null as string | null, remaining_scenes: 0 })
const characterRetrievalConfig = () => ({ enabled: false, limit: 3, max_chars: 1500 })
const retrievalConfig = () => ({ sources: ['memory', 'project', 'chapter'], limit: 5, max_chars: 1500 })

async function stubStudy(page: Page, seeded = false) {
  const state = {
    project: seeded ? { ...work(), agreement: '现代都市小说，叙述克制。' } : null as ReturnType<typeof work> | null,
    others: [] as ReturnType<typeof work>[],
    handoff: [] as (ReturnType<typeof task> & { project_id: string; project_title: string })[],
    lifecycleFails: false,
    versionFails: false,
    restoreFails: false,
    exportFails: false,
    retrievalFails: false,
    retrievalConfigs: { 'work-1': retrievalConfig() } as Record<string, ReturnType<typeof retrievalConfig>>,
    characterFails: false,
    characterDeleteFails: false,
    characters: [] as Character[],
    participationFails: false,
    participations: [] as { position: number; character_id: string | null; name: string | null; revision: number; state: string; created_at: string }[],
    referenceFails: false,
    references: [] as { position: number; source_kind: string; revision: number; start: number; end: number; state: string; title: string | null; text: string | null }[],
    versions: seeded ? [{ ...chapter(), source: 'generated', created_at: '2026-10-23T10:00:00Z' }] : [] as (ReturnType<typeof chapter> & { source: string; created_at: string })[],
    chapters: seeded ? [chapter()] : [] as ReturnType<typeof chapter>[],
    tasks: [] as ReturnType<typeof task>[],
    saveFails: false,
    styleSaveFails: false,
    styleLoadFails: false,
    styleLibrary: { profiles: [], applications: { personality: null, writing: null } } as StyleLibrary,
    writingStyles: {} as Record<string, WritingStyle>,
    schedule: schedule(),
    scheduleFails: false,
    chapterSaves: [] as { title: string; content: object }[],
    requests: [] as { path: string; body: Record<string, unknown>; summary: boolean }[],
  }
  await page.route('**/api/**', async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname.replace('/api', '')
    const body = req.method() === 'GET' ? {} : req.postDataJSON()
    const summary = new URL(req.url()).searchParams.get('summary') === 'true'
    state.requests.push({ path, body, summary })
    if (path === '/auth/me') return route.fulfill({ json: { id: 'synthetic-reader', username: '合成读者' } })
    if (path === '/setup') return route.fulfill({ json: { qq_id: '700000099', initialized: true, persona: { name: '测试小莎', prompt: '只用于合成浏览器测试。', version: 1 }, presets: [] } })
    if (path === '/service-status') return route.fulfill({ json: { status: 'ready', message: '合成服务已就绪。' } })
    if (path === '/schedule') return route.fulfill(state.scheduleFails ? { status: 503, json: { detail: '合成计划状态读取失败。' } } : { json: state.schedule })
    if (path === '/setup/persona') return route.fulfill({ json: { qq_id: '700000099', initialized: true, persona: { ...body, version: 2 }, presets: [] } })
    if (path === '/projects/handoff') return route.fulfill({ json: { finishing_tasks: state.handoff } })
    if (path === '/style/profiles') return route.fulfill(state.styleLoadFails ? { status: 503, json: { detail: '合成风格读取失败。' } } : { json: state.styleLibrary })
    const writingStyle = path.match(/^\/projects\/(work-\d+)\/writing-style$/)
    if (writingStyle) {
      if (state.styleLoadFails || (req.method() === 'PUT' && state.styleSaveFails)) return route.fulfill({ status: 503, json: { detail: '合成风格方案未应用。' } })
      const current = state.writingStyles[writingStyle[1]] ?? { override_version_id: null, effective_version_id: null, effective_profile_name: null, source: 'none', prompt: '' }
      const override = req.method() === 'PUT' ? body.version_id : current.override_version_id
      const effective = override ?? state.styleLibrary.applications.writing
      const profile = state.styleLibrary.profiles.find(item => item.versions.some(version => version.id === effective))
      const version = profile?.versions.find(item => item.id === effective)
      const view: WritingStyle = { override_version_id: override, effective_version_id: effective, effective_profile_name: profile?.name ?? null, source: override ? 'override' : effective ? 'default' : 'none', prompt: version?.prompt ?? '' }
      state.writingStyles[writingStyle[1]] = view
      return route.fulfill({ json: view })
    }
    const retrieval = path.match(/^\/projects\/(work-\d+)\/retrieval$/)
    if (retrieval) {
      if (state.retrievalFails || (req.method() === 'PUT' && state.saveFails)) return route.fulfill({ status: 503, json: { detail: '合成检索配置失败，请重试。' } })
      if (req.method() === 'PUT') state.retrievalConfigs[retrieval[1]] = body
      return route.fulfill({ json: { config: state.retrievalConfigs[retrieval[1]] ?? retrievalConfig(), defaults: retrievalConfig(), enabled: false } })
    }
    if (path === '/characters/defaults') return route.fulfill({ json: { retrieval_config: characterRetrievalConfig(), indexing_enabled: false } })
    if (/^\/tasks\/task-\d+\/references$/.test(path)) return route.fulfill(state.referenceFails ? { status: 503, json: { detail: '合成参考资料读取失败。' } } : { json: { references: state.references } })
    const characterList = path.match(/^\/projects\/(work-\d+)\/characters$/)
    if (characterList) {
      if (state.characterFails || (req.method() === 'POST' && state.saveFails)) return route.fulfill({ status: 503, json: { detail: '合成角色保存或读取失败。' } })
      if (req.method() === 'POST') {
        const role = { ...body, id: `character-${state.characters.length + 1}`, project_id: characterList[1], revision: 1, created_at: '2026-10-23T10:00:00Z' }
        state.characters.push(role)
        return route.fulfill({ status: 201, json: role })
      }
      return route.fulfill({ json: state.characters.filter(item => item.project_id === characterList[1]) })
    }
    const characterPath = path.match(/^\/characters\/(character-\d+)$/)
    if (characterPath) {
      const role = state.characters.find(item => item.id === characterPath[1])!
      if (state.saveFails || (req.method() === 'DELETE' && state.characterDeleteFails)) return route.fulfill({ status: 503, json: { detail: '合成角色修改失败。' } })
      if (req.method() === 'DELETE') {
        state.characters = state.characters.filter(item => item.id !== role.id)
        return route.fulfill({ status: 204 })
      }
      Object.assign(role, body, { revision: role.revision + 1 })
      return route.fulfill({ json: role })
    }
    if (/^\/tasks\/task-\d+\/characters$/.test(path)) return route.fulfill(state.participationFails ? { status: 503, json: { detail: '合成角色参与读取失败。' } } : { json: { participations: state.participations } })
    if (path === '/projects/work-1/export') {
      if (state.exportFails) return route.fulfill({ status: 503, json: { detail: '合成正文导出失败，请重试。' } })
      return route.fulfill({ contentType: 'text/plain; charset=utf-8', headers: { 'Content-Disposition': "attachment; filename*=UTF-8''LISA-%E9%9B%A8%E5%A4%9C%E6%9D%A5%E4%BF%A1.txt" }, body: `${state.project!.title}\n\n${state.chapters.map(item => `${item.title}\n\n${item.content.content[0].content[0].text}`).join('\n\n')}\n` })
    }
    if (path === '/chapters/chapter-1/versions') {
      if (state.versionFails) return route.fulfill({ status: 503, json: { detail: '合成版本读取失败，请重试。' } })
      return route.fulfill({ json: [...state.versions].reverse().map(({ version, title, source, created_at }) => ({ version, title, source, created_at })) })
    }
    const versionPath = path.match(/^\/chapters\/chapter-1\/versions\/(\d+)(\/restore)?$/)
    if (versionPath) {
      const saved = state.versions.find(item => item.version === Number(versionPath[1]))!
      if (versionPath[2]) {
        if (state.restoreFails) return route.fulfill({ status: 503, json: { detail: '合成恢复失败，请重试。' } })
        state.chapters[0] = { ...state.chapters[0], title: saved.title, content: saved.content, version: state.chapters[0].version + 1 }
        state.versions.push({ ...state.chapters[0], source: `restore:${saved.version}`, created_at: '2026-10-23T12:00:00Z' })
        return route.fulfill({ json: state.chapters[0] })
      }
      return route.fulfill({ json: { ...saved, text: saved.content.content[0].content[0].text } })
    }
    if (path === '/projects' && req.method() === 'POST') {
      if (state.lifecycleFails) return route.fulfill({ status: 503, json: { detail: '合成创建失败，请重试。' } })
      const created = { ...work(), id: state.project ? `work-${state.others.length + 2}` : 'work-1', title: body.title, kind: body.kind }
      if (state.project) { state.project.is_current = false; state.others.forEach(item => { item.is_current = false }); state.others.push(created) }
      else state.project = created
      return route.fulfill({ status: 201, json: created })
    }
    const lifecycle = path.match(/^\/projects\/(work-\d+)\/(activate|complete)$/)
    if (lifecycle) {
      if (state.lifecycleFails) return route.fulfill({ status: 503, json: { detail: '合成状态保存失败，请重试。' } })
      const works = state.project ? [state.project, ...state.others] : []
      const selected = works.find(item => item.id === lifecycle[1])!
      if (lifecycle[2] === 'activate') { works.forEach(item => { item.is_current = item.id === selected.id }); selected.completed = false }
      else selected.completed = true
      return route.fulfill({ json: selected })
    }
    const other = state.others.find(item => path.startsWith(`/projects/${item.id}`))
    if (other) {
      if (path.endsWith('/chapters') || path.endsWith('/tasks')) return route.fulfill({ json: [] })
      return route.fulfill({ json: other })
    }
    if (path === '/projects') {
      const items = state.project ? [state.project, ...state.others] : []
      return route.fulfill({ json: items.map(item => summary ? { id: item.id, title: item.title, kind: item.kind, is_current: item.is_current, completed: item.completed, index_revision: 1, material_revision: JSON.stringify(item) } : item) })
    }
    if (path === '/projects/work-1' && req.method() === 'GET') return route.fulfill({ json: { ...state.project, index_revision: 1 } })
    if (path === '/projects/work-1' && req.method() === 'PUT') {
      if (state.saveFails) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请重试。' } })
      state.project = { ...state.project!, ...body }; return route.fulfill({ json: state.project })
    }
    if (path === '/projects/work-1/chapters') return route.fulfill({ json: summary ? state.chapters.map(({ id, title, position, version }) => ({ id, title, position, version })) : state.chapters })
    if (/^\/chapters\/chapter-\d+$/.test(path) && req.method() === 'GET') return route.fulfill({ json: state.chapters.find(item => path.endsWith(item.id)) })
    if (path === '/chapters/chapter-1' && req.method() === 'PUT') {
      if (state.saveFails) return route.fulfill({ status: 503, json: { detail: '合成保存失败，请重试。' } })
      state.chapterSaves.push(body)
      state.chapters[0] = { ...state.chapters[0], ...body, version: state.chapters[0].version + 1 }
      state.versions.push({ ...state.chapters[0], source: 'manual', created_at: '2026-10-23T11:00:00Z' })
      return route.fulfill({ json: state.chapters[0] })
    }
    if (path === '/projects/work-1/tasks' && req.method() === 'POST') {
      const next = { ...task(), ...body, id: `task-${state.tasks.length + 1}` }
      state.tasks.unshift(next); return route.fulfill({ status: 201, json: next })
    }
    if (path === '/projects/work-1/tasks') return route.fulfill({ json: state.tasks })
    if (path === '/tasks/task-1/cancel') {
      state.handoff = state.handoff.filter(item => item.id !== 'task-1')
      state.tasks[0] = { ...state.tasks[0], status: 'cancelled', can_cancel: false, reserved_usd: '0' }
      return route.fulfill({ json: state.tasks[0] })
    }
    if (path === '/tasks/task-1/retry') {
      const next = { ...state.tasks[0], id: 'task-2', status: 'pending', can_cancel: true, can_retry: false, error: null, charged_usd: '0', reserved_usd: '0.02' }
      state.tasks.unshift(next); return route.fulfill({ status: 201, json: next })
    }
    return route.fulfill({ status: 404, json: { detail: `Unexpected synthetic route: ${path}` } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  return state
}

test('guided requirements and discussion preserve legacy fields, examples and only changed inputs', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  Object.assign(state.project!, { constraints: '不可出现魔法。', preferences: '更偏好克制对白。', tentative: '寄信人身份未定。', author_prompt: '保留原有作者指引。' })
  await page.clock.fastForward(5_100)
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('现代都市小说，叙述克制。')
  await expect(page.getByLabel('必须遵守（可选）')).toHaveValue('不可出现魔法。')
  await expect(page.getByRole('button', { name: '作者 prompt', exact: true })).toHaveCount(0)
  const direction = page.locator('.guided-field').filter({ has: page.getByLabel('作品方向（已确认）') })
  await direction.getByText('看一个填写示例', { exact: true }).click()
  await expect(direction.getByRole('button', { name: '填入此示例' })).toBeDisabled()
  await page.getByLabel('作品方向（已确认）').fill('')
  await direction.getByRole('button', { name: '填入此示例' }).click()
  expect(state.project!.agreement).toBe('现代都市小说，叙述克制。')
  state.saveFails = true
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue(/现实悬疑小说/)
  state.saveFails = false
  await page.getByLabel('作品方向（已确认）').press('Control+s')
  await expect.poll(() => state.project!.agreement).toContain('现实悬疑小说')
  expect(state.project!.constraints).toBe('不可出现魔法。')
  await page.getByLabel('必须遵守（可选）').fill('不得靠偶然破解谜题。')
  state.project!.agreement = 'QQ 中新确认的作品方向。'
  await page.clock.fastForward(5_100)
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(() => state.project!.constraints).toBe('不得靠偶然破解谜题。')
  expect(state.project!.agreement).toBe('QQ 中新确认的作品方向。')
  expect(state.requests.filter(item => item.path === '/projects/work-1' && 'constraints' in item.body).at(-1)!.body).toEqual({ constraints: '不得靠偶然破解谜题。' })
  await page.screenshot({ path: testInfo.outputPath('desktop-guided-requirements.png'), fullPage: true })
  await page.getByRole('button', { name: '讨论记录', exact: true }).click()
  await expect(page.getByLabel('偏好倾向内容')).not.toBeVisible()
  await page.getByText('偏好倾向', { exact: true }).click()
  await page.getByText('待确认想法', { exact: true }).click()
  await expect(page.getByLabel('偏好倾向内容')).toHaveValue('更偏好克制对白。')
  await expect(page.getByLabel('待确认想法内容')).toHaveValue('寄信人身份未定。')
  await page.getByLabel('偏好倾向内容').fill('目前偏好短句，可按场景调整。')
  await page.getByRole('button', { name: '创作要求', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await page.getByLabel('偏好倾向内容').press('Control+s')
  await expect.poll(() => state.project!.preferences).toBe('目前偏好短句，可按场景调整。')
  expect(state.project!.tentative).toBe('寄信人身份未定。')
  expect(state.project!.author_prompt).toBe('保留原有作者指引。')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-guided-discussion.png'), fullPage: true })
})

test('work writing settings apply explicit versions with supplements and retain pending selection on partial failure', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.project!.author_prompt = '原有作者指引，不自动拆分。'
  state.styleLibrary = { profiles: [{ id: 'profile-a', kind: 'writing', name: '克制笔法', latest_version: 2, versions: [1, 2].map(version => ({ id: `version-${version}`, version, prompt: `方案指引${version}`, summary: '少解释，多动作。', report: {}, created_at: '2026-10-24T00:00:00Z' })) }], applications: { personality: null, writing: 'version-1' } }
  await page.clock.fastForward(5_100)
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByRole('button', { name: '写作风格', exact: true }).click()
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('克制笔法 · 版本 1')
  await expect(page.getByLabel('本作品补充写法（可选）')).toHaveValue('原有作者指引，不自动拆分。')
  await page.getByLabel('风格方案', { exact: true }).selectOption('version-2')
  expect(state.writingStyles['work-1'].override_version_id).toBeNull()
  await page.getByLabel('本作品补充写法（可选）').fill('采用第三人称限知视角。')
  state.styleSaveFails = true
  await page.getByRole('button', { name: '保存写作设置' }).click()
  await expect(page.getByRole('alert')).toContainText('补充写法已保存，风格方案尚未应用')
  expect(state.project!.author_prompt).toBe('采用第三人称限知视角。')
  await expect(page.getByLabel('风格方案', { exact: true })).toHaveValue('version-2')
  await page.clock.fastForward(5_100)
  await expect(page.getByLabel('风格方案', { exact: true })).toHaveValue('version-2')
  state.styleSaveFails = false
  await page.getByLabel('本作品补充写法（可选）').press('Control+s')
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('本作品单独使用 · 克制笔法 · 版本 2')
  expect(state.styleLibrary.profiles[0].versions[1].prompt).toBe('方案指引2')
  expect(state.writingStyles['work-2']).toBeUndefined()
  await page.getByText('查看所选方案的写作指引', { exact: true }).click()
  await expect(page.getByText('方案指引2', { exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('desktop-work-writing-style.png'), fullPage: true })
  await page.getByLabel('风格方案', { exact: true }).selectOption('')
  await page.getByRole('button', { name: '讨论记录', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await page.getByRole('button', { name: '写作风格', exact: true }).click()
  await expect(page.getByLabel('风格方案', { exact: true })).toHaveValue('version-2')
  await page.getByLabel('风格方案', { exact: true }).selectOption('')
  await page.getByRole('button', { name: '保存写作设置' }).click()
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('跟随 LISA 默认 · 克制笔法 · 版本 1')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-work-writing-style.png'), fullPage: true })
  await page.getByRole('button', { name: '讨论记录', exact: true }).click()
  state.styleLoadFails = true
  await page.getByRole('button', { name: '写作风格', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.getByLabel('本作品补充写法（可选）').fill('风格读取失败时仍可保存补充写法。')
  await page.getByRole('button', { name: '保存写作设置' }).click()
  await expect.poll(() => state.project!.author_prompt).toBe('风格读取失败时仍可保存补充写法。')
  state.styleLoadFails = false
  await page.getByRole('button', { name: '重新读取风格方案' }).click()
  await expect(page.getByRole('status', { name: '当前作品风格' })).toContainText('跟随 LISA 默认')
})

test('worldbook entries preserve edits, search, links and plot state on desktop and mobile', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.project!.worldbook = '时间不可逆转。'
  await page.clock.fastForward(5_100)
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByRole('button', { name: '世界书', exact: true }).click()
  await expect(page.getByLabel('世界书内容')).toHaveValue('时间不可逆转。')
  await page.getByRole('button', { name: '设定条目 · 0' }).click()
  await page.getByRole('button', { name: '新增设定', exact: true }).click()
  await page.getByLabel('设定标题', { exact: true }).fill('火术规则')
  await page.getByLabel('设定分类', { exact: true }).selectOption('rule')
  await page.getByLabel('设定正文', { exact: true }).fill('施术消耗体温。')
  await page.getByRole('button', { name: '新增设定', exact: true }).click()
  await page.getByLabel('设定标题', { exact: true }).fill('北境禁令')
  await page.getByLabel('设定正文', { exact: true }).fill('持证医师可以施术。')
  await page.getByLabel('适用条件（可选）').fill('旧历320年以后，仅限北境。')
  await page.getByText('关联设定 · 0', { exact: true }).click()
  await page.getByRole('checkbox', { name: '火术规则' }).check()
  // Polling must preserve all unsaved entry edits, not only the core textarea.
  await page.clock.fastForward(5_100)
  await expect(page.getByLabel('设定标题', { exact: true })).toHaveValue('北境禁令')
  state.saveFails = true
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByLabel('设定正文', { exact: true })).toHaveValue('持证医师可以施术。')
  state.saveFails = false
  await page.getByLabel('设定正文', { exact: true }).press('Control+s')
  await expect.poll(() => state.project!.worldbook_entries.length).toBe(2)
  expect(state.project!.worldbook).toBe('时间不可逆转。')
  expect(state.project!.worldbook_entries[1].related_ids).toEqual([state.project!.worldbook_entries[0].id])
  await page.getByLabel('搜索设定', { exact: true }).fill('医师')
  await expect(page.getByRole('navigation', { name: '设定条目列表' }).getByRole('button')).toHaveCount(1)
  await page.getByLabel('搜索设定', { exact: true }).fill('')
  await page.screenshot({ path: testInfo.outputPath('desktop-worldbook.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-worldbook.png'), fullPage: true })
  await page.getByLabel('设定正文', { exact: true }).fill('未保存的规则。')
  await page.getByRole('button', { name: '剧情状态', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByRole('complementary', { name: '当前剧情填写提示' })).toBeVisible()
  await page.getByLabel('当前时间', { exact: true }).fill('旧历319年。')
  await page.getByLabel('当前地点', { exact: true }).fill('北境。')
  await page.getByLabel('正在发生的事', { exact: true }).fill('禁令尚未生效。')
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(() => state.project!.plot_state).toContain('旧历319年')
  await page.screenshot({ path: testInfo.outputPath('mobile-plot-state.png'), fullPage: true })
  await page.getByRole('button', { name: '世界书', exact: true }).click()
  await page.getByRole('button', { name: '设定条目 · 2' }).click()
  await page.getByRole('navigation', { name: '设定条目列表' }).getByRole('button', { name: '火术规则' }).click()
  await page.getByRole('button', { name: '删除此设定' }).click()
  await page.getByRole('button', { name: '确认删除设定' }).click()
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(() => state.project!.worldbook_entries.length).toBe(1)
  expect(state.project!.worldbook_entries[0].related_ids).toEqual([])
})

test('material settings separate rules, constraints, preferences, writing and current plot with save protection', async ({ page }, testInfo) => {
  const state = await stubStudy(page, true)
  Object.assign(state.project!, { worldbook: '旧世界书原文。\n多行仍是同一段。', plot_state: '原有剧情：主角仍在路上。', author_prompt: '旧作者补充写法。' })
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByRole('button', { name: '新增约束', exact: true }).click()
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('移除空条目')
  await page.getByLabel('约束 1', { exact: true }).fill('不得靠巧合破解谜题。\n线索必须事先出现。')
  await page.getByRole('button', { name: '新增约束', exact: true }).click()
  await page.getByLabel('约束 2', { exact: true }).fill('主角不能读心。')
  await page.getByRole('button', { name: '世界书', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  expect(readItems(state.project!.constraints, '约束').values).toEqual(['不得靠巧合破解谜题。\n线索必须事先出现。', '主角不能读心。'])
  await page.getByRole('button', { name: '新增规则', exact: true }).click()
  await page.getByLabel('规则 1', { exact: true }).fill('时间只能向前。')
  state.saveFails = true
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByLabel('规则 1', { exact: true })).toHaveValue('时间只能向前。')
  state.saveFails = false
  await page.getByLabel('规则 1', { exact: true }).press('Control+s')
  await expect.poll(() => readItems(state.project!.worldbook, '规则').values).toEqual(['时间只能向前。'])
  expect(readItems(state.project!.worldbook, '规则').legacy).toBe('旧世界书原文。\n多行仍是同一段。')
  await page.getByRole('button', { name: '剧情状态', exact: true }).click()
  await page.getByLabel('当前时间', { exact: true }).fill('第一天傍晚')
  await page.getByLabel('当前地点', { exact: true }).fill('车站')
  await page.getByLabel('未解线索与悬念', { exact: true }).fill('寄信人仍未知')
  await page.getByLabel('人物知情提醒', { exact: true }).fill('沈舟不知道信已送达')
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(() => readSections(state.project!.plot_state, plotFields).values[1]).toBe('车站')
  expect(readSections(state.project!.plot_state, plotFields).legacy).toBe('原有剧情：主角仍在路上。')
  await page.getByRole('button', { name: '写作风格', exact: true }).click()
  await page.getByLabel('叙述视角', { exact: true }).fill('第三人称限知')
  await page.getByLabel('对白写法', { exact: true }).fill('用停顿表现犹豫')
  await page.getByRole('button', { name: '保存写作设置', exact: true }).click()
  await expect.poll(() => readSections(state.project!.author_prompt, writingFields).values[0]).toBe('第三人称限知')
  await page.getByRole('button', { name: '讨论记录', exact: true }).click()
  await page.getByText('偏好倾向', { exact: true }).click()
  await page.getByText('待确认想法', { exact: true }).click()
  await page.getByRole('button', { name: '新增偏好', exact: true }).click()
  await page.getByLabel('偏好 1', { exact: true }).fill('目前更喜欢开放式结尾')
  await page.getByRole('button', { name: '新增想法', exact: true }).click()
  await page.getByLabel('想法 1', { exact: true }).fill('是否让寄信人是旧友？尚未决定')
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(() => readItems(state.project!.tentative, '想法').values.length).toBe(1)
  expect(state.project!.constraints).not.toContain('开放式结尾')
  await page.getByRole('button', { name: '剧情状态', exact: true }).click()
  await expect(page.getByLabel('当前地点', { exact: true })).toHaveValue('车站')
  await page.screenshot({ path: testInfo.outputPath('desktop-structured-plot.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-structured-plot.png'), fullPage: true })
  await page.getByRole('button', { name: '创作要求', exact: true }).click()
  await page.getByRole('button', { name: '移除约束 1', exact: true }).click()
  await page.getByRole('button', { name: '讨论记录', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click()
  await page.getByRole('button', { name: '创作要求', exact: true }).click()
  await expect(page.getByLabel('约束 1', { exact: true })).toHaveValue('不得靠巧合破解谜题。\n线索必须事先出现。')
})

test('individual character dossiers manage definitions, legacy notes and protected switching', async ({ page }, testInfo) => {
  const state = await stubStudy(page, true)
  state.project!.characters = '旧版：许知夏是记者，沈舟是她的旧友。'
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByRole('button', { name: '人物设定', exact: true }).click()
  await expect(page.getByLabel('人物设定内容')).toHaveCount(0)
  await page.getByText('旧版人物资料 · 待整理为独立角色', { exact: true }).click()
  await expect(page.getByText(state.project!.characters, { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '新建角色', exact: true }).click()
  await expect(page.getByLabel('角色 prompt', { exact: true })).not.toBeVisible()
  await page.getByLabel('角色名称', { exact: true }).fill('许知夏')
  for (const [label, value] of [['身份与背景', '调查记者'], ['性格与行为', '谨慎，但涉及旧友时会冲动'], ['目标与动机', '查清失踪真相'], ['人物关系', '沈舟是失联的旧友'], ['说话方式', '短句，先问问题']]) {
    await page.getByLabel(label, { exact: true }).fill(value)
  }
  state.saveFails = true
  await page.getByRole('button', { name: '保存角色', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成角色保存或读取失败')
  await expect(page.getByLabel('目标与动机')).toHaveValue('查清失踪真相')
  state.saveFails = false
  await page.getByLabel('目标与动机').press('Control+s')
  await expect.poll(() => state.characters.length).toBe(1)
  expect(state.characters[0].definition).toEqual({ identity: '调查记者', personality: '谨慎，但涉及旧友时会冲动', motivation: '查清失踪真相', relationships: '沈舟是失联的旧友', voice: '短句，先问问题' })
  expect(state.characters[0].enabled).toBe(false)
  await page.getByRole('button', { name: '新建角色', exact: true }).click()
  await page.getByLabel('角色名称', { exact: true }).fill('沈舟')
  await expect(page.getByLabel('身份与背景')).toHaveValue('')
  await page.getByLabel('身份与背景').fill('修表匠')
  await page.getByRole('button', { name: '保存角色', exact: true }).click()
  await expect.poll(() => state.characters.length).toBe(2)
  await page.getByLabel('性格与行为').fill('尚未保存的性格')
  const first = page.getByRole('navigation', { name: '作品角色列表' }).getByRole('button', { name: /许知夏/ })
  await first.click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑', exact: true }).click()
  await expect(page.getByLabel('角色名称', { exact: true })).toHaveValue('沈舟')
  await first.click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click()
  await expect(page.getByLabel('身份与背景')).toHaveValue('调查记者')
  await page.getByLabel('查找人物').fill('修表')
  await expect(first).toHaveCount(0)
  await page.getByRole('button', { name: /沈舟 修表匠/ }).click()
  await expect(page.getByLabel('性格与行为')).toHaveValue('')
  await page.getByLabel('查找人物').fill('不存在的人物')
  await expect(page.getByText('没有匹配的人物。试试其他姓名或清空搜索。')).toBeVisible()
  await page.getByLabel('查找人物').fill('')
  await first.click()
  await page.getByText('旧版人物资料 · 待整理为独立角色', { exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath('desktop-character-dossiers.png'), fullPage: true })
  await page.setViewportSize({ width: 1024, height: 900 })
  const workspace = await page.locator('.character-workspace').boundingBox()
  const column = await page.locator('.character-workspace > .writing-column').boundingBox()
  expect(column!.width).toBe(workspace!.width)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-character-dossiers.png'), fullPage: true })
  await page.getByText('旧版人物资料 · 待整理为独立角色', { exact: true }).click()
  await page.getByRole('button', { name: '编辑旧版人物资料', exact: true }).click()
  await page.getByLabel('人物设定内容').fill('旧资料经人工整理后保留。')
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await expect.poll(() => state.project!.characters).toBe('旧资料经人工整理后保留。')
  await page.getByRole('button', { name: '管理独立角色', exact: true }).click()
  await expect(page.getByLabel('身份与背景')).toHaveValue('调查记者')
  expect(state.characters[1].definition?.identity).toBe('修表匠')
})

test('character configuration preserves edits, validates knowledge and isolates works', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.others.push({ ...work(), id: 'work-2', title: '另一部作品', is_current: false })
  await expect(page.getByRole('textbox', { name: '章节正文' })).toBeVisible()
  state.characterFails = true
  await page.getByRole('button', { name: '角色', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成角色保存或读取失败')
  state.characterFails = false
  await page.getByRole('button', { name: '重新读取角色', exact: true }).click()
  await expect(page.getByText('尚无角色配置。', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: '新建角色', exact: true }).click()
  await expect(page.getByLabel('允许作者按需调用这个角色')).not.toBeChecked()
  await page.getByLabel('角色名称', { exact: true }).fill('许知夏')
  await page.getByLabel('允许作者按需调用这个角色').check()
  await page.getByRole('button', { name: '保存角色', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('启用角色前必须填写已知信息')
  expect(state.characters).toHaveLength(0)
  await page.getByLabel('角色已知信息', { exact: true }).fill('我刚到雨里的车站，只看见远处有人撑伞，不知道他的身份。')
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑', exact: true }).click()
  state.saveFails = true
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成角色保存或读取失败')
  await expect(page.getByLabel('角色名称', { exact: true })).toHaveValue('许知夏')
  state.saveFails = false
  await page.getByLabel('角色名称', { exact: true }).press('Control+s')
  await expect.poll(() => state.characters.length).toBe(1)
  expect(state.characters[0].enabled).toBe(true)
  await expect(page.getByRole('button', { name: '许知夏 可独立参与创作' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('desktop-characters.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-characters.png'), fullPage: true })
  await page.getByText('高级设置 · 补充指引与知识检索', { exact: true }).click()
  await page.getByLabel('角色 prompt', { exact: true }).fill('说话简短，先观察再行动。')
  await page.getByRole('button', { name: '保存角色', exact: true }).click()
  await expect.poll(() => state.characters[0].revision).toBe(2)
  await page.getByLabel('角色名称', { exact: true }).fill('尚未保存的改名')
  await page.clock.fastForward(5_100)
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click()
  await page.getByRole('button', { name: '打开《另一部作品》', exact: true }).click()
  await page.getByRole('button', { name: '角色', exact: true }).click()
  await expect(page.getByText('尚无角色配置。', { exact: false })).toBeVisible()
  await expect(page.getByLabel('角色名称', { exact: true })).toHaveCount(0)
  expect(state.characters[0].name).toBe('许知夏')
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('button', { name: '打开《雨夜来信》', exact: true }).click()
  await page.getByRole('button', { name: '角色', exact: true }).click()
  await expect(page.getByLabel('角色 prompt', { exact: true })).toHaveValue('说话简短，先观察再行动。')
  await page.getByRole('button', { name: '删除角色', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('不改变已有正文')
  await page.getByRole('button', { name: '保留角色', exact: true }).click()
  expect(state.characters).toHaveLength(1)
  state.characterDeleteFails = true
  await page.getByRole('button', { name: '删除角色', exact: true }).click()
  await page.getByRole('button', { name: '确认删除角色', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成角色修改失败')
  await expect(page.getByLabel('角色名称', { exact: true })).toHaveValue('许知夏')
  state.characterDeleteFails = false
  await page.getByRole('button', { name: '删除角色', exact: true }).click()
  await page.getByRole('button', { name: '确认删除角色', exact: true }).click()
  await expect(page.getByText('尚无角色配置。', { exact: false })).toBeVisible()
  expect(state.characters).toHaveLength(0)
  expect(state.chapters[0].version).toBe(1)
})

test('character scene and retrieval settings save together and remain isolated with explicit defaults', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.characters.push(...['许知夏', '沈舟'].map((name, index) => ({ id: `character-${index + 1}`, project_id: 'work-1', name, prompt: '按自己的经历行动。', known_information: `${name}只知道自己经历的事。`, current_scene: '', retrieval_config: characterRetrievalConfig(), enabled: true, revision: 1, created_at: '2026-10-23T10:00:00Z' })))
  await page.getByRole('button', { name: '角色', exact: true }).click()
  await expect(page.getByLabel('角色名称', { exact: true })).toHaveValue('许知夏')
  await page.getByText('高级设置 · 补充指引与知识检索', { exact: true }).click()
  await expect(page.getByText('服务尚未启用向量检索。开启此配置后暂取已知信息开头；当前可见场景始终保留。')).toBeVisible()
  await page.getByLabel('当前可见场景', { exact: true }).fill('站台只剩一盏灯，门外有人敲门。')
  await page.getByLabel('按当前场景检索角色已知信息').check()
  await page.getByLabel('角色召回数量（条）').fill('6')
  await page.getByLabel('角色知识文本预算（字符）').fill('2100')
  state.saveFails = true
  await page.getByRole('button', { name: '沈舟 可独立参与创作', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成角色修改失败')
  await expect(page.getByLabel('当前可见场景', { exact: true })).toHaveValue('站台只剩一盏灯，门外有人敲门。')
  await expect(page.getByLabel('角色召回数量（条）')).toHaveValue('6')
  expect(state.characters[0].current_scene).toBe('')
  state.saveFails = false
  await page.getByLabel('角色召回数量（条）').press('Control+s')
  await expect.poll(() => state.characters[0].retrieval_config).toEqual({ enabled: true, limit: 6, max_chars: 2100 })
  expect(state.characters[0].current_scene).toBe('站台只剩一盏灯，门外有人敲门。')
  expect(state.characters[1].retrieval_config).toEqual(characterRetrievalConfig())
  expect(state.retrievalConfigs['work-1']).toEqual(retrievalConfig())
  await page.screenshot({ path: testInfo.outputPath('desktop-character-retrieval.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-character-retrieval.png'), fullPage: true })
  await page.getByRole('button', { name: '恢复角色检索默认值', exact: true }).click()
  await expect(page.getByLabel('按当前场景检索角色已知信息')).not.toBeChecked()
  await expect(page.getByLabel('角色召回数量（条）')).toHaveValue('3')
  await expect(page.getByLabel('当前可见场景', { exact: true })).toHaveValue('站台只剩一盏灯，门外有人敲门。')
  expect(state.characters[0].retrieval_config.limit).toBe(6)
  await page.getByRole('button', { name: '沈舟 可独立参与创作', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改', exact: true }).click()
  await expect(page.getByLabel('当前可见场景', { exact: true })).toHaveValue('')
  await expect(page.getByLabel('角色召回数量（条）')).toHaveValue('3')
  await page.getByRole('button', { name: '许知夏 可独立参与创作', exact: true }).click()
  await expect(page.getByLabel('角色召回数量（条）')).toHaveValue('6')
  await page.getByLabel('角色召回数量（条）').fill('9')
  await page.getByLabel('角色召回数量（条）').press('Control+s')
  await expect(page.getByRole('alert')).toContainText('角色召回数量须为 1–8 的整数')
  expect(state.characters[0].retrieval_config.limit).toBe(6)
  await page.getByRole('button', { name: '恢复角色检索默认值', exact: true }).click()
  await page.getByRole('button', { name: '保存角色', exact: true }).click()
  await expect.poll(() => state.characters[0].retrieval_config).toEqual(characterRetrievalConfig())
  expect(state.characters[0].current_scene).toBe('站台只剩一盏灯，门外有人敲门。')
})

test('task character participation shows empty, failure and deleted records without draft content', async ({ page }) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.tasks = [{ ...task(), status: 'running' }]
  await page.clock.fastForward(5_100)
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByRole('button', { name: '查看角色参与', exact: true }).click()
  await expect(page.getByText('这项任务暂无角色参与。')).toBeVisible()
  await page.getByRole('button', { name: '收起角色参与', exact: true }).click()
  state.participationFails = true
  await page.getByRole('button', { name: '查看角色参与', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成角色参与读取失败')
  state.participationFails = false
  state.participations = [
    { position: 1, character_id: 'character-1', name: '许知夏', revision: 2, state: 'completed', created_at: '2026-10-23T10:00:00Z' },
    { position: 2, character_id: null, name: null, revision: 1, state: 'cancelled', created_at: '2026-10-23T10:00:00Z' },
  ]
  await page.getByRole('button', { name: '重新读取角色参与', exact: true }).click()
  await expect(page.getByText('许知夏 · 参与完成', { exact: true })).toBeVisible()
  await expect(page.getByText('角色已删除 · 已取消', { exact: true })).toBeVisible()
  await expect(page.getByText('记录角色调用的状态，不代表建议已经用于正式正文。')).toBeVisible()
})

test('retrieval settings protect edits, save explicitly, restore defaults and reset between works', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.others.push({ ...work(), id: 'work-2', title: '另一部作品', is_current: false })
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toBeVisible()
  await editor.fill('尚未保存的正文。')
  await page.getByRole('button', { name: '检索配置', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(editor).toContainText('尚未保存的正文。')
  state.retrievalFails = true
  await page.getByRole('button', { name: '检索配置', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByRole('alert')).toContainText('合成检索配置失败')
  state.retrievalFails = false
  await page.getByRole('button', { name: '重新读取检索配置' }).click()
  await expect(page.getByText('服务尚未启用向量检索。设定条目仍可按完整标题匹配；语义召回启用后生效。')).toBeVisible()
  await page.getByLabel('召回数量（条）').fill('7')
  await page.getByLabel('文本预算（字符）').fill('2400')
  await page.getByLabel('可用于创作的记忆', { exact: true }).uncheck()
  expect(state.retrievalConfigs['work-1']).toEqual(retrievalConfig())
  await page.clock.fastForward(5_100)
  await expect(page.getByLabel('召回数量（条）')).toHaveValue('7')
  state.saveFails = true
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成检索配置失败')
  await expect(page.getByLabel('召回数量（条）')).toHaveValue('7')
  state.saveFails = false
  await page.getByLabel('召回数量（条）').press('Control+s')
  await expect.poll(() => state.retrievalConfigs['work-1']).toEqual({ sources: ['project', 'chapter'], limit: 7, max_chars: 2400 })
  await page.getByRole('button', { name: '恢复默认', exact: true }).click()
  await expect(page.getByLabel('召回数量（条）')).toHaveValue('5')
  expect(state.retrievalConfigs['work-1'].limit).toBe(7)
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await page.getByRole('button', { name: '检索配置', exact: true }).click()
  await expect(page.getByLabel('召回数量（条）')).toHaveValue('7')
  await page.getByLabel('作品资料', { exact: true }).uncheck()
  await page.getByLabel('已保存章节', { exact: true }).uncheck()
  await page.getByRole('button', { name: '保存检索配置' }).click()
  await expect.poll(() => state.retrievalConfigs['work-1'].sources).toEqual([])
  await page.getByLabel('召回数量（条）').fill('13')
  await page.getByLabel('召回数量（条）').press('Control+s')
  await expect(page.getByRole('alert')).toContainText('召回数量须为 1–12 的整数')
  expect(state.retrievalConfigs['work-1'].limit).toBe(7)
  await page.getByLabel('召回数量（条）').fill('7')
  await page.getByRole('button', { name: '保存检索配置' }).click()
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('desktop-retrieval.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-retrieval.png'), fullPage: true })
  await page.getByLabel('召回数量（条）').fill('9')
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await page.getByRole('button', { name: '打开《另一部作品》' }).click()
  await page.getByRole('button', { name: '检索配置', exact: true }).click()
  await expect(page.getByLabel('召回数量（条）')).toHaveValue('5')
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('button', { name: '打开《雨夜来信》' }).click()
  await page.getByRole('button', { name: '检索配置', exact: true }).click()
  await expect(page.getByLabel('召回数量（条）')).toHaveValue('7')
})

test('task references show current authorized excerpts and unavailable states without disturbing edits', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  state.tasks.push({ ...task(), status: 'completed', can_cancel: false })
  await page.clock.fastForward(5_100)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await editor.fill('正在编辑，不要替换。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(editor).toContainText('正在编辑，不要替换。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  state.referenceFails = true
  await page.getByRole('button', { name: '查看参考资料' }).click()
  await expect(page.getByRole('alert')).toContainText('合成参考资料读取失败')
  state.referenceFails = false
  await page.getByRole('button', { name: '重新读取参考资料' }).click()
  await expect(page.getByText('这项任务暂无额外检索资料。')).toBeVisible()
  await page.getByRole('button', { name: '收起参考资料' }).click()
  state.references = [
    { position: 1, source_kind: 'chapter', revision: 2, start: 0, end: 12, state: 'available', title: '第一章 重逢', text: '只供参考的合成章节片段。' },
    { position: 2, source_kind: 'project', revision: 1, start: 0, end: 9, state: 'changed', title: '不得展示的旧标题', text: '不得展示的旧资料' },
    { position: 3, source_kind: 'memory', revision: 1, start: 0, end: 9, state: 'unavailable', title: null, text: null },
    { position: 4, source_kind: 'character', revision: 2, start: 0, end: 12, state: 'available', title: '许知夏', text: '只记得自己见过的雨夜车站。' },
  ]
  await page.getByRole('button', { name: '查看参考资料' }).click()
  await expect(page.getByText('只供参考的合成章节片段。')).toBeVisible()
  await expect(page.getByText('角色知识 · 许知夏', { exact: true })).toBeVisible()
  await expect(page.getByText('只记得自己见过的雨夜车站。')).toBeVisible()
  await expect(page.getByText('作品资料 · 资料已改变', { exact: true })).toBeVisible()
  await expect(page.getByText('记忆 · 资料已不可用', { exact: true })).toBeVisible()
  await expect(page.getByText('不得展示的旧资料')).toHaveCount(0)
  await expect(page.getByText('不得展示的旧标题')).toHaveCount(0)
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('desktop-references.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-references.png'), fullPage: true })
  await page.getByRole('button', { name: '收起参考资料' }).click()
  state.references[0] = { ...state.references[0], state: 'unavailable', title: null, text: null }
  await page.getByRole('button', { name: '查看参考资料' }).click()
  await expect(page.getByText('章节正文 · 资料已不可用', { exact: true })).toBeVisible()
  await expect(page.getByText('只供参考的合成章节片段。')).toHaveCount(0)
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await expect(editor).toContainText('正在编辑，不要替换。')
})

test('first novel, explicit material confirmation, scene adoption and structured chapter save', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page)
  await page.getByLabel('作品名称').fill('雨夜来信')
  await page.getByRole('button', { name: '创建小说' }).click()
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始写作' })).toBeDisabled()
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByLabel('作品方向（已确认）').fill('现代都市小说，叙述克制。')
  await page.getByRole('button', { name: '世界书', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('现代都市小说，叙述克制。')
  state.saveFails = true
  await page.getByRole('button', { name: '世界书', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('现代都市小说，叙述克制。')
  state.saveFails = false
  await page.getByLabel('作品方向（已确认）').press('Control+s')
  await expect.poll(() => state.project?.agreement).toBe('现代都市小说，叙述克制。')
  await page.screenshot({ path: testInfo.outputPath('desktop-materials.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-materials.png'), fullPage: true })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByLabel('这次的创作要求').fill('描写雨夜重逢的一个场景。')
  await page.getByRole('button', { name: '开始写作' }).click()
  await expect(page.getByText('等待执行', { exact: true })).toBeVisible()
  state.tasks[0] = { ...state.tasks[0], status: 'completed', can_cancel: false, input_tokens: 210, output_tokens: 180, charged_usd: '0.0111', reserved_usd: '0', adopted_at: '2026-10-23T10:00:00Z' }
  state.chapters = [chapter()]
  await page.clock.fastForward(5_100)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(page.getByText('已采用', { exact: true })).toBeVisible()
  await expect(page.getByText(/输入 210 \/ 输出 180 tokens/)).toContainText('¥0.011100')
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await expect(editor).toContainText('她站在雨里的车站。')
  await editor.fill('她在车站等一封迟来的信。')
  await editor.press('Control+s')
  await expect.poll(() => state.chapterSaves.length).toBe(1)
  expect(state.chapterSaves[0].content).toEqual(doc('她在车站等一封迟来的信。'))
  await page.screenshot({ path: testInfo.outputPath('desktop-writing.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-writing.png'), fullPage: true })
})

test('polling preserves edits, chapter and persona navigation protect input, rewrite is explicit, cancel and retry stay visible', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  await editor.fill('仍未保存的正文修改。')
  state.chapters[0] = { ...chapter(), version: 2, content: doc('模型刚刚保存的新版。') }
  state.chapters.push({ ...chapter(), id: 'chapter-2', title: '第二章 来信', position: 2 })
  await page.clock.fastForward(5_100)
  await expect(editor).toContainText('仍未保存的正文修改。')
  await expect(page.getByText(/这一章已有新的正式内容/)).toBeVisible()
  await page.getByRole('button', { name: '第二章 来信' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(editor).toContainText('仍未保存的正文修改。')
  state.saveFails = true
  await page.getByRole('button', { name: '个人设置' }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(editor).toContainText('仍未保存的正文修改。')
  state.saveFails = false
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await page.getByLabel('作品方向（已确认）').fill('尚未保存的共识。')
  await page.getByRole('button', { name: '人物设定', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('region', { name: '作品角色配置' })).toBeVisible()
  await expect(page.getByLabel('人物设定内容')).toHaveCount(0)
  expect(state.project!.agreement).toBe('尚未保存的共识。')
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await expect(editor).toContainText('模型刚刚保存的新版。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByLabel('创作方式').selectOption('rewrite')
  await page.getByLabel('要重写的原文').fill('模型刚刚保存的新版。')
  await page.getByLabel('这次的创作要求').fill('让这句话更加克制。')
  await page.getByRole('button', { name: '开始局部重写' }).click()
  await expect(page.getByRole('button', { name: '取消任务' })).toBeVisible()
  expect(state.requests.find(item => item.path === '/projects/work-1/tasks' && item.body.kind === 'rewrite')?.body).toEqual({ kind: 'rewrite', goal: '让这句话更加克制。', chapter_id: 'chapter-1', target_text: '模型刚刚保存的新版。' })
  await page.getByRole('button', { name: '取消任务' }).click()
  await expect(page.getByText('已取消', { exact: true })).toBeVisible()
  state.tasks[0] = { ...state.tasks[0], status: 'failed', can_retry: true, error: 'model_failed', charged_usd: '0.002' }
  await page.clock.fastForward(5_100)
  await expect(page.getByText('模型调用失败，原文保留，可稍后重试。', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '使用最新保存内容重试' }).click()
  await expect(page.getByText('执行失败', { exact: true })).toBeVisible()
  await expect(page.getByText('等待执行', { exact: true })).toBeVisible()
  await expect(page.getByText(/开发估算 ¥0.002000/)).toBeVisible()
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await expect(editor).toContainText('模型刚刚保存的新版。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: testInfo.outputPath('desktop-task-states.png'), fullPage: true })
  await page.getByRole('button', { name: '个人设置' }).click()
  await page.getByRole('button', { name: '调整 LISA 性格' }).click()
  await page.getByLabel('LISA 性格 prompt', { exact: true }).fill('未保存的合成人格。')
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(page.getByLabel('LISA 性格 prompt', { exact: true })).toHaveValue('未保存的合成人格。')
  await page.getByRole('button', { name: '我的作品', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(editor).toBeVisible()
})

test('project polling receives QQ agreement and author materials while preserving unsaved material baseline', async ({ page }) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  await expect(page.getByRole('textbox', { name: '章节正文' })).toBeVisible()
  state.project!.agreement = ''
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.clock.fastForward(5_100)
  await expect(page.getByRole('button', { name: '开始写作' })).toBeDisabled()

  state.project!.agreement = 'QQ 新确认的共识。'
  await page.clock.fastForward(5_100)
  await expect(page.getByRole('button', { name: '开始写作' })).toBeEnabled()
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('QQ 新确认的共识。')

  await page.getByLabel('作品方向（已确认）').fill('尚未保存的本地修改。')
  state.project!.agreement = '尚未保存的本地修改。'
  await page.clock.fastForward(5_100)
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('尚未保存的本地修改。')
  await page.getByRole('button', { name: '作品摘要', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()

  state.project!.agreement = 'QQ 后续确认的共识。'
  state.project!.summary = '作者刚保存的故事摘要。'
  await page.clock.fastForward(5_100)
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('尚未保存的本地修改。')
  await page.getByRole('button', { name: '作品摘要', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await expect(page.getByLabel('作品摘要内容')).toHaveValue('作者刚保存的故事摘要。')

  state.project!.summary = '作者再次更新的故事摘要。'
  await page.clock.fastForward(5_100)
  await expect(page.getByLabel('作品摘要内容')).toHaveValue('作者再次更新的故事摘要。')
  await page.getByRole('button', { name: '创作要求', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.getByLabel('作品方向（已确认）')).toHaveValue('QQ 后续确认的共识。')
})

test('first diary keeps LISA viewpoint, requires agreement, and writes a bounded segment with ordinary manual editing', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page)
  await page.getByLabel('作品类型', { exact: true }).selectOption('diary')
  await expect(page.getByText('记录 LISA 的视角与虚拟生活，不是你的现实日记。创建后仍需先确认写作方向。', { exact: true })).toBeVisible()
  await page.getByLabel('作品名称', { exact: true }).fill('窗边的日常')
  await page.getByRole('button', { name: '创建日记', exact: true }).click()
  await expect(page.getByRole('heading', { name: '窗边的日常', exact: true })).toBeVisible()
  expect(state.project?.kind).toBe('diary')
  expect(state.tasks).toHaveLength(0)
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始写作', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '正文', exact: true }).click()

  await expect(page.getByRole('heading', { name: '第一段日记，等待落笔。', exact: true })).toBeVisible()
  await expect(page.getByText('这是 LISA 视角的虚拟生活日记。', { exact: false })).toContainText('手动修改正文不会自动更新生活记忆')
  await page.getByRole('button', { name: '创作资料', exact: true }).click()
  await page.getByLabel('作品方向（已确认）', { exact: true }).fill('记录 LISA 的虚拟日常，叙述克制，每次只写一个生活片段。')
  await page.getByRole('button', { name: '保存资料', exact: true }).click()
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(page.getByLabel('创作方式', { exact: true })).toHaveValue('scene')
  await expect(page.getByRole('option', { name: '写一段日记', exact: true })).toBeAttached()
  await page.getByLabel('这次的创作要求', { exact: true }).fill('写整理旧书的片段，停在合上最后一本书时。')
  await page.getByRole('button', { name: '开始写作', exact: true }).click()
  await expect(page.getByText('日记写作', { exact: true })).toBeVisible()
  expect(state.tasks[0].kind).toBe('scene')
  state.tasks[0] = { ...state.tasks[0], status: 'completed', can_cancel: false, adopted_at: '2026-10-23T10:00:00Z' }
  state.chapters = [{ ...chapter(), title: '十月二十三日 · 旧书', content: doc('下午在窗边整理旧书，合上最后一本时天色已暗。'), text: '下午在窗边整理旧书，合上最后一本时天色已暗。' }]
  await page.clock.fastForward(5_100)
  const editor = page.getByRole('textbox', { name: '章节正文', exact: true })
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await expect(editor).toContainText('下午在窗边整理旧书')
  await page.screenshot({ path: testInfo.outputPath('desktop-diary-writing.png'), fullPage: true })
  await editor.fill('下午在窗边整理诗集，合上最后一本时天色已暗。')
  await editor.press('Control+s')
  await expect.poll(() => state.chapterSaves.length).toBe(1)
  expect(state.requests.some(item => item.path.startsWith('/memories'))).toBe(false)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-diary-writing.png'), fullPage: true })
})

test('scheduled continuation stays distinct from running work and a 24-hour pause preserves manual editing', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const note = page.getByRole('status', { name: '自主续写状态' })
  await expect(note).toContainText('暂无后续计划')
  state.schedule = { ...schedule(), status: 'scheduled', due_at: '2026-10-24T09:00:00Z', goal: '继续雨夜来信，只写拆开信封的一小段。', project_id: 'work-1', remaining_scenes: 2 }
  await page.clock.fastForward(5_100)
  await expect(note).toContainText('已有后续计划')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(note).toContainText(state.schedule.goal!)
  await expect(note).toContainText('计划不代表已经开始')
  await expect(note.locator('time')).toHaveAttribute('datetime', state.schedule.due_at!)
  await expect(page.getByText('LISA 正在处理创作任务。', { exact: false })).not.toBeVisible()
  expect(state.tasks).toHaveLength(0)
  await note.scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('desktop-scheduled-continuation.png'), fullPage: true })

  await page.getByRole('button', { name: '正文', exact: true }).click()
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await editor.fill('暂停期间仍未保存的正文修改。')
  state.schedule.status = 'paused'
  state.schedule.pause_reason = 'inactive'
  await page.clock.fastForward(5_100)
  await expect(note).toContainText('连续 24 小时未在 QQ 回复')
  await expect(editor).toContainText('暂停期间仍未保存的正文修改。')
  await expect(page.getByRole('button', { name: '发起创作', exact: true })).toBeEnabled()
  await page.setViewportSize({ width: 390, height: 844 })
  await note.scrollIntoViewIfNeeded()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-paused-continuation.png'), fullPage: true })
  await editor.press('Control+s')
  await expect.poll(() => state.chapterSaves.length).toBe(1)
  expect(state.chapterSaves[0].content).toEqual(doc('暂停期间仍未保存的正文修改。'))
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(note).toContainText('自主续写暂停不影响网页编辑与单次手动创作')
  await expect(page.getByRole('button', { name: '开始写作', exact: true })).toBeEnabled()
})

test('other-project plans and schedule failures never replace the viewed work or interrupt its editor', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const note = page.getByRole('status', { name: '自主续写状态' })
  await expect(note).toContainText('暂无后续计划')
  state.schedule = { ...schedule(), status: 'scheduled', due_at: '2026-10-24T09:00:00Z', goal: '别的作品里寻找蓝色车票。', project_id: 'other-work', remaining_scenes: 1 }
  await page.clock.fastForward(5_100)
  await expect(note).toContainText('计划属于另一部作品')
  await expect(note).not.toContainText(state.schedule.goal!)
  await expect(note.locator('time')).toHaveCount(0)

  const editor = page.getByRole('textbox', { name: '章节正文' })
  await editor.fill('计划接口失败时仍可保存这段修改。')
  state.scheduleFails = true
  state.tasks = [{ ...task(), status: 'completed', adopted_at: '2026-10-23T10:00:00Z', can_cancel: false }]
  await page.clock.fastForward(5_100)
  await expect(note).toContainText('状态暂不可用')
  await expect(page.getByLabel('创作任务摘要')).toContainText('最近任务：已采用')
  await expect(editor).toContainText('计划接口失败时仍可保存这段修改。')
  await editor.press('Control+s')
  await expect.poll(() => state.chapterSaves.length).toBe(1)
  state.scheduleFails = false
  state.schedule = { ...schedule(), status: 'paused', pause_reason: 'limit_reached' }
  await page.clock.fastForward(5_100)
  await expect(note).toContainText('等待你对作品的进一步反馈')
  await expect(note).toContainText('普通闲聊不会重置次数')
  await expect(note).not.toContainText('24 小时')
  await page.setViewportSize({ width: 390, height: 844 })
  await note.scrollIntoViewIfNeeded()
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-creative-feedback-limit.png'), fullPage: true })
  state.schedule = schedule()
  await page.clock.fastForward(5_100)
  await expect(note).toContainText('暂无后续计划')
})


test('new work, reading old work, completion and explicit resume keep distinct states', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  await editor.fill('给旧作留下的未保存修改。')
  await page.getByRole('button', { name: '开启新作品', exact: true }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => state.chapterSaves.length).toBe(1)
  await page.getByLabel('作品类型').selectOption('diary')
  await page.getByLabel('作品名称').fill('窗边的日常')
  state.lifecycleFails = true
  await page.getByRole('button', { name: '创建日记', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成创建失败')
  await expect(page.getByLabel('作品名称')).toHaveValue('窗边的日常')
  state.lifecycleFails = false
  await page.getByRole('button', { name: '创建日记', exact: true }).click()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('窗边的日常')
  expect(state.project!.is_current).toBe(false)
  expect(state.others[0].is_current).toBe(true)
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始写作', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '正文', exact: true }).click()

  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('button', { name: '打开《雨夜来信》', exact: true }).click()
  await expect(editor).toContainText('给旧作留下的未保存修改。')
  await expect(page.getByRole('button', { name: '设为当前作品', exact: true })).toBeVisible()
  expect(state.others[0].is_current).toBe(true)
  await page.getByRole('button', { name: '标记完结', exact: true }).click()
  await expect(page.getByRole('button', { name: '继续创作', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(page.getByLabel('创作方式')).toHaveValue('rewrite')
  await expect(page.getByLabel('创作方式').locator('option[value="scene"]')).toBeDisabled()
  expect(state.project!.is_current).toBe(false)
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await editor.fill('完结后的单次局部修订。')
  await page.getByRole('button', { name: '保存正文', exact: true }).click()
  expect(state.project!.completed).toBe(true)
  expect(state.project!.is_current).toBe(false)
  state.lifecycleFails = true
  await page.getByRole('button', { name: '继续创作', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成状态保存失败')
  expect(state.project!.completed).toBe(true)
  state.lifecycleFails = false
  await page.getByRole('button', { name: '继续创作', exact: true }).click()
  await expect(page.getByRole('region', { name: '作品状态' })).toContainText('当前作品 · 未完结')
  expect(state.project!.is_current).toBe(true)
  expect(state.others[0].is_current).toBe(false)
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.screenshot({ path: testInfo.outputPath('desktop-library.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-library.png'), fullPage: true })
})

test('switching with a running old task exposes scope and a working cancel action', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  await expect(page.getByRole('textbox', { name: '章节正文' })).toContainText('她站在雨里的车站。')
  state.tasks = [{ ...task(), status: 'running' }]
  state.others = [{ ...work(), id: 'work-2', title: '尚未完结的另一部', is_current: false }]
  await page.clock.fastForward(5_100)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await editor.fill('未保存但稍后再写的句子。')
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(editor).toContainText('未保存但稍后再写的句子。')
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '放弃修改' }).click()
  await page.getByRole('button', { name: '打开《尚未完结的另一部》' }).click()
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('尚未完结的另一部')
  expect(state.project!.is_current).toBe(true)
  state.handoff = [{ ...state.tasks[0], project_id: 'work-1', project_title: state.project!.title }]
  await page.getByRole('button', { name: '设为当前作品', exact: true }).click()
  await expect(page.getByRole('complementary', { name: '旧作品任务' })).toContainText('结果只保存到该作品')
  expect(state.tasks[0].status).toBe('running')
  await page.screenshot({ path: testInfo.outputPath('desktop-handoff.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-handoff.png'), fullPage: true })
  await page.getByRole('button', { name: '取消《雨夜来信》的任务', exact: true }).click()
  await expect(page.getByRole('complementary', { name: '旧作品任务' })).toBeHidden()
  expect(state.tasks[0].status).toBe('cancelled')
  expect(state.others[0].is_current).toBe(true)
})

test('version browsing protects edits, restoration retains history, and failed operations remain retryable', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  await editor.fill('手动改写后的第二版正文。')
  await page.getByLabel('章节标题', { exact: true }).fill('第二版标题')
  await page.getByRole('button', { name: '版本记录', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(editor).toContainText('手动改写后的第二版正文。')
  await page.getByRole('button', { name: '版本记录', exact: true }).click()
  state.saveFails = true
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成保存失败')
  await expect(editor).toContainText('手动改写后的第二版正文。')
  state.saveFails = false
  state.versionFails = true
  await page.getByRole('button', { name: '版本记录', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成版本读取失败')
  state.versionFails = false
  await page.getByRole('button', { name: '重新读取版本', exact: true }).click()
  const version = page.getByLabel('选择历史版本')
  await expect(version).toHaveValue('2')
  await expect(page.getByRole('button', { name: '恢复此版本为当前正文' })).toBeDisabled()
  await version.selectOption('1')
  await expect(page.locator('.version-body')).toHaveText('她站在雨里的车站。')
  expect(state.chapters[0].version).toBe(2)
  await page.screenshot({ path: testInfo.outputPath('desktop-versions.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await page.screenshot({ path: testInfo.outputPath('mobile-versions.png'), fullPage: true })
  state.restoreFails = true
  await page.getByRole('button', { name: '恢复此版本为当前正文' }).click()
  await expect(page.getByRole('alert')).toContainText('合成恢复失败')
  expect(state.chapters[0].version).toBe(2)
  state.restoreFails = false
  await page.getByRole('button', { name: '恢复此版本为当前正文' }).click()
  await expect(editor).toContainText('她站在雨里的车站。')
  await expect(page.getByLabel('章节标题', { exact: true })).toHaveValue('第一章 重逢')
  expect(state.versions.map(item => item.version)).toEqual([1, 2, 3])
  await page.getByRole('button', { name: '版本记录', exact: true }).click()
  await expect(version).toHaveValue('3')
  await expect(version.locator('option:checked')).toContainText('恢复自版本 1')
  await version.selectOption('2')
  await expect(page.locator('.version-body')).toHaveText('手动改写后的第二版正文。')
})

test('work sharing creates copyable links, preserves edits and revokes old links', async ({ page }, testInfo) => {
  await stubStudy(page, true)
  let fails = true
  let revoked = false
  await page.route('**/api/projects/*/share', async route => {
    if (route.request().method() === 'DELETE') {
      revoked = true
      return route.fulfill({ status: 204 })
    }
    if (fails) return route.fulfill({ status: 503, json: { detail: '合成分享失败，请重试。' } })
    return route.fulfill({ json: { url: 'http://127.0.0.1:5174/read/synthetic-token' } })
  })
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await editor.fill('尚未保存的内容，分享时不自动保存。')
  await page.getByRole('button', { name: '分享作品', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成分享失败')
  fails = false
  await page.getByRole('button', { name: '分享作品', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '作品链接', exact: true })).toHaveValue('http://127.0.0.1:5174/read/synthetic-token')
  await expect(page.getByRole('textbox', { name: '当前章节链接', exact: true })).toHaveValue(/\?chapter=chapter-1$/)
  await expect(editor).toContainText('尚未保存的内容')
  await page.screenshot({ path: testInfo.outputPath('work-sharing-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: testInfo.outputPath('work-sharing-phone.png'), fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.getByRole('button', { name: '收回已有链接' }).click()
  await expect(page.getByRole('status').filter({ hasText: '此前的作品和章节链接已失效' })).toBeVisible()
  expect(revoked).toBe(true)
  await expect(editor).toContainText('尚未保存的内容')
})

test('TXT download uses saved body only and leaves current unsaved edits intact', async ({ page }) => {
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  await editor.fill('尚未保存，不能进入导出文件。')
  state.project!.worldbook = '不应导出的世界书'
  state.exportFails = true
  await page.getByRole('button', { name: '导出正文', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('合成正文导出失败')
  await expect(editor).toContainText('尚未保存，不能进入导出文件。')
  state.exportFails = false
  const pendingDownload = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出正文', exact: true }).click()
  const downloaded = await pendingDownload
  expect(downloaded.suggestedFilename()).toBe('LISA-雨夜来信.txt')
  const body = await readFile((await downloaded.path())!, 'utf8')
  expect(body).toContain('她站在雨里的车站。')
  expect(body).not.toContain('尚未保存')
  expect(body).not.toContain('世界书')
  await expect(editor).toContainText('尚未保存，不能进入导出文件。')
})


test('reading stays compact while library and task pages keep their own controls and protect edits', async ({ page }, testInfo) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  await expect(page.getByRole('textbox', { name: '章节正文' })).toBeVisible()
  await expect(page.getByRole('region', { name: '作品列表' })).toHaveCount(0)
  await expect(page.getByLabel('这次的创作要求')).toHaveCount(0)
  await expect(page.getByRole('heading', { name: '创作任务', exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('desktop-clean-reading.png'), fullPage: true })
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await editor.fill('切换页签前尚未保存的句子。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '继续编辑' }).click()
  await expect(editor).toContainText('切换页签前尚未保存的句子。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click()
  await expect.poll(() => state.chapterSaves.length).toBe(1)
  await expect(editor).toHaveCount(0)
  await page.getByLabel('这次的创作要求').fill('先保留这次要写的场景。')
  await page.getByLabel('这次的创作要求').press('Control+s')
  expect(state.chapterSaves).toHaveLength(1)
  await page.getByRole('button', { name: '正文', exact: true }).click()
  await expect(editor).toContainText('切换页签前尚未保存的句子。')
  await page.getByRole('button', { name: '创作任务', exact: true }).click()
  await expect(page.getByLabel('这次的创作要求')).toHaveValue('先保留这次要写的场景。')
  await page.getByRole('button', { name: '书架', exact: true }).click()
  await expect(page.getByRole('region', { name: '作品列表' })).toBeVisible()
  await expect(page.getByLabel('这次的创作要求')).toHaveCount(0)
  await page.getByRole('button', { name: '打开《雨夜来信》', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('body')).toHaveJSProperty('scrollWidth', 390)
  await expect(page.getByRole('button', { name: '性格与风格', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('mobile-clean-reading.png'), fullPage: true })
})


test('work refresh transfers summaries and reloads only a changed selected chapter', async ({ page }) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  state.chapters.push({ ...chapter(), id: 'chapter-2', title: '第二章', position: 2 })
  const initialBodyReads = state.requests.filter(item => item.path === '/chapters/chapter-1').length
  const initialProjectReads = state.requests.filter(item => item.path === '/projects/work-1').length
  await page.clock.fastForward(5_100)
  await expect(page.getByRole('button', { name: '第二章', exact: true })).toBeVisible()
  await page.clock.fastForward(5_100)
  expect(state.requests.filter(item => item.path === '/chapters/chapter-1')).toHaveLength(initialBodyReads)
  expect(state.requests.filter(item => item.path === '/projects/work-1')).toHaveLength(initialProjectReads)
  expect(state.requests.filter(item => item.path === '/projects' || item.path.endsWith('/chapters')).every(item => item.summary)).toBe(true)
  expect(state.requests.filter(item => item.path === '/chapters/chapter-2')).toHaveLength(0)
  state.chapters[0] = { ...state.chapters[0], version: 2, content: doc('正式采用的新正文。') }
  await page.clock.fastForward(5_100)
  await expect(editor).toContainText('正式采用的新正文。')
  expect(state.requests.filter(item => item.path === '/chapters/chapter-1')).toHaveLength(initialBodyReads + 1)
})

test('a delayed chapter refresh cannot overwrite edits made while it is loading', async ({ page }) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  let release: (() => void) | undefined
  let requested = false
  await page.route('**/api/chapters/chapter-1', async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    requested = true
    await new Promise<void>(resolve => { release = resolve })
    return route.fulfill({ json: { ...state.chapters[0], content: doc('迟到的远端正文。') } })
  })
  state.chapters[0].version = 2
  await page.clock.fastForward(5_100)
  await expect.poll(() => requested).toBe(true)
  await editor.fill('这段正在编辑，不应被迟到响应替换。')
  release!()
  await expect(page.getByText('这一章已有新的正式内容。', { exact: false })).toBeVisible()
  await expect(editor).toContainText('这段正在编辑，不应被迟到响应替换。')
})


test('hidden work pages stop polling and refresh immediately when visible', async ({ page }) => {
  await page.clock.install()
  const state = await stubStudy(page, true)
  const editor = page.getByRole('textbox', { name: '章节正文' })
  await expect(editor).toContainText('她站在雨里的车站。')
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  const reads = state.requests.filter(item => item.path === '/projects').length
  await page.clock.fastForward(60_000)
  expect(state.requests.filter(item => item.path === '/projects')).toHaveLength(reads)
  state.chapters[0] = { ...state.chapters[0], version: 2, content: doc('返回页面后显示的新正文。') }
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await expect(editor).toContainText('返回页面后显示的新正文。')
})
