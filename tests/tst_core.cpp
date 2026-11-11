#include <QtTest>

#include <QApplication>
#include <QJsonDocument>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QImage>
#include <QMouseEvent>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QTouchDevice>

#include "backendrunner.h"
#include "backendlocator.h"
#include "csvexporter.h"
#include "imagepreview.h"
#include "tabledata.h"

namespace {

// 构建目录既可能在源码树内部（Qt Creator shadow build：<src>/build-<kit>/tests/release），
// 也可能在源码树外部（本机约定：构建产物统一放 AP7000-Build-Directories）。
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

// 第一轮：从起点逐级向上，每级看目录自身是否带项目根标记。
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

// 第二轮：构建目录在源码树之外时，源码树不是构建目录的祖先，
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

QString projectPythonPath(const QString &projectRoot)
{
    if (projectRoot.isEmpty())
        return QString();
#ifdef Q_OS_WIN
    return QFileInfo(QDir(projectRoot).filePath(QStringLiteral(".venv/Scripts/python.exe")))
            .absoluteFilePath();
#else
    return QFileInfo(QDir(projectRoot).filePath(QStringLiteral(".venv/bin/python3")))
            .absoluteFilePath();
#endif
}

// 造一个"结构上像本工程"的目录：backend/ocr_backend.py + .venv 解释器。
// withProductDependency 为真时再放一个本产品依赖标记（cv2 模块目录），
// 用来区分「本工程的 venv」和「同机其它工程的空 venv」。
QString makeFakeProjectVenv(const QString &projectDirectory, bool withProductDependency)
{
    QDir project(projectDirectory);
#ifdef Q_OS_WIN
    const QString pythonPath = project.filePath(QStringLiteral(".venv/Scripts/python.exe"));
    const QString sitePackages = project.filePath(QStringLiteral(".venv/Lib/site-packages"));
#else
    const QString pythonPath = project.filePath(QStringLiteral(".venv/bin/python3"));
    const QString sitePackages = project.filePath(QStringLiteral(".venv/lib/python3.9/site-packages"));
#endif
    if (!QDir().mkpath(QFileInfo(pythonPath).absolutePath()))
        return QString();
    if (!QDir().mkpath(project.filePath(QStringLiteral("backend"))))
        return QString();

    QFile pythonFile(pythonPath);
    if (!pythonFile.open(QIODevice::WriteOnly))
        return QString();
    pythonFile.close();

    QFile backendFile(project.filePath(QStringLiteral("backend/ocr_backend.py")));
    if (!backendFile.open(QIODevice::WriteOnly))
        return QString();
    backendFile.close();

    if (withProductDependency) {
        const QString markerPath = QDir(sitePackages).filePath(QStringLiteral("cv2/__init__.py"));
        if (!QDir().mkpath(QFileInfo(markerPath).absolutePath()))
            return QString();
        QFile marker(markerPath);
        if (!marker.open(QIODevice::WriteOnly))
            return QString();
        marker.close();
    }
    return QFileInfo(pythonPath).absoluteFilePath();
}

} // namespace

class CoreTests : public QObject
{
    Q_OBJECT

private slots:
    void normalizesRaggedRows();
    void expandsWhenEditingOutsideCurrentBounds();
    void exportsUtf8BomAndEscapesCsvFields();
    void exportsCompleteMultirowCsv();
    void writesCsvThroughTransactionalFile();
    void keepsLegalNegativeNumbersOutOfFormulaNeutralization();
    void neutralizesOnlyRealFormulaAndPrecisionLossForms();
    void refusesToWriteCsvForEmptyTable();
    void loadsVersionedBackendCells();
    void acceptsSentinelConfidenceCellsInsteadOfRejectingWholeTable();
    void lenientParserKeepsDataWhenStrictParsingRejects();
    void lenientParserMarksEveryCellForReview();
    void lenientParserRejectsMissingCells();
    void acceptsNegativeReviewSentinelInsteadOfRejectingWholeTable();
    void stillRejectsBeyondSentinelNegatives();
    void keepsSentinelConfidenceOutOfTheAttentionRule();
    void ignoresWhitespaceOnlyCellsInTheAttentionRule();
    void stillRejectsMalformedCellFields();
    void findsProjectPythonBesideQtCreatorShadowBuild();
    void locatesProjectRootAcrossBuildLayouts();
    void mapsHighDpiCropToOriginalPixels();
    void mapsTouchCropToOriginalPixels();
    void cropModeConsumesPreviewClick();
    void publishesBackendResponseOnlyAfterNaturalExit();
    // 2026-10-28 新增：解释器定位的两条已披露缺陷（候选名兜底 / 首选判定）。
    void probesPlatformInterpreterCandidates();
    void prefersProductVenvOverPathSubstringMatch();
};

