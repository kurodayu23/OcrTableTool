# 生成 OcrTableTool 完整交付包（结构对齐 OcrTableTool-V4.zip）
#
# 包结构：
#   OcrTableTool.exe + Qt 运行时 DLL + 插件目录 + ocr-runtime/OcrBackend/
#   + interface-sdk/{qt,example} + CAMERA_OCR_INTERFACE.md + LICENSE
#   + THIRD_PARTY_NOTICES.md + third-party-licenses/ + SHA256SUMS.txt
#
# 用法: pwsh -File package_client_full.ps1 -OutputDirectory <目标目录> [-Zip]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path (Join-Path $repoRoot "backend\ocr_backend.py"))) {
    $repoRoot = "D:\AP\AP7000\OcrTableTool"
}
$guiBin = "D:\AP\AP7000\AP7000-Build-Directories\OcrTableTool\build-msvc2015\src\gui\bin"
$backendPkg = Join-Path $repoRoot "package\backend-delivery-20261110f\ocr-runtime\OcrBackend"

if (-not (Test-Path $guiBin)) { throw "GUI bin 不存在：$guiBin" }
if (-not (Test-Path $backendPkg)) { throw "后端包不存在：$backendPkg" }

if (Test-Path $OutputDirectory) { Remove-Item $OutputDirectory -Recurse -Force }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

function Copy-Relative([string]$source, [string]$relative) {
    $target = Join-Path $OutputDirectory $relative
    $parent = Split-Path -Parent $target
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    Copy-Item $source $target -Force
}

# 1) GUI 主体
Copy-Relative (Join-Path $guiBin "OcrTableTool.exe") "OcrTableTool.exe"
Write-Host "OcrTableTool.exe"

# 2) Qt 运行时 DLL（顶层）
foreach ($dll in Get-ChildItem $guiBin -Filter "*.dll" -File) {
    Copy-Relative $dll.FullName $dll.Name
}
Write-Host "Qt DLLs: $((Get-ChildItem $guiBin -Filter '*.dll' -File).Count)"

# 3) Qt 插件目录（V4 里有这些；backend/ 是脚本回退，不进客户包）
$pluginDirs = @("audio", "bearer", "iconengines", "imageformats", "mediaservice",
                "platforms", "playlistformats", "translations")
foreach ($dir in $pluginDirs) {
    $src = Join-Path $guiBin $dir
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $OutputDirectory $dir) -Recurse -Force
    }
}
Write-Host "Qt plugins: $($pluginDirs.Count) dirs"

# 4) vcredist
$vcredist = Join-Path $guiBin "vcredist_x64.exe"
if (Test-Path $vcredist) { Copy-Relative $vcredist "vcredist_x64.exe" }

# 5) 后端（最新，含全部修复）
Copy-Item $backendPkg (Join-Path $OutputDirectory "ocr-runtime\OcrBackend") -Recurse -Force
Write-Host "OcrBackend.exe: $((Get-Item (Join-Path $OutputDirectory 'ocr-runtime\OcrBackend\OcrBackend.exe')).Length) B"

# 6) 接口 SDK（qt + example）
$sdkQt = Join-Path $repoRoot "interface-sdk\qt"
$sdkExample = Join-Path $repoRoot "interface-sdk\example"
if (Test-Path $sdkQt) { Copy-Item $sdkQt (Join-Path $OutputDirectory "interface-sdk\qt") -Recurse -Force }
if (Test-Path $sdkExample) { Copy-Item $sdkExample (Join-Path $OutputDirectory "interface-sdk\example") -Recurse -Force }
Write-Host "interface-sdk copied"

# 7) 接口文档
$camDoc = Join-Path $repoRoot "docs\CAMERA_OCR_INTERFACE.md"
if (Test-Path $camDoc) { Copy-Relative $camDoc "CAMERA_OCR_INTERFACE.md" }
$sdkReadme = Join-Path $repoRoot "interface-sdk\README.md"
if (Test-Path $sdkReadme) { Copy-Relative $sdkReadme "interface-sdk\README.md" }

# 8) LICENSE + THIRD_PARTY_NOTICES + third-party-licenses
$licenseTarget = Join-Path $OutputDirectory "LICENSE"
if (-not (Test-Path $licenseTarget)) {
    @"
OcrTableTool 版权声明

本软件为 AP7000 项目的离线表格 OCR 工具。
本软件本身的使用条款由授权方另行约定。

本软件使用若干开源组件，其版权与许可证归各自项目所有，
详见 THIRD_PARTY_NOTICES.md 与 third-party-licenses/ 目录。
"@ | Out-File -FilePath $licenseTarget -Encoding UTF8
}

@"
# 第三方组件

本项目使用以下开源组件，版权和许可证归各自项目所有：

- Qt 5.9.6：LGPL-3.0；
- RapidOCR、RapidTable、PaddleOCR 模型、OpenVINO、OpenCV：Apache-2.0；
- ONNX Runtime、openpyxl：MIT；
- NumPy：BSD-3-Clause。
"@ | Out-File -FilePath (Join-Path $OutputDirectory "THIRD_PARTY_NOTICES.md") -Encoding UTF8

$licensesSrc = Join-Path $repoRoot "package\backend-delivery-20261110f\licenses"
if (Test-Path $licensesSrc) {
    Copy-Item $licensesSrc (Join-Path $OutputDirectory "third-party-licenses") -Recurse -Force
    Write-Host "third-party-licenses: $((Get-ChildItem (Join-Path $OutputDirectory 'third-party-licenses') -File).Count) 份"
}

# 9) SHA256SUMS.txt（相对路径 + 小写 hex，对齐 V4 格式）
$lines = New-Object System.Collections.Generic.List[string]
Get-ChildItem $OutputDirectory -Recurse -File | Where-Object { $_.Name -ne "SHA256SUMS.txt" } | ForEach-Object {
    $relative = $_.FullName.Substring($OutputDirectory.Length).TrimStart('\').Replace('\', '/')
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
    $lines.Add("$hash  $relative")
}
$lines | Sort-Object | Out-File -FilePath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding UTF8
Write-Host "SHA256SUMS.txt: $($lines.Count) entries"

# 10) 可选 zip
if ($Zip) {
    $zipPath = "$OutputDirectory.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $OutputDirectory, $zipPath, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    Write-Host "ZIP: $zipPath ($([math]::Round((Get-Item $zipPath).Length / 1MB, 1)) MB)"
}

$totalMb = [math]::Round(((Get-ChildItem $OutputDirectory -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
$totalFiles = (Get-ChildItem $OutputDirectory -Recurse -File | Measure-Object).Count
Write-Host "TOTAL: $totalFiles files, $totalMb MB"
