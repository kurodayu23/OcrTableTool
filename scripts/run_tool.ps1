param(
    [string]$Executable = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Executable) {
    $Executable = Join-Path $projectRoot "build\windows-msvc2015\src\gui\bin\OcrTableTool.exe"
}
if (-not $Python) {
    $Python = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $Executable)) {
    throw "Executable not found: $Executable"
}
if (Test-Path $Python) {
    $env:OCR_TABLE_PYTHON = (Resolve-Path $Python).Path
}
$env:OCR_TABLE_BACKEND = (Resolve-Path (Join-Path $projectRoot "backend\ocr_backend.py")).Path
Start-Process -FilePath $Executable -WorkingDirectory (Split-Path -Parent $Executable)
