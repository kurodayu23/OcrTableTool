# OcrTableTool · 离线图片转表格工具

面向 Windows 桌面和平板的 Qt/C++ 工具：打开图片或使用摄像头拍照，框选识别区域，调用本地 Python OCR 后端重建表格，并编辑、导出 CSV/XLSX。

[下载 Windows 版本](https://github.com/kurodayu23/OcrTableTool/releases/latest) · [摄像头与 Qt 接口](docs/CAMERA_OCR_INTERFACE.md) · [后端协议](docs/BACKEND_API_V1.md) · [回归基准说明](docs/BENCHMARK_CORPUS.md)

## 解决什么问题

把图片中的表格转成可继续处理的数据，同时保留人工检查和编辑的入口。识别在本机运行；首次准备开发环境时需要下载依赖和模型，模型文件不提交到源码仓库。

## 实现与代码入口

- **桌面交互**：Qt/C++ 负责图片预览、摄像头拍照、区域选择与表格编辑，入口见 [`src/gui/mainwindow.cpp`](src/gui/mainwindow.cpp)。
- **进程通信**：通过 `QProcess` 启动 Python 后端，以逐行 JSON 协议交换请求和结果；[`BackendRunner`](src/gui/backendrunner.h) 管理请求关联、取消和进程结束，避免旧请求的输出交付给新任务。
- **本地识别**：[`backend/ocr_backend.py`](backend/ocr_backend.py) 提供后端入口，结合 OpenVINO、RapidOCR 和表格处理管线执行识别。
- **数据与导出**：Qt 侧使用 [`TableData`](src/core/tabledata.h) 管理表格数据，支持 CSV/XLSX 导出。
- **接口复用**：[`interface-sdk`](interface-sdk/README.md) 提供 Qt 客户端和控制台、摄像头调用示例，可用于接入其他 Qt 应用。

主要调用链：Qt 界面 → QProcess / JSONL → Python 识别与表格处理 → Qt 结果编辑 → CSV/XLSX 导出。

## 工程关注点

识别质量、取消行为和内存占用需要一起考虑。后端进程按请求启动，响应交付后结束，避免模型长期驻留；界面在进程停止阶段仍保持忙碌状态，防止取消任务与下一次识别重叠。具体状态与实现见 [`backendrunner.h`](src/gui/backendrunner.h) 和 [`backendrunner.cpp`](src/gui/backendrunner.cpp)。

仓库包含 Python 后端测试、Qt Core 测试，以及 UI 和接口契约测试。测试文件的存在不代表当前版本全部通过；下方提供复现命令，具体识别质量应以对应版本、样本和真值的实测结果为准。

## 下载与开发环境

Windows 下载入口见 [Releases](https://github.com/kurodayu23/OcrTableTool/releases/latest)。

开发环境：Windows 11 x64、Qt 5.9.6 MSVC 2015 x64、Python 3.11、OpenVINO CPU。

### 准备后端

```powershell
.\scripts\setup_backend.ps1
```

该命令创建 `.venv` 并准备离线模型。

### 构建与运行

用 Qt Creator 打开 `ocr-table-tool.pro`，选择 Qt 5.9.6 MSVC 2015 x64 Kit 构建；也可以运行：

```powershell
.\scripts\build_msvc2015.ps1
.\scripts\run_tool.ps1
```

### 接口接入

Qt 接口位于 `interface-sdk/qt`，调用示例位于 `interface-sdk/example`。摄像头接口支持拍照识别、框选区域、修改或添加数据，以及导出 CSV；接入方式见 [`docs/CAMERA_OCR_INTERFACE.md`](docs/CAMERA_OCR_INTERFACE.md)。

## 测试与识别边界

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests -p "test_*.py"
python -m pytest -q tests\test_ui_contract.py tests\test_interface_sdk_contract.py
```

Qt Core 测试工程见 [`tests/tests.pro`](tests/tests.pro)，SDK 测试工程见 [`interface-sdk/tests/sdk-tests.pro`](interface-sdk/tests/sdk-tests.pro)。

[`docs/BENCHMARK_CORPUS.md`](docs/BENCHMARK_CORPUS.md) 说明了回归样本分层、development/holdout 划分和导出回读要求。正式报告应同时记录结构与文本错误、数值精确性、耗时及峰值内存。文档中的基准集规模是评测设计，不是准确率或通过率声明。

复杂表格、模糊照片等输入仍需检查识别结果，尤其是行列结构和数值。源码仓库、具体 Release 与本地开发版本可能不同，复现时请注明提交或版本号。

## 许可证

项目源码采用 [MIT 许可证](LICENSE)，第三方组件说明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。模型及第三方组件的使用与再分发以各自许可证为准。
