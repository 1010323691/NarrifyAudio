import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'
const require = createRequire(import.meta.url)
const vue = require('vue')
const source = readFileSync(new URL('../src/views/Admin.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const compiled = ts.transpileModule(source + '\nexport { load, users, loading, error, loadedTabs, userRole, userState, userSearch, userSort, matchingUsers, shownUsers, userPage, runAction, actionBusy, lastAdmin };', { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function deferred() { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
function harness(overrides = {}) {
  const route = vue.reactive({ query: { tab: 'users' } })
  const deactivated = []
  const module = { exports: {} }
  runInNewContext(compiled, {
    module, exports: module.exports, setInterval() { return 1 }, clearInterval() {}, document: { visibilityState: 'visible' },
    require(name) {
      if (name === 'vue') return { ...vue, onMounted() {}, onActivated() {}, onDeactivated(fn) { deactivated.push(fn) }, onBeforeUnmount() {} }
      if (name === 'vue-router') return { useRoute: () => route }
      if (name === '@/stores/clientDisplay') return { useClientDisplayStore: () => ({}) }
      if (name === '@/components/ui/toast') return { useToast: () => ({ push() {} }) }
      if (name === '@/api/admin') return { listUsers: async () => [], ...overrides }
      return { default: {} }
    },
  })
  return { ...module.exports, route, deactivate: () => deactivated.forEach(fn => fn()) }
}
test('a late refresh cannot overwrite the newest user data', async () => {
  const old = deferred(); let calls = 0
  const h = harness({ listUsers: () => ++calls === 1 ? old.promise : Promise.resolve([{ id: 'new' }]) })
  const pending = h.load(); await h.load(); old.resolve([{ id: 'old' }]); await pending
  assert.equal(h.users.value[0].id, 'new'); assert.equal(h.loading.value, false)
})
test('leaving a cached page invalidates its in-flight response', async () => {
  const old = deferred(); const h = harness({ listUsers: () => old.promise })
  const pending = h.load(); h.deactivate(); old.resolve([{ id: 'old' }]); await pending
  assert.equal(h.users.value.length, 0); assert.equal(h.loadedTabs.value.has('users'), false)
})
test('failed first loads expose retry feedback without displaying an empty success state', async () => {
  const h = harness({ listUsers: async () => { throw new Error('unavailable') } }); await h.load()
  assert.equal(h.error.value, 'unavailable'); assert.equal(h.loading.value, false); assert.equal(h.loadedTabs.value.has('users'), false)
})
test('user filters combine role, status, and trimmed case-insensitive search', () => {
  const h = harness(); h.users.value = [
    { id:'1', username:'Alice', email:'alice@example.test', role:'user', is_active:true, storage_bytes:20 },
    { id:'2', username:'Bob', email:'bob@example.test', role:'admin', is_active:false, storage_bytes:40 },
  ]
  h.userRole.value='user'; h.userState.value='active'; h.userSearch.value=' ALICE '
  assert.equal(h.matchingUsers.value.length,1); assert.equal(h.matchingUsers.value[0].id,'1')
  h.userRole.value='all'; h.userState.value='all'; h.userSearch.value=''; h.userSort.value='storage'
  assert.equal(h.matchingUsers.value[0].id,'2'); assert.equal(h.users.value[0].id,'1')
})
test('filtering resets pagination and shrinking results clamps the current page', async () => {
  const h=harness(); h.users.value=Array.from({length:45},(_,i)=>({id:String(i),username:`person${i}`,email:'',role:'user',is_active:true}))
  h.userPage.value=3; h.userSearch.value='person1'; await vue.nextTick()
  assert.equal(h.userPage.value,1); assert.equal(h.shownUsers.value.length,11)
})
test('mutations cannot run twice while a confirmation or request is pending', async () => {
  const wait=deferred(); const h=harness(); let calls=0
  const action=async()=>{ calls++; await wait.promise }
  const first=h.runAction(action); await h.runAction(action)
  assert.equal(calls,1); assert.equal(h.actionBusy.value,true)
  wait.resolve(); await first; assert.equal(h.actionBusy.value,false)
})
test('the final active administrator remains protected', () => {
  const h=harness(); const admin={role:'admin',is_active:true}; h.users.value=[admin]
  assert.equal(h.lastAdmin(admin),true); h.users.value.push({role:'admin',is_active:true}); assert.equal(h.lastAdmin(admin),false)
})
