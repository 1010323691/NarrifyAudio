import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

function load(file) {
  const module = { exports: {} }
  const source = readFileSync(new URL(`../src/utils/${file}.ts`, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } })
  runInNewContext(outputText, { module, exports: module.exports, require: name => load(name.replace('./', '')) })
  return module.exports
}
const { previewCompletion, stageProgressColor } = load('projectStageProgress')
test('progress colors interpolate continuously from red to violet', () => {
  assert.equal(stageProgressColor(0), '239 68 68')
  assert.equal(stageProgressColor(50), '182 80 157')
  assert.equal(stageProgressColor(100), '124 92 246')
  assert.equal(stageProgressColor(120), stageProgressColor(100))
})

test('preview readiness uses available audio over parsed dialogue even when whole-project totals are unknown', () => {
  const value = { completed: 25, total: 73, unit: '段', percent: null }
  assert.equal(previewCompletion(value).percent, 34)
  assert.equal(value.percent, null)
  assert.equal(previewCompletion({ ...value, completed: 73 }).percent, 100)
  assert.equal(previewCompletion({ ...value, completed: 0, total: 0 }).percent, 0)
})
