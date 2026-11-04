# 门禁"验不到"时的判定策略（2026-11-02 新增）
#
# 背景（真实假绿）：
#   `scripts/verify_all.ps1` 原来在"要找的东西找不到"时一律 `Add-Skip`：
#   * 契约测试文件缺失 → SKIP；
#   * 点了 `-IncludeDelivery` 但交付包/ exe 不存在 → SKIP ×3；
#   * 点了 `-IncludeCpp` 但测试可执行文件没构建出来 → SKIP ×3。
#   于是**"根本没验"和"验过了"在汇总里长得一模一样**，门禁照样整体 PASS。
#   这与项目自己写下的原则直接矛盾（见 `scripts/verify_delivery_package.py` 的
#   2026-10-22 改动说明：「判定语义不变：仍然找不到源码时依旧 FAIL ——
#   "没查"必须等同于"不通过"」）。
#
# 规则：
#   * 用户**点名**要的检查项（-IncludeDelivery / -IncludeCpp / 必跑的契约测试）
#     找不到 ⇒ **FAIL**（不能因为"没查"就算通过）；
#   * 用户**明确要求跳过**（-SkipCppBuild）⇒ SKIP（这是用户的本意）；
#   * 没有点名、也不该存在该项 ⇒ SKIP。
#
# 单独成文件是为了能被单元测试**直接点调用**
# （verify_all.ps1 一执行就跑整套门禁，没法只测这一个函数）。

function Get-MissingStepStatus {
    [CmdletBinding()]
    param(
        [string]$Name = "",
        [string]$Reason = "",
        [bool]$Requested = $true,
        [switch]$ExplicitlySkipped
    )

    if ($ExplicitlySkipped) {
        return "SKIP"
    }
    if ($Requested) {
        return "FAIL"
    }
    return "SKIP"
}
