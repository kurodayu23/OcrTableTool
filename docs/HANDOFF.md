# OCR 表格接口交接说明

交付目录同时包含可直接运行的 `OcrTableTool.exe` 和可被同事程序调用的离线接口 `ocr-runtime/OcrBackend/OcrBackend.exe`。

## 同事接入顺序

1. 启动 `OcrBackend.exe --persistent`，工作目录设为该 EXE 所在目录；
2. 通过 stdin/stdout 使用 UTF-8 JSONL，一行一个请求/响应；stderr 只作日志；
3. 依次调用 `health`、`warmup`、`recognize`；
4. 只有满足 `BACKEND_API_V1.md` 第 5 节全部发布条件，才能自动写入同事的业务数据；
5. 需要 XLSX 时调用 `export_xlsx`；CSV 由客户端从 `cells[].text` 生成；
6. 取消识别时结束整个后端进程，再重新启动并预热。

稳定协议、错误码、字段含义和生命周期见 [BACKEND_API_V1.md](BACKEND_API_V1.md)，机器可读契约见 [backend-api-v1.schema.json](schemas/backend-api-v1.schema.json)，Qt 5.9.6/QProcess 用法见 [qt-qprocess-client](examples/qt-qprocess-client/README.md)。

## 验收边界

- 接口会保留 `needs_review`、`publication_blocked`、`structure_verified`，不能只读取文字后忽略这些字段。
- `status=ok` 只代表请求执行成功，不代表识别结果可以自动发布。
- `rectified_image` 属于调用方指定的请求临时目录；调用方只能清理自己创建的目录。
- 接口当前为单进程串行。多个业务任务应排队，或由上层创建相互隔离的进程和输出目录。