void CoreTests::normalizesRaggedRows()
{
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("名称") << QStringLiteral("数值"));
    rows << (QStringList() << QStringLiteral("带宽"));

    const TableData table = TableData::fromRows(rows);

    QCOMPARE(table.rowCount(), 2);
    QCOMPARE(table.columnCount(), 2);
    QCOMPARE(table.cell(1, 0), QStringLiteral("带宽"));
    QCOMPARE(table.cell(1, 1), QString());
}

void CoreTests::expandsWhenEditingOutsideCurrentBounds()
{
    TableData table;

    table.setCell(2, 3, QStringLiteral("QPSK"), 0.72);

    QCOMPARE(table.rowCount(), 3);
    QCOMPARE(table.columnCount(), 4);
    QCOMPARE(table.cell(2, 3), QStringLiteral("QPSK"));
    QCOMPARE(table.confidence(2, 3), 0.72);
    QCOMPARE(table.cell(0, 0), QString());
}

void CoreTests::exportsUtf8BomAndEscapesCsvFields()
{
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("名称") << QStringLiteral("说明"));
    rows << (QStringList() << QStringLiteral("A,B") << QStringLiteral("一行\n\"二行\""));
    const TableData table = TableData::fromRows(rows);

    const QByteArray csv = CsvExporter::encode(table);

    QVERIFY(csv.startsWith(QByteArray::fromHex("EFBBBF")));
    QCOMPARE(QString::fromUtf8(csv.mid(3)),
             QStringLiteral("名称,说明\r\n\"A,B\",\"一行\n\"\"二行\"\"\"\r\n"));
}

void CoreTests::exportsCompleteMultirowCsv()
{
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("编号") << QStringLiteral("频率")
                           << QStringLiteral("信号类型") << QStringLiteral("备注"));
    rows << (QStringList() << QStringLiteral("1") << QStringLiteral("515.128MHz")
                           << QStringLiteral("数字") << QStringLiteral("正常"));
    rows << (QStringList() << QStringLiteral("2") << QStringLiteral("516.347MHz")
                           << QStringLiteral("模拟") << QStringLiteral("需复核"));

    const QByteArray csv = CsvExporter::encode(TableData::fromRows(rows));

    QCOMPARE(QString::fromUtf8(csv.mid(3)),
             QStringLiteral("编号,频率,信号类型,备注\r\n"
                            "1,515.128MHz,数字,正常\r\n"
                            "2,516.347MHz,模拟,需复核\r\n"));
}

void CoreTests::writesCsvThroughTransactionalFile()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString output = temporary.filePath(QStringLiteral("result.csv"));
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("名称") << QStringLiteral("数值"));
    rows << (QStringList() << QStringLiteral("频率") << QStringLiteral("515.221"));

    QString error;
    QVERIFY2(CsvExporter::writeFile(output, TableData::fromRows(rows), &error),
             qPrintable(error));
    QFile file(output);
    QVERIFY(file.open(QIODevice::ReadOnly));
    const QByteArray data = file.readAll();
    QVERIFY(data.startsWith(QByteArray::fromHex("EFBBBF")));
    QVERIFY(data.contains("515.221"));
}

// 2026-10-28 回归：CSV 公式中和只打「真·公式前缀」和「真·丢精度」的形态。
// 缺陷版本对任何以 = + - @ 开头的文本都前置单引号，把合法负数 -12.5 写成
// '-12.5：导出值与识别原文不一致，Excel 里数字列变成文本（绿三角），
// pandas / SQL LOAD DATA 读到的也是带撇号的字符串。
void CoreTests::keepsLegalNegativeNumbersOutOfFormulaNeutralization()
{
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("-12.5") << QStringLiteral("-3")
                           << QStringLiteral("+1") << QStringLiteral("515.472")
                           << QStringLiteral("-12.5%"));

    const QByteArray csv = CsvExporter::encode(TableData::fromRows(rows));

    QCOMPARE(QString::fromUtf8(csv.mid(3)),
             QStringLiteral("-12.5,-3,+1,515.472,-12.5%\r\n"));
}

void CoreTests::neutralizesOnlyRealFormulaAndPrecisionLossForms()
{
    QVector<QStringList> rows;
    rows << (QStringList() << QStringLiteral("=cmd|'/c calc'!A0")
                           << QStringLiteral("-1+2")
                           << QStringLiteral("+SUM(A1)")
                           << QStringLiteral("@SUM(A1)"));
    rows << (QStringList() << QStringLiteral("123456789012345678")
                           << QStringLiteral("007")
                           << QStringLiteral("123456789012345")
                           << QStringLiteral("0.5"));

    const QByteArray csv = CsvExporter::encode(TableData::fromRows(rows));

    // 前四格：真·公式前缀（含 18 位数字、前导零编号之外的危险形态）必须中和；
    // 后四格：15 位以内数字、正常整数/小数不得加撇号。
    QCOMPARE(QString::fromUtf8(csv.mid(3)),
             QStringLiteral("'=cmd|'/c calc'!A0,'-1+2,'+SUM(A1),'@SUM(A1)\r\n"
                            "'123456789012345678,'007,123456789012345,0.5\r\n"));
}

