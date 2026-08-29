param(
    [string]$QMake = "",
    [string]$VcVarsAll = "",
    [string]$WindowsSdkVersion = "10.0.19041.0",
    [string]$BuildDirectory = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $BuildDirectory) {
    $BuildDirectory = Join-Path $projectRoot "build\windows-msvc2015"
}

if (-not $QMake) {
    $command = Get-Command qmake -ErrorAction SilentlyContinue
    if ($command) {
        $QMake = $command.Source
    } elseif ($env:QTDIR -and (Test-Path (Join-Path $env:QTDIR "bin\qmake.exe"))) {
        $QMake = Join-Path $env:QTDIR "bin\qmake.exe"
    } else {
        $candidate = Get-ChildItem "C:\Qt\*\5.9.6\msvc2015_64\bin\qmake.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
        if ($candidate) {
            $QMake = $candidate
        }
    }
}
if (-not $QMake -or -not (Test-Path $QMake)) {
    throw "Qt 5.9.6 msvc2015_64 qmake was not found. Pass -QMake or configure QTDIR/PATH."
}

if (-not $VcVarsAll) {
    if ($env:VS140COMNTOOLS) {
        $VcVarsAll = [IO.Path]::GetFullPath((Join-Path $env:VS140COMNTOOLS "..\..\VC\vcvarsall.bat"))
    } else {
        $VcVarsAll = "C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\vcvarsall.bat"
    }
}
if (-not (Test-Path $VcVarsAll)) {
    throw "MSVC 2015 vcvarsall.bat was not found. Pass -VcVarsAll."
}

New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null
# Python 后端不参与 C++ 链接；增量构建时 qmake 的 POST_LINK 可能不会再次执行。
# 只同步正式运行文件，避免把缓存或构建目录递归复制到后端目录。
$backendDirectory = Join-Path $BuildDirectory "src\gui\bin\backend"
New-Item -ItemType Directory -Path $backendDirectory -Force | Out-Null
$backendFiles = @(
    "ocr_backend.py",
    "table_pipeline.py",
    "recognition_scheduler.py",
    "requirements.txt",
    "warmup.py"
)
foreach ($backendFile in $backendFiles) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "backend\$backendFile") `
        -Destination (Join-Path $backendDirectory $backendFile) -Force
}
$sourceBackendHash = (Get-FileHash -Algorithm SHA256 `
    -LiteralPath (Join-Path $projectRoot "backend\ocr_backend.py")).Hash
$deployedBackendHash = (Get-FileHash -Algorithm SHA256 `
    -LiteralPath (Join-Path $backendDirectory "ocr_backend.py")).Hash
if ($sourceBackendHash -ne $deployedBackendHash) {
    throw "Deployed OCR backend does not match current source."
}

$qtPrefix = Split-Path -Parent (Split-Path -Parent $QMake)
$translationSource = Join-Path $qtPrefix "translations\qt_zh_CN.qm"
if (-not (Test-Path $translationSource)) {
    throw "Qt Simplified Chinese translation was not found: $translationSource"
}
$translationDirectory = Join-Path $BuildDirectory "src\gui\bin\translations"
New-Item -ItemType Directory -Path $translationDirectory -Force | Out-Null
Copy-Item -LiteralPath $translationSource `
    -Destination (Join-Path $translationDirectory "qt_zh_CN.qm") -Force

$deployTool = Join-Path (Split-Path -Parent $QMake) "windeployqt.exe"
if (-not (Test-Path $deployTool)) {
    throw "windeployqt was not found."
}
$projectFile = Join-Path $projectRoot "ocr-table-tool.pro"
$executable = Join-Path $BuildDirectory "src\gui\bin\OcrTableTool.exe"
$commandLine = 'call "{0}" amd64 {1} && "{2}" "{3}" -spec win32-msvc && nmake /NOLOGO && "{4}" --no-translations --compiler-runtime "{5}"' -f `
    $VcVarsAll, $WindowsSdkVersion, $QMake, $projectFile, $deployTool, $executable
Push-Location $BuildDirectory
try {
    & cmd.exe /d /c $commandLine
    if ($LASTEXITCODE -ne 0) {
        throw "qmake/nmake/windeployqt failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "Build ready: $executable"
