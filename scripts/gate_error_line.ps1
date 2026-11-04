# OcrTableTool 一键门禁的"首要错误行"提取。
#
# 单独成文件（而不是塞在 verify_all.ps1 里）是为了能**被单元测试直接点调用**：
# verify_all.ps1 一执行就跑整套门禁，没法只测这一个函数。
# 被 scripts\verify_all.ps1 以点源方式引入：. (Join-Path $PSScriptRoot "gate_error_line.ps1")
#
# 背景（2026-11-02 实测出的真缺陷）：
#   旧实现在命中不到已知模式时退化成"取最后一行非空行"。而 delivery_notes.py verify
#   的输出最后一行是 facts 清单的末行（RUNTIME_MAX_MTIME  2026-10-30 05:52:14），
#   于是门禁把"首要错误行"报成了 RUNTIME_MAX_MTIME —— 而那个值其实**完全正确**，
#   真实首处差异是 DELTA_KB（模板算出 997 KB，交付位写 995 KB），被整个吞掉了。
#   "报错指错地方"比不报错更糟：会把维护者引到本来没问题的地方去查。
#
# 因此这里做三件事：
#   1. 认识脚本自报的 "[FAIL] ..." / "[ERROR] ..." 首行（自报失败的一手行）；
#   2. 命中后**带上下文**返回（首行 + 后续 2 行），差异对照通常就在紧邻行；
#   3. 兜底不再盲目取末行：先找"失败/不一致/过期"类行，都没有才回到末行。

# 取从 $Index 起最多 $Extra 行的非空文本，拼成一行返回（截断超长行）。
function Get-ErrorContext {
    param(
        [string[]]$Lines,
        [int]$Index,
        [int]$Extra = 2
    )
    if (-not $Lines -or $Lines.Count -eq 0) { return "" }
    $parts = @()
    $last = [Math]::Min($Lines.Count - 1, $Index + $Extra)
    for ($i = $Index; $i -le $last; $i++) {
        $text = [string]$Lines[$i]
        $text = $text.Trim()
        if (-not $text) { continue }
        if ($text.Length -gt 300) { $text = $text.Substring(0, 300) + "…" }
        $parts += $text
    }
    return ($parts -join " | ")
}

# 从一段门禁输出里挑出"最能说明失败原因"的一行。
function Get-PrimaryErrorLine {
    param([string[]]$Lines)
    if (-not $Lines -or $Lines.Count -eq 0) { return "" }

    # 一手失败标记：unittest 的 FAIL:/ERROR:、脚本自报的 [FAIL]/[ERROR]、编译错误等。
    $strong = @(
        '^\s*\[FAIL\]',
        '^\s*\[ERROR\]',
        '^\s*(FAIL|ERROR):\s',
        '^\s*FAILED\s',
        '^\s*ERROR\s',
        'AssertionError',
        '\berror C\d{4}\b',
        '\bfatal error\b',
        '^\s*Traceback \(most recent call last\)',
        '\bModuleNotFoundError\b',
        '\bImportError\b'
    )
    # 静态门禁（gate_checks.py）的明细行：只打印第一条，别把几十条清单刷屏。
    $detail = @(
        '缺 BOM:',
        '非 UTF-8:',
        '写死本项目路径:',
        '镜像缺文件:',
        '镜像多文件:',
        '内容不同:'
    )
    # 兜底用的软信号：中文失败措辞（英文模式命中不到时用）。
    $soft = @(
        '失败',
        '不一致',
        '过期',
        '不通过',
        '缺失',
        '异常'
    )

    for ($p = 0; $p -lt $strong.Count; $p++) {
        for ($i = 0; $i -lt $Lines.Count; $i++) {
            if ($Lines[$i] -match $strong[$p] -and ([string]$Lines[$i]).Trim()) {
                return Get-ErrorContext -Lines $Lines -Index $i
            }
        }
    }
    for ($p = 0; $p -lt $detail.Count; $p++) {
        for ($i = 0; $i -lt $Lines.Count; $i++) {
            if (([string]$Lines[$i]) -like ('*' + $detail[$p] + '*')) {
                return Get-ErrorContext -Lines $Lines -Index $i
            }
        }
    }
    for ($p = 0; $p -lt $soft.Count; $p++) {
        for ($i = 0; $i -lt $Lines.Count; $i++) {
            if (([string]$Lines[$i]) -like ('*' + $soft[$p] + '*')) {
                return Get-ErrorContext -Lines $Lines -Index $i
            }
        }
    }
    $tail = @($Lines | Where-Object { ([string]$_).Trim() })
    if ($tail.Count -gt 0) { return ([string]$tail[-1]).Trim() }
    return ""
}
