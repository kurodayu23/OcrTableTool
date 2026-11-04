// GUI 回归测试。
//
// 覆盖范围：主窗口空布局、图片预览与缩放、识别失败提示、已有结果发布、
// 结构证书校验（缺失 / 越界）、换行内容不被裁剪、哨兵置信度不误报、
// 真实跑一次识别、关闭时停止自有 worker，以及"识别之后关闭主窗口不崩溃"
// 的回归锁（2026-10-23 修掉的 0xC0000374 堆损坏）。
//
// 白盒断言需要读取 MainWindow / ImagePreview 的私有**数据成员**，
// 因此在包含产品头文件之前临时把 private 打开成 public。
// 必须先把要用到的 Qt 头文件全部包含进来（Qt 头文件里的 private 不能被改写），
// 再打开这个开关，否则 Qt 自身的类定义会被破坏。
//
// 注意：MSVC 会把成员函数的访问级别编进符号名（public=Q，private=A）。
// 所以 private -> public 只对**数据成员**安全（数据成员不参与符号名）；
// 直接调用私有成员函数时，测试 TU 会去找 public 修饰名（QEAA...），
// 而产品编译产物定义的是 private 修饰名（AEAA...），链接必然 LNK2019。
// 私有成员函数一律改走下面两个通道：
//   - 私有槽：QMetaObject::invokeMethod 直接触发（访问级别由元对象绕过）。
//   - 私有非槽：publishRecognitionResult 改由私有槽 backendSucceeded 代劳
//     （它内部在"用户已请求显示"分支上正是调用 publishRecognitionResult）；
//     pendingReviewCount 元对象调用不了，改读私有**数据成员**
//     m_resultSummaryLabel->text()（产品格式："%1 行 × %2 列 · %3 项待确认"）。

#include <QtTest>

#include <QApplication>
#include <QByteArray>
#include <QCameraImageCapture>
#include <QColor>
#include <QCoreApplication>
#include <QDialog>
#include <QDir>
#include <QElapsedTimer>
#include <QEventLoop>
#include <QFile>
#include <QFileInfo>
#include <QFontMetrics>
#include <QFrame>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QMainWindow>
#include <QPainter>
#include <QPixmap>
#include <QObject>
#include <QPoint>
#include <QProcess>
#include <QProgressBar>
#include <QPushButton>
#include <QRect>
#include <QSignalSpy>
#include <QStackedWidget>
#include <QTableWidget>
#include <QTableWidgetItem>
#include <QTemporaryDir>
#include <QTimer>
#include <QVariant>
#include <QWidget>

#define private public
#include "backendrunner.h"
#include "cameracapturedialog.h"
#include "imagepreview.h"
#include "imageviewerdialog.h"
#include "mainwindow.h"
#undef private

#include "ocrtableclient.h"
#include "tabledata.h"

// 产品文件导出的自由函数（外部链接）。单元测试直接声明并调用它们，
// 绕开 MSVC 把访问级别编进符号名的问题（见文件头说明）：私有成员函数在
// 产品编译产物里的符号名带访问级别修饰，从测试 TU 调用必然 LNK2019。
namespace OcrTableToolGui {
QJsonObject parseBackendResponse(const QByteArray &output, QString *errorMessage);
bool isSafeLocalImagePath(const QString &path);
// 拍照保存路径解析（cameracapturedialog.cpp 导出）。目录建不出来时必须返回
// 空串并给出原因，不再返回一个指向不存在目录的路径。
QString nextCapturePathIn(const QString &baseDirectory, QString *failureReason);
}

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace {

// 与 tests/tst_core.cpp 保持一致：构建目录既可能在源码树内部（Qt Creator
// shadow build），也可能在源码树外部（本机统一放 AP7000-Build-Directories）。
// 两种布局下「相对 exe 往上数固定层数」都会指到错误的根，改为逐级向上找项目根标记。
const int kProjectRootSearchLevels = 8;

// 项目根标记：ocr-table-tool.pro（工程文件）或 .venv/Scripts/python.exe（解释器）。
QString projectRootMarker(const QDir &directory)
{
    if (QFileInfo::exists(directory.filePath(QStringLiteral("ocr-table-tool.pro"))))
        return QFileInfo(directory.absolutePath()).absoluteFilePath();
    if (QFileInfo::exists(directory.filePath(QStringLiteral(".venv/Scripts/python.exe"))))
        return QFileInfo(directory.absolutePath()).absoluteFilePath();
    return QString();
}

QString projectRootByWalkingUp(const QString &startDirectory)
{
    if (startDirectory.isEmpty())
        return QString();

    QDir directory(startDirectory);
    for (int level = 0; level < kProjectRootSearchLevels; ++level) {
        const QString marker = projectRootMarker(directory);
        if (!marker.isEmpty())
            return marker;
        if (!directory.cdUp())
            break;
    }
    return QString();
}

// 构建目录在源码树之外时，源码树不是构建目录的祖先，
// 只能在祖先目录的直接子目录里找（源码树与构建目录互为兄弟）。
QString projectRootByScanningSiblings(const QString &startDirectory)
{
    if (startDirectory.isEmpty())
        return QString();

    QDir directory(startDirectory);
    for (int level = 0; level < kProjectRootSearchLevels; ++level) {
        const QFileInfoList children =
                directory.entryInfoList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
        for (int index = 0; index < children.size(); ++index) {
            const QString marker = projectRootMarker(QDir(children.at(index).absoluteFilePath()));
            if (!marker.isEmpty())
                return marker;
        }
        if (!directory.cdUp())
            break;
    }
    return QString();
}

// 编译期源文件位置能精确指向源码树，用来兜住「构建目录与源码树既无祖先
// 也无兄弟关系」的情况（例如构建目录在别的盘符）。
QString compiledSourceDirectory()
{
    const QString sourceFile = QString::fromLocal8Bit(__FILE__);
    if (sourceFile.isEmpty())
        return QString();

    const QStringList bases = QStringList()
            << QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral(".."))
            << QCoreApplication::applicationDirPath()
            << QDir::currentPath();
    for (int index = 0; index < bases.size(); ++index) {
        const QFileInfo candidate(QDir(bases.at(index)).absoluteFilePath(sourceFile));
        if (candidate.exists())
            return candidate.absolutePath();
    }
    return QString();
}

QString findProjectRoot(const QStringList &startDirectories)
{
    for (int index = 0; index < startDirectories.size(); ++index) {
        const QString root = projectRootByWalkingUp(startDirectories.at(index));
        if (!root.isEmpty())
            return root;
    }
    for (int index = 0; index < startDirectories.size(); ++index) {
        const QString root = projectRootByScanningSiblings(startDirectories.at(index));
        if (!root.isEmpty())
            return root;
    }
    return QString();
}

QString findProjectRoot()
{
    QStringList starts;
    starts << QCoreApplication::applicationDirPath();
    const QString sourceDirectory = compiledSourceDirectory();
    if (!sourceDirectory.isEmpty())
        starts << sourceDirectory;
    return findProjectRoot(starts);
}

// 桩后端要用真实解释器跑起来，优先用测试专用环境变量，其次用项目自带的 .venv。
QString testPythonPath()
{
    const QString fromEnvironment =
            QString::fromLocal8Bit(qgetenv("OCR_TABLE_TEST_PYTHON"));
    if (!fromEnvironment.isEmpty() && QFileInfo::exists(fromEnvironment))
        return fromEnvironment;

    const QString projectRoot = findProjectRoot();
    if (!projectRoot.isEmpty()) {
        const QString projectPython =
                QFileInfo(QDir(projectRoot).filePath(QStringLiteral(".venv/Scripts/python.exe")))
                        .absoluteFilePath();
        if (QFileInfo::exists(projectPython))
            return projectPython;
    }
    return QStringLiteral("python");
}

// 桩后端的动作由模式文件决定，父进程随时可以切换，
// 同一个脚本既能快速返回结果，也能挂住模拟长任务。
QString stubModeFilePath()
{
    return QString::fromLocal8Bit(qgetenv("OCR_TABLE_TEST_STUB_MODE_FILE"));
}

bool writeStubMode(const QString &modeFilePath, const QString &mode)
{
    if (modeFilePath.isEmpty())
        return false;
    QFile file(modeFilePath);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text))
        return false;
    const QByteArray data = mode.toUtf8();
    const bool written = file.write(data) == data.size();
    file.close();
    return written;
}

// 写一个只依赖标准库的桩后端：
//   ok    -> 返回一份合法的识别结果（含哨兵置信度格）
//   error -> 返回结构化错误
//   hang  -> 读掉请求后一直睡，模拟耗时任务
QString writeStubBackend(const QString &directory)
{
    const QString scriptPath =
            QDir(directory).filePath(QStringLiteral("stub_ocr_backend.py"));
    QFile script(scriptPath);
    if (!script.open(QIODevice::WriteOnly | QIODevice::Text))
        return QString();

    const QByteArray source = QStringLiteral(
        "import json, os, sys, time\n"
        "mode_path = os.environ.get('OCR_TABLE_TEST_STUB_MODE_FILE', '')\n"
        "mode = 'ok'\n"
        "if mode_path and os.path.exists(mode_path):\n"
        "    mode = open(mode_path, 'r', encoding='utf-8').read().strip()\n"
        "payload = sys.stdin.read()\n"
        "request = json.loads(payload) if payload.strip() else {}\n"
        "request_id = request.get('request_id', 0)\n"
        "if mode == 'hang':\n"
        "    time.sleep(120)\n"
        "    sys.exit(0)\n"
        "if mode == 'error':\n"
        "    print(json.dumps({'protocol': 1, 'status': 'error',\n"
        "                      'request_id': request_id,\n"
        "                      'message': 'stub backend failure',\n"
        "                      'error_code': 'STUB_FAILURE',\n"
        "                      'retryable': False}), flush=True)\n"
        "    sys.exit(0)\n"
        // ok_exit_nonzero：先打印一份合法的 ok 响应，再以非 0 退出码退出。
        // 这是"响应 ok + 进程没正常收尾"，界面过去会判成功（findings/61 F3）。
        "if mode == 'ok_exit_nonzero':\n"
        "    print(json.dumps({'protocol': 1, 'status': 'ok',\n"
        "                      'request_id': request_id}), flush=True)\n"
        "    sys.exit(3)\n"
        // fail_once：第一次请求返回可重试的结构化错误，之后一律返回 ok。
        // 用来验证自动重试仍在、但只用剩余预算（findings B1）。
        "if mode == 'fail_once':\n"
        "    counter = mode_path + '.count'\n"
        "    seen = os.path.exists(counter)\n"
        "    open(counter, 'w').write('1')\n"
        "    if not seen:\n"
        "        print(json.dumps({'protocol': 1, 'status': 'error',\n"
        "                          'request_id': request_id,\n"
        "                          'message': 'stub first attempt failure',\n"
        "                          'error_code': 'STUB_RETRYABLE',\n"
        "                          'retryable': True}), flush=True)\n"
        "        sys.exit(0)\n"
        "    mode = 'ok'\n"
        "cells = [[{'text': 'ORDER-1', 'confidence': 0.91, 'needs_review': False},\n"
        "          {'text': '122-5686', 'confidence': 0.77, 'needs_review': False,\n"
        "           'confidence_sentinel': True},\n"
        "          {'text': 'LATE', 'confidence': 0.70, 'needs_review': False}]]\n"
        "response = {'protocol': 1, 'status': 'ok', 'request_id': request_id,\n"
        "            'rows': 1, 'columns': 3, 'cells': cells, 'spans': [],\n"
        "            'publication_blocked': False,\n"
        "            'recognition_state': 'needs_review',\n"
        "            'structure_verified': True,\n"
        "            'structure_certificate': {'version': 1, 'verified': True,\n"
        "                                     'rows': 1, 'columns': 3, 'spans': [],\n"
        "                                     'geometry_hash': 'stub-geometry',\n"
        "                                     'structure_hash': 'stub-structure'},\n"
        "            'review_cell_count': 1}\n"
        "print(json.dumps(response), flush=True)\n"
        "sys.exit(0)\n").toUtf8();
    script.write(source);
    script.close();
    return scriptPath;
}

