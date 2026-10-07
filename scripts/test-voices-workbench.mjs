import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const source = readFileSync(new URL('../src/views/Voices.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const compiled = ts.transpileModule(source + '\nexport { cloneBadge, cloneBatch, cloneSubmitting, cloneBusy, foundationBadge, foundationBatch, foundationSubmitting, doFoundations, reattachTasks, scheduleProgressRefresh, clearProgressRefresh, loadVoices, speakers, prompts, voicesLoading, voicesLoadError, hasScript, regenFoundation, doClones, openMerge, closeMerge, openPicker, closePicker, openMergeConfirm, overlayKeydown, pickerPanel, mergePanel, mergeConfirmPanel, mergeConfirm, mergeTarget, mergeBusy, pickerBusy };', {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function harness(listVoices) {
  const project = vue.reactive({ activeProjectId: 'project-a' })
  const auth = vue.reactive({ user: { id: 'user-a' } })
  const calls = []
  const taskStore = vue.reactive({ projectTasks: [], activeTasks(module) {
    return this.projectTasks.filter(row => ['pending', 'running', 'paused'].includes(row.status) && (!module || row.module === module))
  }, refresh: async () => {} })
  const document = { activeElement: null }
  const api = {
    listVoices,
    prepareFoundations: async body => { calls.push({ type: 'foundation', body }); return { task_ids: ['foundation-a'], task_id: 'foundation-a' } },
    generateVoiceCandidates: async body => { calls.push({ type: 'clone', body }); return { task_ids: ['clone-a'], task_id: 'clone-a' } },
  }
  const cache = new Map()
  function loadComposable(name) {
    const base = name.split('/').pop()
    if (!cache.has(base)) {
      const module = { exports: {} }
      const code = ts.transpileModule(readFileSync(new URL(`../src/composables/${base}.ts`, import.meta.url), 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS },
      }).outputText
      runInNewContext(code, { module, exports: module.exports, DOMException, AbortController, require: resolve })
      cache.set(base, module.exports)
    }
    return cache.get(base)
  }
  function resolve(name) {
    if (((name.includes('useListPage') || name.includes('useWorkbench')) || name.includes('useLabelDerivedTasks'))) return loadComposable(name)
    return dependencies(name)
  }
  function dependencies(name) {
    if (name === 'vue') return { ...vue, onMounted() {}, onActivated() {}, onDeactivated() {}, onBeforeUnmount() {} }
    if (name === '@/stores/auth') return { useAuthStore: () => auth }
    if (name === '@/stores/project') return { useProjectStore: () => project }
    if (name === '@/stores/task') return { useTaskStore: () => taskStore }
    return { default: {} }
  }
  const module = { exports: {} }
  runInNewContext(compiled, {
    module, exports: module.exports, AbortController, document, setTimeout, clearTimeout,
    require(name) {
      if (((name.includes('useListPage') || name.includes('useWorkbench')) || name.includes('useLabelDerivedTasks'))) return loadComposable(name)
      if (name === 'vue') return { ...vue, onMounted() {}, onActivated() {}, onBeforeUnmount() {} }
      if (name === 'vue-router') return { useRouter: () => ({ push() {} }) }
      if (name === '@/api/tts') return api
      if (name === '@/stores/auth') return { useAuthStore: () => auth }
      if (name === '@/stores/project') return { useProjectStore: () => project }
      if (name === '@/stores/settings') return { useSettingsStore: () => ({ loaded: true }) }
      if (name === '@/stores/pipelineState') return { usePipelineStateStore: () => vue.reactive({ activeScript: '' }) }
      if (name === '@/stores/task') return { useTaskStore: () => taskStore }
      if (name === '@/composables/useProjectGate') return { useProjectGate: () => ({ projectSet: vue.ref(true) }) }
      if (name === '@/components/ui/toast') return { useToast: () => ({ push() {} }) }
      return { default: {} }
    },
  })
  return { ...module.exports, project, auth, calls, document, taskStore, api }
}
const result = name => ({ has_script: true, speakers: [{ name }], script_path: '', voice_config_path: '' })