void CoreTests::refusesToWriteCsvForEmptyTable()
{
    // 空表导出原来会成功写出一个只有 UTF-8 BOM 的 3 字节文件：Excel 打开是
    // "一个空单元格"，客户拿到"成功但空白"，必须显式失败。
    QCOMPARE(CsvExporter::encode(TableData()).size(), 0);

    QVector<QStringList> blankRows;
    blankRows << (QStringList() << QStringLiteral("   ") << QString());
    QCOMPARE(CsvExporter::encode(TableData::fromRows(blankRows)).size(), 0);

    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString output = temporary.filePath(QStringLiteral("empty.csv"));
    QString error;
    QVERIFY(!CsvExporter::writeFile(output, TableData(), &error));
    QVERIFY(!error.isEmpty());
    QVERIFY(!QFileInfo::exists(output));
}

void CoreTests::loadsVersionedBackendCells()
{
    const QByteArray json = "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":2,\"cells\":[["
                            "{\"text\":\"515.221\",\"confidence\":0.91,\"needs_review\":false},"
                            "{\"text\":\"\",\"confidence\":0.0,\"needs_review\":true}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();

    QString error;
    const TableData table = TableData::fromBackendJson(object, &error);

    QVERIFY2(error.isEmpty(), qPrintable(error));
    QCOMPARE(table.rowCount(), 1);
    QCOMPARE(table.columnCount(), 2);
    QCOMPARE(table.cell(0, 1), QString());
    QCOMPARE(table.confidence(0, 1), 0.0);
    QVERIFY(table.needsReview(0, 1));
}

void CoreTests::acceptsSentinelConfidenceCellsInsteadOfRejectingWholeTable()
{
    // 0.77 是后端硬写的保守上限哨兵值，不是实测置信度。整表因这种格子
    // 被拒收会让真实语料里 63.8% 的表完全不可用，这里必须放行；
    // MainWindow::showTable 会把它显示成浅黄待核对格。载荷采用纯 ASCII，
    // 不依赖源码编码，也不触发中文转义审查。
    const QByteArray json = "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":3,\"cells\":[["
                            "{\"text\":\"ORDER-1\",\"confidence\":0.86,\"needs_review\":false},"
                            "{\"text\":\"122-5686\",\"confidence\":0.77,\"needs_review\":false},"
                            "{\"text\":\"LATE\",\"confidence\":0.77,\"needs_review\":false}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString error;
    const TableData table = TableData::fromBackendJson(object, &error);

    QVERIFY2(error.isEmpty(), qPrintable(error));
    QCOMPARE(table.rowCount(), 1);
    QCOMPARE(table.columnCount(), 3);
    QCOMPARE(table.cell(0, 1), QStringLiteral("122-5686"));
    QCOMPARE(table.cell(0, 2), QStringLiteral("LATE"));
    QCOMPARE(table.confidence(0, 1), 0.77);
    // 放行不等于当成已确认：needs_review 保持后端给的值，
    // 显示层再按 confidence<0.78 补黄格。
    QVERIFY(!table.needsReview(0, 1));
}

void CoreTests::acceptsNegativeReviewSentinelInsteadOfRejectingWholeTable()
{
    // 2026-11-05（lead 复查，数据全部丢失 P0）：后端把"这一格不可信"编码成
    // 负的 confidence（-1.0 锚点格 / -2.0 判定为空），同时置 needs_review=true。
    // 旧实现见负值就整表拒收，被测用例 FC-00024 的 38 个 -1.0 格导致整表空白。
    // 现在负值按"复核哨兵"接受：落表夹到 0.0、强制 needsReview=true；
    // 只有真正畸形的读数（NaN/Inf/超界/类型错）才拒收。
    const QByteArray json = "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":3,\"cells\":[["
                            "{\"text\":\"OK-1\",\"confidence\":0.93,\"needs_review\":false},"
                            "{\"text\":\"UNTRUSTED\",\"confidence\":-1.0,\"needs_review\":true},"
                            "{\"text\":\"EMPTY-CANDIDATE\",\"confidence\":-2.0,\"needs_review\":true}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString error;
    const TableData table = TableData::fromBackendJson(object, &error);

    QVERIFY2(error.isEmpty(), qPrintable(error));
    QCOMPARE(table.rowCount(), 1);
    QCOMPARE(table.columnCount(), 3);
    // 负值格必须留下来，且置信度夹到 0.0（显示层仍按低置信处理）
    QCOMPARE(table.cell(0, 1), QStringLiteral("UNTRUSTED"));
    QCOMPARE(table.confidence(0, 1), 0.0);
    QVERIFY(table.needsReview(0, 1));
    QVERIFY(table.requiresAttention(0, 1));
    QCOMPARE(table.cell(0, 2), QStringLiteral("EMPTY-CANDIDATE"));
    QCOMPARE(table.confidence(0, 2), 0.0);
    QVERIFY(table.requiresAttention(0, 2));
    // 正常格不受影响
    QCOMPARE(table.confidence(0, 0), 0.93);
    QVERIFY(!table.requiresAttention(0, 0));
}

