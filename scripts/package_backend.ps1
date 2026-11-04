param(
    [string]$OutputDirectory = "",
    [string]$PythonExe = "",
    [switch]$SkipDeliveryNotes,
    [switch]$VerifyDeliveryNotes
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonExe) {
    $PythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
$PythonExe = [System.IO.Path]::GetFullPath($PythonExe)
$modelDirectory = Join-Path $projectRoot "runtime\models"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Backend Python is missing: $PythonExe. Run scripts\setup_backend.ps1 or pass -PythonExe."
}
$backendDirectoryForValidation = Join-Path $projectRoot "backend"
& $PythonExe -c "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from ocr_backend import validate_model_files; validate_model_files(Path(sys.argv[2]))" $backendDirectoryForValidation $modelDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Portable OCR models are missing or damaged. Run scripts\setup_backend.ps1 first."
}
if (-not $OutputDirectory) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputDirectory = Join-Path $projectRoot "package\backend-$stamp"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "Output directory already exists: $OutputDirectory"
}

& $PythonExe -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is missing. Install it with: .venv\Scripts\python.exe -m pip install 'pyinstaller>=6,<7'"
}

$distDirectory = Join-Path $OutputDirectory "ocr-runtime"
# 2026-10-30（lead 复查 P1）：构建工作目录**不能放在交付目录里**。
# delivery_notes.py 按目录实际遍历统计"整包文件数/体积"，PyInstaller 的中间产物
# 会被一起算进客户可见的数字（实测一次构建把整包统计从 430 文件 / 537.7 MiB
# 抬到 446 文件 / 571.0 MiB，虚高 34.2 MB）。这里放到产品树外的 build 目录，
# 并在脚本收尾删除；即使中途失败也不会污染交付目录。
$buildStamp = if ($stamp) { $stamp } else { Get-Date -Format "yyyyMMdd-HHmmss" }
$workDirectory = Join-Path $projectRoot ("build\pyinstaller-work-" + $buildStamp)
$specDirectory = Join-Path $OutputDirectory "pyinstaller-spec"
& $PythonExe -m PyInstaller `
    --clean `
    --onedir `
    --name OcrBackend `
    --distpath $distDirectory `
    --workpath $workDirectory `
    --specpath $specDirectory `
    --paths (Join-Path $projectRoot "backend") `
    --collect-all rapidocr `
    --collect-all rapid_table `
    --collect-all openvino `
    --hidden-import recognition_scheduler `
    --hidden-import openpyxl `
    --hidden-import fast_stats `
    --hidden-import table_pipeline `
    --hidden-import colored_table `
    (Join-Path $projectRoot "backend\ocr_backend.py")
if ($LASTEXITCODE -ne 0) {
    throw "Backend packaging failed."
}

$backendDirectory = Join-Path $distDirectory "OcrBackend"
$packagedModelDirectory = Join-Path $backendDirectory "models"
New-Item -ItemType Directory -Path $packagedModelDirectory -Force | Out-Null
$requiredModelFiles = @(
    "PP-OCRv6_det_medium.xml",
    "PP-OCRv6_det_medium.bin",
    "PP-OCRv6_det_small.onnx",
    "PP-OCRv6_rec_small.xml",
    "PP-OCRv6_rec_small.bin",
    "PP-OCRv6_rec_medium.xml",
    "PP-OCRv6_rec_medium.bin",
    "ch_ppocr_mobile_v2.0_cls_mobile.xml",
    "ch_ppocr_mobile_v2.0_cls_mobile.bin",
    "ch_PP-OCRv5_rec_mobile.onnx",
    "ch_PP-OCRv5_rec_server.onnx",
    "en_PP-OCRv5_rec_mobile.onnx",
    "latin_PP-OCRv5_rec_mobile.onnx",
    "latin_PP-OCRv5_dict.txt",
    "slanet-plus.onnx"
)
foreach ($modelName in $requiredModelFiles) {
    Copy-Item `
        -LiteralPath (Join-Path $modelDirectory $modelName) `
        -Destination (Join-Path $packagedModelDirectory $modelName)
}