// 造一张测试用图片。
QString writeTestImage(const QString &directory, const QSize &size)
{
    const QString imagePath = QDir(directory).filePath(QStringLiteral("table-photo.png"));
    QImage image(size, QImage::Format_RGB32);
    image.fill(Qt::white);
    QPainter painter(&image);
    painter.setPen(QColor(20, 40, 80));
    painter.drawRect(8, 8, size.width() - 16, size.height() - 16);
    painter.drawLine(8, size.height() / 2, size.width() - 8, size.height() / 2);
    painter.end();
    if (!image.save(imagePath, "PNG"))
        return QString();
    return imagePath;
}

QJsonObject makeCell(const QString &text,
                     double confidence,
                     bool needsReview,
                     bool confidenceSentinel = false)
{
    QJsonObject cell;
    cell.insert(QStringLiteral("text"), text);
    cell.insert(QStringLiteral("confidence"), confidence);
    cell.insert(QStringLiteral("needs_review"), needsReview);
    if (confidenceSentinel)
        cell.insert(QStringLiteral("confidence_sentinel"), true);
    return cell;
}

// 造一份能被 MainWindow::publishRecognitionResult 接受的结果。
// structure_verified 为真时必须附带与网格一致的校验证书。
QJsonObject makeResponse(const QJsonArray &rows,
                         int columns,
                         const QJsonArray &spans = QJsonArray())
{
    QJsonObject response;
    response.insert(QStringLiteral("protocol"), 1);
    response.insert(QStringLiteral("status"), QStringLiteral("ok"));
    response.insert(QStringLiteral("rows"), rows.size());
    response.insert(QStringLiteral("columns"), columns);
    response.insert(QStringLiteral("cells"), rows);
    response.insert(QStringLiteral("spans"), spans);
    response.insert(QStringLiteral("publication_blocked"), false);
    response.insert(QStringLiteral("recognition_state"), QStringLiteral("needs_review"));
    response.insert(QStringLiteral("structure_verified"), true);
    response.insert(QStringLiteral("review_cell_count"), 0);

    QJsonObject certificate;
    certificate.insert(QStringLiteral("version"), 1);
    certificate.insert(QStringLiteral("verified"), true);
    certificate.insert(QStringLiteral("rows"), rows.size());
    certificate.insert(QStringLiteral("columns"), columns);
    certificate.insert(QStringLiteral("spans"), spans);
    certificate.insert(QStringLiteral("geometry_hash"), QStringLiteral("stub-geometry"));
    certificate.insert(QStringLiteral("structure_hash"), QStringLiteral("stub-structure"));
    response.insert(QStringLiteral("structure_certificate"), certificate);
    return response;
}

// 校验证书被拒时，界面必须给出可读提示且不发布结果。
void verifyResultRejected(MainWindow &window, const QString &expectedFragment)
{
    QCOMPARE(window.m_table->rowCount(), 0);
    QCOMPARE(window.m_table->columnCount(), 0);
    QVERIFY(window.m_spans.isEmpty());
    QCOMPARE(window.m_resultStack->currentIndex(), 0);
    QVERIFY(!window.m_csvButton->isEnabled());
    QVERIFY(!window.m_xlsxButton->isEnabled());

    // 产品把**具体**拒绝原因放在状态栏的 tooltip（"详细信息"）里，
    // 状态栏正文只显示通用兜底文案
    // "识别失败，已保留原始错误信息。请展开"详细信息"查看后重试。"，
    // 所以两个位置都算用户可见，断言同时看两处。
    const QString statusText = window.m_statusLabel->text();
    const QString detailText = window.m_statusLabel->toolTip();
    QVERIFY2(statusText.contains(expectedFragment) || detailText.contains(expectedFragment),
             qPrintable(QStringLiteral("expected fragment %1; status=%2; tooltip=%3")
                            .arg(expectedFragment, statusText, detailText)));
    QVERIFY2(statusText.contains(QStringLiteral("识别未完成")),
             qPrintable(QStringLiteral("status=%1; tooltip=%2").arg(statusText, detailText)));
}

// 发布一份识别结果。
// MainWindow::publishRecognitionResult 是私有非槽成员函数，不能直接调用（见文件头说明）。
// 私有槽 backendSucceeded 在“用户已请求显示”分支上正是调用 publishRecognitionResult，
// 语义与直接调用等价。
bool publishRecognitionResult(MainWindow &window, const QJsonObject &response)
{
    // 走“用户已请求显示”分支，否则结果只会被暂存而不会发布到表格。
    window.m_recognitionDisplayRequested = true;
    return QMetaObject::invokeMethod(&window, "backendSucceeded", Qt::DirectConnection,
                                     Q_ARG(QString, QStringLiteral("recognize")),
                                     Q_ARG(QJsonObject, response));
}

// 触发一次识别。recognizeImage 是私有槽，用元对象触发。
bool triggerRecognition(MainWindow &window)
{
    return QMetaObject::invokeMethod(&window, "recognizeImage", Qt::DirectConnection);
}

// 进程是否还活着（Windows 上按进程句柄判断，跨盘符/跨会话都可靠）。
bool processIsAlive(qint64 processId)
{
#ifdef Q_OS_WIN
    if (processId <= 0)
        return false;
    HANDLE handle = OpenProcess(SYNCHRONIZE, FALSE, static_cast<DWORD>(processId));
    if (!handle)
        return false;
    const DWORD waitResult = WaitForSingleObject(handle, 0);
    CloseHandle(handle);
    return waitResult == WAIT_TIMEOUT;
#else
    Q_UNUSED(processId);
    return false;
#endif
}

void clearStubEnvironment()
{
    qunsetenv("OCR_TABLE_BACKEND");
    qunsetenv("OCR_TABLE_PYTHON");
    qunsetenv("OCR_TABLE_TEST_STUB_MODE_FILE");
    qunsetenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY");
}

} // namespace

class GuiTests : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase();
    void emptyLayout();
    void imageViewerPresentation();
    void processingAndFailure();
    void existingResult();
    void rejectIncompleteCertificate();
    void rejectInvalidCertificateBoundaries();
    void geometryDoesNotClipWrappedContent();
    void freshRecognition();
    void closingStopsOwnedWorker();
    void sentinelConfidenceIsNotFlagged();
    void closingAfterRecognitionExitsCleanly();
    void sdkCsvExportNeutralizesSpreadsheetFormulas();
    void backendResponseParsingDistinguishesEmptyObjectFromGarbage();
    void rectifiedImagePathRejectsRemoteAndTraversalPaths();
    // 2026-10-28 新增：F3 / F5 / F6 / B1 四条已披露 GUI 缺陷的回归用例。
    void backendExitCodeIsNotIgnored();
    void busyOrStoppingStartClickIsNotSwallowed();
    void exportRequestCarriesStructureCertificate();
    void retryStaysWithinOneRecognitionBudget();
    // 2026-11-02 合流补入：仓库侧 A23 加固带来的 6 条 GUI 回归用例。
    // ⚠ 它们的**定义**已随合并进入本文件，但类内声明曾随冲突块一起被丢弃
    //   ⇒ moc 不认 ⇒ 编译报 C2039「不是 GuiTests 的成员」。
    void statusBarElapsedMatchesRecognitionBudget();
    void previewScalesToDevicePixelRatio();
    void previewResizeReusesSingleScaleSource();
    void imageViewerDialogKeepsCallerTitle();
    void cameraErrorOffersRetry();
    void capturePathReportsDirectoryFailure();
};

void GuiTests::initTestCase()
{
    // 桩后端需要真实解释器。这里只做环境体检，缺解释器时由具体用例报错。
    const QString python = testPythonPath();
    QVERIFY2(!python.isEmpty(),
             "no python interpreter found for the stub backend");
    QVERIFY2(QFileInfo::exists(python), qPrintable(python));
}

void GuiTests::sdkCsvExportNeutralizesSpreadsheetFormulas()
{
    // 公开 SDK 的 exportCsv() 必须与界面侧 CsvExporter::escaped() 同口径：
    // 以 = + - @ 开头的单元格文本前置单引号。否则集成方拿 SDK 导出的 CSV
    // 在 Excel 里打开时，公式会被执行。
    QJsonObject formula;
    formula.insert(QStringLiteral("text"), QStringLiteral("=cmd|'/c calc'!A0"));
    QJsonObject withComma;
    withComma.insert(QStringLiteral("text"), QStringLiteral("A,B"));
    QJsonObject plain;
    plain.insert(QStringLiteral("text"), QStringLiteral("OK"));
    QJsonArray columns;
    columns.append(formula);
    columns.append(withComma);
    columns.append(plain);
    QJsonArray cells;
    cells.append(columns);

    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    const QString path = directory.filePath(QStringLiteral("sdk-formula.csv"));
    QString errorMessage;
    QVERIFY2(OcrTableClient::exportCsv(path, cells, &errorMessage),
             qPrintable(errorMessage));

    QFile file(path);
    QVERIFY(file.open(QIODevice::ReadOnly));
    const QByteArray actual = file.readAll();
    const QByteArray expected("\xEF\xBB\xBF'=cmd|'/c calc'!A0,\"A,B\",OK\r\n");
    QCOMPARE(actual, expected);
}

