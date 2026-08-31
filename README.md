# 离线 OCR 识图工具

OcrTableTool 是面向 Windows 平板的离线图片转表格工具，支持打开图片和摄像头拍照。

## 下载

Windows 版本见 [Releases](https://github.com/kurodayu23/OcrTableTool/releases/latest)。

## 开发环境

- Windows 11 x64
- Qt 5.9.6 MSVC 2015 x64
- Python 3.11
- OpenVINO CPU

## 准备后端

```powershell
.\scripts\setup_backend.ps1
```

该命令会创建 `.venv` 并准备离线模型。模型文件不提交到仓库。

## 构建

用 Qt Creator 打开 `ocr-table-tool.pro`，选择 Qt 5.9.6 MSVC 2015 x64 Kit 构建；也可以运行：

```powershell
.\scripts\build_msvc2015.ps1
```

## 本地运行

```powershell
.\scripts\run_tool.ps1
```

## 摄像头接口

Qt 接口位于 `interface-sdk/qt`，调用示例位于 `interface-sdk/example`。接口支持摄像头拍照识别、框选区域、修改或添加数据，以及导出 CSV。

说明见 `docs/CAMERA_OCR_INTERFACE.md`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend\tests -p "test_*.py"
python -m pytest -q tests\test_ui_contract.py tests\test_interface_sdk_contract.py
```

## 许可证

项目源码采用 MIT 许可证，第三方组件说明见 `THIRD_PARTY_NOTICES.md`。
