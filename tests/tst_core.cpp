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

#include "backendrunner.h"
#include "backendlocator.h"
#include "csvexporter.h"
#include "imagepreview.h"
#include "tabledata.h"

class CoreTests : public QObject
{
    Q_OBJECT

private slots:
    void normalizesRaggedRows();
    void expandsWhenEditingOutsideCurrentBounds();
    void exportsUtf8BomAndEscapesCsvFields();
    void exportsCompleteMultirowCsv();
    void writesCsvThroughTransactionalFile();
    void loadsVersionedBackendCells();
    void findsProjectPythonBesideQtCreatorShadowBuild();
    void mapsHighDpiCropToOriginalPixels();
    void cropModeConsumesPreviewClick();
    void publishesBackendResponseOnlyAfterNaturalExit();
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

    const QString projectRoot = QDir(QCoreApplication::applicationDirPath())
                                    .absoluteFilePath(QStringLiteral("../../../.."));
    QString python = QString::fromLocal8Bit(qgetenv("OCR_TABLE_TEST_PYTHON"));
    if (python.isEmpty()) {
        python = QDir(projectRoot).filePath(
            QStringLiteral(".venv/Scripts/python.exe"));
    }
    QVERIFY2(QFileInfo::exists(python), qPrintable(python));
    qputenv("OCR_TABLE_BACKEND", QFile::encodeName(scriptPath));
    qputenv("OCR_TABLE_PYTHON", QFile::encodeName(python));
    {
        BackendRunner runner;
        QSignalSpy successSpy(&runner, SIGNAL(requestSucceeded(QString,QJsonObject)));
        QSignalSpy failureSpy(&runner, SIGNAL(requestFailed(QString,QString)));
        runner.recognize(QStringLiteral("ignored.png"), temporary.path(), QStringLiteral("auto"));
        QTRY_COMPARE(successSpy.count(), 1);
        QCOMPARE(failureSpy.count(), 0);
        QVERIFY(QFileInfo::exists(markerPath));
    }
    qunsetenv("OCR_TABLE_BACKEND");
    qunsetenv("OCR_TABLE_PYTHON");
}

QTEST_MAIN(CoreTests)

#include "tst_core.moc"