void GuiTests::emptyLayout()
{
    MainWindow window;
    window.show();
    QTest::qWait(80);

    QCOMPARE(window.windowTitle(), QStringLiteral("图片转表格"));
    QVERIFY(!window.windowIcon().isNull());
    QVERIFY(window.centralWidget() != nullptr);

    // 控件齐全：任何一条缺失都会让用户卡在某个不可达的状态上。
    QVERIFY(window.m_openButton != nullptr);
    QVERIFY(window.m_cropButton != nullptr);
    QVERIFY(window.m_cameraButton != nullptr);
    QVERIFY(window.m_recognizeButton != nullptr);
    QVERIFY(window.m_cancelButton != nullptr);
    QVERIFY(window.m_csvButton != nullptr);
    QVERIFY(window.m_xlsxButton != nullptr);
    QVERIFY(window.m_table != nullptr);
    QVERIFY(window.m_originalPreview != nullptr);
    QVERIFY(window.m_rectifiedPreview != nullptr);
    QVERIFY(window.m_progress != nullptr);
    QVERIFY(window.m_statusLabel != nullptr);
    QVERIFY(window.m_readyStateLabel != nullptr);
    QVERIFY(window.m_resultSummaryLabel != nullptr);
    QVERIFY(window.m_reviewNotice != nullptr);

    // 空布局：预览区停在拖拽引导页，结果区停在空状态页。
    QCOMPARE(window.m_previewStack->currentIndex(), 0);
    QCOMPARE(window.m_resultStack->currentIndex(), 0);
    QCOMPARE(window.m_table->rowCount(), 0);
    QCOMPARE(window.m_table->columnCount(), 0);
    QVERIFY(window.m_originalPreview->image().isNull());
    QVERIFY(window.m_rectifiedPreview->image().isNull());
    QVERIFY(!window.m_fileNameLabel->text().isEmpty());
    QVERIFY(!window.m_readyStateLabel->text().isEmpty());

    // 没有图片时不能识别、不能导出，也不能显示进度条。
    QVERIFY(!window.m_recognizeButton->isEnabled());
    QVERIFY(!window.m_csvButton->isEnabled());
    QVERIFY(!window.m_xlsxButton->isEnabled());
    QVERIFY(!window.m_progress->isVisible());
    QVERIFY(!window.m_cancelButton->isVisible());

    // 打不开的图片必须给出提示而不是崩掉。
    QVERIFY(!window.loadImageFile(QStringLiteral("missing-file-for-ui-test.png")));
    QVERIFY(window.m_statusLabel->text().contains(QStringLiteral("无法打开图片")));
    QVERIFY(window.m_openButton->isEnabled());
    QCOMPARE(window.m_table->rowCount(), 0);
}

void GuiTests::imageViewerPresentation()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString imagePath = writeTestImage(temporary.path(), QSize(640, 480));
    QVERIFY(!imagePath.isEmpty());

    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    QVERIFY(window.loadImageFile(imagePath));
    QTest::qWait(80);

    // 预览区切到图片页，图片按原始像素载入。
    QCOMPARE(window.m_previewStack->currentIndex(), 1);
    QVERIFY(!window.m_originalPreview->image().isNull());
    QCOMPARE(window.m_originalPreview->image().size(), QSize(640, 480));
    QCOMPARE(window.m_fileNameLabel->text(), QFileInfo(imagePath).fileName());

    // 缩放预览必须落在控件内并且保持宽高比。
    const QRect inner = window.m_originalPreview->rect().adjusted(8, 8, -8, -8);
    QVERIFY(!inner.isEmpty());
    const QPixmap scaled = window.m_originalPreview->m_scaledPreview;
    QVERIFY(!scaled.isNull());
    QVERIFY(scaled.width() <= inner.width());
    QVERIFY(scaled.height() <= inner.height());
    const double sourceRatio = 640.0 / 480.0;
    const double scaledRatio = double(scaled.width()) / double(scaled.height());
    QVERIFY2(qAbs(sourceRatio - scaledRatio) < 0.02,
             qPrintable(QStringLiteral("scaled ratio %1").arg(scaledRatio)));

    // 单击预览区弹出大图查看器（模态），这里用定时器把它关掉。
    QWidget *viewer = nullptr;
    QTimer::singleShot(200, &window, [&viewer]() {
        viewer = QApplication::activeModalWidget();
        if (viewer)
            viewer->close();
    });
    QTest::mouseClick(window.m_originalPreview, Qt::LeftButton, Qt::NoModifier, QPoint(20, 20));
    QVERIFY2(viewer != nullptr, "image viewer dialog did not open");
    QVERIFY(viewer->isWindow());
}

void GuiTests::processingAndFailure()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    QVERIFY(writeStubMode(modeFile, QStringLiteral("error")));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));

    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));
    qputenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY", "1");

    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    QVERIFY(!imagePath.isEmpty());

    {
        MainWindow window;
        window.show();
        QSignalSpy failureSpy(window.m_backend,
                              SIGNAL(requestFailed(QString,QString,QString,bool)));
        QVERIFY(window.loadImageFile(imagePath));

        // 后端报错后界面必须收敛到"空闲 + 可读提示"，不能一直转圈。
        QTRY_VERIFY_WITH_TIMEOUT(failureSpy.count() >= 1, 20000);
        QTRY_VERIFY_WITH_TIMEOUT(!window.m_backend->isRunning(), 20000);
        QTRY_VERIFY_WITH_TIMEOUT(window.m_statusLabel->text().contains(
                                     QStringLiteral("识别未完成")), 20000);

        const QString status = window.m_statusLabel->text();
        QVERIFY2(!status.isEmpty(), "no readable failure message");
        QVERIFY(status.contains(QStringLiteral("识别未完成")));
        QVERIFY(!window.m_progress->isVisible());
        QVERIFY(window.m_recognizeButton->isVisible());
        QVERIFY(window.m_recognizeButton->isEnabled());
        // 失败结果不能留下可导出的旧表格。
        QCOMPARE(window.m_table->rowCount(), 0);
        QVERIFY(!window.m_csvButton->isEnabled());
    }
    clearStubEnvironment();
}

void GuiTests::existingResult()
{
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(QStringLiteral("ORDER-1"), 0.95, false)
                             << makeCell(QString(), 0.95, false)
                             << makeCell(QString(), 0.95, false));
    rows.append(QJsonArray() << makeCell(QStringLiteral("A"), 0.93, false)
                             << makeCell(QStringLiteral("B"), 0.94, false)
                             << makeCell(QStringLiteral("C"), 0.92, false));
    QJsonArray spans;
    QJsonObject span;
    span.insert(QStringLiteral("row"), 0);
    span.insert(QStringLiteral("column"), 0);
    span.insert(QStringLiteral("row_span"), 1);
    span.insert(QStringLiteral("column_span"), 3);
    span.insert(QStringLiteral("role"), QStringLiteral("title"));
    spans.append(span);

    const QJsonObject response = makeResponse(rows, 3, spans);
    QVERIFY(publishRecognitionResult(window, response));

    QCOMPARE(window.m_table->rowCount(), 2);
    QCOMPARE(window.m_table->columnCount(), 3);
    QCOMPARE(window.m_table->item(0, 0)->text(), QStringLiteral("ORDER-1"));
    QCOMPARE(window.m_table->item(1, 2)->text(), QStringLiteral("C"));
    QCOMPARE(window.m_spans.size(), 1);
    QCOMPARE(window.m_table->columnSpan(0, 0), 3);
    QCOMPARE(window.m_table->rowSpan(0, 0), 1);

    // 结果区切到表格页，并给出可导出的状态。
    QCOMPARE(window.m_resultStack->currentIndex(), 1);
    QVERIFY(window.m_reviewNotice->isVisible());
    QVERIFY(window.m_resultSummaryLabel->text().contains(
        QStringLiteral("2 行 × 3 列")));
    QVERIFY(window.m_csvButton->isEnabled());
    QVERIFY(window.m_statusLabel->text().contains(QStringLiteral("识别完成")));
    // 待复核数量由私有方法 pendingReviewCount() 统计，但它不是槽、元对象调用不了；
    // 改读私有数据成员 m_resultSummaryLabel（产品格式："%1 行 × %2 列 · %3 项待确认"）。
    QVERIFY2(window.m_resultSummaryLabel->text().contains(QStringLiteral("0 项待确认")),
             qPrintable(window.m_resultSummaryLabel->text()));
}

void GuiTests::rejectIncompleteCertificate()
{
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);
    // 让失败走"用户已请求显示"的分支，提示才会直接落到状态栏上。
    window.m_recognitionDisplayRequested = true;

    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(QStringLiteral("A"), 0.93, false)
                             << makeCell(QStringLiteral("B"), 0.94, false));

    // 情况一：声明结构已确认，却没有校验证书。
    QJsonObject withoutCertificate = makeResponse(rows, 2);
    withoutCertificate.remove(QStringLiteral("structure_certificate"));
    QVERIFY(publishRecognitionResult(window, withoutCertificate));
    verifyResultRejected(window, QStringLiteral("缺少校验证书"));

    // 情况二：证书缺字段（没有 structure_hash / geometry_hash）。
    QJsonObject incomplete = makeResponse(rows, 2);
    QJsonObject incompleteCertificate =
            incomplete.value(QStringLiteral("structure_certificate")).toObject();
    incompleteCertificate.remove(QStringLiteral("structure_hash"));
    incompleteCertificate.remove(QStringLiteral("geometry_hash"));
    incomplete.insert(QStringLiteral("structure_certificate"), incompleteCertificate);
    QVERIFY(publishRecognitionResult(window, incomplete));
    verifyResultRejected(window, QStringLiteral("校验证书与识别结果不一致"));

    // 情况三：结构未确认，却带着证书且允许发布。
    QJsonObject unverifiedWithCertificate = makeResponse(rows, 2);
    unverifiedWithCertificate.insert(QStringLiteral("structure_verified"), false);
    QVERIFY(publishRecognitionResult(window, unverifiedWithCertificate));
    verifyResultRejected(window, QStringLiteral("结构未确认的结果"));

    // 情况四：结构未确认且没有证书，但发布状态却是"可发布"。
    QJsonObject unverifiedPublished = makeResponse(rows, 2);
    unverifiedPublished.insert(QStringLiteral("structure_verified"), false);
    unverifiedPublished.remove(QStringLiteral("structure_certificate"));
    QVERIFY(publishRecognitionResult(window, unverifiedPublished));
    verifyResultRejected(window, QStringLiteral("结构未确认的结果"));
}

