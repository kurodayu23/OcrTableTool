# 摄像头表格识别接口

用于在 Qt 软件中调用摄像头完成表格识别。对外只使用 `CameraOcrClient`。

接口不包含原工具界面，不支持导入图片，不显示颜色标记，也不弹出提示窗口。错误通过信号返回，提示信息为中文。

## 1. 接入

在项目的 `.pro` 文件中加入：

```qmake
include(path/to/interface-sdk/qt/ocrtableclient.pri)
```

程序目录必须保留完整的 OCR 后端：

```text
应用程序.exe
ocr-runtime/OcrBackend/OcrBackend.exe
ocr-runtime/OcrBackend/models/
ocr-runtime/OcrBackend/_internal/
```

## 2. 调用

```cpp
#include "cameraocrclient.h"

QString backend =
    QDir(QCoreApplication::applicationDirPath())
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
    // cells[row][column]["text"] 为单元格文字。
});

connect(ocr, &CameraOcrClient::failed,
        this, [](const QString &errorCode,
                 const QString &message,
                 bool retryable) {
    // message 为中文错误说明。
});

ocr->startCamera();
```

同一画面有多个表格时，先由产品界面通过平板单指拖拽或鼠标拖拽框选其中一个表格，再把相对于最终照片的归一化区域传给接口。框选手势由界面采集，接口统一接收同一套归一化坐标，不需要分别实现触控和鼠标识别路线：

```cpp
QString errorMessage;
ocr->setTableRegion(QRectF(0.08, 0.12, 0.84, 0.72), &errorMessage);
ocr->captureAndRecognize();
```

坐标范围为 `0.0～1.0`。接口会在高清原始照片上裁剪，不会对预览图做低清放大。识别整张照片时调用 `clearTableRegion()`。
框选区域首次识别异常时，接口只会扩大少量框选边缘后重试，不会退回整张多表照片。

## 3. 返回数据

- `rows`：表格行数；
- `columns`：表格列数；
- `cells`：二维单元格数组；
- `cells[row][column].text`：单元格文字；
- `spans`：合并单元格信息；
- `response`：完整识别结果。

## 4. 添加和修改数据

修改单元格：

```cpp
QString errorMessage;
ocr->setCellText(2, 3, "已修改的数据", &errorMessage);
```

行号和列号从 `0` 开始。修改后的单元格直接写入当前识别结果。

新增一行：

```cpp
QJsonArray row;
row.append("1");
row.append("设备A");
row.append("正常");
ocr->appendRow(row, &errorMessage);
```

新增行的单元格数量必须与当前表格列数一致。可通过 `tableCells()` 读取修改后的完整二维数据。

## 5. 导出 CSV

识别完成后调用：

```cpp
QString errorMessage;
bool ok = ocr->exportLastCsv(
    "D:/output/table.csv",
    &errorMessage);
```

CSV 使用 UTF-8 BOM 编码，可直接使用 Excel 打开。风险状态不会阻止 CSV 导出。
导出内容以当前数据为准，包括通过 `setCellText()` 修改的单元格和通过 `appendRow()` 新增的行。

其他方法：`captureAndRecognize()` 拍照识别，`cancel()` 取消识别，`stopCamera()` 关闭摄像头。