void CoreTests::stillRejectsBeyondSentinelNegatives()
{
    // 夹取边界：-2.0 是后端定义的最深哨兵；再往下（-3.0）视为畸形读数照旧拒收，
    // 避免"任何负值都放行"把真正的坏响应也当合法。
    const QByteArray json = "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":1,\"cells\":[["
                            "{\"text\":\"BAD\",\"confidence\":-3.0,\"needs_review\":true}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString error;
    const TableData table = TableData::fromBackendJson(object, &error);
    QVERIFY(!error.isEmpty());
    QCOMPARE(table.rowCount(), 0);
}

void CoreTests::keepsSentinelConfidenceOutOfTheAttentionRule()
{
    // 后端用可选字段 confidence_sentinel 显式表达「这格的 confidence 是类别
    // 哨兵」。显示层据此决定要不要补黄格：哨兵格不补，实测低置信格照旧补。
    // 老后端不写这个字段时按 false 处理（= 老行为）。
    const QByteArray json = "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":3,\"cells\":[["
                            "{\"text\":\"SENTINEL\",\"confidence\":0.77,\"needs_review\":false,\"confidence_sentinel\":true},"
                            "{\"text\":\"MEASURED\",\"confidence\":0.70,\"needs_review\":false},"
                            "{\"text\":\"OLDBACKEND\",\"confidence\":0.77,\"needs_review\":false}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString error;
    const TableData table = TableData::fromBackendJson(object, &error);

    QVERIFY2(error.isEmpty(), qPrintable(error));
    QCOMPARE(table.rowCount(), 1);
    QCOMPARE(table.columnCount(), 3);
    // 哨兵格：不是实测置信度，不算待复核。
    QVERIFY(table.confidenceSentinel(0, 0));
    QVERIFY(!table.requiresAttention(0, 0));
    // 实测低置信格：照旧算待复核。
    QVERIFY(!table.confidenceSentinel(0, 1));
    QCOMPARE(table.confidence(0, 1), 0.70);
    QVERIFY(table.requiresAttention(0, 1));
    // 老后端（没有该字段）：按 false 处理，仍是原来的待复核行为。
    QVERIFY(!table.confidenceSentinel(0, 2));
    QVERIFY(table.requiresAttention(0, 2));
    // 后端明确要求复核的格子，无论是否哨兵都要复核。
    TableData reviewed;
    reviewed.setCell(0, 0, QStringLiteral("X"), 0.77, true, true);
    QVERIFY(reviewed.requiresAttention(0, 0));
    QVERIFY(reviewed.confidenceSentinel(0, 0));
}

void CoreTests::ignoresWhitespaceOnlyCellsInTheAttentionRule()
{
    // 纯空白格（text 只有空格）不含任何内容。公开 SDK 用
    // !text.trimmed().isEmpty() 判它 valid；界面必须用同一条判据，
    // 否则同一个格子界面刷黄、SDK 判通过，界面误报。
    // 载荷用纯 ASCII，不依赖源码编码。
    const QByteArray json = "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":3,\"cells\":[["
                            "{\"text\":\"   \",\"confidence\":0.50,\"needs_review\":false},"
                            "{\"text\":\"X\",\"confidence\":0.50,\"needs_review\":false},"
                            "{\"text\":\"SENTINEL\",\"confidence\":0.77,\"needs_review\":false,"
                            "\"confidence_sentinel\":true}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString error;
    const TableData table = TableData::fromBackendJson(object, &error);

    QVERIFY2(error.isEmpty(), qPrintable(error));
    QCOMPARE(table.rowCount(), 1);
    QCOMPARE(table.columnCount(), 3);
    // 纯空白格 + 低置信 + 非哨兵：没有内容，不算待复核（不标黄）。
    QCOMPARE(table.cell(0, 0), QStringLiteral("   "));
    QVERIFY(!table.confidenceSentinel(0, 0));
    QVERIFY(!table.requiresAttention(0, 0));
    // 普通非空格 + 低置信 + 非哨兵：照旧算待复核。
    QVERIFY(table.requiresAttention(0, 1));
    // 哨兵格：不是实测置信度，不算待复核。
    QVERIFY(table.confidenceSentinel(0, 2));
    QVERIFY(!table.requiresAttention(0, 2));
    // 显式 needs_review 不受这条判据影响：纯空白格也必须复核。
    TableData reviewed;
    reviewed.setCell(0, 0, QStringLiteral("   "), 0.50, true, false);
    QVERIFY(reviewed.requiresAttention(0, 0));
}