void GuiTests::rejectInvalidCertificateBoundaries()
{
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);
    window.m_recognitionDisplayRequested = true;

    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(QStringLiteral("A"), 0.93, false)
                             << makeCell(QStringLiteral("B"), 0.94, false));
    rows.append(QJsonArray() << makeCell(QStringLiteral("C"), 0.93, false)
                             << makeCell(QStringLiteral("D"), 0.94, false));

    // 情况一：证书里的行数越界（比实际网格多一行）。
    QJsonObject tooManyRows = makeResponse(rows, 2);
    QJsonObject rowsCertificate =
            tooManyRows.value(QStringLiteral("structure_certificate")).toObject();
    rowsCertificate.insert(QStringLiteral("rows"), 3);
    tooManyRows.insert(QStringLiteral("structure_certificate"), rowsCertificate);
    QVERIFY(publishRecognitionResult(window, tooManyRows));
    verifyResultRejected(window, QStringLiteral("校验证书与识别结果不一致"));

    // 情况二：证书里的列数越界。
    QJsonObject tooManyColumns = makeResponse(rows, 2);
    QJsonObject columnsCertificate =
            tooManyColumns.value(QStringLiteral("structure_certificate")).toObject();
    columnsCertificate.insert(QStringLiteral("columns"), 5);
    tooManyColumns.insert(QStringLiteral("structure_certificate"), columnsCertificate);
    QVERIFY(publishRecognitionResult(window, tooManyColumns));
    verifyResultRejected(window, QStringLiteral("校验证书与识别结果不一致"));

    // 情况三：证书版本号不是 1。
    QJsonObject wrongVersion = makeResponse(rows, 2);
    QJsonObject versionCertificate =
            wrongVersion.value(QStringLiteral("structure_certificate")).toObject();
    versionCertificate.insert(QStringLiteral("version"), 2);
    wrongVersion.insert(QStringLiteral("structure_certificate"), versionCertificate);
    QVERIFY(publishRecognitionResult(window, wrongVersion));
    verifyResultRejected(window, QStringLiteral("校验证书与识别结果不一致"));

    // 情况四：证书没有 verified = true。
    QJsonObject notVerified = makeResponse(rows, 2);
    QJsonObject verifiedCertificate =
            notVerified.value(QStringLiteral("structure_certificate")).toObject();
    verifiedCertificate.insert(QStringLiteral("verified"), false);
    notVerified.insert(QStringLiteral("structure_certificate"), verifiedCertificate);
    QVERIFY(publishRecognitionResult(window, notVerified));
    verifyResultRejected(window, QStringLiteral("校验证书与识别结果不一致"));

    // 情况五：证书里的合并关系与结果里的不一致。
    QJsonObject mismatchedSpans = makeResponse(rows, 2);
    QJsonObject spansCertificate =
            mismatchedSpans.value(QStringLiteral("structure_certificate")).toObject();
    QJsonArray certificateSpans;
    QJsonObject certificateSpan;
    certificateSpan.insert(QStringLiteral("row"), 0);
    certificateSpan.insert(QStringLiteral("column"), 0);
    certificateSpan.insert(QStringLiteral("row_span"), 1);
    certificateSpan.insert(QStringLiteral("column_span"), 2);
    certificateSpans.append(certificateSpan);
    spansCertificate.insert(QStringLiteral("spans"), certificateSpans);
    mismatchedSpans.insert(QStringLiteral("structure_certificate"), spansCertificate);
    QVERIFY(publishRecognitionResult(window, mismatchedSpans));
    verifyResultRejected(window, QStringLiteral("校验证书与识别结果不一致"));

    // 全部被拒之后，界面仍然停在可继续操作的空状态。
    QCOMPARE(window.m_table->rowCount(), 0);
    QVERIFY(!window.m_statusLabel->text().isEmpty());
}

void GuiTests::geometryDoesNotClipWrappedContent()
{
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    const QString wrapped = QStringLiteral("第一行内容\n第二行内容");
    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(wrapped, 0.93, false)
                             << makeCell(QStringLiteral("B"), 0.94, false));
    const QJsonObject response = makeResponse(rows, 2);
    QVERIFY(publishRecognitionResult(window, response));

    QCOMPARE(window.m_table->rowCount(), 1);
    QTableWidgetItem *item = window.m_table->item(0, 0);
    QVERIFY(item != nullptr);

    // 内容不能被裁掉：换行符和两行文字都要完整保留。
    QCOMPARE(item->text(), wrapped);
    QVERIFY(item->text().contains(QLatin1Char('\n')));
    QVERIFY(window.m_table->wordWrap());

    // 行高必须容得下两行，不能按一行的高度把第二行裁掉。
    //
    // 口径修订（2026-10-28）：原断言拿 **QFontMetrics 的字形墨迹盒**
    // （boundingRect）与 **Qt 的文本布局盒**（rowHeight）直接比大小 ——
    // 这是两个不同的度量体系。多行文本时墨迹盒 = 2 × height + **行间 leading**：
    //   实测宋体（height=18、leading=3）：墨迹盒 39、布局盒 37 ⇒ 37 < 39 恒失败；
    //   leading=0 的字体（微软雅黑等）：墨迹盒 == 2 × height ⇒ 恒通过。
    // ⇒ 该断言的结果随**本机 UI 字体**翻转，与产品代码无关：同一份二进制
    //   2026-10-27 02:32 通过、2026-10-28 02:32 失败（三个不同构建逐位相同）。
    // 行与行之间的空白**不是**行高不足，量它属于口径错配。
    // 正确口径：行高须容得下**两行墨迹本身**（行间空白不算）。
    const QFontMetrics metrics(item->font());
    const int readableHeight =
            metrics.boundingRect(QRect(0, 0, 10000, 10000), Qt::TextWordWrap, wrapped).height();
    const int requiredHeight = readableHeight - metrics.leading();
    QVERIFY2(requiredHeight > metrics.height(),
             "wrapped text should occupy more than one line");
    // QTableView::sizeHintForRow 是 **protected**，Qt 自己的类定义已经在上面
    // #define private public 之前就固定了（改不动），所以这里只用公开 API
    // rowHeight() 断言行高 —— 它就是 sizeHintForRow 的最终落地结果。
    QVERIFY2(window.m_table->rowHeight(0) >= requiredHeight,
             qPrintable(QStringLiteral("row height %1 < required height %2 (ink box %3, leading %4)")
                            .arg(window.m_table->rowHeight(0))
                            .arg(requiredHeight)
                            .arg(readableHeight)
                            .arg(metrics.leading())));
}

void GuiTests::freshRecognition()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    QVERIFY(writeStubMode(modeFile, QStringLiteral("ok")));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));

    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));
    qputenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY", "1");

    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    QVERIFY(!imagePath.isEmpty());

    {
        MainWindow window;
        window.show();
        QSignalSpy successSpy(window.m_backend,
                              SIGNAL(requestSucceeded(QString,QJsonObject)));
        QVERIFY(window.loadImageFile(imagePath));

        // 真跑一次识别：等结果真正进表格。
        QTRY_VERIFY_WITH_TIMEOUT(window.m_table->rowCount() > 0, 30000);
        QCOMPARE(successSpy.count(), 1);
        QCOMPARE(window.m_table->rowCount(), 1);
        QCOMPARE(window.m_table->columnCount(), 3);
        QCOMPARE(window.m_table->item(0, 0)->text(), QStringLiteral("ORDER-1"));
        QCOMPARE(window.m_table->item(0, 1)->text(), QStringLiteral("122-5686"));
        QCOMPARE(window.m_table->item(0, 2)->text(), QStringLiteral("LATE"));
        QVERIFY(window.m_statusLabel->text().contains(QStringLiteral("识别完成")));
        QVERIFY(!window.m_backend->isRunning());
    }
    clearStubEnvironment();
}

void GuiTests::closingStopsOwnedWorker()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    QVERIFY(writeStubMode(modeFile, QStringLiteral("hang")));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));

    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));
    qputenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY", "1");

    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    QVERIFY(!imagePath.isEmpty());

    qint64 workerProcessId = 0;
    {
        MainWindow window;
        window.show();
        QVERIFY(window.loadImageFile(imagePath));

        // 等到后端 worker 真的起来了。
        QTRY_VERIFY_WITH_TIMEOUT(window.m_backend->isRunning(), 20000);
        QTRY_VERIFY_WITH_TIMEOUT(window.m_backend->m_process->state() == QProcess::Running,
                                  20000);
        workerProcessId = window.m_backend->m_process->processId();
        QVERIFY(workerProcessId > 0);
        QVERIFY(processIsAlive(workerProcessId));

        // 关闭主窗口：析构里的 m_backend->shutdown() 必须把 worker 停掉。
    }
    QTRY_VERIFY_WITH_TIMEOUT(!processIsAlive(workerProcessId), 15000);
    clearStubEnvironment();
}

void GuiTests::sentinelConfidenceIsNotFlagged()
{
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(QStringLiteral("SENTINEL"), 0.77, false, true)
                             << makeCell(QStringLiteral("MEASURED"), 0.70, false, false)
                             << makeCell(QStringLiteral("CONFIRMED"), 0.95, false, false));
    const QJsonObject response = makeResponse(rows, 3);
    QVERIFY(publishRecognitionResult(window, response));

    QCOMPARE(window.m_table->rowCount(), 1);
    QCOMPARE(window.m_table->columnCount(), 3);

    QTableWidgetItem *sentinelItem = window.m_table->item(0, 0);
    QTableWidgetItem *measuredItem = window.m_table->item(0, 1);
    QTableWidgetItem *confirmedItem = window.m_table->item(0, 2);
    QVERIFY(sentinelItem != nullptr);
    QVERIFY(measuredItem != nullptr);
    QVERIFY(confirmedItem != nullptr);

    // 哨兵格：confidence=0.77 是类别标记，不是实测置信度，不能判为待复核。
    QVERIFY(sentinelItem->data(Qt::UserRole + 3).toBool());
    QVERIFY(!sentinelItem->data(Qt::UserRole + 1).toBool());
    QVERIFY(!sentinelItem->data(Qt::UserRole + 2).toBool());
    QVERIFY(sentinelItem->icon().isNull());

    // 实测低置信格：confidence=0.70 必须判为待复核。
    QVERIFY(!measuredItem->data(Qt::UserRole + 3).toBool());
    QVERIFY(measuredItem->data(Qt::UserRole + 2).toBool());
    QVERIFY(!measuredItem->icon().isNull());

    // 实测高置信格：不标待复核。
    QVERIFY(!confirmedItem->data(Qt::UserRole + 2).toBool());
    QVERIFY(confirmedItem->icon().isNull());

    // 待复核计数只算真正被标记的格：汇总标签上的"1 项待确认"就是用户看到的结果。
    // pendingReviewCount() 是普通私有方法，元对象调用不了，这里读私有数据成员
    // m_resultSummaryLabel（产品格式："%1 行 × %2 列 · %3 项待确认"）。
    QVERIFY2(window.m_resultSummaryLabel->text().contains(
                 QStringLiteral("1 项待确认")),
             qPrintable(window.m_resultSummaryLabel->text()));
}

