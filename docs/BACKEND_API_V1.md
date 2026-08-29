# OCR 后端进程接口 V1

本文档面向集成 `OcrBackend.exe` 的同事。接口是本机、离线、单进程串行的 JSONL 协议，不需要把 OCR 算法源码并入业务程序。

## 1. 启动与分帧

```text
OcrBackend.exe --persistent
stdin  : UTF-8 JSON，每个请求一行
stdout : ASCII-safe JSON，每个响应一行
stderr : 日志，不属于协议
```

- 一个进程同一时间只允许一个在途请求；调用方必须排队。
- `stdout` 必须缓存并按换行拆帧，不能假设一次 `readyRead` 等于一个响应。
- 每次请求带 `request_id`（1～2147483647），响应原样回传；迟到或不匹配的响应必须丢弃。
- 未知响应字段必须忽略，禁止用中文 `message`、模型名或诊断字段决定业务状态。
- 推荐顺序：`health → warmup → recognize → 校验发布门禁 → export_xlsx`。

## 2. 通用请求和错误

```json
{"protocol":1,"action":"health","request_id":1}
```

`protocol` 固定为 `1`；`action` 支持 `health`、`warmup`、`recognize`、`export_xlsx`。

成功响应至少包含：

```json
{"protocol":1,"status":"ok","action":"health","request_id":1}
```

失败响应：

```json
{
  "protocol": 1,
  "status": "error",
  "action": "recognize",
  "request_id": 2,
  "error_code": "IMAGE_NOT_FOUND",
  "field": "image_path",
  "message": "image does not exist: D:\\input\\table.png",
  "retryable": false,
  "error_type": "ValueError"
}
```

程序只能根据 `error_code`、`field`、`retryable` 分支；`message` 仅用于显示/日志。V1 错误码包括：

`UNSUPPORTED_PROTOCOL`、`UNSUPPORTED_ACTION`、`INVALID_REQUEST`、`INVALID_OPTION`、`IMAGE_PATH_REQUIRED`、`IMAGE_NOT_FOUND`、`IMAGE_READ_FAILED`、`OUTPUT_DIRECTORY_INVALID`、`OUTPUT_WRITE_FAILED`、`MODEL_MISSING`、`MODEL_HASH_MISMATCH`、`EXPORT_PATH_REQUIRED`、`EXPORT_GRID_INVALID`、`EXPORT_SPAN_INVALID`、`EXPORT_WRITE_FAILED`、`INTERNAL_ERROR`。

## 3. health 与 warmup

```json
{"protocol":1,"action":"health","request_id":1}
```

`health` 校验五个离线模型文件及 SHA-256，但不加载模型；响应中的 `models_valid=true` 才能继续。

```json
{"protocol":1,"action":"warmup","request_id":2}
```

`warmup` 加载模型。同一进程只需调用一次；进程被终止或崩溃后必须重新 `health`、`warmup`。

## 4. recognize

```json
{
  "protocol": 1,
  "action": "recognize",
  "request_id": 3,
  "image_path": "D:\\input\\table.png",
  "output_directory": "D:\\work\\ocr\\request-3",
  "options": {
    "crop_mode": "auto",
    "accuracy_mode": "maximum",
    "deadline_seconds": 0,
    "input_rectified": false,
    "selected_table_region": false
  }
}
```

- `image_path`：本机绝对图片路径，支持 Unicode。
- `output_directory`：本请求独占的可写临时目录。
- 正式集成固定使用 `crop_mode=auto`、`accuracy_mode=maximum`、`deadline_seconds=0`，不要向普通用户暴露降低精度的开关。
- `input_rectified=true` 表示输入图已经完成透视矫正。
- `selected_table_region=true` 表示输入图来自用户框选的单个表格区域，后端会启用框选区域的运动模糊恢复路线。

成功响应的稳定业务字段：`rows`、`columns`、`cells`、`spans`、`recognition_state`、`publication_blocked`、`publication_block_reasons`、`structure_verified`、`structure_certificate`、`image_quality`、`rectified_image`、`elapsed_seconds`、`review_cell_count`。

单元格结构：

```json
{"text":"515.472","confidence":0.96,"needs_review":false}
```

必须满足：`cells.length == rows`，每行长度等于 `columns`，置信度在 0～1。合并格坐标从 0 开始：

```json
{"row":0,"column":0,"row_span":1,"column_span":7,"role":"title"}
```

## 5. 唯一发布门禁

同事的业务程序只有在以下条件全部成立时，才能自动使用或发布结果：

```text
status == "ok"
AND recognition_state == "verified"
AND publication_blocked == false
AND structure_verified == true
AND 所有 cells[*][*].needs_review == false
```

- `needs_review`：可以展示，必须人工确认后才能进入业务发布。
- `blocked`：只能展示草稿，禁止自动发布；原因见 `publication_block_reasons`。
- `structure_verified=false` 时必须有 `structure_certificate=null` 并进入 `blocked`。
- 结构证书证明结果与后端锁定的物理边界自洽，不代表外部真值 100% 正确。
- 用户修改任一单元格或行列后，原证书不能再代表编辑后的结果；客户端要记录“人工确认”来源。

## 6. rectified_image 生命周期

`rectified_image` 是响应返回时存在的本机绝对路径，位于本次 `output_directory`。它是请求级临时预览，不保证永久保存：

1. 预览控件释放文件句柄后，由调用方清理自己创建的请求目录；
2. 需要留档时先复制到正式业务目录；
3. 不同请求/进程不能共享目录；
4. 禁止根据返回路径递归删除非本应用创建的目录。

推荐目录：`%LOCALAPPDATA%\Company\Product\ocr-temp\<request_id>\`。

## 7. export_xlsx

```json
{
  "protocol": 1,
  "action": "export_xlsx",
  "request_id": 4,
  "output_path": "D:\\output\\table.xlsx",
  "cells": [[{"text":"编号","confidence":1.0,"needs_review":false}]],
  "spans": []
}
```

导出成功只表示文件写入成功，不代表 OCR 通过发布门禁。V1 不提供 `export_csv`；需要 CSV 时，客户端从 `cells[].text` 生成 UTF-8 BOM CSV。

## 8. 取消与恢复

V1 没有单独 `cancel`：取消时终止整个后端进程，等待退出，丢弃在途 `request_id`，然后启动新进程并重新 `health`、`warmup`。旧进程未退出前不得发送新请求。

机器可读字段定义见 [backend-api-v1.schema.json](schemas/backend-api-v1.schema.json)，Qt 5.9.6 调用示例见 [qt-qprocess-client](examples/qt-qprocess-client/README.md)。