const foundationRow = (name, status, seq = 1) => ({
  id: `foundation-${name}-${seq}`, module: 'voices-foundation',
  label: `语音推理基础 · ${name}`, status, seq, progress: status === 'succeeded' ? 1 : 0,
  current: '', logs: [], result: {},
})

test('regeneration immediately replaces the old completed badge for its selected role', async () => {
  const h = harness(async () => result('A'))
  const waiting = deferred()
  h.api.prepareFoundations = () => waiting.promise
  const a = { name: 'A', foundation_status: 'done' }
  const b = { name: 'B', foundation_status: 'done' }
  assert.equal(h.foundationBadge(a).label, '已完成')
  const request = h.doFoundations({ speakers: ['A'] })
  assert.equal(h.foundationBadge(a).label, '处理中')
  assert.equal(h.foundationBadge(a).spin, true)
  assert.equal(h.foundationBadge(b).label, '已完成')
  waiting.reject(new Error('submit failed'))
  await request
  assert.equal(h.foundationBadge(a).label, '已完成')
  assert.equal(h.foundationSubmitting.value, false)
})

test('each foundation row completes independently while other regenerated roles are running', async () => {
  const h = harness(async () => result('A'))
  const a = { name: 'A', foundation_status: 'done' }
  const b = { name: 'B', foundation_status: 'done' }
  h.taskStore.projectTasks = [foundationRow('A', 'pending'), foundationRow('B', 'pending')]
  h.foundationBatch.track(h.taskStore.projectTasks.map(row => row.id))
  assert.equal(h.foundationBadge(a).label, '排队中')
  h.taskStore.projectTasks[0].status = 'running'
  assert.equal(h.foundationBadge(a).label, '处理中')
  h.taskStore.projectTasks[0].status = 'succeeded'
  h.taskStore.projectTasks[1].status = 'running'
  assert.equal(h.foundationBadge(a).label, '已完成')
  assert.equal(h.foundationBadge(a).spin, false)
  assert.equal(h.foundationBadge(b).label, '处理中')
  await vue.nextTick()
  h.clearProgressRefresh()
  // Replay can age out a completed character while the batch is still running.
  h.taskStore.projectTasks = [h.taskStore.projectTasks[1]]
  assert.equal(h.foundationBadge(a).label, '已完成')
  assert.equal(h.foundationBadge(b).label, '处理中')
})

test('replayed tasks override previous completion and use the latest character attempt', async () => {
  const h = harness(async () => result('A'))
  const a = { name: 'A', foundation_status: 'done' }
  h.taskStore.projectTasks = [foundationRow('A', 'succeeded', 1), foundationRow('A', 'running', 2)]
  h.reattachTasks()
  assert.equal(h.foundationBadge(a).label, '处理中')
  h.taskStore.projectTasks[1].status = 'paused'
  assert.equal(h.foundationBadge(a).label, '已暂停')
  assert.equal(h.foundationBadge(a).spin, false)
  h.taskStore.projectTasks[1].status = 'failed'
  assert.equal(h.foundationBadge(a).label, '失败')
  h.taskStore.projectTasks.push(foundationRow('A', 'running', 3))
  assert.equal(h.foundationBadge(a).label, '处理中')
  h.taskStore.projectTasks[2].status = 'cancelled'
  assert.equal(h.foundationBadge(a).label, '已完成')
  h.clearProgressRefresh()
})

test('new-only submission leaves completed roles untouched', async () => {
  const h = harness(async () => result('A'))
  const waiting = deferred()
  h.api.prepareFoundations = () => waiting.promise
  h.speakers.value = [{ name: 'A', foundation_status: 'done' }, { name: 'B', foundation_status: 'none' }]
  const request = h.doFoundations({ new_only: true })
  assert.equal(h.foundationBadge(h.speakers.value[0]).label, '已完成')
  assert.equal(h.foundationBadge(h.speakers.value[1]).label, '处理中')
  waiting.reject(new Error('submit failed'))
  await request
  assert.equal(h.foundationBadge(h.speakers.value[1]).label, '未生成')
})

