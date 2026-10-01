import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const vue = require('vue')
const source = readFileSync(new URL('../src/views/Voices.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const compiled = ts.transpileModule(source + '\nexport { loadVoices, speakers, prompts, voicesLoading, voicesLoadError, hasScript, regenFoundation, doClones, openMerge, closeMerge, openPicker, closePicker, openMergeConfirm, overlayKeydown, pickerPanel, mergePanel, mergeConfirmPanel, mergeConfirm, mergeTarget, mergeBusy, pickerBusy };', {
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
  const document = { activeElement: null }
  const api = {
    listVoices,
    prepareFoundations: async body => { calls.push({ type: 'foundation', body }); return { task_id: 'foundation-a' } },
    generateVoiceCandidates: async body => { calls.push({ type: 'clone', body }); return { task_id: 'clone-a' } },
  }
  const module = { exports: {} }
  runInNewContext(compiled, {
    module, exports: module.exports, document,
    require(name) {
      if (name === 'vue') return { ...vue, onMounted() {}, onActivated() {}, onBeforeUnmount() {} }
      if (name === 'vue-router') return { useRouter: () => ({ push() {} }) }
      if (name === '@/api/tts') return api
      if (name === '@/stores/auth') return { useAuthStore: () => auth }
      if (name === '@/stores/project') return { useProjectStore: () => project }
      if (name === '@/stores/settings') return { useSettingsStore: () => ({ loaded: true }) }
      if (name === '@/stores/pipelineState') return { usePipelineStateStore: () => vue.reactive({ activeScript: '' }) }
      if (name === '@/stores/task') return { useTaskStore: () => ({ projectTasks: [], activeTasks: () => [], refresh: async () => {} }) }
      if (name === '@/composables/useProjectGate') return { useProjectGate: () => ({ projectSet: vue.ref(true) }) }
      if (name === '@/components/ui/toast') return { useToast: () => ({ push() {} }) }
      return { default: {} }
    },
  })
  return { ...module.exports, project, auth, calls, document }
}
const result = name => ({ has_script: true, speakers: [{ name }], script_path: '', voice_config_path: '' })

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
  assert.equal(JSON.stringify(h.calls[0].body), JSON.stringify({ new_only: true, concurrency: 4, script: '__all__', candidate_count: null }))
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