void CoreTests::stillRejectsMalformedCellFields()
{
    // 放开低置信不等于放开格式校验。缺字段 / 类型错 / 越界的响应
    // 必须照常整表拒收，否则表格里会直接出现 "None" 之类的脏值。
    const QList<QByteArray> broken = QList<QByteArray>()
        << "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":2,\"cells\":[["
           "{\"text\":\"a\",\"confidence\":0.5},"
           "{\"text\":\"b\",\"confidence\":0.5,\"needs_review\":false}]]}"
        << "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":2,\"cells\":[["
           "{\"text\":\"a\",\"confidence\":0.5,\"needs_review\":\"no\"},"
           "{\"text\":\"b\",\"confidence\":0.5,\"needs_review\":false}]]}"
        << "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":2,\"cells\":[["
           "{\"text\":\"a\",\"confidence\":1.4,\"needs_review\":false},"
           "{\"text\":\"b\",\"confidence\":0.5,\"needs_review\":false}]]}"
        << "{\"protocol\":1,\"status\":\"ok\",\"rows\":1,\"columns\":2,\"cells\":[["
           "{\"text\":null,\"confidence\":0.5,\"needs_review\":false},"
           "{\"text\":\"b\",\"confidence\":0.5,\"needs_review\":false}]]}";

    for (const QByteArray &json : broken) {
        const QJsonObject object = QJsonDocument::fromJson(json).object();
        QString error;
        const TableData table = TableData::fromBackendJson(object, &error);
        QVERIFY2(!error.isEmpty(), json.constData());
        QCOMPARE(table.rowCount(), 0);
    }
}

void CoreTests::findsProjectPythonBesideQtCreatorShadowBuild()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString workspace = temporary.path();
    const QString project = workspace + QStringLiteral("/ocr-table-tool");
    const QString python = project + QStringLiteral("/.venv/Scripts/python.exe");
    const QString sourceBackend = project + QStringLiteral("/backend/ocr_backend.py");
    const QString backend = workspace
        + QStringLiteral("/build-ocr-table-tool-Debug/src/gui/bin/backend/ocr_backend.py");
    QVERIFY(QDir().mkpath(QFileInfo(python).absolutePath()));
    QVERIFY(QDir().mkpath(QFileInfo(sourceBackend).absolutePath()));
    QVERIFY(QDir().mkpath(QFileInfo(backend).absolutePath()));
    QFile pythonFile(python);
    QVERIFY(pythonFile.open(QIODevice::WriteOnly));
    pythonFile.close();
    QFile sourceBackendFile(sourceBackend);
    QVERIFY(sourceBackendFile.open(QIODevice::WriteOnly));
    sourceBackendFile.close();
    QFile backendFile(backend);
    QVERIFY(backendFile.open(QIODevice::WriteOnly));
    backendFile.close();

    QCOMPARE(BackendLocator::findPython(backend, QStringList() << QFileInfo(backend).absolutePath()),
             QFileInfo(python).absoluteFilePath());
}

void CoreTests::locatesProjectRootAcrossBuildLayouts()
{
    // 锁死「向上搜索项目根」这个行为：构建目录在源码树内（Qt Creator shadow build）
    // 和在源码树外（本机统一放 AP7000-Build-Directories）两种布局都必须能找到
    // 同一个项目根，并由此算出 .venv/Scripts/python.exe。
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString workspace = temporary.path();
    const QString projectRoot = workspace + QStringLiteral("/OcrTableTool");
    const QString python = projectRoot + QStringLiteral("/.venv/Scripts/python.exe");
    const QString projectFile = projectRoot + QStringLiteral("/ocr-table-tool.pro");

    QVERIFY(QDir().mkpath(QFileInfo(python).absolutePath()));
    QFile pythonFile(python);
    QVERIFY(pythonFile.open(QIODevice::WriteOnly));
    pythonFile.close();
    QFile projectFileHandle(projectFile);
    QVERIFY(projectFileHandle.open(QIODevice::WriteOnly));
    projectFileHandle.close();

    // 布局 A：构建目录在源码树内部。
    const QString shadowBuild = projectRoot
            + QStringLiteral("/build-ocr-table-tool-Release/tests/release");
    QVERIFY(QDir().mkpath(shadowBuild));
    const QString shadowRoot = findProjectRoot(QStringList() << shadowBuild);
    QCOMPARE(shadowRoot, QFileInfo(projectRoot).absoluteFilePath());
    QCOMPARE(projectPythonPath(shadowRoot), QFileInfo(python).absoluteFilePath());

    // 布局 B：构建目录在源码树外部，与源码树互为兄弟。
    const QString externalBuild = workspace
            + QStringLiteral("/AP7000-Build-Directories/OcrTableTool/build-msvc2015/tests/release");
    QVERIFY(QDir().mkpath(externalBuild));
    const QString externalRoot = findProjectRoot(QStringList() << externalBuild);
    QCOMPARE(externalRoot, QFileInfo(projectRoot).absoluteFilePath());
    QCOMPARE(projectPythonPath(externalRoot), QFileInfo(python).absoluteFilePath());

    // 没有标记的目录不能被当成项目根；找不到根时也不能编出一个 python 路径。
    const QString unrelated = temporary.filePath(QStringLiteral("unrelated"));
    QVERIFY(QDir().mkpath(unrelated));
    QVERIFY(projectRootMarker(QDir(unrelated)).isEmpty());
    QVERIFY(projectPythonPath(QString()).isEmpty());
}