test('foundation badge distinguishes the character outcome from task completion', () => {
  const h = harness(async () => result('A'))
  const row = foundationRow('A', 'succeeded')
  row.result = { results: [{ speaker: 'A', ok: false }] }
  h.taskStore.projectTasks = [row]
  assert.equal(h.foundationBadge({ name: 'A', foundation_status: 'done' }).label, '失败')
  h.taskStore.projectTasks[0].result = { results: [{ speaker: 'A', ok: true }] }
  // The SSE completion can arrive before the role-list response.
  assert.equal(h.foundationBadge({ name: 'A', foundation_status: 'none' }).label, '已完成')
})

test('progress bursts coalesce and never overlap role refresh requests', async () => {
  const waiting = deferred()
  let calls = 0
  const h = harness(() => { calls++; return waiting.promise })
  for (let i = 0; i < 100; i++) h.scheduleProgressRefresh()
  await new Promise(resolve => setTimeout(resolve, 450))
  assert.equal(calls, 1)
  for (let i = 0; i < 100; i++) h.scheduleProgressRefresh()
  await new Promise(resolve => setTimeout(resolve, 450))
  assert.equal(calls, 1)
  waiting.resolve(result('new'))
  await vue.nextTick()
  await new Promise(resolve => setTimeout(resolve, 450))
  assert.equal(calls, 2)
  h.clearProgressRefresh()
})

test('project switch cancels a scheduled progress refresh', async () => {
  let calls = 0
  const h = harness(async () => { calls++; return result('new') })
  h.scheduleProgressRefresh()
  h.project.activeProjectId = 'project-b'
  await vue.nextTick()
  await new Promise(resolve => setTimeout(resolve, 450))
  assert.equal(calls, 0)
})

test('out-of-order role requests retain the newest response and loading state', async () => {
  const first = deferred(), second = deferred()
  let count = 0
  const h = harness(() => (++count === 1 ? first.promise : second.promise))
  const old = h.loadVoices(), latest = h.loadVoices()
  first.resolve(result('old'))
  await old
  assert.equal(h.voicesLoading.value, true)
  assert.equal(h.speakers.value.length, 0)
  second.resolve(result('new'))
  await latest
  assert.equal(h.speakers.value[0].name, 'new')
  assert.equal(h.voicesLoading.value, false)
})

test('refresh failure retains known roles and exposes an actionable error', async () => {
  let fail = false
  const h = harness(async () => { if (fail) throw new Error('connection lost'); return result('known') })
  await h.loadVoices()
  fail = true
  await h.loadVoices()
  assert.equal(h.speakers.value[0].name, 'known')
  assert.equal(h.hasScript.value, true)
  assert.equal(h.voicesLoadError.value, 'connection lost')
  assert.equal(h.voicesLoading.value, false)
  fail = false
  await h.loadVoices()
  assert.equal(h.voicesLoadError.value, '')
})

for (const changed of ['project', 'account']) {
  test(`a late response cannot restore roles or prompts after ${changed} switch`, async () => {
    const waiting = deferred()
    const h = harness(() => waiting.promise)
    h.prompts.old = 'previous description'
    const request = h.loadVoices()
    if (changed === 'project') h.project.activeProjectId = 'project-b'
    else h.auth.user = { id: 'user-b' }
    await vue.nextTick()
    waiting.resolve(result('previous'))
    await request
    assert.deepEqual(Array.from(h.speakers.value), [])
    assert.deepEqual(Object.keys(h.prompts), [])
    assert.equal(h.voicesLoading.value, false)
  })
}

test('single-role foundation regeneration preserves prompt override across all chapters', async () => {
  const h = harness(async () => result('role'))
  await vue.nextTick()
  h.prompts.role = '  warm voice  '
  h.regenFoundation({ name: 'role' })
  await vue.nextTick()
  assert.equal(JSON.stringify(h.calls[0].body), JSON.stringify({ speakers: ['role'], overrides: { role: 'warm voice' }, script: '__all__' }))
})

