# OcrTableTool

OcrTableTool 是面向 Windows 平板和桌面设备的图片转表格工具。它支持导入图片、摄像头拍照和单指框选表格，识别结果可在界面中修改，并导出为 CSV 或 XLSX。

[下载 Windows 版本](https://github.com/kurodayu23/OcrTableTool/releases/latest)

## Vibe Coding / AI 辅助开发

本项目采用 AI 辅助开发，通过需求描述与迭代反馈，使用 AI 辅助编写和修改代码。Vibe Coding 在这里描述开发方式；项目的具体功能与完成度以源码、运行说明和验证记录为准。

项目本身使用 OCR 和表格结构识别模型。展示重点是 Qt/C++ 桌面交互、Python 识别后端、进程通信与数据导出。

维护时以明确需求、审查代码改动和可复现验证为准；具体测试及尚未验证的部分见下方说明。

## 主要功能

- 读取 PNG、JPG、BMP、TIFF 和 WebP 图片。
- 使用摄像头拍照识别，支持平板单指或鼠标框选目标表格。
- 校正旋转和透视，恢复行列、单元格及合并关系。
- 在结果表中修改单元格或添加数据。
- 导出 UTF-8 BOM CSV 和带基础排版的 XLSX。
- 根据设备可用内存调整识别批量；单次任务结束后退出后端进程。

## 工作方式

```text
图片或摄像头照片
    -> Qt Widgets：拍照、框选、预览、编辑
    -> QProcess + JSONL
    -> Python / OpenVINO：文字识别和表格结构恢复
    -> cells + spans
    -> CSV / XLSX
```

Qt 与识别后端通过本机进程通信。界面、拍照和 CSV 由 Qt 侧处理；图像校正、文字识别、表格结构和 XLSX 由后端处理。

识别结果的核心字段包括：

- `rows`、`columns`：表格行列数；
- `cells[row][column].text`：单元格文字；
- `cells[row][column].confidence`：识别置信度；
- `spans`：合并单元格位置和跨度；
- `memory_mode`：本次任务使用的内存模式。

完整协议见 [BACKEND_API_V1.md](docs/BACKEND_API_V1.md)。

## 开发环境

- Windows 11 x64
- Qt 5.9.6 MSVC 2015 x64
- Python 3.11
- OpenVINO CPU

## 构建与运行

首次准备后端：

```powershell
.\scripts\setup_backend.ps1
```

该命令会创建 `.venv` 并准备模型文件。模型文件不提交到仓库。

用 Qt Creator 打开 `ocr-table-tool.pro`，选择 Qt 5.9.6 MSVC 2015 x64 Kit 构建；也可以运行：

```powershell
.\scripts\build_msvc2015.ps1
.\scripts\run_tool.ps1
```

## Qt 接口

Qt 接口位于 `interface-sdk/qt`，示例位于 `interface-sdk/example`。

- `captureAndRecognize()`：拍照并识别；
- `setTableRegion()`：设置归一化框选区域；
- `clearTableRegion()`：恢复整图识别；
- `setCellText()`、`appendRow()`：修改识别结果；
- `exportLastCsv()`：导出当前数据。

调用说明见 [CAMERA_OCR_INTERFACE.md](docs/CAMERA_OCR_INTERFACE.md)。

## 验证

当前源码在 Windows 11 x64 环境完成以下回归检查：

| 检查项 | 结果 |
| --- | ---: |
| Python 后端单元测试 | 930 通过，3 跳过 |
| UI 与接口源码契约 | 41 通过 |
| Qt 核心测试 | 13 通过 |
| Qt 接口测试 | 10 通过 |

运行 Python 测试：

```powershell
$env:PYTHONPATH = (Resolve-Path .\backend).Path
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest tests.test_ui_contract tests.test_interface_sdk_contract
```

## 使用边界

- 识别效果取决于图片清晰度、拍摄角度、反光和表格密度，复杂图片可能需要人工复核。
- CSV 不保存字体、边框和列宽，需要保留基础排版时使用 XLSX。
- 后端为本机单进程串行接口；多个任务应排队，或使用相互隔离的进程和输出目录。

## 许可证

项目源码采用 MIT 许可证，第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