void CoreTests::mapsHighDpiCropToOriginalPixels()
{
    ImagePreview preview;
    preview.setFixedSize(400, 320);
    QImage image(800, 400, QImage::Format_RGB32);
    image.fill(Qt::white);
    image.setDevicePixelRatio(2.0);
    preview.setImage(image);
    preview.show();
    QTest::qWait(20);

    QSignalSpy cropSpy(&preview, SIGNAL(cropSelected(QRect)));
    preview.beginCropSelection();
    QTest::mousePress(&preview, Qt::LeftButton, Qt::NoModifier, QPoint(50, 100));
    QMouseEvent moveEvent(QEvent::MouseMove,
                          QPointF(350, 220),
                          Qt::NoButton,
                          Qt::LeftButton,
                          Qt::NoModifier);
    QApplication::sendEvent(&preview, &moveEvent);
    QTest::mouseRelease(&preview, Qt::LeftButton, Qt::NoModifier, QPoint(350, 220));

    QCOMPARE(cropSpy.count(), 1);
    QCOMPARE(cropSpy.takeFirst().at(0).toRect(), QRect(89, 77, 628, 253));
}

void CoreTests::cropModeConsumesPreviewClick()
{
    ImagePreview preview;
    preview.resize(320, 240);
    QImage image(640, 480, QImage::Format_RGB32);
    image.fill(Qt::white);
    preview.setImage(image);
    preview.show();
    QTest::qWait(20);

    QSignalSpy activatedSpy(&preview, SIGNAL(activated()));
    preview.beginCropSelection();
    QTest::mouseClick(&preview, Qt::LeftButton, Qt::NoModifier, QPoint(160, 120));
    QCOMPARE(activatedSpy.count(), 0);

    preview.cancelCropSelection();
    QTest::mouseClick(&preview, Qt::LeftButton, Qt::NoModifier, QPoint(160, 120));
    QCOMPARE(activatedSpy.count(), 1);
}

void CoreTests::mapsTouchCropToOriginalPixels()
{
    ImagePreview preview;
    preview.setFixedSize(400, 320);
    QImage image(800, 400, QImage::Format_RGB32);
    image.fill(Qt::white);
    preview.setImage(image);
    preview.show();
    QTest::qWait(20);

    QTouchDevice device;
    device.setType(QTouchDevice::TouchScreen);
    device.setCapabilities(QTouchDevice::Position);
    QSignalSpy cropSpy(&preview, SIGNAL(cropSelected(QRect)));
    QSignalSpy activatedSpy(&preview, SIGNAL(activated()));
    preview.beginCropSelection();
    QTouchEvent::TouchPoint point(0);
    point.setPos(QPointF(50, 100));
    point.setState(Qt::TouchPointPressed);
    QTouchEvent beginEvent(QEvent::TouchBegin,
                           &device,
                           Qt::NoModifier,
                           Qt::TouchPointPressed,
                           QList<QTouchEvent::TouchPoint>() << point);
    QApplication::sendEvent(&preview, &beginEvent);
    point.setPos(QPointF(350, 220));
    point.setState(Qt::TouchPointMoved);
    QTouchEvent updateEvent(QEvent::TouchUpdate,
                            &device,
                            Qt::NoModifier,
                            Qt::TouchPointMoved,
                            QList<QTouchEvent::TouchPoint>() << point);
    QApplication::sendEvent(&preview, &updateEvent);
    point.setState(Qt::TouchPointReleased);
    QTouchEvent endEvent(QEvent::TouchEnd,
                         &device,
                         Qt::NoModifier,
                         Qt::TouchPointReleased,
                         QList<QTouchEvent::TouchPoint>() << point);
    QApplication::sendEvent(&preview, &endEvent);

    QCOMPARE(cropSpy.count(), 1);
    QCOMPARE(activatedSpy.count(), 0);
    QCOMPARE(cropSpy.takeFirst().at(0).toRect(), QRect(89, 77, 628, 253));
}

