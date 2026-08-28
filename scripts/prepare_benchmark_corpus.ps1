param(
    [string]$Downloads = "D:\AP\AP7000\ai-workspace\ocr-benchmark\downloads",
    [string]$Output = "D:\AP\AP7000\ai-workspace\ocr-benchmark\corpus"
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "prepare_benchmark_corpus.py"
$uv = Get-Command uv.exe -ErrorAction Stop

& $uv.Source run --with duckdb --with pillow python $scriptPath --downloads $Downloads --output $Output
if ($LASTEXITCODE -ne 0) {
    throw "基准集准备失败，退出码：$LASTEXITCODE"
}
