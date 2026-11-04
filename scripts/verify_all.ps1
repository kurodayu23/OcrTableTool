#Requires -Version 5.1
<#
.SYNOPSIS
    OcrTableTool 一键门禁：把"流程型"缺陷挡在提交/交付之前。

.DESCRIPTION
    四类事故各有对应门禁（详见 docs\TESTING.md）：

      1. BOM        —— 改 .cpp/.h 丢了 UTF-8 BOM ⇒ MSVC2015 整树 C2001/C1057。
      2. 测试树身份 —— 测试里写死 D:\AP\AP7000\OcrTableTool，unittest discover
                       第一个模块加载即把产品树镜像塞进 sys.modules，
                       整套单测"测镜像"。
      3. 镜像一致性 —— 产品树镜像与仓库工作树靠人工同步，会静默漂移。
      4. 交付件可复现 —— selfcheck.bat / 交付说明.md 曾不在版本库。

    任一失败 ⇒ 退出码非 0，并只打印**首要错误行**（完整日志落盘）。
    默认跳过要跑几分钟的交付包校验与 C++ 编译，按需 -IncludeDelivery / -IncludeCpp。

.PARAMETER Json
    只输出一个机器可读 JSON 摘要（CI 用），人类可读进度不打印。

.EXAMPLE
    pwsh -File scripts\verify_all.ps1
    pwsh -File scripts\verify_all.ps1 -Json
    pwsh -File scripts\verify_all.ps1 -IncludeDelivery -DeliveryPackageDir D:\AP\AP7000\OcrTableTool\package\backend-delivery
    pwsh -File scripts\verify_all.ps1 -IncludeCpp -SkipCppBuild