void CoreTests::publishesBackendResponseOnlyAfterNaturalExit()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString scriptPath = temporary.filePath(QStringLiteral("fake_backend.py"));
    const QString markerPath = temporary.filePath(QStringLiteral("natural-exit.txt"));
    QFile script(scriptPath);
    QVERIFY(script.open(QIODevice::WriteOnly | QIODevice::Text));
    const QString normalizedMarker = QDir::fromNativeSeparators(markerPath);
    script.write(QStringLiteral(
        "import json,sys,time\n"
        "request=json.loads(sys.stdin.read())\n"
        "print(json.dumps({'protocol':1,'status':'ok','request_id':request['request_id']}),flush=True)\n"
        "time.sleep(0.2)\n"
        "open(r'%1','w',encoding='utf-8').write('done')\n")
                     .arg(normalizedMarker)
                     .toUtf8());
    script.close();

    // 原来这里用「相对 exe 往上数 4 层」猜项目根，只在构建目录位于源码树内部
    // （Qt Creator shadow build）时成立；构建目录搬到 AP7000-Build-Directories 之后
    // 就会指到错误的根。改成向上逐级搜索项目根标记，两种布局都能定位。
    QString python = QString::fromLocal8Bit(qgetenv("OCR_TABLE_TEST_PYTHON"));
    if (python.isEmpty()) {
        python = projectPythonPath(findProjectRoot());
    }
    if (python.isEmpty()) {
        QFAIL(qPrintable(QStringLiteral("project root not found from %1")
                             .arg(QCoreApplication::applicationDirPath())));
    }
    QVERIFY2(QFileInfo::exists(python), qPrintable(python));
    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    {
        BackendRunner runner;
        QSignalSpy successSpy(&runner, SIGNAL(requestSucceeded(QString,QJsonObject)));
        QSignalSpy failureSpy(&runner, SIGNAL(requestFailed(QString,QString,QString,bool)));
        runner.recognize(QStringLiteral("ignored.png"), temporary.path(), QStringLiteral("auto"));
        QTRY_COMPARE(successSpy.count(), 1);
        QCOMPARE(failureSpy.count(), 0);
        QVERIFY(QFileInfo::exists(markerPath));
    }
    qunsetenv("OCR_TABLE_BACKEND");
    qunsetenv("OCR_TABLE_PYTHON");
}

// 解释器候选名兜底：过去找不到 venv 时写死裸名 python。麒麟 / 其它 Linux
// 发行版上解释器常常只叫 python3 ⇒ 必须按平台候选名逐个探测。
// 说明：Linux 侧的实际命中无法在本机（Windows）验证，这里锁的是探测机制本身。
void CoreTests::probesPlatformInterpreterCandidates()
{
    const QStringList names = BackendLocator::interpreterFileNames();
    QVERIFY(!names.isEmpty());
#ifdef Q_OS_WIN
    QCOMPARE(names.first(), QStringLiteral("python.exe"));
#else
    QCOMPARE(names.first(), QStringLiteral("python3"));
    QVERIFY(names.contains(QStringLiteral("python")));
#endif

    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString binDirectory = QDir(temporary.path()).filePath(QStringLiteral("bin"));
    QVERIFY(QDir().mkpath(binDirectory));
    const QString python3Path = QDir(binDirectory).filePath(QStringLiteral("python3"));
    QFile python3File(python3Path);
    QVERIFY(python3File.open(QIODevice::WriteOnly));
    python3File.write("#!/bin/sh\n");
    python3File.close();

    const QStringList candidates = QStringList()
        << QStringLiteral("python3") << QStringLiteral("python");
    // 目录里只有 python3 时也必须探到它（过去会落到裸名 python）。
    QCOMPARE(BackendLocator::findInterpreter(candidates, QStringList() << binDirectory),
             QFileInfo(python3Path).absoluteFilePath());

    // 两个候选都在时，优先级由候选名顺序决定：python3 先。
    const QString pythonPath = QDir(binDirectory).filePath(QStringLiteral("python"));
    QFile pythonFile(pythonPath);
    QVERIFY(pythonFile.open(QIODevice::WriteOnly));
    pythonFile.write("#!/bin/sh\n");
    pythonFile.close();
    QCOMPARE(BackendLocator::findInterpreter(candidates, QStringList() << binDirectory),
             QFileInfo(python3Path).absoluteFilePath());

    // 探测失败必须返回空串：绝不编出一个裸名字当解释器路径。
    const QString emptyDirectory = QDir(temporary.path()).filePath(QStringLiteral("empty"));
    QVERIFY(QDir().mkpath(emptyDirectory));
    QVERIFY(BackendLocator::findInterpreter(candidates, QStringList() << emptyDirectory).isEmpty());
}

