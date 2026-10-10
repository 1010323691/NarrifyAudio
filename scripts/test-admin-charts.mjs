import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import test from 'node:test'
import ts from 'typescript'

// Load the real pure geometry module (no DOM) into a vm sandbox.
const code = readFileSync(new URL('../src/components/admin/charts/scale.ts', import.meta.url), 'utf8')
const module = { exports: {} }
runInNewContext(
  ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
  { module, exports: module.exports, Date, Math, Number, Infinity, Array },
)
const s = module.exports

test('niceTicks rounds the domain to 1/2/2.5/5 steps and honours integer + fixed max', () => {
  assert.deepEqual(Array.from(s.niceTicks(0, 87).ticks), [0, 25, 50, 75, 100])
  assert.equal(s.niceTicks(0, 87).max, 100)
  const fixed = s.niceTicks(0, 100, { fixedMax: true })
  assert.deepEqual(Array.from(fixed.ticks), [0, 25, 50, 75, 100])
  assert.equal(fixed.max, 100)
  assert.ok(s.niceTicks(0, 3, { integer: true }).ticks.every(Number.isInteger))
  assert.ok(s.niceTicks(0, 0).max > 0)
})

test('segments break lines at null instead of interpolating', () => {
  const parts = s.segments([{ x: 0, y: 1 }, { x: 1, y: null }, { x: 2, y: 3 }, { x: 3, y: 4 }])
  assert.equal(parts.length, 2)
  assert.equal(parts[0].length, 1)
  assert.equal(parts[1].length, 2)
})

test('sparkPaths maps into y∈[4,30] on a zero baseline and closes the area to the bottom', () => {
  assert.equal(s.sparkPaths([1]).line, '')
  const { line, area } = s.sparkPaths([0, 10], 10)
  assert.equal(line, 'M0,30L100,4')
  assert.ok(area.endsWith('Z') && area.includes('L100,32'))
  assert.equal(s.sparkPaths([5, null, 5]).line, 'M0,4h0M100,4h0')
})

test('gauge arc follows the 270° / r=50 contract and clamps the ratio', () => {
  assert.equal(s.gaugeDash(0), `0.0 ${s.GAUGE_CIRC.toFixed(1)}`)
  assert.equal(s.gaugeDash(1).split(' ')[0], '235.6')
  assert.equal(s.gaugeDash(2).split(' ')[0], '235.6')
  assert.equal(s.gaugeDash(-1).split(' ')[0], '0.0')
})

test('donutSlices accumulates offsets clockwise and handles an empty total', () => {
  const slices = s.donutSlices([1, 1, 2], 100)
  assert.deepEqual(Array.from(slices, item => item.offset), [0, 25, 50])
  assert.deepEqual(Array.from(slices, item => item.length), [25, 25, 50])
  assert.ok(s.donutSlices([1, 1], 100, 2).every(item => item.length === 48))
  assert.ok(s.donutSlices([0, 0], 100).every(item => item.length === 0))
})

test('nearestIndex and truncate behave for hover / label fitting', () => {
  assert.equal(s.nearestIndex([10, 20, 40], 33), 2)
  assert.equal(s.nearestIndex([10, 20, 40], 12), 0)
  assert.equal(s.truncate('abc', 100), 'abc')
  assert.ok(s.truncate('一二三四五六七八九十', 44).endsWith('…'))
})

test('roundedRect keeps corner radii within the rect and closes the path', () => {
  const d = s.roundedRect(0, 0, 4, 100, { tl: 4, tr: 4 })
  assert.ok(d.startsWith('M2,0') && d.endsWith('Z'))
})

test('time labels use the requested granularity', () => {
  const t = new Date(2026, 0, 5, 7, 9).getTime()
  assert.equal(s.timeLabel(t, 'minute'), '07:09')
  assert.equal(s.timeLabel(t, 'hour'), '01-05 07:00')
  assert.equal(s.timeLabel(t, 'day'), '01-05')
  assert.equal(s.tooltipTime(t, 'day'), '01-05')
  assert.equal(s.tooltipTime(t, 'minute'), '01-05 07:09')
})