# The English word list is read at runtime by the backend to tell a real word
# from a mis-segmented glyph.  ocr_backend._english_words() looks for
# en_US.dic next to the module first and next to the executable second, so the
# dictionary has to travel with the package or the check silently degrades to
# an empty word set.  SCOWL-LICENSE.txt must accompany it: the dictionary is
# redistributed under the SCOWL licence and the licence has to stay with it.
$dictionaryFiles = @(
    "en_US.dic",
    "SCOWL-LICENSE.txt"
)
foreach ($dictionaryName in $dictionaryFiles) {
    $dictionarySource = Join-Path $projectRoot "backend\$dictionaryName"
    if (-not (Test-Path -LiteralPath $dictionarySource -PathType Leaf)) {
        throw "Backend dictionary file is missing: $dictionarySource"
    }
    Copy-Item `
        -LiteralPath $dictionarySource `
        -Destination (Join-Path $backendDirectory $dictionaryName)
}

# PyInstaller 6 can omit RapidOCR's YAML data files when the collection step is
# interrupted or when package metadata is resolved through the compatibility
# loader.  The runtime reads both files before applying our explicit model
# paths, so stage them deterministically from the active environment.
$rapidOcrPackage = (& $PythonExe -c "from pathlib import Path; import rapidocr; print(Path(rapidocr.__file__).resolve().parent)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $rapidOcrPackage) {
    # 2026-10-30（lead 复查 P3）：同文件其余 6 处都检查了 $LASTEXITCODE，只有这里漏；
    # 漏检时错误会被推到后面那句 "RapidOCR runtime config is missing"，指向错地方。
    throw "无法定位 rapidocr 包目录（python -c 返回 $LASTEXITCODE）：$rapidOcrPackage"
}
$rapidOcrRuntime = Join-Path $backendDirectory "_internal\rapidocr"
New-Item -ItemType Directory -Path $rapidOcrRuntime -Force | Out-Null
foreach ($configName in @("config.yaml", "default_models.yaml")) {
    $configSource = Join-Path $rapidOcrPackage $configName
    if (-not (Test-Path -LiteralPath $configSource)) {
        throw "RapidOCR runtime config is missing: $configSource"
    }
    Copy-Item -LiteralPath $configSource -Destination (Join-Path $rapidOcrRuntime $configName) -Force
}

# The application always uses the explicit portable model paths above.  The
# copies bundled inside the Python packages are smaller fallback models and a
# duplicate table model; keeping them only increases the tablet footprint.
$unusedBundledModels = @(
    (Join-Path $backendDirectory "_internal\rapidocr\models\PP-OCRv6_det_small.onnx"),
    (Join-Path $backendDirectory "_internal\rapidocr\models\PP-OCRv6_rec_small.onnx"),
    (Join-Path $backendDirectory "_internal\rapidocr\models\ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
    (Join-Path $backendDirectory "_internal\rapid_table\models\slanet-plus.onnx")
)
foreach ($unusedModel in $unusedBundledModels) {
    if (Test-Path -LiteralPath $unusedModel) {
        Remove-Item -LiteralPath $unusedModel -Force
    }
}

# The production backend loads Paddle-exported OpenVINO IR models for OCR and
# an ONNX model for table structure, all on OpenVINO CPU. PyInstaller's
# collect-all also stages unused GPU/NPU plugins, unrelated frontends and
# static import libraries that this executable cannot select. Removing only
# those generated staging files keeps the tablet package smaller without
# changing models, CPU inference, precision, preprocessing or quality gates.
$openVinoLibs = Join-Path $backendDirectory "_internal\openvino\libs"
$unusedOpenVinoFiles = @(
    "cache.json",
    "openvino_auto_batch_plugin.dll",
    "openvino_auto_plugin.dll",
    "openvino_c.dll",
    "openvino_hetero_plugin.dll",
    "openvino_intel_gpu_plugin.dll",
    "openvino_intel_npu_compiler.dll",
    "openvino_intel_npu_compiler_loader.dll",
    "openvino_intel_npu_plugin.dll",
    "openvino_jax_frontend.dll",
    "openvino_paddle_frontend.dll",
    "openvino_pytorch_frontend.dll",
    "openvino_tensorflow_frontend.dll",
    "openvino_tensorflow_lite_frontend.dll"
)
foreach ($fileName in $unusedOpenVinoFiles) {
    $target = Join-Path $openVinoLibs $fileName
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Force
    }
}
Get-ChildItem -LiteralPath $openVinoLibs -Filter "*.lib" -File |
    Remove-Item -Force