// 首选判定：过去是 `originalRoot.contains(child.dirName())`（路径子串匹配），
// 同机其它工程的 venv 很容易命中 ⇒ 拿别人的解释器跑本产品后端。
// 现在要求 venv 里装了本产品依赖标记（cv2 / openvino / onnxruntime / rapidocr）。
void CoreTests::prefersProductVenvOverPathSubstringMatch()
{
    QTemporaryDir temporary;
    QVERIFY(temporary.isValid());
    const QString workspace = temporary.path();

    // AA-... 按目录名先被扫到，venv 是空的（同机别的工程）；
    // OcrTableTool 装了 cv2，才是本工程的 venv。
    const QString otherPython =
        makeFakeProjectVenv(workspace + QStringLiteral("/AA-other-project"), false);
    const QString productPython =
        makeFakeProjectVenv(workspace + QStringLiteral("/OcrTableTool"), true);
    QVERIFY2(!otherPython.isEmpty(), "fixture: other project venv");
    QVERIFY2(!productPython.isEmpty(), "fixture: product venv");
    QVERIFY(otherPython != productPython);

    const QString backend = workspace
        + QStringLiteral("/build/src/gui/bin/backend/ocr_backend.py");
    QVERIFY(QDir().mkpath(QFileInfo(backend).absolutePath()));
    QFile backendFile(backend);
    QVERIFY(backendFile.open(QIODevice::WriteOnly));
    backendFile.close();

    QVERIFY(BackendLocator::providesProductDependencies(
        workspace + QStringLiteral("/OcrTableTool/.venv")));
    QVERIFY(!BackendLocator::providesProductDependencies(
        workspace + QStringLiteral("/AA-other-project/.venv")));
    QVERIFY(!BackendLocator::providesProductDependencies(QString()));

    QCOMPARE(BackendLocator::findPython(backend, QStringList() << workspace), productPython);
}

void CoreTests::lenientParserKeepsDataWhenStrictParsingRejects()
{
    // 2026-11-10（用户实测反馈）：后端返回的响应未通过严格契约校验时
    // （这里 status 不是 ok、rows/columns 与实际不符），旧行为是**丢弃整表**、
    // 界面显示"识别组件返回的结果不完整，本次结果未被采用"。
    // 用户要求"任何情况一定要先传出数据出来，不要出现识别失败的情况"，
    // 所以宽松解析器必须把 cells 里的内容完整读出来。
    const QByteArray json = "{\"protocol\":1,\"status\":\"error\","
                            "\"rows\":99,\"columns\":99,\"cells\":["
                            "[{\"text\":\"A1\"},{\"text\":\"55\"}],"
                            "[{\"text\":\"B2\"},{\"text\":\"91\"}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString strictError;
    const TableData strict = TableData::fromBackendJson(object, &strictError);
    QVERIFY2(!strictError.isEmpty(), "strict parser should reject this payload");
    QCOMPARE(strict.rowCount(), 0);

    QString notice;
    const TableData lenient = TableData::fromBackendJsonLenient(object, &notice);
    QCOMPARE(lenient.rowCount(), 2);
    QCOMPARE(lenient.columnCount(), 2);
    QCOMPARE(lenient.cell(0, 0), QStringLiteral("A1"));
    QCOMPARE(lenient.cell(0, 1), QStringLiteral("55"));
    QCOMPARE(lenient.cell(1, 1), QStringLiteral("91"));
    QVERIFY2(!notice.isEmpty(), "notice should explain the downgrade");
}

void CoreTests::lenientParserMarksEveryCellForReview()
{
    // 降级发布的数据必须全部标黄待核对 —— 它能显示、能导出，
    // 但不允许被当成"已认证"结果。
    const QByteArray json = "{\"cells\":["
                            "[{\"text\":\"X\"},{\"text\":\"Y\"}],"
                            "[{\"text\":\"Z\"},{\"text\":\"W\"}]]}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString notice;
    const TableData lenient = TableData::fromBackendJsonLenient(object, &notice);
    QCOMPARE(lenient.rowCount(), 2);
    QCOMPARE(lenient.columnCount(), 2);
    for (int row = 0; row < lenient.rowCount(); ++row)
        for (int column = 0; column < lenient.columnCount(); ++column)
            QVERIFY2(lenient.needsReview(row, column),
                     "lenient cells must all be marked for review");
}

void CoreTests::lenientParserRejectsMissingCells()
{
    // 真的没有表格数据时仍然返回空表（界面给失败提示）。
    const QByteArray json = "{\"status\":\"error\",\"message\":\"boom\"}";
    const QJsonObject object = QJsonDocument::fromJson(json).object();
    QVERIFY2(!object.isEmpty(), "test JSON failed to parse");

    QString notice;
    const TableData lenient = TableData::fromBackendJsonLenient(object, &notice);
    QCOMPARE(lenient.rowCount(), 0);
    QVERIFY2(!notice.isEmpty(), "notice should explain the empty result");
}

QTEST_MAIN(CoreTests)

#include "tst_core.moc"
