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
  "message": "指定的图片文件不存在，请确认路径后重试。",
  "retryable": false,
  "error_type": "ValueError"
}
```

> 2026-10-30 订正：`message` 已是**本地化中文、且不包含本机绝对路径**（原示例里的
> `image does not exist: D:\input\table.png` 是旧实现）。**不要**对 `message` 做关键字匹配，
> 只认 `error_code` / `field` / `retryable`。

程序只能根据 `error_code`、`field`、`retryable` 分支；`message` 仅用于显示/日志。V1 实际会发出的错误码共 **19** 个：

| error_code | 何时出现 | field | retryable |
|---|---|---|---|
| `UNSUPPORTED_PROTOCOL` | `protocol` 不是 1 | `protocol` | false |
| `UNSUPPORTED_ACTION` | `action` 不是四个动作之一 | `action` | false |
| `INVALID_REQUEST` | 请求不合法（字段缺失/类型错/值域错），或错误无法归到更具体的码；**"图里没检测到表格""没有检出任何文字"也归这里** | `request_id` / `null` | false |
| `INVALID_OPTION` | `options` 里 `crop_mode` / `accuracy_mode` / `deadline_seconds` 非法 | `options` | false |
| `IMAGE_PATH_REQUIRED` | `image_path` 缺失或为空 | `image_path` | false |
| `IMAGE_NOT_FOUND` | 图片文件不存在 | `image_path` | false |
| `IMAGE_READ_FAILED` | 图片读不出（空/损坏/非图片/超尺寸上限） | `image_path` | false |
| `OUTPUT_DIRECTORY_INVALID` | `output_directory` 为空或非字符串 | `output_directory` | false |
| `OUTPUT_WRITE_FAILED` | 结果文件写不进 `output_directory` | `output_directory` | true |
| `MODEL_MISSING` | 模型文件缺失 | `null` | false |
| `MODEL_HASH_MISMATCH` | 模型文件校验失败 | `null` | false |
| `EXPORT_PATH_REQUIRED` | `output_path` 缺失或为空 | `output_path` | false |
| `EXPORT_GRID_INVALID` | `cells` 不是非空矩形二维数组，**或整个网格全是空白** | `cells` | false |
| `EXPORT_SPAN_INVALID` | `spans` 越界/重叠/与 `cells` 对不上 | `spans` | false |
| `EXPORT_WRITE_FAILED` | xlsx 写不进 `output_path` | `output_path` | true |
| `NETWORK_ACCESS_DENIED` | 离线运行时拦截到联网行为 | `null` | false |
| `RECOGNITION_BUDGET_EXCEEDED` | 60 秒墙钟 / 请求预算内没能确认表格结构（见 §4.1） | `null` | **true** |
| `INSUFFICIENT_MEMORY` | 内存分配失败（原生 OOM 为 false，`MemoryError` 为 true） | `null` | 见左 |
| `INTERNAL_ERROR` | 未归类的内部错误 | `null` | true |

## 3. health 与 warmup

```json
{"protocol":1,"action":"health","request_id":1}
```

`health` 校验**15 个**离线模型文件及其 SHA-256（不加载模型）；响应中的 `models_valid=true` 才能继续。
2026-10-30 订正：旧版本这里写的是"五个"，实际随包分发的模型是 15 个（与交付说明、客户自检脚本一致）。

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
- `output_directory`：**可选**（2026-10-30 订正：旧版本当作必需项描述）。给的必须是本请求独占的
  可写目录；**省略时后端落到共享的 `%TEMP%\ocr-table-tool`**，此时 §6 的"请求目录隔离"不再成立，
  且后端全程不清理该目录（`rectified_image` 会持续堆积）⇒ **多请求/多进程集成必须显式提供本请求目录**。
- 正式集成固定使用 `crop_mode=auto`、`accuracy_mode=maximum`、`deadline_seconds=0`，不要向普通用户暴露降低精度的开关。
- `input_rectified=true` 表示输入图已经完成透视矫正。
- `selected_table_region=true` 表示输入图来自用户框选的单个表格区域，后端会启用框选区域的运动模糊恢复路线。

成功响应的稳定业务字段：`rows`、`columns`、`cells`、`spans`、`recognition_state`、`publication_blocked`、`publication_block_reasons`、`structure_verified`、`structure_certificate`、`image_quality`、`rectified_image`、`elapsed_seconds`、`review_cell_count`、`memory_mode`。

`memory_mode=normal` 使用正常批量；`memory_mode=low_memory` 在可用内存低于
`max(2 GB, min(4 GB, 物理内存 × 35%))` 时切到串行小批量（8 GB 机器上约 2.8 GB），识别与复核步骤不变。
⚠ 2026-10-30 订正：旧版本这里写的"低于 4 GB 进低内存档"和"物理内存 <1.5 GB 或提交空间 <1 GB 才返回
`INSUFFICIENT_MEMORY`"**都没有实现依据** —— 低内存档只**降并发、不拒绝**；`INSUFFICIENT_MEMORY`
只在真的内存分配失败（原生 OOM / `MemoryError`）时出现。阈值可用环境变量
`OCRTABLE_LOW_MEMORY_THRESHOLD_MB` 现场标定。

### 4.1 60 秒墙钟契约（原文档缺失，2026-10-30 补）

- 单次识别有 **60 秒上限**，由后端在若干检查点（解码/几何/结构/逐格复核/发布前恢复）判定；
  超限返回 `error_code=RECOGNITION_BUDGET_EXCEEDED`、`retryable=true`。
- 判定是**检查点式**，不是硬超时：完成瞬间可能略微越界（实测最慢成功例 ~60.6 秒、最慢失败例 ~80.7 秒）。
  也可能**提前**返回（例如 22 秒就判定"逐格复核预计需 64 秒"而放弃）。
- ⇒ **不要把 60 秒当作接口的超时上限**；调用方超时请留足余量（建议 ≥120 秒），
  并对 `retryable=true` 的失败按"缩小框选范围/换更清晰图片"重试。
- `options.deadline_seconds` 传正数会**收紧**本次上限为 `min(60, 该值)`；传 `0`（正式集成固定值）保持 60 秒。
- 环境变量 `OCRTABLE_BUDGET_DISABLED=1` 可**整条关闭**该墙钟（仅供批量验收，生产不建议）。

单元格结构：

```json
{"text":"515.472","confidence":0.96,"needs_review":false}
```

可能还带一个**可选**字段：

```json
{"text":"515.472","confidence":0.77,"needs_review":false,"confidence_sentinel":true}
```

- `confidence_sentinel`：**可选**（老客户端可以完全忽略）。为 `true` 表示这一格的 `confidence`
  是识别路径硬写的**类别哨兵**（`0.77` = 未取得双模型一致确认，`0.86` = 已取得），
  **不是实测置信度**。消费方**不要**把它当成"低置信度告警"，也**不要**拿它跟 `0.78` 之类的
  阈值比大小 —— 那样会把逐格全对的表整片标成待复核。老客户端没有这个字段时，
  应以 `needs_review` 为唯一的人工复核依据。

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

- `needs_review`：可以展示，必须人工确认后才能进入业务发布。**这是唯一的人工复核依据**；
  `confidence_sentinel=true` 不等于要复核（见 §4）。
- `blocked`：只能展示草稿，禁止自动发布；原因见 `publication_block_reasons`。
- ⚠ **2026-10-30 订正（重要）**：旧版本写"`structure_verified=false` 时…进入 `blocked`"，**与实现不符** ——
  结构认证被撤销时后端只清 `structure_certificate`、置 `structure_verified=false` 并追加一条
  *提示性* 说明（`processing_notices`），**不会**把 `publication_blocked` 置 true、也不改 `recognition_state`。
  这类结果可能同时出现 `status=ok` / `recognition_state=verified` / `publication_blocked=false` /
  `structure_verified=false` / `structure_certificate=null`。
  ⇒ **客户端必须自己校验 `structure_verified == true`**（上面门禁五行缺一不可），
  绝不能因为 `publication_blocked=false` 就认为结构已经过认证。
- 结构证书证明结果与后端锁定的物理边界自洽，不代表外部真值 100% 正确。
- 用户修改任一单元格或行列后，原证书不能再代表编辑后的结果；客户端要记录"人工确认"来源。

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

字段与边界（2026-10-30 补全）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `output_path` | 是 | xlsx 落盘路径；父目录不存在会自动创建 |
| `cells` | 是 | 非空**矩形**二维数组，元素为 `{text, confidence, needs_review}`；`text` 为 `null` 时按空串导出 |
| `spans` | 否 | 合并单元格 `[{row, column, row_span, column_span, role?}]`，坐标从 0 开始 |
| `structure_certificate` | 否 | `recognize` 返回的证书原样透传；**非法证书**（缺 boundaries/长度不符/非数值）返回 `INVALID_REQUEST`（`field=structure_certificate`，不可重试） |

⚠ **两条容易踩的失败条件**：
1. **整个网格全是空白**（矩形且行数≥1，但所有 `text` 都为空）⇒ `EXPORT_GRID_INVALID`。空表没有可导出内容，后端显式拒绝而不是写一张空白工作簿。
2. **合并区会吞掉别的格文字**（`spans` 覆盖的格子里有非空文本）⇒ 后端**丢弃这一条合并**、保留每格文字，并把锚点与冲突格标 `needs_review`（`status` 仍是 `ok`）。这不会让导出失败，但客户端应把该表按"结构需人工确认"处理。

## 8. 取消与恢复

V1 没有单独 `cancel`：取消时终止整个后端进程，等待退出，丢弃在途 `request_id`，然后启动新进程并重新 `health`、`warmup`。旧进程未退出前不得发送新请求。

机器可读字段定义见 [backend-api-v1.schema.json](schemas/backend-api-v1.schema.json)，Qt 5.9.6 调用示例见 [qt-qprocess-client](examples/qt-qprocess-client/README.md)。
