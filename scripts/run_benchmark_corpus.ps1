param(
    [string]$Python = "",
    [string]$Corpus = "D:\AP\AP7000\ai-workspace\ocr-benchmark\corpus",
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [string]$Ids = "",
    [string]$Source = "",
    [ValidateSet("", "development", "holdout")]
    [string]$Split = "",
    [int]$Limit = 0,
    [switch]$SkipExport
)

$ErrorActionPreference = "Stop"
if (-not $Python) {
    if ($env:OCR_TABLE_PYTHON) {
        $Python = $env:OCR_TABLE_PYTHON
    } else {
        $Python = Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe"
    }
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到 OCR Python 环境：$Python"
}

$arguments = @(
    (Join-Path $PSScriptRoot "run_benchmark_corpus.py"),
    "--corpus", $Corpus,
    "--output", $Output,
    "--model-dir", (Join-Path (Split-Path $PSScriptRoot -Parent) "runtime\models")
)
if ($Ids) { $arguments += @("--ids", $Ids) }
if ($Source) { $arguments += @("--source", $Source) }
if ($Split) { $arguments += @("--split", $Split) }
if ($Limit -gt 0) { $arguments += @("--limit", $Limit) }
if ($SkipExport) { $arguments += "--skip-export" }

& $Python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "OCR 基准回归失败，退出码：$LASTEXITCODE"
}
