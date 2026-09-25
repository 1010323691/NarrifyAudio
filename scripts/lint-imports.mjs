import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const root = process.cwd()

const candidates = process.platform === 'win32'
  ? [join(root, '.venv', 'Scripts', 'lint-imports.exe')]
  : [join(root, '.venv', 'bin', 'lint-imports')]
const bin = candidates.find((candidate) => existsSync(candidate))

if (!bin) {
  console.error('.venv 中未找到 lint-imports（import-linter），请先运行 install_tts_env.ps1 安装依赖。')
  process.exit(1)
}

const result = spawnSync(bin, [], { cwd: root, stdio: 'inherit', shell: false })
process.exit(result.status ?? 1)