# Keep the standalone tablet package runtime-only.  These generated folders
# belong to engines and developer SDK surfaces that the production code never
# selects: all five OCR/table models run through OpenVINO CPU on static images.
#
# NOTE: "_internal\onnxruntime" is deliberately NOT pruned.  The table
# structure engine (rapid_table SLANETPLUS) defaults to the ONNX Runtime
# backend -- see rapid_table/table_structure/pp_structure/main.py, where an
# unset cfg["engine_type"] falls back to EngineType.ONNXRUNTIME, and
# inference_engine/base.py get_engine(), which raises ImportError when the
# onnxruntime package is missing.  The table model is slanet-plus.onnx, so
# pruning onnxruntime makes the shipped tablet silently lose table structure
# detection while the development machine (which has onnxruntime installed)
# keeps working.  backend/requirements.txt declares onnxruntime as a runtime
# dependency for exactly this reason.
$backendRoot = [System.IO.Path]::GetFullPath($backendDirectory).TrimEnd('\') + '\'
$unusedRuntimeDirectories = @(
    (Join-Path $backendDirectory "_internal\openvino\include"),
    (Join-Path $backendDirectory "_internal\openvino\cmake"),
    (Join-Path $backendDirectory "_internal\openvino\lib"),
    (Join-Path $backendDirectory "_internal\openvino\tools"),
    (Join-Path $backendDirectory "_internal\openvino\frontend\jax"),
    (Join-Path $backendDirectory "_internal\openvino\frontend\paddle"),
    (Join-Path $backendDirectory "_internal\openvino\frontend\pytorch"),
    (Join-Path $backendDirectory "_internal\openvino\frontend\tensorflow"),
    (Join-Path $backendDirectory "_internal\openvino\torch"),
    (Join-Path $backendDirectory "_internal\openvino\properties\intel_gpu"),
    (Join-Path $backendDirectory "_internal\openvino\properties\intel_npu"),
    (Join-Path $backendDirectory "_internal\rapidocr\inference_engine\pytorch")
)
foreach ($directory in $unusedRuntimeDirectories) {
    $resolvedDirectory = [System.IO.Path]::GetFullPath($directory)
    if (-not $resolvedDirectory.StartsWith(
            $backendRoot,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to prune outside generated backend: $resolvedDirectory"
    }
    if (Test-Path -LiteralPath $resolvedDirectory) {
        Remove-Item -LiteralPath $resolvedDirectory -Recurse -Force
    }
}

$unusedImageCodecs = @(
    (Get-ChildItem -LiteralPath (Join-Path $backendDirectory "_internal\cv2") `
        -Filter "opencv_videoio_ffmpeg*_64.dll" -File -ErrorAction SilentlyContinue),
    (Get-ChildItem -LiteralPath (Join-Path $backendDirectory "_internal\PIL") `
        -Filter "_avif*.pyd" -File -ErrorAction SilentlyContinue)
)
foreach ($codecGroup in $unusedImageCodecs) {
    foreach ($codec in @($codecGroup)) {
        Remove-Item -LiteralPath $codec.FullName -Force
    }
}

# Type stubs and repository placeholders are build-time metadata.  The frozen
# application imports Python modules from its archive and never reads these
# files at runtime.
Get-ChildItem -LiteralPath $backendDirectory -Recurse -File |
    Where-Object {
        $_.Extension -eq ".pyi" -or
        $_.Name -eq "py.typed" -or
        $_.Name -eq ".gitkeep"
    } |
    Remove-Item -Force

$requiredOpenVinoFiles = @(
    "openvino.dll",
    "openvino_intel_cpu_plugin.dll",
    "openvino_ir_frontend.dll",
    "openvino_onnx_frontend.dll"
)
foreach ($fileName in $requiredOpenVinoFiles) {
    $requiredFile = Join-Path $openVinoLibs $fileName
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required OpenVINO runtime file is missing after pruning: $requiredFile"
    }
}

# --- Runtime dependency cross-check -------------------------------------
# The pruning lists above are hand written, so they can drift away from
# backend/requirements.txt and silently remove a dependency that the frozen
# application imports at runtime.  That is exactly how onnxruntime was
# removed once: the development machine kept working because it has the
# package installed, while the shipped tablet lost table structure detection
# with no error surfaced.
#
# Note: only packages that ship native libraries or data files appear as a
# directory under _internal.  Pure-Python packages such as openpyxl are
# frozen into the PYZ archive, so "no _internal\openpyxl directory" is
# normal and must not be treated as a missing dependency.  The authoritative
# check for those is the runtime self-verification at the end of this script,
# which actually executes the packaged executable.
$requirementsFile = Join-Path $projectRoot "backend\requirements.txt"
if (-not (Test-Path -LiteralPath $requirementsFile -PathType Leaf)) {
    throw "Runtime requirement manifest is missing: $requirementsFile"
}
$distributionToPackage = @{
    "onnxruntime"   = "onnxruntime"
    "openvino"      = "openvino"
    "openpyxl"      = ""
    "opencv-python" = "cv2"
    "rapid-table"   = "rapid_table"
    "rapidocr"      = "rapidocr"
}
$declaredRequirements = @(Get-Content -LiteralPath $requirementsFile |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith("#") } |
    ForEach-Object { ($_ -split '[[;<>=!\[\] ]')[0].Trim().ToLowerInvariant() })
