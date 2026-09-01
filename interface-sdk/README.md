# 摄像头表格识别接口

接口用于 Qt 软件直接调用摄像头拍照并识别表格，不包含图片导入界面、弹窗或黄色样式。识别结果通过信号返回，可继续修改数据、增加行并导出 CSV。

## 接入

在项目 `.pro` 文件中加入：

```qmake
include(path/to/interface-sdk/qt/ocrtableclient.pri)
```

程序旁必须保留完整后端目录：

```text
应用程序.exe
ocr-runtime/OcrBackend/OcrBackend.exe
ocr-runtime/OcrBackend/models/
ocr-runtime/OcrBackend/_internal/
```

## 基本调用

```cpp
#include "cameraocrclient.h"

QString backend = QDir(QCoreApplication::applicationDirPath())
    .filePath("ocr-runtime/OcrBackend/OcrBackend.exe");

CameraOcrClient *ocr =
    new CameraOcrClient(backend, "D:/ocr-work", this);

connect(ocr, &CameraOcrClient::cameraReadyChanged,
        this, [ocr](bool ready, int, const QString &) {
    if (ready)
        ocr->captureAndRecognize();
});

connect(ocr, &CameraOcrClient::tableRecognized,
        this, [](int rows, int columns,
                 const QJsonArray &cells,
                 const QJsonArray &spans,
                 const QJsonObject &response) {
    // cells[row][column]["text"] 是单元格文字。
});

ocr->startCamera();
```

## 框选一个表格

界面框选后，把区域换算为照片宽高的 `0.0～1.0` 坐标：

```cpp
QString error;
ocr->setTableRegion(QRectF(0.08, 0.12, 0.84, 0.72), &error);
ocr->captureAndRecognize();
```

框选坐标必须是最终照片的归一化坐标，不能直接传屏幕像素坐标。`CameraOcrClient` 会按摄像头照片处理框选图；直接使用底层客户端时，普通照片调用 `recognizeCameraPhoto()`，只有已经完成透视矫正的图片才调用 `recognizeRectifiedTable()`。

接口会在高清原图上裁剪，自动保留少量边缘，并使用 PNG 交给 OCR，避免 JPEG 二次压缩。`resolvedTableRegion(imageSize)` 可用于预览最终实际裁剪范围。框选结果若连续运行异常，接口会保留原图并做一次安全回退；成功结果的 `response["table_region_fallback"]` 为 `true`。不需要框选时调用 `clearTableRegion()`。

## 修改和导出

```cpp
QString error;
ocr->setCellText(2, 3, "修改后的内容", &error);

QJsonArray row;
row.append("15");
row.append("新增数据");
ocr->appendRow(row, &error);

ocr->exportLastCsv("D:/output/result.csv", &error);
```

`tableCells()` 返回当前修改后的完整二维数据。CSV 使用 UTF-8 BOM，可直接用 Excel 打开。风险状态不会阻止接口返回数据或导出；如需判断能否自动发布，调用 `canAutoPublish()`。
