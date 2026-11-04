# 门禁"步骤清单"核对（2026-11-02 新增，F21）
#
# 背景（真实假绿）：
#   `scripts/verify_all.ps1` 里原本只有一个写死数字的 PlannedGates（12），
#   它只是**显示用**的计数器（"[3/12]"），**没有任何东西**去核对
#   「实际跑了多少步 / 跑了哪些步 / 有没有重名」。于是：
#     * 从执行列表里删掉一步  → 少跑一项，汇总照样 PASS；
#     * 加了一步忘了改计数器   → 计数变成 13/12，仍然 PASS；
#     * 步骤名改了              → 仍然 PASS；
#     * 同一个名字用了两次      → 仍然 PASS。
#   一句话：**门禁自己的"检查清单"没有任何强制力**。
#   这与 F16（"验不到"只标 SKIP）、F20（扫了 0 个文件也 PASS）同源 ——
#   「"没查"必须等同于"不通过"」被漏掉了最后这一环。
#
# 修法：把"必须跑的步骤"写成一份**独立清单**（本文件 = manifest），
# 与"怎么跑这些步骤"的代码分开维护；跑完逐项核对，对不上就判 FAIL：
#     少跑一步 => missing    => FAIL
#     多跑一步 => unexpected => FAIL（新步骤必须先登记）
#     步骤重名 => duplicated => FAIL
#
# 为何清单必须**独立维护**、不能由执行代码推导出来：
#   若清单 = 执行代码推导的结果，执行代码少跑一步时清单也跟着少，
#   核对就变成自己跟自己比 => 永远相等 => 缺陷照样漏网。
#   所以清单是"合同"，执行代码是"实现"，两者对不上就是错。
#
# ⚠ 5.1 兼容：本文件按 UTF-8 无 BOM 落盘，PowerShell 5.1 会按 ANSI 读它，
#   注释里的中文会变乱码（C22 已登记）。因此**所有会出现在输出/判定里的
#   字符串一律用 ASCII**，中文只放在注释里 —— 判定与输出都不受乱码影响。
#
# 单独成文件是为了能被单元测试**直接点调用**
# （verify_all.ps1 一执行就跑整套门禁，没法只测这一个函数）。

function Get-ExpectedGateSteps {
    [CmdletBinding()]
    param(
        [switch]$IncludeDelivery,
        [switch]$IncludeCpp,
        [switch]$All
    )

    $steps = @(
        "bom",
        "identity",
        "backend-unit",
        "contract-ui",
        "contract-sdk",
        "hygiene-unit",
        "gate-log-unit",
        "gate-delivery-unit",
        "gate-policy-unit",
        "gate-identity-unit",
        "gate-vacuous-unit",
        "gate-coverage-unit",
        "hygiene-isolation-unit",
        "qt-integrity-unit",
        "mirror"
    )
    if ($IncludeDelivery -or $All) {
        $steps += @("delivery-notes", "delivery-verify", "delivery-selfcheck")
    }
    if ($IncludeCpp -or $All) {
        # cpp-build 即使在 -SkipCppBuild 下也会被记成一条 SKIP 结果，
        # 所以这里恒为 4 步。（旧版 PlannedGates 在 -SkipCppBuild 时只算 3 步，
        # 计数器本来就少算 1 —— 显示上就已经对不上了。）
        $steps += @("cpp-build", "cpp-core-test", "cpp-gui-test", "cpp-sdk-test")
    }
    return $steps
}

function Test-GateStepCoverage {
    [CmdletBinding()]
    param(
        [object[]]$Results = @(),
        [string[]]$Expected = @()
    )

    $actual = @()
    foreach ($item in $Results) {
        if ($null -eq $item) { continue }
        $name = [string]$item.name
        if ($name) { $actual += $name }
    }

    $missing = @()
    foreach ($name in $Expected) {
        if ($actual -notcontains $name) { $missing += $name }
    }
    $unexpected = @()
    foreach ($name in $actual) {
        if ($Expected -notcontains $name) { $unexpected += $name }
    }
    $duplicated = @()
    foreach ($name in @($actual | Select-Object -Unique)) {
        $count = @($actual | Where-Object { $_ -eq $name }).Count
        if ($count -gt 1) { $duplicated += $name }
    }

    $ok = ($missing.Count -eq 0) -and ($unexpected.Count -eq 0) -and ($duplicated.Count -eq 0)

    $parts = @()
    if ($missing.Count -gt 0) { $parts += ("missing: " + ($missing -join ", ")) }
    if ($unexpected.Count -gt 0) { $parts += ("unexpected: " + ($unexpected -join ", ")) }
    if ($duplicated.Count -gt 0) { $parts += ("duplicated: " + ($duplicated -join ", ")) }
    if ($parts.Count -eq 0) {
        $message = ("step coverage ok: {0}/{1}" -f $actual.Count, $Expected.Count)
    } else {
        $message = ("step coverage mismatch (actual {0} / expected {1}): {2}" -f $actual.Count, $Expected.Count, ($parts -join "; "))
    }

    return [pscustomobject]@{
        ok             = $ok
        missing        = @($missing)
        unexpected     = @($unexpected)
        duplicated     = @($duplicated)
        actual_count   = $actual.Count
        expected_count = $Expected.Count
        message        = $message
    }
}