foreach ($distribution in $declaredRequirements) {
    if (-not $distributionToPackage.ContainsKey($distribution)) {
        throw "Runtime requirement '$distribution' has no packaging mapping. Add it to `$distributionToPackage in scripts\package_backend.ps1 before shipping."
    }
    $packageFolderName = $distributionToPackage[$distribution]
    if (-not $packageFolderName) {
        # Pure-Python package: frozen into the archive, no directory to check.
        continue
    }
    $packageFolder = Join-Path $backendDirectory ("_internal\" + $packageFolderName)
    if (-not (Test-Path -LiteralPath $packageFolder -PathType Container)) {
        throw "Runtime dependency '$distribution' is declared in backend\requirements.txt but missing from the package after pruning: $packageFolder"
    }
}

# The table structure engine loads the ONNX Runtime native library; the
# Python folder alone is not enough to make rapid_table usable.
$onnxRuntimeNative = Join-Path $backendDirectory "_internal\onnxruntime\capi\onnxruntime_pybind11_state.pyd"
if (-not (Test-Path -LiteralPath $onnxRuntimeNative -PathType Leaf)) {
    throw "ONNX Runtime native library is missing from the package: $onnxRuntimeNative"
}
$tableEngineShim = Join-Path $backendDirectory "_internal\rapid_table\inference_engine\onnxruntime\main.py"
if (-not (Test-Path -LiteralPath $tableEngineShim -PathType Leaf)) {
    throw "rapid_table ONNX Runtime engine shim is missing from the package: $tableEngineShim"
}

# --- Runtime self-verification -------------------------------------------
# Everything above only inspects files on disk.  The only proof that the
# package actually works is to run it: models present but unusable, a missing
# pure-Python dependency frozen into the wrong archive, or a table structure
# engine that cannot start all look identical from the outside.  Run the
# packaged executable through health / export_xlsx / recognize before the
# package is allowed to ship.
$verificationScript = Join-Path $projectRoot "scripts\verify_delivery_package.py"
if (-not (Test-Path -LiteralPath $verificationScript -PathType Leaf)) {
    throw "Delivery verification script is missing: $verificationScript"
}
& $PythonExe $verificationScript `
    --package-dir $backendDirectory `
    --backend-source (Join-Path $projectRoot "backend")
if ($LASTEXITCODE -ne 0) {
    throw "Delivery package self-verification failed. The package is not shippable."
}

# --- 交付件落地：从版本库取，不再手工维护 --------------------------------
# 2026-10-27 之前 selfcheck.bat 与 交付说明.md 只存在于交付位上、不在版本库：
# 换台机器重打包就会丢，而且交付说明里的文件数 / 体积 / 指纹会静默过期。
# 现在源文件在版本库：
#   packaging\delivery\selfcheck.bat          —— 客户双击的一键自检（逐字节源）
#   packaging\delivery\交付说明.md.tmpl       —— 模板，随包数字由 delivery_notes.py 重算
#   scripts\customer_selfcheck.py             —— 自检脚本本体（客户侧纯标准库）
# 注意：交付说明里的数字是**按目录实际遍历**算出来的，所以 licenses\ 与
# PyInstaller 的 spec 目录必须在渲染**之前**就位；本脚本只产出 ocr-runtime\ 与
# pyinstaller-spec\，licenses\ 由包负责人补入 ⇒ 补完后必须重跑一次渲染：
#   python scripts\delivery_notes.py render --package-dir <交付包目录>
# 或在收尾时用 -VerifyDeliveryNotes 让本脚本自己验。
$deliveryNotesScript = Join-Path $projectRoot "scripts\delivery_notes.py"
if ($SkipDeliveryNotes) {
    Write-Host "Skipped delivery notes staging (-SkipDeliveryNotes)."
} else {
    $deliveryAssets = Join-Path $projectRoot "packaging\delivery"
    $selfcheckSource = Join-Path $deliveryAssets "selfcheck.bat"
    $templateSource = Join-Path $deliveryAssets "交付说明.md.tmpl"
    $customerSelfcheckSource = Join-Path $projectRoot "scripts\customer_selfcheck.py"
    foreach ($required in @($selfcheckSource, $templateSource, $customerSelfcheckSource, $deliveryNotesScript)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "交付件源文件缺失：$required（版本库应包含 packaging\delivery\* 与 scripts\customer_selfcheck.py）"
        }
    }
    Copy-Item -LiteralPath $selfcheckSource -Destination (Join-Path $OutputDirectory "selfcheck.bat") -Force
    Copy-Item -LiteralPath $customerSelfcheckSource -Destination (Join-Path $OutputDirectory "customer_selfcheck.py") -Force
    & $PythonExe -B $deliveryNotesScript render --package-dir $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "交付说明.md 渲染失败。"
    }
    Write-Host "交付件已落地：selfcheck.bat / customer_selfcheck.py / 交付说明.md"
    Write-Host "提醒：licenses\ 补入后必须重跑交付说明渲染，否则文件数与体积是旧的。"
}

