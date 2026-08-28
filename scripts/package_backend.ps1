param(
    [string]$OutputDirectory = "",
    [string]$PythonExe = ""
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
$workDirectory = Join-Path $OutputDirectory "pyinstaller-work"
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
    "PP-OCRv6_rec_small.xml",
    "PP-OCRv6_rec_small.bin",
    "PP-OCRv6_rec_medium.xml",
    "PP-OCRv6_rec_medium.bin",
    "ch_ppocr_mobile_v2.0_cls_mobile.xml",
    "ch_ppocr_mobile_v2.0_cls_mobile.bin",
    "slanet-plus.onnx"
)
foreach ($modelName in $requiredModelFiles) {
    Copy-Item `
        -LiteralPath (Join-Path $modelDirectory $modelName) `
        -Destination (Join-Path $packagedModelDirectory $modelName)
}

# PyInstaller 6 can omit RapidOCR's YAML data files when the collection step is
# interrupted or when package metadata is resolved through the compatibility
# loader.  The runtime reads both files before applying our explicit model
# paths, so stage them deterministically from the active environment.
$rapidOcrPackage = (& $PythonExe -c "from pathlib import Path; import rapidocr; print(Path(rapidocr.__file__).resolve().parent)").Trim()
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
$backendRoot = [System.IO.Path]::GetFullPath($backendDirectory).TrimEnd('\') + '\'
$unusedRuntimeDirectories = @(
    (Join-Path $backendDirectory "_internal\onnxruntime"),
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
Write-Host "Portable backend ready: $backendDirectory"
Write-Host "Copy the ocr-runtime directory beside OcrTableTool.exe."
