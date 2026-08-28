param(
    [string]$Python = "3.11",
    [string]$EnvironmentPath = "",
    [switch]$SkipModelWarmup
)

$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [string]$FilePath,
        [object[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $EnvironmentPath) {
    $EnvironmentPath = Join-Path $projectRoot ".venv"
}
$requirements = Join-Path $projectRoot "backend\requirements.txt"
$modelDirectory = Join-Path $projectRoot "runtime\models"
$uv = Get-Command uv -ErrorAction SilentlyContinue
$pythonExe = Join-Path $EnvironmentPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonExe)) {
    if ($uv) {
        Invoke-Checked $uv.Source @("venv", $EnvironmentPath, "--python", $Python)
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if (-not $launcher) {
            throw "Python launcher or uv was not found. Install Python 3.11 or pass a usable tool through PATH."
        }
        Invoke-Checked $launcher.Source @("-$Python", "-m", "venv", $EnvironmentPath)
    }
}

if ($uv) {
    Invoke-Checked $uv.Source @("pip", "install", "--python", $pythonExe, "--requirement", $requirements)
} else {
    Invoke-Checked $pythonExe @("-m", "pip", "install", "--requirement", $requirements)
}

if (-not $SkipModelWarmup) {
    Invoke-Checked $pythonExe @((Join-Path $projectRoot "backend\warmup.py"), "--model-dir", $modelDirectory)
}

Write-Host "Backend ready: $pythonExe"
Write-Host "Model directory: $modelDirectory"
Write-Host "Models are local after warmup; recognition does not upload images."