test('whole-book clone request keeps the original batch range and defaults', async () => {
  const h = harness(async () => result('role'))
  await vue.nextTick()
  await h.doClones({ new_only: true })
  assert.equal(JSON.stringify(h.calls[0].body), JSON.stringify({ new_only: true, script: '__all__' }))
})

function focusFixture(h, names, initial = names[0]) {
  const controls = names.map(name => ({
    name, isConnected: true,
    focus() { h.document.activeElement = this },
    getClientRects() { return [{}] },
  }))
  const panel = {
    querySelector: () => controls.find(control => control.name === initial),
    querySelectorAll: () => controls,
    contains: el => controls.includes(el),
  }
  return { panel, controls }
}
async function flushFocus() { await vue.nextTick(); await vue.nextTick() }
function key(key, shiftKey = false) {
  return { key, shiftKey, prevented: false, preventDefault() { this.prevented = true }, stopPropagation() {} }
}

test('merge window and nested confirmation own focus and restore each opener', async () => {
  const h = harness(async () => result('role'))
  await h.loadVoices()
  const opener = focusFixture(h, ['drawer-merge']).controls[0]
  const merge = focusFixture(h, ['close', 'search', 'target', 'next'], 'search')
  const confirm = focusFixture(h, ['cancel', 'confirm'])
  h.mergePanel.value = merge.panel
  h.mergeConfirmPanel.value = confirm.panel
  opener.focus()
  h.openMerge(h.speakers.value[0])
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'search')
  merge.controls.at(-1).focus()
  const tab = key('Tab')
  h.overlayKeydown(tab)
  assert.equal(tab.prevented, true)
  assert.equal(h.document.activeElement.name, 'close')
  h.overlayKeydown(key('Tab', true))
  assert.equal(h.document.activeElement.name, 'next')
  h.mergeTarget.value = 'target'
  h.openMergeConfirm()
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'cancel')
  h.overlayKeydown(key('Escape'))
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'next')
  h.overlayKeydown(key('Escape'))
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'drawer-merge')
})

test('voice picker owns Tab and Shift+Tab, and closing restores the drawer opener', async () => {
  const h = harness(async () => result('role'))
  await h.loadVoices()
  const opener = focusFixture(h, ['drawer-picker']).controls[0]
  const picker = focusFixture(h, ['close', 'candidate', 'confirm'], 'candidate')
  h.pickerPanel.value = picker.panel
  opener.focus()
  h.openPicker(h.speakers.value[0])
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'candidate')
  picker.controls.at(-1).focus()
  h.overlayKeydown(key('Tab'))
  assert.equal(h.document.activeElement.name, 'close')
  h.overlayKeydown(key('Tab', true))
  assert.equal(h.document.activeElement.name, 'confirm')
  h.pickerBusy.value = true
  h.overlayKeydown(key('Escape'))
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'confirm')
  h.pickerBusy.value = false
  h.overlayKeydown(key('Escape'))
  await flushFocus()
  assert.equal(h.document.activeElement.name, 'drawer-picker')
})

test('role loading always requests all parsed chapters', async () => {
  const scopes = []
  const h = harness(async script => { scopes.push(script); return result('role') })
  await h.loadVoices()
  assert.deepEqual(scopes, ['__all__'])
})

test('an in-flight picker without focusable controls does not swallow Tab', async () => {
  const h = harness(async () => result('role'))
  await h.loadVoices()
  const picker = focusFixture(h, []) // busy request: every control in the panel is disabled
  h.pickerPanel.value = picker.panel
  h.openPicker(h.speakers.value[0])
  await flushFocus()
  h.pickerBusy.value = true
  const tab = key('Tab')
  h.overlayKeydown(tab)
  assert.equal(tab.prevented, false)
  const shift = key('Tab', true)
  h.overlayKeydown(shift)
  assert.equal(shift.prevented, false)
})