#>
[CmdletBinding()]
param(
    [string]$RepoPath = "",
    [string]$ProductPath = "D:\AP\AP7000\OcrTableTool",
    [string]$PythonExe = "",
    [ValidateSet("worktree", "head")][string]$MirrorBaseline = "worktree",
    [switch]$IncludeDelivery,
    [string]$DeliveryPackageDir = "",
    [string]$DeliveryBackendSource = "",
    [switch]$IncludeCpp,
    [switch]$SkipCppBuild,
    [string]$BuildDirectory = "",
    [string]$QMake = "",
    [string]$VcVarsAll = "",
    [string]$OutDir = "",
    [string]$TmpDir = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$startedAt = Get-Date
$totalWatch = [System.Diagnostics.Stopwatch]::StartNew()

if (-not $RepoPath) {
    $RepoPath = Split-Path -Parent $PSScriptRoot
}
$RepoPath = [System.IO.Path]::GetFullPath($RepoPath)
$ProductPath = [System.IO.Path]::GetFullPath($ProductPath)

if (-not $PythonExe) {
    $candidates = @(
        (Join-Path $RepoPath ".venv\Scripts\python.exe"),
        (Join-Path $ProductPath ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $PythonExe = $candidate
            break
        }
    }
}
if (-not $PythonExe -or -not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "找不到 Python 解释器。用 -PythonExe 指定（约定：$ProductPath\.venv\Scripts\python.exe）。"
}
if (-not $OutDir) {
    $OutDir = Join-Path $ProductPath ("_work\gate-r1\run-" + $startedAt.ToString("yyyyMMdd-HHmmss"))
}
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
$logDir = Join-Path $OutDir "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
if (-not $TmpDir) {
    # ⚠ 临时目录**不能**放在仓库或产品树内部：后端单测里有"沿包路径向上找
    # backend\ocr_backend.py"的定位逻辑，测试用的临时包一旦建在产品树子树下，
    # 向上遍历就会命中真实的 backend 源树，把"找不到源码"的用例测成"找到了"。
    # 所以默认落到系统临时目录（一次运行一个子目录，跑完不自动删，便于取证）。
    $TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ocrtable-gate-" + $startedAt.ToString("yyyyMMdd-HHmmss"))
}
$TmpDir = [System.IO.Path]::GetFullPath($TmpDir)
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
if (-not $BuildDirectory) {
    $BuildDirectory = Join-Path $RepoPath "build\windows-msvc2015"
}
if (-not $DeliveryBackendSource) {
    $DeliveryBackendSource = Join-Path $RepoPath "backend"
}

$gateChecks = Join-Path $RepoPath "scripts\gate_checks.py"
if (-not (Test-Path -LiteralPath $gateChecks -PathType Leaf)) {
    throw "门禁检查脚本缺失：$gateChecks"
}

$script:Results = New-Object System.Collections.ArrayList

function Write-Head([string]$Text) {
    if (-not $Json) { Write-Host $Text }
}

# 错误行提取单独成文件（scripts\gate_error_line.ps1）：这样它能被单元测试**直接点调用**
#（verify_all.ps1 一执行就跑整套门禁，没法只测这一个函数）。
# 2026-11-02：旧实现退化成"取最后一行非空行"，把 delivery_notes 的 facts 末行
# （RUNTIME_MAX_MTIME，正好是对的）当成了首要错误行，真差异（DELTA_KB 997 vs 995）被吞掉。
. (Join-Path $PSScriptRoot "gate_error_line.ps1")

# 当前交付位解析也单独成文件（scripts\gate_delivery_package.ps1），理由同上：
# 它有独立的判定规则，必须能被单元测试直接点调用。
# 2026-11-02：旧版把交付位**写死**成 package\backend-delivery ⇒ 这一步验的是
# **客户拿不到、也不是当前位**的旧包 ⇒ 当前位即使缺文件也照样 PASS ⇒ 假绿。
. (Join-Path $PSScriptRoot "gate_delivery_package.ps1")

# "验不到"的判定策略（scripts\gate_step_policy.ps1）：
# 2026-11-02：原来找不到东西一律 SKIP ⇒「根本没验」和「验过了」在汇总里一样，
# 门禁照样整体 PASS —— 与项目自己的原则（「没查」必须等同于「不通过」）矛盾。
. (Join-Path $PSScriptRoot "gate_step_policy.ps1")

# 门禁"步骤清单"核对（scripts\gate_step_coverage.ps1）：
# 2026-11-02（F21）：旧版的 PlannedGates 是写死的数字（12），而且只是**显示用**的
# 计数器，没有任何东西核对"实际跑了哪些步" ⇒ 从执行列表里删掉一步，门禁照样整体 PASS。
# 现在把"必须跑的步骤"写成**独立清单**，跑完逐项核对（少跑/多跑/重名都判 FAIL）。
. (Join-Path $PSScriptRoot "gate_step_coverage.ps1")

# 交付位默认值：按磁盘实测解析**当前位**（exe mtime 最新者），不再写死包名。
if (-not $DeliveryPackageDir) {
    $resolvedPackage = Resolve-CurrentDeliveryPackage -PackageRoot (Join-Path $ProductPath "package")
    if ($resolvedPackage) {
        $DeliveryPackageDir = $resolvedPackage
        Write-Head ("[resolve] 当前交付位 = {0}" -f $DeliveryPackageDir)
    } else {
        Write-Head "[warn] product\package\ 下没有候选交付包，交付位相关检查将标记为 SKIP"
    }
}

function Add-Result {
    param(
        [string]$Name,
        [string]$Status,
        [double]$Seconds,
        [string]$Summary,
        [string]$FirstError,
        [string]$Log
    )
    $result = [pscustomobject]@{
        name        = $Name
        status      = $Status
        seconds     = [math]::Round($Seconds, 2)
        summary     = $Summary
        first_error = $FirstError
        log         = $Log
    }
    [void]$script:Results.Add($result)
    if (-not $Json) {
        $counter = "{0}/{1}" -f $script:Results.Count, $script:PlannedGates
        $line = "[{0}] {1,-16} {2,-4} {3,6:N2}s  {4}" -f $counter, $Name, $Status, $Seconds, $Summary
        Write-Host $line
        if ($FirstError) {
            Write-Host ("        > " + $FirstError) -ForegroundColor DarkYellow
        }
    }
}

function Invoke-GateProcess {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [hashtable]$Environment = @{},
        [string]$WorkDir = $RepoPath,
        [string]$JsonFile = ""
    )
    $log = Join-Path $logDir ("{0}.log" -f $Name)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $saved = @{}
    foreach ($key in $Environment.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, $Environment[$key])
    }
    $lines = @()
    $exitCode = 0
    $savedEap = $ErrorActionPreference
    # 原生命令往 stderr 写（unittest 的进度、nmake 的告警）在
    # $ErrorActionPreference = "Stop" 下会被当成终止错误（PS5.1 的
    # NativeCommandError）。门禁要的是退出码 + 日志，不是中断。
    $ErrorActionPreference = "Continue"
    Push-Location -LiteralPath $WorkDir
    try {
        $lines = @(& $FilePath @Arguments 2>&1 | ForEach-Object {
                if ($_ -is [System.Management.Automation.ErrorRecord]) {
                    $_.Exception.Message
                } else {
                    [string]$_
                }
            })
        $exitCode = $LASTEXITCODE
    } catch {
        $lines = @($lines + $_.Exception.Message)
        $exitCode = 1
    } finally {
        Pop-Location
        $ErrorActionPreference = $savedEap
        foreach ($key in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($key, $saved[$key])
        }
        $watch.Stop()
    }
    $lines | Set-Content -LiteralPath $log -Encoding UTF8

    $summary = ""
    $jsonOk = $null
    if ($JsonFile -and (Test-Path -LiteralPath $JsonFile -PathType Leaf)) {
        $payload = Get-Content -LiteralPath $JsonFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $jsonOk = [bool]$payload.ok
        $check = @($payload.checks)[0]
        $summary = [string]$check.summary
    }
    if (-not $summary) {
        $ran = @($lines | Where-Object { $_ -match '^Ran \d+ test' })
        if ($ran.Count -gt 0) {
            $summary = $ran[-1].Trim()
            $verdict = @($lines | Where-Object { $_ -match '^(OK|FAILED)\b' })
            if ($verdict.Count -gt 0) { $summary = $summary + " / " + $verdict[-1].Trim() }
        } else {
            # 门禁类脚本自己会打一行总计：优先取它，别拿最后一行明细凑数。
            $verdict = @($lines | Where-Object { $_ -match '^\[(PASS|FAIL)\]|^自检结果：|^结论：|^summary: ' })
            if ($verdict.Count -gt 0) {
                $summary = $verdict[0].Trim()
            } else {
                $nonEmpty = @($lines | Where-Object { $_.Trim() })
                if ($nonEmpty.Count -gt 0) { $summary = $nonEmpty[-1].Trim() } else { $summary = "无输出" }
            }
        }
    }

    $passed = ($exitCode -eq 0)
    if ($null -ne $jsonOk) { $passed = $passed -and $jsonOk }
    $status = "PASS"
    if (-not $passed) { $status = "FAIL" }
    $firstError = ""
    if (-not $passed) { $firstError = Get-PrimaryErrorLine -Lines $lines }
    if (-not $passed -and $jsonOk -eq $false) {
        # 静态门禁：明细在 JSON 里，比翻人类可读日志准，也只要第一条。
        foreach ($key in @(
                "missing_bom", "undecodable", "violations",
                "missing_in_product", "unexpected_in_product", "content_diff"
            )) {
            $values = @($check.$key)
            if ($values.Count -gt 0 -and $values[0]) {
                $entry = $values[0]
                if ($entry -is [string]) {
                    $firstError = $entry
                } elseif ($entry.path) {
                    $firstError = ("{0}:{1}: {2}" -f $entry.path, $entry.line, $entry.text)
                }
                break
            }
        }
    }
    Add-Result -Name $Name -Status $status -Seconds $watch.Elapsed.TotalSeconds `
        -Summary $summary -FirstError $firstError -Log $log
    return $status -eq "PASS"
}

function Add-Skip {
    param([string]$Name, [string]$Reason)
    Add-Result -Name $Name -Status "SKIP" -Seconds 0 -Summary $Reason -FirstError "" -Log ""
}

$gitBranch = ""
$gitHead = ""
try {
    $gitBranch = (& git -C $RepoPath rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
    $gitHead = (& git -C $RepoPath rev-parse --short HEAD 2>$null | Select-Object -First 1)
} catch {
    $gitBranch = "unknown"
    $gitHead = "unknown"
}

# 必须跑的步骤清单（**独立于执行代码维护** —— 执行代码少跑一步时清单不变，
# 核对才抓得到；反过来若清单由执行代码推导，就变成自己跟自己比，永远相等）。
# +1 = 核对步骤自身（gate-coverage），它在所有步骤跑完之后才产生。
$expectedGateSteps = @(Get-ExpectedGateSteps -IncludeDelivery:$IncludeDelivery -IncludeCpp:$IncludeCpp)
$script:PlannedGates = $expectedGateSteps.Count + 1

# 必跑的契约测试清单（步骤名 + 用例路径）
$contractCases = @(
    @{ name = "contract-ui"; path = "tests/test_ui_contract.py" }
    @{ name = "contract-sdk"; path = "tests/test_interface_sdk_contract.py" }
    @{ name = "hygiene-unit"; path = "tests/test_repo_hygiene.py" }
    @{ name = "gate-log-unit"; path = "tests/test_gate_error_line.py" }
    @{ name = "gate-delivery-unit"; path = "tests/test_gate_delivery_package.py" }
    @{ name = "gate-policy-unit"; path = "tests/test_gate_step_policy.py" }
    @{ name = "gate-identity-unit"; path = "tests/test_gate_identity_scope.py" }
    @{ name = "gate-vacuous-unit"; path = "tests/test_gate_vacuous_scan.py" }
    @{ name = "gate-coverage-unit"; path = "tests/test_gate_step_coverage.py" }
    @{ name = "hygiene-isolation-unit"; path = "tests/test_hygiene_fixture_isolation.py" }
    @{ name = "qt-integrity-unit"; path = "tests/test_qt_test_integrity.py" }
)

Write-Head "=== OcrTableTool 一键门禁 (verify_all) ==="
Write-Head ("repo     : {0}" -f $RepoPath)
Write-Head ("branch   : {0} @ {1}" -f $gitBranch, $gitHead)
Write-Head ("product  : {0}" -f $ProductPath)
Write-Head ("python   : {0}" -f $PythonExe)
Write-Head ("mirror   : baseline={0}" -f $MirrorBaseline)
Write-Head ("logs     : {0}" -f $logDir)
Write-Head ""

# --- 1/2/3 静态门禁（BOM / 测试树身份 / 镜像一致性）----------------------
foreach ($check in @("bom", "identity")) {
    $jsonFile = Join-Path $logDir ("{0}.json" -f $check)
    [void](Invoke-GateProcess -Name $check -FilePath $PythonExe `
            -Arguments @("-B", $gateChecks, $check, "--root", $RepoPath, "--product", $ProductPath, "--json-out", $jsonFile) `
            -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1" } `
            -JsonFile $jsonFile)
}

