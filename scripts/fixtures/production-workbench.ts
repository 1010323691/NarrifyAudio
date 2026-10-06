// Development-only browser fixture. No request reaches the real backend.
import { createApp, h, ref, nextTick } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory, RouterView } from 'vue-router'
import MainLayout from '../../src/layouts/MainLayout.vue'
import AdminLayout from '../../src/layouts/AdminLayout.vue'
import Admin from '../../src/views/Admin.vue'
import MusicLibrary from '../../src/views/MusicLibrary.vue'
import Login from '../../src/views/Login.vue'
import AccessDenied from '../../src/views/AccessDenied.vue'
import { adminLayoutResponse } from './admin-layout-data'
import BatchTTS from '../../src/views/BatchTTS.vue'
import Merge from '../../src/views/Merge.vue'
import BGM from '../../src/views/BGM.vue'
import Voices from '../../src/views/Voices.vue'
import Toaster from '../../src/components/ui/Toaster.vue'
import DialogHost from '../../src/components/ui/DialogHost.vue'
import { useAuthStore } from '../../src/stores/auth'
import { useProjectStore } from '../../src/stores/project'
import { useSettingsStore } from '../../src/stores/settings'
import { useTaskStore } from '../../src/stores/task'
import type { AppConfig, BgmChapterRow, TaskSnapshot } from '../../src/types'
import '../../src/style.css'
import '../../src/styles/mobile.css'