const cloneRow = (name, status, seq = 1) => ({
  id: `clone-${name}`, module: 'voices-clone', task_type: 'voices.clone', label: `克隆音频 · ${name}`, seq,
  status, progress: 0, logs: [], result: {},
})
test('clone regeneration overrides old completed audio and each role settles independently', async () => {
  const h = harness(async () => result('A'))
  const a = { name: 'A', clone_status: 'done' }, b = { name: 'B', clone_status: 'done' }
  h.taskStore.projectTasks = [cloneRow('A', 'pending'), cloneRow('B', 'running')]
  h.cloneBatch.track(['clone-A', 'clone-B'])
  assert.equal(h.cloneBadge(a).label, '排队中')
  assert.equal(h.cloneBadge(b).label, '制作中')
  h.taskStore.projectTasks[0].status = 'succeeded'
  h.taskStore.projectTasks[0].result = { results: [{ speaker: 'A', ok: true }] }
  await vue.nextTick()
  assert.equal(h.cloneBadge(a).label, '已完成')
  assert.equal(h.cloneBadge(b).label, '制作中')
  h.taskStore.projectTasks[1].status = 'paused'
  assert.equal(h.cloneBadge(b).label, '已暂停')
  h.taskStore.projectTasks[1].status = 'cancelling'
  assert.equal(h.cloneBadge(b).label, '取消中')
  h.taskStore.projectTasks[1].status = 'failed'
  assert.equal(h.cloneBadge(b).label, '失败')
})

test('clone outcome failure overrides an old usable take and timeout releases batch gating', async () => {
  const h = harness(async () => result('A'))
  const row = cloneRow('A', 'succeeded')
  row.result = { results: [{ speaker: 'A', ok: false }] }
  h.taskStore.projectTasks = [row]
  h.cloneBatch.track([row.id])
  assert.equal(h.cloneBadge({ name: 'A', clone_status: 'done' }).label, '失败')
  h.taskStore.projectTasks[0].status = 'running'
  await vue.nextTick()
  h.cloneBusy.value = true
  h.taskStore.projectTasks[0].status = 'timeout'
  await vue.nextTick()
  assert.equal(h.cloneBusy.value, false)
})

test('1000 clone tasks refresh in bursts without traversing historical logs', async () => {
  let requests = 0, historyReads = 0
  const h = harness(async () => { requests++; return result('A') })
  const rows = Array.from({ length: 1000 }, (_, index) => {
    const row = cloneRow(`role-${index}`, index === 0 ? 'running' : 'pending')
    row.logs = [Object.defineProperty({}, 'msg', {
      enumerable: true, get() { historyReads++; return 'historical log' },
    })]
    return row
  })
  h.taskStore.projectTasks = rows
  h.cloneBatch.track(rows.map(row => row.id))
  for (let index = 1; index <= 100; index++) {
    h.taskStore.projectTasks[0].progress = index / 2
    await vue.nextTick()
    assert.equal(h.cloneBadge({ name: 'role-0', clone_status: 'done' }).label, '制作中')
    assert.equal(h.cloneBadge({ name: 'role-999', clone_status: 'done' }).label, '排队中')
  }
  assert.equal(historyReads, 0)
  await new Promise(resolve => setTimeout(resolve, 450))
  assert.equal(requests, 1)
  h.clearProgressRefresh()
})

test('a coordinator role keeps its completed badge while the shared clone process finishes', () => {
  const h = harness(async () => result('A'))
  const row = cloneRow('A', 'running')
  row.progress = 100
  row.current = '克隆音频已完成'
  h.taskStore.projectTasks = [row]
  assert.equal(h.cloneBadge({ name: 'A', clone_status: 'done' }).label, '已完成')
  h.taskStore.projectTasks[0].current = '克隆音频失败'
  assert.equal(h.cloneBadge({ name: 'A', clone_status: 'done' }).label, '失败')
  h.taskStore.projectTasks[0].current = '候选 1/1'
  assert.equal(h.cloneBadge({ name: 'A', clone_status: 'done' }).label, '制作中')
})
