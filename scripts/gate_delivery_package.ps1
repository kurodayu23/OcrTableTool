# 当前交付位解析（门禁用，2026-11-02 新增）
#
# 背景（真实假绿）：
#   `scripts/verify_all.ps1` 原来把交付位**写死**成 `package\backend-delivery`。
#   产品树里同时存在多个候选包（backend-delivery / -20261027 / -20261030），
#   而**当前交付位是最新构建的那个**。写死旧包 ⇒ 这一步验的是
#   **客户拿不到、也不是当前位**的那个包 ⇒ 当前位即使少了模型、少了文件，
#   这一步照样 PASS ⇒ 门禁给出一张假绿牌。
#
# 判据（与 selfcheck/self_check.py 的 resolve_packages()、
#      ai-workspace/ocr-tests/check_model_manifests.py 的 resolve_current_package() 同规则）：
#   候选 = <packageRoot>\ 下名字以 `backend-delivery` 开头、且含
#          `ocr-runtime\OcrBackend` 的目录；
#   当前位 = **exe mtime 最大者**；包目录 mtime 只在 exe 缺失时兜底，
#            并列时作次级键。
#
# 为什么不是 `max(包目录 mtime, exe mtime)`：
#   包目录 mtime 会被「往旧包里拷一个文件」顶高 ⇒ 旧包被误判成当前位 ⇒ 假绿重演。
#   exe 是构建产物，它的时间戳才是"这包什么时候构建出来的"的权威信号。
#
# 单独成文件是为了能被单元测试**直接点调用**
# （verify_all.ps1 一执行就跑整套门禁，没法只测这一个函数）。
#
# 用法：
#   . <此文件>
#   $dir = Resolve-CurrentDeliveryPackage -PackageRoot "D:\AP\AP7000\OcrTableTool\package"
#   if (-not $dir) { "package\ 下没有候选交付包" } else { $dir }

function Resolve-CurrentDeliveryPackage {
    [CmdletBinding()]
    param(
        [string]$PackageRoot = ""
    )

    if (-not $PackageRoot) {
        $PackageRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "package"
    }
    if (-not (Test-Path -LiteralPath $PackageRoot -PathType Container)) {
        return ""
    }

    $candidates = @()
    foreach ($child in (Get-ChildItem -LiteralPath $PackageRoot -Directory -ErrorAction SilentlyContinue)) {
        if (-not $child.Name.StartsWith("backend-delivery")) { continue }
        $runtime = Join-Path $child.FullName "ocr-runtime\OcrBackend"
        if (-not (Test-Path -LiteralPath $runtime -PathType Container)) { continue }

        $dirMtime = $child.LastWriteTimeUtc
        $exe = Join-Path $runtime "OcrBackend.exe"
        $exeMtime = $dirMtime
        if (Test-Path -LiteralPath $exe -PathType Leaf) {
            $exeMtime = (Get-Item -LiteralPath $exe).LastWriteTimeUtc
        }
        $candidates += [PSCustomObject]@{
            Package = $child.FullName
            ExeMtime = $exeMtime
            DirMtime = $dirMtime
        }
    }

    if ($candidates.Count -eq 0) {
        return ""
    }
    $best = $candidates |
        Sort-Object -Property @{ Expression = { $_.ExeMtime }; Descending = $true },
                              @{ Expression = { $_.DirMtime }; Descending = $true } |
        Select-Object -First 1
    return $best.Package
}