void GuiTests::closingAfterRecognitionExitsCleanly()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    QVERIFY(writeStubMode(modeFile, QStringLiteral("ok")));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));

    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));
    // 子进程里 loadImageFile 之后必须真的走"开始识别"这条路径，
    // 否则结果只暂存不发布，子进程会一直等不到表格内容。
    qputenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY", "1");

    // 回归锁：识别完成之后关闭主窗口，进程必须以 0 退出。
    // 2026-10-23 之前这里必崩（Windows 0xC0000374 堆损坏）：
    // ~MainWindow 时 BackendRunner 析构里的 kill() 会同步触发 finished() ->
    // requestSucceeded() -> MainWindow::backendSucceeded() 写已析构的成员。
    // 子进程跑完整流程，父进程只认退出码。
    QProcess child;
    child.setProcessChannelMode(QProcess::SeparateChannels);
    const QString executable = QCoreApplication::applicationFilePath();
    child.start(executable, QStringList() << QStringLiteral("--close-child"));
    QVERIFY2(child.waitForStarted(20000), "close-child process did not start");
    QVERIFY2(child.waitForFinished(120000), "close-child process did not finish");

    const QByteArray standardOutput = child.readAllStandardOutput();
    const QByteArray standardError = child.readAllStandardError();
    QCOMPARE(child.exitStatus(), QProcess::NormalExit);
    QVERIFY2(child.exitCode() == 0,
             qPrintable(QStringLiteral("exit code %1, stderr: %2")
                            .arg(child.exitCode())
                            .arg(QString::fromLocal8Bit(standardError))));
    // qInfo 默认写到 stderr，成功标记要 stdout/stderr 两处都查。
    const QString childOutput = QString::fromLocal8Bit(standardOutput)
            + QString::fromLocal8Bit(standardError);
    QVERIFY2(childOutput.contains("CLOSE_CHILD_OK"), qPrintable(childOutput));

    clearStubEnvironment();
}

void GuiTests::backendResponseParsingDistinguishesEmptyObjectFromGarbage()
{
    // 情况一：合法但为空的 JSON 对象。
    // 过去这里会给界面一条**空的**提示，用户看不到任何原因。
    QString emptyObjectError;
    const QJsonObject emptyObject = OcrTableToolGui::parseBackendResponse(
        QByteArray("{}"), &emptyObjectError);
    QVERIFY(emptyObject.isEmpty());
    QVERIFY2(!emptyObjectError.isEmpty(),
             "an empty JSON object must not report an empty reason");
    QVERIFY2(emptyObjectError.contains(QStringLiteral("空结果")),
             qPrintable(emptyObjectError));

    // 情况二：这一行根本不是 JSON。
    QString garbageError;
    const QJsonObject garbage = OcrTableToolGui::parseBackendResponse(
        QByteArray("not json at all"), &garbageError);
    QVERIFY(garbage.isEmpty());
    QVERIFY2(!garbageError.isEmpty(),
             "a non JSON line must not report an empty reason");
    QVERIFY2(garbageError != emptyObjectError,
             "an empty JSON object and a non JSON line must not share one reason");
    QVERIFY(garbageError.contains(QStringLiteral("没有返回有效结果")));

    // 情况三：正常响应必须原样通过，行为与改动前完全一致。
    QString successError;
    const QJsonObject success = OcrTableToolGui::parseBackendResponse(
        QByteArray("{\"protocol\":1,\"status\":\"ok\"}"), &successError);
    QCOMPARE(success.value(QStringLiteral("protocol")).toInt(), 1);
    QCOMPARE(success.value(QStringLiteral("status")).toString(), QStringLiteral("ok"));
    QVERIFY(successError.isEmpty());
}

void GuiTests::rectifiedImagePathRejectsRemoteAndTraversalPaths()
{
    // 情况一：UNC 路径会让进程去认证网络共享，必须拒绝。
    QVERIFY(!OcrTableToolGui::isSafeLocalImagePath(
        QStringLiteral("\\\\host\\share\\rectified.png")));
    QVERIFY(!OcrTableToolGui::isSafeLocalImagePath(
        QStringLiteral("//host/share/rectified.png")));

    // 情况二：带 ".." 的路径会读到输出目录之外，必须拒绝。
    QVERIFY(!OcrTableToolGui::isSafeLocalImagePath(
        QStringLiteral("C:/Users/test/AppData/Local/AP/OcrTableTool/../../secret.png")));
    QVERIFY(!OcrTableToolGui::isSafeLocalImagePath(
        QStringLiteral("..\\secret.png")));

    // 情况三：空路径说明响应不完整，必须拒绝。
    QVERIFY(!OcrTableToolGui::isSafeLocalImagePath(QString()));
    QVERIFY(!OcrTableToolGui::isSafeLocalImagePath(QStringLiteral("   ")));

    // 情况四：本机输出目录里的正常路径必须继续可用。
    QVERIFY(OcrTableToolGui::isSafeLocalImagePath(
        QStringLiteral("C:/Users/test/AppData/Local/AP/OcrTableTool/rectified-1234.png")));

    // 端到端：后端给出 UNC 路径时，界面不打开它，并在状态栏给出中文提示。
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(QStringLiteral("A"), 0.93, false));
    QJsonObject response = makeResponse(rows, 1);
    response.insert(QStringLiteral("rectified_image"),
                    QStringLiteral("\\\\host\\share\\rectified.png"));
    QVERIFY(publishRecognitionResult(window, response));
    QVERIFY2(window.m_statusLabel->text().contains(QStringLiteral("本地安全路径")),
             qPrintable(window.m_statusLabel->text()));
    // 识别结果本身照常发布，被忽略的只有矫正图。
    QCOMPARE(window.m_table->rowCount(), 1);
}

namespace {

// 读 GuiTrace 写出的 JSONL（OCR_TABLE_GUI_TRACE 指向的文件）。
QStringList readTraceRecords(const QString &path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text))
        return QStringList();
    return QString::fromUtf8(file.readAll()).split(QLatin1Char('\n'), QString::SkipEmptyParts);
}

// 取出某个事件名的全部记录（按写入顺序）。
QList<QJsonObject> traceEvents(const QStringList &records, const QString &eventName)
{
    QList<QJsonObject> events;
    for (int index = 0; index < records.size(); ++index) {
        const QJsonDocument document = QJsonDocument::fromJson(records.at(index).toUtf8());
        if (!document.isObject())
            continue;
        const QJsonObject record = document.object();
        if (record.value(QStringLiteral("event")).toString() == eventName)
            events.append(record);
    }
    return events;
}

// 造一份满足后端 xlsx 导出边界契约的结构证书。
QJsonObject makeBoundaryCertificate(int rows, int columns, const QJsonArray &spans = QJsonArray())
{
    QJsonArray columnBoundaries;
    for (int index = 0; index <= columns; ++index)
        columnBoundaries.append(index * 60);
    QJsonArray rowBoundaries;
    for (int index = 0; index <= rows; ++index)
        rowBoundaries.append(index * 40);

    QJsonObject certificate;
    certificate.insert(QStringLiteral("version"), 1);
    certificate.insert(QStringLiteral("verified"), true);
    certificate.insert(QStringLiteral("rows"), rows);
    certificate.insert(QStringLiteral("columns"), columns);
    certificate.insert(QStringLiteral("spans"), spans);
    certificate.insert(QStringLiteral("geometry_hash"), QStringLiteral("stub-geometry"));
    certificate.insert(QStringLiteral("structure_hash"), QStringLiteral("stub-structure"));
    certificate.insert(QStringLiteral("column_boundaries"), columnBoundaries);
    certificate.insert(QStringLiteral("row_boundaries"), rowBoundaries);
    return certificate;
}

} // namespace

void GuiTests::backendExitCodeIsNotIgnored()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));
    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    QVERIFY(!imagePath.isEmpty());

    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));

    // 情形一（对照）：合法 ok 响应 + 退出码 0 ⇒ 仍然成功。改动不得误伤正常路径。
    {
        QVERIFY(writeStubMode(modeFile, QStringLiteral("ok")));
        BackendRunner runner;
        QSignalSpy successSpy(&runner, SIGNAL(requestSucceeded(QString,QJsonObject)));
        QSignalSpy failureSpy(&runner, SIGNAL(requestFailed(QString,QString,QString,bool)));
        runner.recognize(imagePath, temporary.path(), QStringLiteral("auto"));
        QTRY_VERIFY_WITH_TIMEOUT(successSpy.count() + failureSpy.count() > 0, 30000);
        QCOMPARE(failureSpy.count(), 0);
        QCOMPARE(successSpy.count(), 1);
    }

    // 情形二（findings/61 F3）：合法 ok 响应 + 退出码 3 ⇒ 必须判失败。
    // 改动前退出码只进 trace、不参与判定，这里会误报成功。
    {
        QVERIFY(writeStubMode(modeFile, QStringLiteral("ok_exit_nonzero")));
        BackendRunner runner;
        QSignalSpy successSpy(&runner, SIGNAL(requestSucceeded(QString,QJsonObject)));
        QSignalSpy failureSpy(&runner, SIGNAL(requestFailed(QString,QString,QString,bool)));
        runner.recognize(imagePath, temporary.path(), QStringLiteral("auto"));
        QTRY_VERIFY_WITH_TIMEOUT(successSpy.count() + failureSpy.count() > 0, 30000);
        QCOMPARE(successSpy.count(), 0);
        QCOMPARE(failureSpy.count(), 1);
        const QList<QVariant> arguments = failureSpy.at(0);
        QCOMPARE(arguments.at(0).toString(), QStringLiteral("recognize"));
        const QString message = arguments.at(1).toString();
        QVERIFY2(message.contains(QStringLiteral("本次结果未采用")), qPrintable(message));
        // 文案中性：不承诺结果完整、不承诺可恢复。
        QVERIFY2(!message.contains(QStringLiteral("已保存")), qPrintable(message));
        QVERIFY2(!message.contains(QStringLiteral("请展开")), qPrintable(message));
        QCOMPARE(arguments.at(2).toString(), QStringLiteral("BACKEND_CRASHED"));
        QCOMPARE(arguments.at(3).toBool(), true);
    }

    clearStubEnvironment();
}

