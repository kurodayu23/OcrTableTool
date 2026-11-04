#!/usr/bin/env pwsh
# Build every C++ suite the delivery gate runs: main app + core tests + GUI tests + SDK tests.
#
# ⚠ 2026-11-02 (C53): why this file exists.
#   `scripts/verify_all.ps1 -IncludeCpp` used to build only via
#   `scripts/build_msvc2015.ps1`, and that script builds exactly what
#   `ocr-table-tool.pro` declares: SUBDIRS = gui + tests.
#   => the **GUI test suite** (`tests/tst_gui.pro`) and the **SDK test suite**
#      (`interface-sdk/tests/sdk-tests.pro`) were **never built** by the gate.
#   => on a clean build directory `-IncludeCpp` always reported
#      `cpp-gui-test` / `cpp-sdk-test` as FAIL ("not built"), even though the
#      product was fine. The gate was broken, not the code.
#   The suites are built by `ocr-tests/build-qt-tests.ps1` on the AI side, but
#   that file is not part of the repository, so CI could not reuse it.
#
#   This script is the single shared build entry point: it lives in the product
#   tree so both the gate and the AI-side self check can call it.
#
# ⚠ PowerShell 5.1 reads this file as ANSI (no BOM), so **every string that can
#   appear in output or in a judgement must be ASCII**. Chinese belongs in
#   comments only.
#
# Usage:
#   pwsh -NoProfile -File scripts\build_qt_all.ps1 -BuildDirectory <dir>

[CmdletBinding()]
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

function Invoke-SuiteBuild([string]$proFile, [string]$workDir) {
    Write-Host ("--- qmake+nmake : " + $proFile + "  ->  " + $workDir)
    if (-not (Test-Path $proFile)) {
        throw ("project file was not found: " + $proFile)
    }
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null
    $commandLine = 'call "{0}" amd64 {1} && "{2}" "{3}" -spec win32-msvc && nmake /NOLOGO' -f `
        $VcVarsAll, $WindowsSdkVersion, $QMake, $proFile
    Push-Location $workDir
    try {
        & cmd.exe /d /c $commandLine
        if ($LASTEXITCODE -ne 0) {
            throw ("qmake/nmake failed with exit code " + $LASTEXITCODE + ": " + $proFile)
        }
    } finally {
        Pop-Location
    }
}

# 1/3 main app + core tests (ocr-table-tool.pro => gui + tests)
Write-Host "=== 1/3 main app + core tests ==="
& pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_msvc2015.ps1") `
    -BuildDirectory $BuildDirectory -QMake $QMake -VcVarsAll $VcVarsAll -WindowsSdkVersion $WindowsSdkVersion
if ($LASTEXITCODE -ne 0) {
    throw ("build_msvc2015.ps1 failed with exit code " + $LASTEXITCODE)
}

# 2/3 GUI test suite (tests/tst_gui.pro)
Write-Host "=== 2/3 gui tests ==="
Invoke-SuiteBuild (Join-Path $projectRoot "tests\tst_gui.pro") (Join-Path $BuildDirectory "gui-tests")

# 3/3 SDK test suite (interface-sdk/tests/sdk-tests.pro)
Write-Host "=== 3/3 sdk tests ==="
Invoke-SuiteBuild (Join-Path $projectRoot "interface-sdk\tests\sdk-tests.pro") (Join-Path $BuildDirectory "sdk-tests")

Write-Host ("BUILD_DIR=" + $BuildDirectory)
exit 0
