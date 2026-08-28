param(
    [string]$Downloads = "",
    [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $Downloads) {
    $Downloads = Join-Path $projectRoot "benchmark-data\downloads"
}
if (-not $Output) {
    $Output = Join-Path $projectRoot "benchmark-data\corpus"
}
$scriptPath = Join-Path $PSScriptRoot "prepare_benchmark_corpus.py"
$uv = Get-Command uv.exe -ErrorAction Stop

& $uv.Source run --with duckdb --with pillow python $scriptPath --downloads $Downloads --output $Output
if ($LASTEXITCODE -ne 0) {
    throw "基准集准备失败，退出码：$LASTEXITCODE"
}