# --- 4 后端单测 -----------------------------------------------------------
[void](Invoke-GateProcess -Name "backend-unit" -FilePath $PythonExe `
        -Arguments @("-B", "-m", "unittest", "discover", "-s", "backend\tests", "-t", "backend\tests") `
        -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1"; TMPDIR = $TmpDir; TEMP = $TmpDir; TMP = $TmpDir })

# --- 5 Python 契约测试 ----------------------------------------------------
foreach ($case in $contractCases) {
    $testPath = Join-Path $RepoPath ($case.path -replace '/', '\')
    if (-not (Test-Path -LiteralPath $testPath -PathType Leaf)) {
        # 契约测试是**必跑项**：文件不在 ⇒ 这一项根本没跑 ⇒ FAIL，不是 SKIP。
        $reason = ("缺失: " + $case.path)
        $status = Get-MissingStepStatus -Name $case.name -Reason $reason -Requested $true
        Add-Result -Name $case.name -Status $status -Seconds 0 -Summary $reason -FirstError $reason -Log ""
        continue
    }
    [void](Invoke-GateProcess -Name $case.name -FilePath $PythonExe `
            -Arguments @("-B", "-m", "unittest", $case.path) `
            -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1"; TMPDIR = $TmpDir; TEMP = $TmpDir; TMP = $TmpDir })
}

# --- 6 镜像一致性 ---------------------------------------------------------
$mirrorJson = Join-Path $logDir "mirror.json"
[void](Invoke-GateProcess -Name "mirror" -FilePath $PythonExe `
        -Arguments @("-B", $gateChecks, "mirror", "--root", $RepoPath, "--product", $ProductPath, "--baseline", $MirrorBaseline, "--json-out", $mirrorJson) `
        -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1" } `
        -JsonFile $mirrorJson)

# --- 7 交付包校验（可选：几分钟，需要已打好的包）-------------------------
if ($IncludeDelivery) {
    $verifyScript = Join-Path $RepoPath "scripts\verify_delivery_package.py"
    $selfcheckScript = Join-Path $RepoPath "scripts\customer_selfcheck.py"
    $deliveryNotesScript = Join-Path $RepoPath "scripts\delivery_notes.py"
    $executable = Join-Path $DeliveryPackageDir "ocr-runtime\OcrBackend\OcrBackend.exe"
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        # 点了 -IncludeDelivery 就是"我要验交付位"：验不到 ≠ 通过 ⇒ FAIL。
        $reason = ("交付包不存在: " + $DeliveryPackageDir)
        $status = Get-MissingStepStatus -Reason $reason -Requested $true
        foreach ($name in @("delivery-notes", "delivery-verify", "delivery-selfcheck")) {
            Add-Result -Name $name -Status $status -Seconds 0 -Summary $reason -FirstError $reason -Log ""
        }
    } else {
        # 交付说明.md 的数字（文件数 / 体积 / 最新 mtime）必须由模板 + 实际目录重算，
        # 否则客户拿到的是上一版包的规模。秒级，随 -IncludeDelivery 一起跑。
        [void](Invoke-GateProcess -Name "delivery-notes" -FilePath $PythonExe `
                -Arguments @("-B", $deliveryNotesScript, "verify", "--package-dir", $DeliveryPackageDir) `
                -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1"; TMPDIR = $TmpDir; TEMP = $TmpDir; TMP = $TmpDir })
        [void](Invoke-GateProcess -Name "delivery-verify" -FilePath $PythonExe `
                -Arguments @("-B", $verifyScript, "--package-dir", (Join-Path $DeliveryPackageDir "ocr-runtime\OcrBackend"), "--backend-source", $DeliveryBackendSource) `
                -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1"; TMPDIR = $TmpDir; TEMP = $TmpDir; TMP = $TmpDir })
        [void](Invoke-GateProcess -Name "delivery-selfcheck" -FilePath $PythonExe `
                -Arguments @("-B", $selfcheckScript, "--package-dir", (Join-Path $DeliveryPackageDir "ocr-runtime\OcrBackend")) `
                -Environment @{ PYTHONIOENCODING = "utf-8"; PYTHONDONTWRITEBYTECODE = "1"; TMPDIR = $TmpDir; TEMP = $TmpDir; TMP = $TmpDir })
    }
}

# --- 8 C++ 编译 + 测试（可选）-------------------------------------------
if ($IncludeCpp) {
    if ($SkipCppBuild) {
        Add-Skip -Name "cpp-build" -Reason "-SkipCppBuild：复用已有构建目录"
    } else {
        # ⚠ 2026-11-02（C53）：这里必须走 **build_qt_all.ps1**，不能只走
        #   build_msvc2015.ps1 —— 后者只构建 `ocr-table-tool.pro` 声明的
        #   SUBDIRS = gui + tests ⇒ **GUI 测试套件**（tests/tst_gui.pro）与
        #   **SDK 测试套件**（interface-sdk/tests/sdk-tests.pro）根本不会被构建
        #   ⇒ 在干净构建目录上 `-IncludeCpp` 必然报 cpp-gui-test / cpp-sdk-test
        #   两项 FAIL（"未构建"），而产品本身没有任何问题。
        $buildScript = Join-Path $RepoPath "scripts\build_qt_all.ps1"
        $buildArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $buildScript, "-BuildDirectory", $BuildDirectory)
        if ($QMake) { $buildArgs += @("-QMake", $QMake) }
        if ($VcVarsAll) { $buildArgs += @("-VcVarsAll", $VcVarsAll) }
        [void](Invoke-GateProcess -Name "cpp-build" -FilePath "pwsh" -Arguments $buildArgs `
                -Environment @{ TMPDIR = $TmpDir; TEMP = $TmpDir; TMP = $TmpDir })
    }

    # 测试可执行文件由 qmake 落地：tests.pro 没有 DESTDIR（落 release\），
    # tst_gui.pro 的 DESTDIR 是 $$OUT_PWD/bin，SDK 测试是独立 .pro。
    # ⚠ 2026-11-02（C53）：路径必须与 build_qt_all.ps1 的产物目录一致 ——
    #   GUI 套件落在 <BuildDirectory>\gui-tests\bin\（旧模板写的 tstgui\bin\ 是错的，
    #   之前只是靠"全目录递归兜底搜索"侥幸找到）。
    $qtBin = ""
    if ($QMake) {
        $qtBin = Split-Path -Parent $QMake
    } elseif ($env:QTDIR) {
        $qtBin = Join-Path $env:QTDIR "bin"
    } else {
        $found = Get-ChildItem "C:\Qt\*\5.9.6\msvc2015_64\bin\qmake.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($found) { $qtBin = Split-Path -Parent $found.FullName }
    }
    $testPathParts = @()
    if ($qtBin) { $testPathParts += $qtBin }
    $testPathParts += (Join-Path $ProductPath ".venv\Scripts")
    if ($env:PATH) { $testPathParts += $env:PATH }
    $cppEnv = @{
        PATH             = ($testPathParts -join ";")
        QT_QPA_PLATFORM  = "offscreen"
        TMPDIR           = $TmpDir
        TEMP             = $TmpDir
        TMP              = $TmpDir
    }
    $testBinaries = @(
        @{ name = "cpp-core-test"; pattern = "tests\release\tst_ocrtable_core.exe" },
        @{ name = "cpp-gui-test"; pattern = "gui-tests\bin\tst_ocrtable_gui.exe" },
        @{ name = "cpp-sdk-test"; pattern = "sdk-tests\release\tst_ocrtableclient.exe" }
    )
    foreach ($binary in $testBinaries) {
        $candidate = Join-Path $BuildDirectory $binary.pattern
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $found = Get-ChildItem -LiteralPath $BuildDirectory -Recurse -File -Filter (Split-Path -Leaf $binary.pattern) -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($found) { $candidate = $found.FullName } else { $candidate = "" }
        }
        if (-not $candidate) {
            # 点了 -IncludeCpp 就是要跑 C++ 测试；只有用户**明确** -SkipCppBuild
            # （= 本来就打算复用旧构建目录）才允许 SKIP，否则"没构建出来"必须报 FAIL。
            $reason = ("未构建（先跑 -IncludeCpp 不带 -SkipCppBuild）: " + $binary.pattern)
            $status = Get-MissingStepStatus -Reason $reason -Requested $true -ExplicitlySkipped:$SkipCppBuild
            Add-Result -Name $binary.name -Status $status -Seconds 0 -Summary $reason -FirstError $reason -Log ""
            continue
        }
        [void](Invoke-GateProcess -Name $binary.name -FilePath $candidate -Arguments @() `
                -Environment $cppEnv -WorkDir (Split-Path -Parent $candidate))
    }
}

# --- 9 步骤清单核对（"少跑一步"必须暴露）---------------------------------
# 2026-11-02（F21）：旧版没有这一层 —— 从执行列表里删掉一步，门禁照样 PASS。
$coverage = Test-GateStepCoverage -Results $script:Results -Expected $expectedGateSteps
Add-Result -Name "gate-coverage" `
        -Status $(if ($coverage.ok) { "PASS" } else { "FAIL" }) `
        -Seconds 0 -Summary $coverage.message `
        -FirstError $(if ($coverage.ok) { "" } else { $coverage.message }) -Log ""

# --- 汇总 ---------------------------------------------------------------
$totalWatch.Stop()
$failed = @($script:Results | Where-Object { $_.status -eq "FAIL" })
$passed = @($script:Results | Where-Object { $_.status -eq "PASS" })
$skipped = @($script:Results | Where-Object { $_.status -eq "SKIP" })
$ok = $failed.Count -eq 0
$elapsed = [math]::Round($totalWatch.Elapsed.TotalSeconds, 1)

$summaryObject = [ordered]@{
    gate           = "verify_all"
    ok             = $ok
    repo           = $RepoPath
    product        = $ProductPath
    branch         = $gitBranch
    head           = $gitHead
    mirror_baseline = $MirrorBaseline
    started_at     = $startedAt.ToString("yyyy-MM-ddTHH:mm:ssK")
    finished_at    = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
    elapsed_seconds = $elapsed
    passed         = $passed.Count
    failed         = $failed.Count
    skipped        = $skipped.Count
    out_dir        = $OutDir
    gates          = @($script:Results)
}
$summaryPath = Join-Path $OutDir "summary.json"
$summaryObject | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding UTF8

if ($Json) {
    $summaryObject | ConvertTo-Json -Depth 6
} else {
    Write-Host ""
    Write-Host ("结果: {0}   通过 {1} / 失败 {2} / 跳过 {3}   总耗时 {4}s" -f `
        ($(if ($ok) { "PASS" } else { "FAIL" })), $passed.Count, $failed.Count, $skipped.Count, $elapsed)
    if (-not $ok) {
        Write-Host ""
        Write-Host "首要错误行："
        foreach ($result in $failed) {
            Write-Host ("  - {0}: {1}" -f $result.name, $result.first_error)
        }
    }
    Write-Host ("日志目录: {0}" -f $logDir)
    Write-Host ("摘要文件: {0}" -f $summaryPath)
}

if ($ok) { exit 0 } else { exit 1 }
