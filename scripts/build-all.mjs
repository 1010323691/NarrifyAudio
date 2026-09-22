import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const root = process.cwd()

function run(command, args, label) {
  console.log(`\n===== ${label} =====`)
  const result = spawnSync(command, args, {
    cwd: root,
    stdio: 'inherit',
    shell: false,
  })

  if (result.error) {
    console.error(`无法启动 ${label}: ${result.error.message}`)
    process.exit(1)
  }

  if (result.status !== 0) {
    process.exit(result.status ?? 1)
  }
}

const npmExecPath = process.env.npm_execpath
const npm = npmExecPath
  ? process.execPath
  : process.platform === 'win32'
    ? (process.env.ComSpec ?? 'cmd.exe')
    : 'npm'
const npmArgs = npmExecPath
  ? [npmExecPath, 'run', 'build']
  : process.platform === 'win32'
    ? ['/d', '/s', '/c', 'npm.cmd', 'run', 'build']
    : ['run', 'build']
const pythonCandidates = process.platform === 'win32'
  ? [join(root, '.venv', 'Scripts', 'python.exe')]
  : [join(root, '.venv', 'bin', 'python')]
const python = pythonCandidates.find((candidate) => existsSync(candidate))

if (!python) {
  console.error('未找到共享 Python 环境 .venv，请先运行 install_tts_env.ps1。')
  process.exit(1)
}

// npm run build 已包含 vue-tsc 类型检查和 Vite 生产构建。
run(npm, npmArgs, '前端 TypeScript + Vite 构建')

// compileall 只做 Python 语法/字节码检查，不导入 torch，也不会下载模型。
run(python, ['-m', 'compileall', '-q', 'backend', 'tts-engine'], '后端 Python 编译检查')

console.log('\n===== build:all complete =====')