void GuiTests::busyOrStoppingStartClickIsNotSwallowed()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    QVERIFY(writeStubMode(modeFile, QStringLiteral("ok")));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));
    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));
    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    QVERIFY(!imagePath.isEmpty());

    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);
    // 直接给状态赋值，不走 loadImage()：那条路会顺带启动后台识别，
    // 这里要的是可控的"忙碌/停止"两个窗口。
    window.m_imagePath = imagePath;
    window.m_sourceImagePath = imagePath;

    // 变体 A（findings/61 F5）：识别组件正在跑别的任务（导出 XLSX），
    // m_recognitionActive == false。改动前这里零反馈 —— 用户点了没反应。
    window.m_backend->m_requestActive = true;
    QVERIFY(QMetaObject::invokeMethod(&window, "backendStarted", Qt::DirectConnection,
                                      Q_ARG(QString, QStringLiteral("export_xlsx"))));
    // 组件忙时按钮被禁用，用户点不到；提示必须说明原因（否则"灰了"等于没反应）。
    QVERIFY(!window.m_recognizeButton->isEnabled());
    QVERIFY2(window.m_recognizeButton->toolTip().contains(QStringLiteral("另一项任务")),
             qPrintable(window.m_recognizeButton->toolTip()));

    const QString beforeClick = window.m_statusLabel->text();
    QVERIFY(triggerRecognition(window));
    const QString busyStatus = window.m_statusLabel->text();
    QVERIFY2(busyStatus != beforeClick, "点击被静默丢弃：状态栏没有任何变化");
    QVERIFY2(busyStatus.contains(QStringLiteral("另一项任务")), qPrintable(busyStatus));
    QVERIFY(!window.m_startRequestedWhileStopping);

    window.m_backend->m_requestActive = false;
    QVERIFY(QMetaObject::invokeMethod(&window, "backendFailed", Qt::DirectConnection,
                                      Q_ARG(QString, QStringLiteral("export_xlsx")),
                                      Q_ARG(QString, QStringLiteral("操作已取消")),
                                      Q_ARG(QString, QString()),
                                      Q_ARG(bool, false)));
    QVERIFY(window.m_recognizeButton->toolTip().isEmpty());

    // 变体 B：停止窗口（m_stopping）。改动前：先显示"正在精确识别"，
    // 停止完成后又被改写成"识别已取消"——用户的点击彻底丢失。
    window.m_backend->m_stopping = true;
    window.m_recognitionActive = true;
    window.m_recognitionDisplayRequested = false;
    QVERIFY(triggerRecognition(window));
    QVERIFY2(window.m_statusLabel->text().contains(QStringLiteral("正在结束上一个任务")),
             qPrintable(window.m_statusLabel->text()));
    QVERIFY2(window.m_startRequestedWhileStopping,
             "停止窗口内的点击意图没有被记住（等于静默丢弃）");
    window.m_backend->m_stopping = false;

    // 停止完成（后端上报"操作已取消"）⇒ 必须按用户刚才的点击补跑，
    // 而不是把状态栏改写成"识别已取消"。
    QVERIFY(QMetaObject::invokeMethod(&window, "backendFailed", Qt::DirectConnection,
                                      Q_ARG(QString, QStringLiteral("recognize")),
                                      Q_ARG(QString, QStringLiteral("操作已取消")),
                                      Q_ARG(QString, QString()),
                                      Q_ARG(bool, false)));
    QVERIFY2(window.m_statusLabel->text().contains(QStringLiteral("重新识别")),
             qPrintable(window.m_statusLabel->text()));
    QVERIFY(!window.m_startRequestedWhileStopping);
    // 补跑是真的会发生：桩后端返回 ok ⇒ 表格最终被填上。
    QTRY_VERIFY_WITH_TIMEOUT(window.m_table->rowCount() > 0, 30000);
    QCOMPARE(window.m_table->columnCount(), 3);

    clearStubEnvironment();
}

void GuiTests::exportRequestCarriesStructureCertificate()
{
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("A") << QStringLiteral("B"));
    rows << (QStringList() << QStringLiteral("C") << QStringLiteral("D"));
    const TableData table = TableData::fromRows(rows);
    QCOMPARE(table.rowCount(), 2);
    QCOMPARE(table.columnCount(), 2);
    const QJsonArray spans;

    // 1) 满足后端导出边界契约的证书必须出现在请求里（F6 的修法）。
    const QJsonObject certificate = makeBoundaryCertificate(2, 2);
    QVERIFY(OcrTableToolGui::isXlsxCompatibleStructureCertificate(certificate, 2, 2));
    const QJsonObject request = OcrTableToolGui::buildExportRequest(
        QStringLiteral("out.xlsx"), table, spans, certificate);
    QVERIFY(request.contains(QStringLiteral("structure_certificate")));
    QVERIFY(request.value(QStringLiteral("structure_certificate")).toObject() == certificate);
    // 其余字段口径不变（GUI 不得裁剪/覆盖导出请求）。
    QCOMPARE(request.value(QStringLiteral("action")).toString(), QStringLiteral("export_xlsx"));
    QCOMPARE(request.value(QStringLiteral("output_path")).toString(), QStringLiteral("out.xlsx"));
    QCOMPARE(request.value(QStringLiteral("cells")).toArray().size(), 2);
    QVERIFY(request.value(QStringLiteral("spans")).toArray() == spans);

    // 2) 不满足后端契约的证书一律不带：带上会让后端抛
    //    invalid XLSX structure certificate boundaries，整次导出失败。
    QJsonObject withoutBoundaries = makeBoundaryCertificate(2, 2);
    withoutBoundaries.remove(QStringLiteral("column_boundaries"));
    withoutBoundaries.remove(QStringLiteral("row_boundaries"));
    QVERIFY(!OcrTableToolGui::isXlsxCompatibleStructureCertificate(withoutBoundaries, 2, 2));
    QVERIFY(!OcrTableToolGui::buildExportRequest(QStringLiteral("out.xlsx"), table, spans,
                                                 withoutBoundaries)
                 .contains(QStringLiteral("structure_certificate")));

    QJsonObject shortBoundaries = makeBoundaryCertificate(2, 2);
    shortBoundaries.insert(QStringLiteral("column_boundaries"), QJsonArray() << 0 << 60);
    QVERIFY(!OcrTableToolGui::isXlsxCompatibleStructureCertificate(shortBoundaries, 2, 2));

    QJsonObject nonIncreasing = makeBoundaryCertificate(2, 2);
    nonIncreasing.insert(QStringLiteral("row_boundaries"), QJsonArray() << 0 << 40 << 40);
    QVERIFY(!OcrTableToolGui::isXlsxCompatibleStructureCertificate(nonIncreasing, 2, 2));

    QJsonObject unverified = makeBoundaryCertificate(2, 2);
    unverified.insert(QStringLiteral("verified"), false);
    QVERIFY(!OcrTableToolGui::isXlsxCompatibleStructureCertificate(unverified, 2, 2));

    // 证书里的行列数与当前网格不一致（用户改过结构）⇒ 不带。
    QVERIFY(!OcrTableToolGui::isXlsxCompatibleStructureCertificate(certificate, 3, 2));
    // 空证书（结构未确认的结果）⇒ 不带，行为与改动前一致。
    QVERIFY(!OcrTableToolGui::buildExportRequest(QStringLiteral("out.xlsx"), table, spans,
                                                 QJsonObject())
                 .contains(QStringLiteral("structure_certificate")));

    // 3) 界面侧：校验通过的证书要留存，供导出使用；结构被人工改动后必须清空。
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    QJsonArray cellRows;
    cellRows.append(QJsonArray() << makeCell(QStringLiteral("A"), 0.93, false)
                                 << makeCell(QStringLiteral("B"), 0.93, false));
    QJsonObject response = makeResponse(cellRows, 2);
    const QJsonObject publishedCertificate = makeBoundaryCertificate(1, 2);
    response.insert(QStringLiteral("structure_certificate"), publishedCertificate);
    QVERIFY(publishRecognitionResult(window, response));
    QCOMPARE(window.m_table->rowCount(), 1);
    QCOMPARE(window.m_table->columnCount(), 2);
    QVERIFY2(window.m_structureCertificate == publishedCertificate,
             qPrintable(QString::fromUtf8(
                 QJsonDocument(window.m_structureCertificate).toJson(QJsonDocument::Compact))));

    // 加了行列之后网格与证书边界不再对应 ⇒ 必须清空（宁可不发）。
    QVERIFY(QMetaObject::invokeMethod(&window, "addRow", Qt::DirectConnection));
    QVERIFY(window.m_structureCertificate.isEmpty());
}

