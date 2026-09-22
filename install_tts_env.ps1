# install_tts_env.ps1
# Install the complete application and TTS stack into the shared .venv.
# The default target is Python 3.14. To use the known fallback target:
#   powershell -ExecutionPolicy Bypass -File .\install_tts_env.ps1 -PythonVersion 3.10 -Recreate
#
# The worker remains a child process, so torch/CUDA/model memory is still kept
# out of the FastAPI process even though both processes use the same .venv.

param(
    [ValidateSet("3.14", "3.10")]
    [string]$PythonVersion = "3.14",
    [switch]$Recreate
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

Write-Output ""
Write-Output "===== [1/5] ensure uv ====="
py -m uv --version
if ($LASTEXITCODE -ne 0) {
    py -m pip install --quiet --upgrade uv
    if ($LASTEXITCODE -ne 0) { throw "pip install uv failed (exit $LASTEXITCODE)" }
}
Write-Output ("uv: " + (py -m uv --version))

Write-Output ""
Write-Output "===== [2/5] ensure shared .venv (Python $PythonVersion) ====="
py -m uv python install $PythonVersion
if ($LASTEXITCODE -ne 0) { throw "uv python install $PythonVersion failed (exit $LASTEXITCODE)" }

if (Test-Path -LiteralPath $py) {
    $actualVersion = (& $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    if ($actualVersion -ne $PythonVersion) {
        if (-not $Recreate) {
            throw ".venv is Python $actualVersion, but Python $PythonVersion was requested. Re-run with -Recreate to replace it."
        }
        Write-Output ".venv is Python $actualVersion; recreating it for Python $PythonVersion..."
        Remove-Item -LiteralPath $venv -Recurse -Force
        & py -m uv venv $venv --python $PythonVersion
        if ($LASTEXITCODE -ne 0) { throw "uv venv failed (exit $LASTEXITCODE)" }
    }
} else {
    & py -m uv venv $venv --python $PythonVersion
    if ($LASTEXITCODE -ne 0) { throw "uv venv failed (exit $LASTEXITCODE)" }
}

Write-Output ("venv python: " + (& $py --version))

Write-Output ""
Write-Output "===== [3/5] install application and TTS dependencies ====="
& py -m uv pip install -p $py -r (Join-Path $root "backend\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "application/TTS dependency install failed (exit $LASTEXITCODE)" }

Write-Output ""
Write-Output "===== [4/5] install CUDA torch last ====="
# Installing qwen-tts first can bring in a CPU torch wheel on Windows. Replace
# it last with the CUDA build so the worker sees the intended GPU runtime.
& py -m uv pip install -p $py "torch==2.11.0" "torchaudio==2.11.0" --index-url "https://download.pytorch.org/whl/cu128" --force-reinstall --no-deps
if ($LASTEXITCODE -ne 0) { throw "CUDA torch install failed (exit $LASTEXITCODE)" }

Write-Output ""
Write-Output "===== [5/5] verify shared environment ====="
$gpu = @(& $py -c "import torch;print('python='+__import__('sys').version.split()[0]);print('torch='+torch.__version__);print('cuda_available='+str(torch.cuda.is_available()));print('device='+(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE'))")
$gpu | ForEach-Object { Write-Output $_ }
if (($gpu -join ' ') -notmatch 'cuda_available=True') {
    throw "GPU check FAILED. The shared .venv has no usable CUDA torch build."
}
& $py -c "import qwen_tts;from qwen_tts import Qwen3TTSModel;print('qwen_tts import OK')"
if ($LASTEXITCODE -ne 0) { throw "qwen_tts import check failed (exit $LASTEXITCODE)" }

Write-Output ""
Write-Output "===== done ====="
Write-Output "The backend and TTS worker now share .venv. The worker still runs as a child process."
