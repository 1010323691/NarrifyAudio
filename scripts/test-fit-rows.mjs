import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'

// 真实 fitRows.ts 转译后加载；directive 部分只引用类型，solveRowHeight 不依赖 DOM。
const source = readFileSync(new URL('../src/directives/fitRows.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { solveRowHeight, settleHeight, growHeight, tallyBurst } = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)

// 模拟母容器：盒子之外固定占 chrome 像素，盒子本身 = rows × 行高，容器可用高度 = avail。
const overflowAt = (rows, chrome, avail) => (height) => Math.max(0, chrome + rows * height - avail)

test('视口足够高时保持上限行高', () => {
  assert.equal(solveRowHeight(56, 40, 10, overflowAt(10, 300, 1000)), 56)
})

test('溢出被平摊到十行并收敛到刚好装下', () => {
  const height = solveRowHeight(56, 40, 10, overflowAt(10, 400, 900))
  assert.ok(height < 56 && height >= 40)
  assert.ok(overflowAt(10, 400, 900)(height) <= 0.5)
})

test('视口过矮时停在下限，不继续压缩', () => {
  assert.equal(solveRowHeight(56, 40, 10, overflowAt(10, 600, 800)), 40)
})

test('溢出为 0 不改动，行高与内容无关', () => {
  let calls = 0
  const height = solveRowHeight(50, 30, 10, () => { calls += 1; return 0 })
  assert.equal(height, 50)
  assert.equal(calls, 1)
})

test('迟滞：变化不足 1px 沿用上次行高，否则采用新值', () => {
  assert.equal(settleHeight(40, 40.4), 40)
  assert.equal(settleHeight(40, 41.5), 41.5)
})

test('迟滞：上次行高缺失（NaN）时直接采用新值', () => {
  assert.equal(settleHeight(NaN, 44), 44)
})

test('放大：剩余空间平摊到十行并按 64 分之一取整，不超过上限', () => {
  assert.equal(growHeight(40, 0, 10, 52), 40)
  assert.equal(growHeight(40, 120, 10, 52), 52)
  assert.ok(growHeight(40, 60, 10, 52) > 40 && growHeight(40, 60, 10, 52) < 52)
})

test('断路器：1 秒窗口内第 9 次适配触发跳闸，窗口外的记录会过期', () => {
  let burst = []
  for (let i = 0; i < 8; i++) {
    const r = tallyBurst(burst, 1000 + i * 10)
    assert.equal(r.tripped, false)
    burst = r.burst
  }
  assert.equal(tallyBurst(burst, 1100).tripped, true)
  // 1 秒后旧记录全部过期，计数重新开始
  assert.equal(tallyBurst(burst, 2100).tripped, false)
  assert.equal(tallyBurst(burst, 2100).burst.length, 1)
})