void GuiTests::retryStaysWithinOneRecognitionBudget()
{
    // A. 预算函数的不变量（findings B1）：无论第一次用了多久，
    //    「已用 + 重试间隔 + 重试预算」都必须落在一次识别预算（60 秒）之内。
    for (int used = 0; used <= 130000; used += 100) {
        const int budget = OcrTableToolGui::remainingRecognitionBudgetSeconds(used);
        QVERIFY2(budget >= 0, qPrintable(QStringLiteral("used=%1 budget=%2").arg(used).arg(budget)));
        if (budget < 5)
            continue; // 不足后端最小预算 ⇒ 不会重试
        const qint64 total = qint64(used) + 350 + qint64(budget) * 1000;
        QVERIFY2(total <= 60000,
                 qPrintable(QStringLiteral("used=%1 budget=%2 total=%3")
                                .arg(used)
                                .arg(budget)
                                .arg(total)));
    }
    QCOMPARE(OcrTableToolGui::remainingRecognitionBudgetSeconds(0), 59);
    QCOMPARE(OcrTableToolGui::remainingRecognitionBudgetSeconds(30000), 29);
    QCOMPARE(OcrTableToolGui::remainingRecognitionBudgetSeconds(55600), 4);
    QCOMPARE(OcrTableToolGui::remainingRecognitionBudgetSeconds(60000), 0);
    QCOMPARE(OcrTableToolGui::remainingRecognitionBudgetSeconds(180000), 0);

    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString modeFile = temporary.filePath(QStringLiteral("stub-mode.txt"));
    const QString scriptPath = writeStubBackend(temporary.path());
    QVERIFY(!scriptPath.isEmpty());
    const QString python = testPythonPath();
    QVERIFY(QFileInfo::exists(python));
    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    QVERIFY(!imagePath.isEmpty());
    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    qputenv("OCR_TABLE_TEST_STUB_MODE_FILE", QFile::encodeName(modeFile));

    // B. 预算 > 0 时父进程侧必须有墙钟上限；首次识别（预算 0）必须没有，
    //    这样「单次识别 60 秒墙钟」的对外口径不变。
    QVERIFY(writeStubMode(modeFile, QStringLiteral("hang")));
    {
        BackendRunner runner;
        runner.recognize(imagePath, temporary.path(), QStringLiteral("auto"), false, false, 7);
        QVERIFY(runner.m_requestActive);
        QVERIFY2(runner.m_requestTimer->isActive(), "重试请求缺少父进程侧墙钟上限");
        QCOMPARE(runner.m_requestTimer->interval(), 7000);
    }
    {
        BackendRunner runner;
        runner.recognize(imagePath, temporary.path(), QStringLiteral("auto"));
        QVERIFY(runner.m_requestActive);
        QVERIFY2(!runner.m_requestTimer->isActive(), "首次识别不应引入父进程侧超时");
    }

    // C. 端到端：首次失败且可重试 ⇒ 仍然自动重试（能力保留），
    //    且第二次请求只带剩余预算，而不是又一轮完整时长。
    QVERIFY(writeStubMode(modeFile, QStringLiteral("fail_once")));
    const QString tracePath = temporary.filePath(QStringLiteral("gui-trace.jsonl"));
    qputenv("OCR_TABLE_GUI_TRACE", QFile::encodeName(tracePath));
    qputenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY", "1");
    {
        MainWindow window;
        window.show();
        QVERIFY(window.loadImageFile(imagePath));
        QTRY_VERIFY_WITH_TIMEOUT(window.m_table->rowCount() > 0, 60000);
    }
    const QList<QJsonObject> requestEvents =
        traceEvents(readTraceRecords(tracePath), QStringLiteral("recognition_requested"));
    QVERIFY2(requestEvents.size() >= 2,
             qPrintable(QStringLiteral("recognition_requested 事件数 = %1")
                            .arg(requestEvents.size())));
    QCOMPARE(requestEvents.at(0).value(QStringLiteral("budget_seconds")).toInt(), 0);
    QCOMPARE(requestEvents.at(1).value(QStringLiteral("attempt")).toInt(), 2);
    const int retryBudget = requestEvents.at(1).value(QStringLiteral("budget_seconds")).toInt();
    QVERIFY2(retryBudget >= 5 && retryBudget <= 59, qPrintable(QStringLiteral("retry budget = %1").arg(retryBudget)));
    QVERIFY(!traceEvents(readTraceRecords(tracePath), QStringLiteral("recognition_retry_scheduled")).isEmpty());
    QVERIFY(traceEvents(readTraceRecords(tracePath), QStringLiteral("recognition_retry_skipped")).isEmpty());

    clearStubEnvironment();
    qunsetenv("OCR_TABLE_GUI_TRACE");
}

// findings F4：状态栏「用时」必须与识别组件自身计时（后端口径）一致。
// worker_wall_seconds 是 worker 全生命周期（含进程启停与模型卸载），
// 显示的秒数过去系统性偏大，且与「单次识别 60 秒上限」不能相减。
void GuiTests::statusBarElapsedMatchesRecognitionBudget()
{
    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);

    QJsonArray rows;
    rows.append(QJsonArray() << makeCell(QStringLiteral("515.128MHz"), 0.95, false)
                             << makeCell(QStringLiteral("数字"), 0.95, false));

    // 识别组件自身计时 12.5 秒；worker 全生命周期 18.0 秒，其中 4.0 秒花在
    // 进程启动 / 退出与模型卸载上。
    QJsonObject response = makeResponse(rows, 2);
    response.insert(QStringLiteral("elapsed_seconds"), 12.5);
    response.insert(QStringLiteral("worker_wall_seconds"), 18.0);
    response.insert(QStringLiteral("response_exit_seconds"), 4.0);
    QVERIFY(publishRecognitionResult(window, response));

    const QString status = window.m_statusLabel->text();
    QVERIFY2(status.contains(QStringLiteral("用时")), qPrintable(status));
    QVERIFY2(status.contains(QStringLiteral("12.5")), qPrintable(status));
    QVERIFY2(!status.contains(QStringLiteral("18.0")),
             qPrintable(QStringLiteral("状态栏仍在用 worker 全生命周期：%1").arg(status)));

    // 口径要点明：12.5 秒是识别组件自身计时（可比 60 秒上限），4.0 秒是退出开销。
    const QString detail = window.m_statusLabel->toolTip();
    QVERIFY2(detail.contains(QStringLiteral("用时口径")), qPrintable(detail));
    QVERIFY2(detail.contains(QStringLiteral("4.0")), qPrintable(detail));

    // 识别组件没上报自身计时（桩后端 / 老版本）时退回「全生命周期 − 退出开销」，
    // 而不是把退出开销也算进用时。
    QJsonObject legacy = makeResponse(rows, 2);
    legacy.insert(QStringLiteral("worker_wall_seconds"), 18.0);
    legacy.insert(QStringLiteral("response_exit_seconds"), 4.0);
    QVERIFY(publishRecognitionResult(window, legacy));
    const QString legacyStatus = window.m_statusLabel->text();
    QVERIFY2(legacyStatus.contains(QStringLiteral("14.0")), qPrintable(legacyStatus));
    QVERIFY2(!legacyStatus.contains(QStringLiteral("18.0")), qPrintable(legacyStatus));
    QVERIFY(window.m_statusLabel->toolTip().contains(QStringLiteral("估算")));
}

// 高 DPI：预览位图必须按物理像素生成并带设备像素比，否则高 DPI 屏上被
// 降采样成半分辨率（模糊）。控件自身与 scaledPreviewForDevicePixelRatio()
// 走的是同一条缩放路径，所以这里可以脱离高 DPI 屏幕断言。
void GuiTests::previewScalesToDevicePixelRatio()
{
    ImagePreview preview;
    preview.resize(400, 320);
    QImage image(800, 400, QImage::Format_RGB32);
    image.fill(Qt::white);
    image.setDevicePixelRatio(2.0);
    preview.setImage(image);
    preview.show();
    QTest::qWait(20);

    // 逻辑目标区固定：400×320 内缩 8px ⇒ 384×304；800×400 按比例落成 384×192。
    const QPixmap singleRatio = preview.scaledPreviewForDevicePixelRatio(1.0);
    const QPixmap doubleRatio = preview.scaledPreviewForDevicePixelRatio(2.0);
    QVERIFY2(!singleRatio.isNull(), "1x preview missing");
    QVERIFY2(!doubleRatio.isNull(), "2x preview missing");

    QCOMPARE(singleRatio.width(), 384);
    QCOMPARE(singleRatio.height(), 192);
    QCOMPARE(singleRatio.devicePixelRatio(), 1.0);

    QCOMPARE(doubleRatio.width(), 768);
    QCOMPARE(doubleRatio.height(), 384);
    QCOMPARE(doubleRatio.devicePixelRatio(), 2.0);
    // 逻辑尺寸不变：2 倍图在屏幕上占同一块地方，只是像素翻倍。
    QVERIFY2(qFuzzyCompare(doubleRatio.width() / doubleRatio.devicePixelRatio(),
                           singleRatio.width() / singleRatio.devicePixelRatio()),
             "2x preview changed the logical size");

    // 控件真正用的那份预览位图必须携带控件所在屏的设备像素比。
    QCOMPARE(preview.m_scaledPreview.devicePixelRatio(), preview.previewDevicePixelRatio());
    QCOMPARE(preview.previewLogicalSize(), QSize(384, 192));
}

// resize 不能深拷贝原图：缩放源只在换图时构建一次，后续 resize 只重采样。
void GuiTests::previewResizeReusesSingleScaleSource()
{
    ImagePreview preview;
    preview.resize(400, 320);
    // 2400×1800 ≈ 17 MB 的图，每次 resize 拷一遍就是拖动卡顿的来源。
    QImage image(2400, 1800, QImage::Format_RGB32);
    image.fill(Qt::white);
    image.setDevicePixelRatio(2.0);
    preview.setImage(image);
    preview.show();
    QTest::qWait(20);

    QCOMPARE(preview.scaleSourceBuildCount(), 1);
    QCOMPARE(preview.scaleSource().size(), QSize(2400, 1800));
    // 缩放源必须是逻辑像素（1:1），框选坐标才能和鼠标坐标对上。
    QCOMPARE(preview.scaleSource().devicePixelRatio(), 1.0);
    const uchar *sourceBits = preview.scaleSource().constBits();
    QVERIFY(sourceBits != nullptr);

    for (int step = 1; step <= 4; ++step) {
        preview.resize(400 + step * 60, 320 + step * 40);
        QTest::qWait(10);
        QVERIFY2(!preview.m_scaledPreview.isNull(), "预览位图在 resize 后消失了");
        QCOMPARE(preview.scaleSourceBuildCount(), 1);
        // 像素缓冲没换 = 没有重新深拷贝整张原图。
        QVERIFY2(preview.scaleSource().constBits() == sourceBits,
                 "resize 期间缩放源被重新分配（仍在深拷贝原图）");
    }

    // 换图才重建，且仍然只重建一次。
    QImage next(600, 400, QImage::Format_RGB32);
    next.fill(Qt::white);
    preview.setImage(next);
    QCOMPARE(preview.scaleSourceBuildCount(), 2);
    QCOMPARE(preview.scaleSource().size(), QSize(600, 400));
}