const scenario = ref('populated')
const calls = ref<string[]>([])
let projectId = 'fixture-a'
const tags = { scene: ['室内'], mood: ['平静'], emotion: [], custom: [] }
const names = Array.from({ length: 80 }, (_, i) =>
  i === 1
    ? '第二章_这是一个用于验证名称截断和列宽稳定性的特别长的章节文件名_雨夜里的旧车站与遥远的灯火'
    : `第${String(i + 1).padStart(2, '0')}章`,
)
let complete = new Set([0, 1, 2, 3, 4, 5])
let mixed = new Set([0, 1])
let taskRows: TaskSnapshot[] = []
let taskCounter = 0
const streams = new Set<FixtureEventSource>()
class FixtureEventSource {
  static CLOSED = 2
  readyState = 1
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  constructor() {
    streams.add(this)
    setTimeout(() => this.send(), 10)
  }
  send() {
    this.onmessage?.({ data: JSON.stringify({ type: 'snapshot_all', tasks: taskRows }) })
  }
  close() {
    this.readyState = 2
    streams.delete(this)
  }
}
window.EventSource = FixtureEventSource as unknown as typeof EventSource
function snapshot() {
  streams.forEach((stream) => stream.send())
}
function task(module: string, key: string, status: 'running' | 'failed' = 'running') {
  const id = `fixture-${++taskCounter}`
  const value = {
    id,
    module,
    project_id: projectId,
    project_name: '验收样本',
    label: `制作：${key}`,
    task_type:
      module === 'merge'
        ? 'tts.merge'
        : module === 'tts-batch'
          ? 'tts.batch'
          : module.replace('-', '.'),
    status,
    progress: 0.42,
    current: '测试夹具任务',
    logs: [],
    result: {},
    error: status === 'failed' ? '测试错误：输入暂不可用，请重试。' : '',
    created: 1,
    created_at: '2026-10-04T00:00:00Z',
    started: 1,
    finished: 0,
    seq: taskCounter,
  } as TaskSnapshot
  taskRows.push(value)
  snapshot()
  return value
}
const assignmentOverrides = new Map<string, BgmChapterRow['assignment']>()
function chapters(): BgmChapterRow[] {
  return names.map((stem, i) => ({
    stem,
    narration_exists: i < 7,
    mix_exists: mixed.has(i),
    music_missing: i === 3 && !assignmentOverrides.has(stem),
    segment_music_missing: false,
    assignment: assignmentOverrides.get(stem) ?? (
      i < 5
        ? {
            music: i === 4 ? null : '平静的夜晚.wav',
            locked: i === 1,
            manual: false,
            score: null,
            reason: '测试夹具：场景标签匹配',
            matched_at: '',
            tags,
            segment: i === 2,
          }
        : null),
    segment_analysis: i === 2 ? { analyzed_at: '', entry_count: 12, stale: false, tags } : null,
    timeline: i === 2 ? { sections: 2, duration: 125, generated_at: '测试时间' } : null,
  }))
}
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input, options) => {
  const url = new URL(
    typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
    location.origin,
  )
  const path = decodeURIComponent(url.pathname)
  const method = options?.method ?? 'GET'
  const body = options?.body ? JSON.parse(String(options.body)) : {}
  if (path.startsWith('/api/')) {
    calls.value.push(`${method} ${path}`)
    calls.value = calls.value.slice(-200)
  }
  if (path === '/api/health') return json({ ok: true, service: 'fixture', port: 0 })
  const adminSample = path.startsWith('/api/v1/admin/') ? adminLayoutResponse(path) : undefined
  if (adminSample !== undefined) return json(adminSample)
  if (scenario.value === 'loading' && method === 'GET')
    await new Promise((resolve) => setTimeout(resolve, 500))
  if (
    scenario.value === 'error' &&
    (path.includes('/files/list') || path.includes('/bgm/chapters'))
  )
    return json({ detail: '测试夹具：读取失败，请重试。' }, 503)
  const empty = scenario.value === 'empty'
  if (path === '/api/tts/status') return json({ implemented: true, ready: true, message: '' })
  if (path.startsWith('/api/files/list/')) {
    const module = path.split('/').pop()
    const items = empty
      ? []
      : names
          .filter((_, i) => module !== '06_audio_merge' || i < 7)
          .map((name, i) => ({
            name:
              module === '03_parsed_json'
                ? `${name}.json`
                : module === '06_audio_merge'
                  ? `${name}.${i === 6 ? 'wav' : 'mp3'}`
                  : name,
            is_dir: module === '05_audio_chunk',
            size: 1024,
          }))
    return json({ module, items })
  }
  if (path === '/api/tts/batch-status')
    return json({
      files: empty
        ? []
        : names.map((name, i) => ({
            name: `${name}.json`,
            total: 24,
            completed: complete.has(i) ? 24 : i === 7 ? 12 : 0,
            remaining: complete.has(i) ? 0 : i === 7 ? 12 : 24,
            complete: complete.has(i),
            speakers: 3,
            ready: i === 8 ? 2 : 3,
            missing: i === 8 ? ['林舟'] : [],
            stale_speakers: i === 9 ? ['旁白'] : [],
          })),
    })
  if (path === '/api/tts/merge-status')
    return json({
      packages: names.map((name, i) => ({
        name,
        total: 24,
        completed: complete.has(i) ? 24 : 12,
        remaining: complete.has(i) ? 0 : 12,
        complete: complete.has(i),
      })),
    })
  if (path === '/api/bgm/chapters')
    return json({ chapters: empty ? [] : chapters(), mode: 'random' })
  if (path === '/api/music/library')
    return json({ tags, tracks: Object.fromEntries(['平静的夜晚', ...names].map(name => [`${name}.wav`, { enabled: true, tags, duration: 60, size_bytes: 1024, folder: '', description: '隔离曲目说明'.repeat(20) }])) })
  if (path === '/api/tts/voices')
    return json({
      has_script: true,
      speakers: empty
        ? []
        : names
            .slice(0, 15)
            .map((_, i) => ({
              name: i === 0 ? '旁白' : `角色${i}`,
              gender: i % 2 ? 'male' : 'female',
              line_count: 42,
              status: i < 5 ? 'ready' : 'pending',
              foundation_status: i < 5 ? 'done' : 'pending',
              description: '验收样本声音描述。',
              preview: '',
              alias_of: null,
            })),
      script_path: '',
      voice_config_path: '',
    })
  if (path === '/api/tts/batch') return json({ task_id: task('tts-batch', '所选解析文件').id })
  if (path === '/api/tts/merge') {
    ;(body.packages ?? []).forEach((name: string) => task('merge', name))
    return json({ task_ids: taskRows.map((t) => t.id) })
  }
  if (path === '/api/bgm/mix' || path === '/api/bgm/analyze-segment') {
    ;(body.chapters ?? []).forEach((name: string) =>
      task(path.endsWith('mix') ? 'bgm-mix' : 'bgm-segment', name),
    )
    return json({ task_ids: taskRows.map((t) => t.id) })
  }
  if (/\/api\/v1\/tasks\/[^/]+\/(retry|cancel)$/.test(path)) {
    const t = taskRows.find((t) => t.id === path.split('/')[4])
    if (t) {
      t.status = path.endsWith('retry') ? 'running' : 'cancelled'
      t.error = ''
      snapshot()
    }
    return json({ id: t?.id, status: t?.status })
  }
  if (path.startsWith('/api/bgm/chapters/') && method === 'PUT') {
    const stem = path.split('/').pop()!
    const assignment = { music: null, locked: false, manual: true, score: null, reason: '测试夹具：手动选曲', matched_at: '', tags, ...chapters().find(r => r.stem === stem)?.assignment, ...body }
    assignmentOverrides.set(stem, assignment)
    return json(assignment)
  }
  if (path === '/api/bgm/match') {
    const scope = (body.chapters ?? names) as string[]
    let skipped = 0
    for (const stem of scope) {
      if (chapters().find(row => row.stem === stem)?.assignment?.locked) { skipped++; continue }
      assignmentOverrides.set(stem, { music: '平静的夜晚.wav', locked: false, manual: false, score: null, reason: '测试夹具：重新匹配', matched_at: '', tags })
    }
    return json({ matched: scope.length - skipped, no_bgm: 0, skipped_locked: skipped })
  }
  if (path.startsWith('/api/bgm/timeline/'))
    return json({
      timeline: {
        version: 2,
        stem: names[2],
        generated_at: '测试夹具时间',
        model: '测试分析模型',
        duration: 125,
        entry_count: 12,
        fingerprint: 'fixture',
        timeline: [0, 1].map((i) => ({
          start: i * 60,
          end: (i + 1) * 60,
          music_id: '平静的夜晚.wav',
          intensity: i + 1,
          volume: 0.15,
          tags,
          reason: '测试段落匹配依据',
          scene_desc: '室内对话',
          mood_desc: '平静',
        })),
      },
    })
  if (path === '/api/config/client-logs') return json({ enabled: false })
  if (path === '/api/v1/projects/active') return json(project.current)
  if (path === '/api/v1/projects') return json(project.projects)
  return json({ detail: `测试夹具未实现该请求：${path}` }, 404)
}
const pinia = createPinia()
const auth = useAuthStore(pinia)
auth.user = {
  id: 'fixture-user',
  role: location.hash.startsWith('#/admin') ? 'admin' : 'user',
  username: 'fixture',
  display_name: '验收样本',
} as typeof auth.user
auth.loaded = true
const project = useProjectStore(pinia)
project.current = {
  project_id: projectId,
  project_name: '浏览器验收（隔离数据）',
  set: true,
} as typeof project.current
project.loaded = true
project.projects = [
  {
    id: projectId,
    name: '浏览器验收（隔离数据）',
    directory_key: 'fixture',
    created_at: '',
    updated_at: '',
  },
]
const settings = useSettingsStore(pinia)
settings.config = {
  paths: { working_dir: 'fixture' },
  ui: { theme: 'light', show_audio_split: true },
  tts: { batch_concurrency: 4, batch_auto: false },
} as AppConfig
settings.loaded = true
const tasks = useTaskStore(pinia)
tasks.bindProject(projectId)
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', component: Login },
    { path: '/admin/login', component: Login },
    { path: '/access-denied', component: AccessDenied },
    { path: '/admin', component: AdminLayout, children: [{ path: '', component: Admin }, { path: 'music', component: MusicLibrary }] },
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: 'batch', component: BatchTTS },
        { path: 'merge', component: Merge },
        { path: 'bgm', component: BGM },
        { path: 'voices', component: Voices },
      ],
    },
  ],
})
const control = h(
  'div',
  {
    style:
      'position:fixed;bottom:0;left:0;z-index:70;display:flex;flex-wrap:wrap;gap:8px;background:hsl(var(--background));border:1px solid hsl(var(--border));padding:4px;font-size:11px',
  },
  [
    h('strong', '隔离测试数据'),
    ...['batch', 'merge', 'bgm', 'voices'].map((view) =>
      h('button', { onClick: () => router.push('/' + view) }, view),
    ),
    ...['populated', 'empty', 'error', 'loading'].map((value) =>
      h(
        'button',
        {
          onClick: () => {
            scenario.value = value
            document.querySelector<HTMLButtonElement>('[aria-label="刷新列表"]')?.click()
          },
        },
        value,
      ),
    ),
    h(
      'button',
      { onClick: () => document.documentElement.classList.toggle('dark') },
      '切换测试主题',
    ),
    h(
      'button',
      {
        onClick: () => {
          task('merge', names[2], 'failed')
          task('bgm-mix', names[4], 'failed')
          task('merge', names[3])
          task('bgm-segment', names[5])
          snapshot()
          void tasks.refresh()
        },
      },
      '运行与失败任务',
    ),
    h(
      'button',
      {
        onClick: async () => {
          taskRows.forEach((t) => {
            t.status = 'succeeded'
            t.error = ''
            t.result = { file: names[3] + '.mp3', completed: 1, total: 1, failed: [] }
          })
          complete.add(7)
          mixed.add(4)
          snapshot()
          await nextTick()
        },
      },
      '完成任务',
    ),
    h(
      'button',
      {
        onClick: () => {
          taskRows.forEach((t) => {
            t.status = 'succeeded'
            t.error = ''
            t.result = {
              completed: 0, total: 80, done_count: 0, all_count: 80,
              failed: names.map((script, index) => ({ script, index, speaker: '旁白', error: '隔离测试：用于验收展开后的长报告与错误内容。'.repeat(8) })),
              files: names.map(script => ({ script, done_count: 0, all_count: 1, failed: 1 })),
            }
          })
          snapshot()
        },
      },
      '完成并展开长报告',
    ),
    h(
      'button',
      {
        onClick: () => {
          projectId = projectId === 'fixture-a' ? 'fixture-b' : 'fixture-a'
          taskRows = []
          tasks.bindProject(projectId)
          project.current = { ...project.current!, project_id: projectId }
          snapshot()
        },
      },
      '切换测试项目',
    ),
  ],
)
createApp({
  render: () =>
    h('div', [
      h(RouterView),
      h(Toaster),
      h(DialogHost),
      control,
      h('output', { style: 'display:none' }, calls.value.join('\n')),
    ]),
})
  .use(pinia)
  .use(router)
  .mount('#app')
if (!location.hash) void router.push('/batch')
