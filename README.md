# 离线 OCR 表格工具

Windows Qt 5.9.6桌面工具：将一张手机拍摄的纸质或屏幕表格照片离线转换为可编辑表格，默认导出UTF-8 BOM CSV，并支持XLSX。

## 已实现

- 单张PNG、JPG、BMP、TIFF、WebP图片导入。
- 导入或拍摄图片后后台静默开始最高质量识别，点击“开始识别”后才显示进度或发布已完成结果；每次任务结束后退出识别进程，可靠归还OpenVINO原生内存。
- 自动检测表格线、校正旋转、裁剪表格区域并增强局部对比度。
- 自动判断屏幕截图、规则表格、纸张拍照和无表格线的成行数据。
- PP-OCRv6 Small整页主读、PP-OCRv6 Medium仅复核低置信、可见漏格、数值规律异常和标识符等风险格，并使用RapidTable SLANetPlus恢复表格结构；图片不上传。
- 原图/矫正结果对照；点击任一预览可在屏幕中央打开大图，支持滚轮缩放、拖动、适应窗口和1:1查看。
- 一图多表时可进入“框选表格”状态，支持平板单指或鼠标拖动选择一个目标表；框选状态会独占触控，避免误点打开图片，高 DPI 坐标按原图映射并为表格边界保留少量安全边距。
- 平板触控支持结果表惯性滚动、点按单元格编辑并调起软键盘、图片单指平移/双指缩放、相机拍摄和全部主要操作按钮；同时保留完整鼠标键盘操作。
- 最高质量模式不设置识别时限；V6 Small完成整页主读，V6 Medium只读取选中的物理风险格，仍无法确认时说明原因并留待人工复核而不猜测。完成后界面显示本次实测耗时。
- 相机明确使用静态拍照模式，拍摄前等待自动对焦、曝光和白平衡稳定。低于7MP或暗光、低对比度、模糊、过曝时仍返回中文质量提示并继续安全识别；无法确认的内容保留风险状态，不猜测数据。
- UTF-8 BOM CSV导出和带边框、列宽、合并单元格的XLSX导出。
- C++核心通过`src/core/ocrtablecore.pri`复用，独立GUI通过`src/gui/gui.pro`构建。

## 开发环境

- Qt 5.9.6 `msvc2015_64`
- Visual Studio 2015 Update 3，x64编译器
- Windows SDK 10.0.19041.0（命令行脚本默认值，可通过参数修改）
- Python 3.11，仅用于离线OCR后端；它不参与Qt GUI编译

项目文件不包含开发者机器的绝对路径。其他开发者可以在Qt Creator中直接打开根目录的`ocr-table-tool.pro`，选择Qt 5.9.6 MSVC 2015 x64 Kit后构建。GUI即使尚未安装Python后端也可以编译。

## 首次准备后端

在PowerShell中执行：

```powershell
.\scripts\setup_backend.ps1
```

脚本在项目内创建`.venv`，安装`backend/requirements.txt`中的固定版本，并把模型准备到`runtime/models`。当前配置使用PP-OCRv6 Medium检测、PP-OCRv6 Small整页识别、PP-OCRv6 Medium风险格复核、OpenVINO CPU推理和SLANetPlus；准备阶段首次获取模型需要联网，完成后识别阶段不发起模型下载。每次启动识别前都会校验5个必需模型文件的SHA-256，缺失或损坏时直接返回错误。

也可以指定Python或环境目录：

```powershell
.\scripts\setup_backend.ps1 -Python 3.11 -EnvironmentPath D:\LocalRuntime\ocr-table-venv
```

指定外部环境时，运行前设置`OCR_TABLE_PYTHON`为该环境的`python.exe`。

## Qt Creator构建

1. 打开`ocr-table-tool.pro`。
2. 选择Qt 5.9.6 `msvc2015_64` Kit。
3. 使用shadow build构建Release。
4. 运行`OcrTableTool`子项目。

根工程包含GUI和核心测试。`.pro/.pri`只使用仓库相对路径；构建结束后会把后端脚本复制到可执行文件旁边。

## 设备运行包

开发时Qt Creator会使用项目内`.venv`和`runtime/models`。正式设备不需要安装Python：先安装一次PyInstaller，然后生成独立后端目录。

```powershell
.\.venv\Scripts\python.exe -m pip install "pyinstaller>=6,<7"
.\scripts\package_backend.ps1
```

也可以复用项目外的隔离环境，避免在源码目录创建开发文件：

```powershell
.\scripts\package_backend.ps1 `
  -PythonExe D:\LocalRuntime\ocr-table-venv\Scripts\python.exe
```

脚本使用带时间戳的新目录，不覆盖已有文件。把生成的`ocr-runtime`目录放在`OcrTableTool.exe`旁边；程序会优先启动其中的`OcrBackend.exe`，开发环境的Python只作为后备方式。

目标设备基线为Windows 11 x64、Intel i7-1255U、16 GB内存、1280×800屏幕和150%缩放。程序启用Qt高DPI缩放，根据可用工作区计算初始窗口，最小尺寸760×460；设备端建议让Windows自动管理分页文件。

## 命令行构建

```powershell
.\scripts\build_msvc2015.ps1
```

如果工具链不在常见位置：

```powershell
.\scripts\build_msvc2015.ps1 `
  -QMake C:\Qt\Qt5.9.6\5.9.6\msvc2015_64\bin\qmake.exe `
  -VcVarsAll "C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\vcvarsall.bat" `
  -WindowsSdkVersion 10.0.19041.0
```

运行默认构建：

```powershell
.\scripts\run_tool.ps1
```

Windows日常使用可直接双击项目根目录的`run_tool.cmd`。不要直接双击Qt Creator的`*-Debug` shadow build输出；Debug程序依赖Qt调试运行库，应从Qt Creator启动。

## 测试

Python后端：

```powershell
$env:PYTHONPATH = (Resolve-Path .\backend).Path
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests -v
```

Qt核心测试会随根工程构建，也可以单独打开`tests/tests.pro`构建运行。

## 运行边界

- CSV不保存字体、边框和列宽；需要排版时使用XLSX。
- 拍摄距离过远、严重失焦、强反光遮挡或表格线完全不可见时，仍可能需要重新拍照或人工校正。
- 最高质量模式没有固定完成时间，耗时随表格行列数、透视、褶皱、阴影和疑难单元格数量增长；用户点击“取消”仍可主动终止。
- 表格模板、字段名称、行列数量、输入目录和输出目录均未写死。
- 正式分发设备版本前，应生成独立运行包，并随包保留RapidOCR、RapidTable、ONNX Runtime、OpenCV和openpyxl的许可证文件。