// 大图查看器必须使用调用方传入的标题：原来构造函数 Q_UNUSED(title) +
// setWindowTitle(QString())，标题栏是空的。
void GuiTests::imageViewerDialogKeepsCallerTitle()
{
    QImage image(64, 48, QImage::Format_RGB32);
    image.fill(Qt::white);

    ImageViewerDialog titled(image, QStringLiteral("原图预览"), nullptr);
    QCOMPARE(titled.windowTitle(), QStringLiteral("原图预览"));

    ImageViewerDialog untitled(image, QString(), nullptr);
    QVERIFY2(!untitled.windowTitle().isEmpty(), "空标题时窗口标题栏仍然是空的");

    // 界面上真正弹出来的那个窗口也要带标题（走 MainWindow::showImageViewer 链路）。
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString imagePath = writeTestImage(temporary.path(), QSize(640, 480));
    QVERIFY(!imagePath.isEmpty());

    MainWindow window;
    window.resize(1200, 800);
    window.show();
    QTest::qWait(80);
    QVERIFY(window.loadImageFile(imagePath));
    QTest::qWait(80);

    QString viewerTitle;
    bool viewerSeen = false;
    QTimer::singleShot(200, &window, [&viewerTitle, &viewerSeen]() {
        QWidget *viewer = QApplication::activeModalWidget();
        if (!viewer)
            return;
        viewerSeen = true;
        viewerTitle = viewer->windowTitle();
        viewer->close();
    });
    QTest::mouseClick(window.m_originalPreview, Qt::LeftButton, Qt::NoModifier, QPoint(20, 20));
    QVERIFY2(viewerSeen, "image viewer dialog did not open");
    QCOMPARE(viewerTitle, QStringLiteral("原图预览"));
}

// 摄像头报错后快门永久禁用：错误态必须留一个可点的重试入口，能重新打开设备。
void GuiTests::cameraErrorOffersRetry()
{
    CameraCaptureDialog dialog;
    QVERIFY(dialog.m_retryButton != nullptr);
    // ⚠ 2026-11-02：分支依据用 **m_cameraIndex**（设备到底能不能用），
    //   不是 `QCameraInfo::availableCameras().isEmpty()`（设备列表是否为空）。
    //   这两者在「本机有摄像头、但测试环境打不开」时结论相反
    //   （本机 HD Webcam：列表非空，但 startCamera 失败 ⇒ m_cameraIndex = -1），
    //   旧断言把环境当契约，在这种机器上必然失败。
    // ⚠ 2026-11-02：这里**不做任何关于设备可用性的推断**。
    //   「设备存在 / 设备被选中 / 设备能打开」是三件不同的事：
    //   本机有 HD Webcam，前两件为真，但测试环境里设备打不开 ⇒ 第三件为假。
    //   m_cameraIndex 只表示「选中了几号设备」，**不代表能打开**
    //   （cameracapturedialog.cpp:296 只做赋值，能否打开是另一回事）。
    //   ⇒ 设备不可用时，快门必须不可用，但**必须留一个重试入口**。
    //   （有设备时快门是否可点取决于设备就绪与否，这里不写死。）
    if (dialog.m_cameraIndex < 0) {
        QVERIFY(!dialog.m_captureButton->isEnabled());
        QVERIFY2(!dialog.m_retryButton->isHidden(), "设备不可用时没有重试入口");
    }

    // 触发摄像头错误（私有槽，走元对象）。
    QVERIFY(QMetaObject::invokeMethod(&dialog, "cameraError", Qt::DirectConnection));
    QVERIFY(!dialog.m_captureButton->isEnabled());
    QVERIFY2(!dialog.m_retryButton->isHidden(), "摄像头报错后重试入口消失");
    QVERIFY(dialog.m_retryButton->isEnabled());
    const QString errorStatus = dialog.m_statusLabel->text();
    QVERIFY2(errorStatus.contains(QStringLiteral("重试")), qPrintable(errorStatus));

    // 点重试必须真的重新尝试打开设备，而不是停在错误文案上。
    const int retriesBefore = dialog.m_cameraRetryCount;
    dialog.m_retryButton->click();
    QCOMPARE(dialog.m_cameraRetryCount, retriesBefore + 1);
    const QString retriedStatus = dialog.m_statusLabel->text();
    QVERIFY2(retriedStatus != errorStatus, qPrintable(retriedStatus));
    // 末尾按**实测到的状态**判定，不猜设备能不能用：
    //   - 变成「正在打开摄像头…」⇒ 真的去重开设备了（成功路径）；
    //   - 没变成 ⇒ 重开失败 ⇒ 必须仍留着一个**可点**的重试入口，
    //     否则用户就被卡死在这个对话框里，只能关掉重开。
    // ⚠ 2026-11-02：点完重试之后，入口的恢复有三条路径：
    //   1. 设备就绪  → captureReadyChanged(true) → 入口隐藏（快门能用了，不需要它）
    //   2. 设备报错  → cameraError() → 入口立刻回来
    //   3. 设备既不就绪也不报错（本机：设备存在但起不来、信号不来）
    //      → 由 startCamera 的兜底定时器（8 s）把入口亮出来
    //   ⇒ 断言「立刻可见」在路径 1 上本就不成立、路径 3 上要等 8 秒；
    //   ⇒ 正确口径是：**设备仍未就绪时，入口最终必须回来**（用户不被卡死）。
    //   ⇒ 用 QTRY_VERIFY 等兜底定时器触发（重试超时 12 s > 兜底 8 s）。
    const bool captureReady =
        dialog.m_capture && dialog.m_capture->isReadyForCapture();
    if (!captureReady) {
        // ⚠ Qt 5.9 的签名：QTRY_VERIFY2_WITH_TIMEOUT(expr, messageExpression, timeout)
        //   —— 消息在前、超时在后。
        QTRY_VERIFY2_WITH_TIMEOUT(!dialog.m_retryButton->isHidden(),
             qPrintable(QStringLiteral("设备未就绪，但重试入口没有回来：%1")
                            .arg(retriedStatus)),
             12000);
        QTRY_VERIFY_WITH_TIMEOUT(dialog.m_retryButton->isEnabled(), 12000);
    }
}

// 拍照保存路径：目录建不出来时必须如实报错，不能再返回一个指向不存在目录的路径。
void GuiTests::capturePathReportsDirectoryFailure()
{
    // 正常情况：目录不存在就建出来，返回落在这个目录里的 jpg 路径。
    QTemporaryDir goodRoot;
    QVERIFY(goodRoot.isValid());
    QString reason;
    const QString goodPath = OcrTableToolGui::nextCapturePathIn(goodRoot.path(), &reason);
    QVERIFY2(!goodPath.isEmpty(), qPrintable(reason));
    QVERIFY2(reason.isEmpty(), qPrintable(reason));
    QCOMPARE(QFileInfo(goodPath).suffix(), QStringLiteral("jpg"));
    const QString expectedDirectory =
        QFileInfo(QDir(goodRoot.path()).filePath(QStringLiteral("OcrTableTool"))).absoluteFilePath();
    QCOMPARE(QFileInfo(goodPath).absolutePath(), expectedDirectory);
    QVERIFY(QFileInfo(expectedDirectory).isDir());

    // 失败情况：OcrTableTool 这个位置上放的是同名文件，mkpath 必然失败。
    QTemporaryDir blockedRoot;
    QVERIFY(blockedRoot.isValid());
    const QString blocker = QDir(blockedRoot.path()).filePath(QStringLiteral("OcrTableTool"));
    QFile blockerFile(blocker);
    QVERIFY(blockerFile.open(QIODevice::WriteOnly));
    blockerFile.write("not a directory");
    blockerFile.close();
    QVERIFY(QFileInfo(blocker).isFile());

    QString failureReason;
    const QString failedPath = OcrTableToolGui::nextCapturePathIn(blockedRoot.path(), &failureReason);
    QVERIFY2(failedPath.isEmpty(),
             qPrintable(QStringLiteral("目录创建失败却返回了路径：%1").arg(failedPath)));
    QVERIFY2(!failureReason.isEmpty(), "目录创建失败被静默吞掉");
    QVERIFY2(failureReason.contains(QStringLiteral("无法创建照片保存目录")),
             qPrintable(failureReason));
    QVERIFY2(failureReason.contains(QStringLiteral("OcrTableTool")), qPrintable(failureReason));
}

// 子进程场景：完成一次识别，再让 worker 挂住，最后关闭主窗口。
// 正常返回 0；一旦复现堆损坏，进程会被 Windows 杀掉，退出码非 0。
int runCloseChildScenario(int argc, char *argv[])
{
    QApplication application(argc, argv);
    application.setApplicationName(QStringLiteral("OcrTableTool"));
    application.setOrganizationName(QStringLiteral("AP"));

    const QString modeFile = stubModeFilePath();
    if (modeFile.isEmpty() || !writeStubMode(modeFile, QStringLiteral("ok"))) {
        qWarning("close-child: stub mode file missing");
        return 2;
    }

    QTemporaryDir temporary;
    if (!temporary.isValid()) {
        qWarning("close-child: temporary directory unavailable");
        return 3;
    }
    const QString imagePath = writeTestImage(temporary.path(), QSize(320, 240));
    if (imagePath.isEmpty()) {
        qWarning("close-child: test image unavailable");
        return 4;
    }

    MainWindow *window = new MainWindow;
    window->show();
    if (!window->loadImageFile(imagePath)) {
        delete window;
        return 5;
    }

    // 等识别真正完成，结果进表格。
    QElapsedTimer timer;
    timer.start();
    while (window->m_table->rowCount() == 0 && timer.elapsed() < 60000)
        QCoreApplication::processEvents(QEventLoop::AllEvents, 50);
    if (window->m_table->rowCount() == 0) {
        qWarning("close-child: recognition did not finish");
        delete window;
        return 6;
    }

    // 再起一个 worker 并让它挂住：这才走到"识别过之后关闭"的崩溃路径。
    if (!writeStubMode(modeFile, QStringLiteral("hang"))) {
        delete window;
        return 7;
    }
    triggerRecognition(*window);
    timer.restart();
    while (!window->m_backend->isRunning() && timer.elapsed() < 30000)
        QCoreApplication::processEvents(QEventLoop::AllEvents, 50);
    if (!window->m_backend->isRunning()) {
        qWarning("close-child: worker did not start");
        delete window;
        return 8;
    }
    const qint64 workerProcessId = window->m_backend->m_process->processId();

    // 关闭主窗口：这里必须不崩。
    delete window;
    QCoreApplication::processEvents(QEventLoop::AllEvents, 50);

    if (processIsAlive(workerProcessId)) {
        qWarning("close-child: worker was not stopped");
        return 9;
    }
    qInfo("CLOSE_CHILD_OK");
    return 0;
}

int main(int argc, char *argv[])
{
    for (int index = 1; index < argc; ++index) {
        if (qstrcmp(argv[index], "--close-child") == 0)
            return runCloseChildScenario(argc, argv);
    }

    QApplication application(argc, argv);
    application.setApplicationName(QStringLiteral("OcrTableTool"));
    application.setOrganizationName(QStringLiteral("AP"));
    GuiTests tests;
    return QTest::qExec(&tests, argc, argv);
}

#include "tst_gui.moc"