# 2026-10-30（lead 复查 P1）：校验改为**无条件执行**。原来它由
# -VerifyDeliveryNotes 开关控制且默认关闭，于是默认流水线跑完没人验，交付说明里的
# 文件数/体积可以是过期的而没有任何一步会红。校验本身很便宜且幂等。
if (-not $SkipDeliveryNotes) {
    & $PythonExe -B $deliveryNotesScript verify --package-dir $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "交付说明.md 与交付包实际内容不一致（数字过期），跑 delivery_notes.py render 重算。"
    }
    $licenseCount = @(Get-ChildItem -LiteralPath (Join-Path $OutputDirectory "licenses") -File -ErrorAction SilentlyContinue).Count
    if ($licenseCount -le 0) {
        Write-Warning "交付根 licenses\ 为空（0 份）。合格交付包必须包含第三方许可文件；补入后请重跑本脚本或 delivery_notes.py render。"
    }
}

# 2026-10-30（lead 复查 P1）：清理构建工作目录（已移出交付目录），
# 避免产品树下堆积中间产物。
if (Test-Path -LiteralPath $workDirectory) {
    try {
        Remove-Item -LiteralPath $workDirectory -Recurse -Force
    } catch {
        Write-Warning "构建工作目录未能删除，可手动清理：$workDirectory"
    }
}

Write-Host "Portable backend ready: $backendDirectory"
Write-Host "Copy the ocr-runtime directory beside OcrTableTool.exe."
