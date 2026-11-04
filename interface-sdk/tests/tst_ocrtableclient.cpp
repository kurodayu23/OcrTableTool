#include <QtTest>

// 白盒断言需要读取 OcrTableClient 的私有**数据成员**，因此在包含产品头文件
// 之前临时把 private 打开成 public。必须先把要用到的 Qt 头文件全部包含进来
// （Qt 头文件里的 private 不能被改写），再打开这个开关。
#include <QByteArray>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonParseError>
#include <QObject>
#include <QStringList>
#include <QTemporaryDir>

// 产品文件导出的自由函数（外部链接）：单元测试直接声明并调用它。
// MSVC 把访问级别编进符号名，私有成员函数无法从测试 TU 调用。
namespace OcrTableSdk {
QStringList duplicatedJsonKeys(const QByteArray &json);
}

#define private public
#include "ocrtableclient.h"
#undef private

#include "cameraocrclient.h"

class OcrTableClientTests : public QObject
{
    Q_OBJECT

private:
    static QJsonObject validResponse()
    {
        QJsonObject cell;
        cell.insert(QStringLiteral("text"), QStringLiteral("A1"));
        cell.insert(QStringLiteral("confidence"), 0.99);
        cell.insert(QStringLiteral("needs_review"), false);
        QJsonArray columns;
        columns.append(cell);
        QJsonArray cells;
        cells.append(columns);
        QJsonArray spans;

        QJsonObject certificate;
        certificate.insert(QStringLiteral("version"), 1);
        certificate.insert(QStringLiteral("verified"), true);
        certificate.insert(QStringLiteral("rows"), 1);
        certificate.insert(QStringLiteral("columns"), 1);
        certificate.insert(QStringLiteral("spans"), spans);
        certificate.insert(QStringLiteral("geometry_hash"), QStringLiteral("geometry"));
        certificate.insert(QStringLiteral("structure_hash"), QStringLiteral("structure"));

        QJsonObject imageQuality;
        imageQuality.insert(QStringLiteral("issues"), QJsonArray());
        imageQuality.insert(QStringLiteral("issue_labels"), QJsonArray());
        imageQuality.insert(QStringLiteral("needs_recapture"), false);

        QJsonObject response;
        response.insert(QStringLiteral("protocol"), 1);
        response.insert(QStringLiteral("status"), QStringLiteral("ok"));
        response.insert(QStringLiteral("action"), QStringLiteral("recognize"));
        response.insert(QStringLiteral("request_id"), 1);
        response.insert(QStringLiteral("rows"), 1);
        response.insert(QStringLiteral("columns"), 1);
        response.insert(QStringLiteral("cells"), cells);
        response.insert(QStringLiteral("spans"), spans);
        response.insert(QStringLiteral("recognition_state"), QStringLiteral("verified"));
        response.insert(QStringLiteral("publication_blocked"), false);
        response.insert(QStringLiteral("publication_block_reasons"), QJsonArray());
        response.insert(QStringLiteral("structure_verified"), true);
        response.insert(QStringLiteral("structure_certificate"), certificate);
        response.insert(QStringLiteral("image_quality"), imageQuality);
        response.insert(QStringLiteral("rectified_image"), QStringLiteral("D:/work/rectified.png"));
        return response;
    }

private slots:
    void verifiedResponseCanAutoPublish()
    {
        QStringList reasons;
        QVERIFY(OcrTableClient::validateRecognitionResponse(validResponse(), &reasons));
        QVERIFY2(reasons.isEmpty(), qPrintable(reasons.join(QStringLiteral("; "))));
        QVERIFY(OcrTableClient::canAutoPublish(validResponse(), &reasons));
    }

    void reviewCellCannotAutoPublish()
    {
        QJsonObject response = validResponse();
        QJsonArray rows = response.value(QStringLiteral("cells")).toArray();
        QJsonArray columns = rows.at(0).toArray();
        QJsonObject cell = columns.at(0).toObject();
        cell.insert(QStringLiteral("needs_review"), true);
        columns.replace(0, cell);
        rows.replace(0, columns);
        response.insert(QStringLiteral("cells"), rows);

        QStringList reasons;
        QVERIFY(OcrTableClient::validateRecognitionResponse(response, &reasons));
        QVERIFY(!OcrTableClient::canAutoPublish(response, &reasons));
        QVERIFY(!reasons.isEmpty());
    }

    void unverifiedBlockedResultIsDisplayableButNotPublishable()
    {
        QJsonObject response = validResponse();
        response.insert(QStringLiteral("recognition_state"), QStringLiteral("blocked"));
        response.insert(QStringLiteral("publication_blocked"), true);
        response.insert(QStringLiteral("structure_verified"), false);
        response.insert(QStringLiteral("structure_certificate"), QJsonValue::Null);

        QStringList reasons;
        QVERIFY(OcrTableClient::validateRecognitionResponse(response, &reasons));
        QVERIFY(!OcrTableClient::canAutoPublish(response, &reasons));
    }

    void mergedSpanCannotHideSubordinateText()
    {
        QJsonObject response = validResponse();
        QJsonObject secondCell;
        secondCell.insert(QStringLiteral("text"), QStringLiteral("hidden"));
        secondCell.insert(QStringLiteral("confidence"), 0.99);
        secondCell.insert(QStringLiteral("needs_review"), false);
        QJsonArray columns = response.value(QStringLiteral("cells")).toArray().at(0).toArray();
        columns.append(secondCell);
        QJsonArray rows;
        rows.append(columns);
        response.insert(QStringLiteral("cells"), rows);
        response.insert(QStringLiteral("columns"), 2);

        QJsonObject span;
        span.insert(QStringLiteral("row"), 0);
        span.insert(QStringLiteral("column"), 0);
        span.insert(QStringLiteral("row_span"), 1);
        span.insert(QStringLiteral("column_span"), 2);
        QJsonArray spans;
        spans.append(span);
        response.insert(QStringLiteral("spans"), spans);
        QJsonObject certificate = response.value(QStringLiteral("structure_certificate")).toObject();
        certificate.insert(QStringLiteral("columns"), 2);
        certificate.insert(QStringLiteral("spans"), spans);
        response.insert(QStringLiteral("structure_certificate"), certificate);

        QStringList reasons;
        QVERIFY(!OcrTableClient::validateRecognitionResponse(response, &reasons));
        QVERIFY(reasons.join(QStringLiteral("; ")).contains(QStringLiteral("hides non-anchor text")));
    }

    void csvExportUsesUtf8BomAndQuotesSpecialText()
    {
        QJsonObject first;
        first.insert(QStringLiteral("text"), QStringLiteral("A,B"));
        QJsonObject second;
        second.insert(QStringLiteral("text"), QStringLiteral("line1\n\"line2\""));
        QJsonArray columns;
        columns.append(first);
        columns.append(second);
        QJsonArray cells;
        cells.append(columns);

        QTemporaryDir directory;
        QVERIFY(directory.isValid());
        const QString path = directory.filePath(QStringLiteral("result.csv"));
        QString errorMessage;
        QVERIFY2(OcrTableClient::exportCsv(path, cells, &errorMessage),
                 qPrintable(errorMessage));

        QFile file(path);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QByteArray actual = file.readAll();
        const QByteArray expected("\xEF\xBB\xBF\"A,B\",\"line1\n\"\"line2\"\"\"\r\n");
        QCOMPARE(actual, expected);
    }

    void csvExportNeutralizesSpreadsheetFormulas()
    {
        // Text starting with = + - @ is a formula in spreadsheet software.
        // The SDK must neutralize it the same way the product side exporter
        // (src/core/csvexporter.cpp) does, otherwise the payload runs when the
        // integration opens the exported file in Excel.
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
        const QString path = directory.filePath(QStringLiteral("formula.csv"));
        QString errorMessage;
        QVERIFY2(OcrTableClient::exportCsv(path, cells, &errorMessage),
                 qPrintable(errorMessage));

        QFile file(path);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QByteArray actual = file.readAll();
        // Formula cell prefixed with a single quote, comma cell still quoted,
        // plain cell untouched, UTF-8 BOM kept.
        const QByteArray expected("\xEF\xBB\xBF'=cmd|'/c calc'!A0,\"A,B\",OK\r\n");
        QCOMPARE(actual, expected);
    }
    void csvExportKeepsLegalNegativeNumbers()
    {
        // 2026-10-28：负数不是公式前缀。旧实现把首字符 - 一律中和，导出成
        // '-12.5，Excel 按文本处理、不参与求和。与产品侧 csvexporter.cpp 同口径。
        QJsonObject negative;   negative.insert(QStringLiteral("text"), QStringLiteral("-12.5"));
        QJsonObject scientific; scientific.insert(QStringLiteral("text"), QStringLiteral("-1.5e3"));
        QJsonObject percent;    percent.insert(QStringLiteral("text"), QStringLiteral("-12.5%"));
        QJsonObject formulaLike; formulaLike.insert(QStringLiteral("text"), QStringLiteral("-1+2"));
        QJsonObject zeroPadded; zeroPadded.insert(QStringLiteral("text"), QStringLiteral("007"));
        QJsonArray columns;
        columns.append(negative);
        columns.append(scientific);
        columns.append(percent);
        columns.append(formulaLike);
        columns.append(zeroPadded);
        QJsonArray cells;
        cells.append(columns);

        QTemporaryDir directory;
        QVERIFY(directory.isValid());
        const QString path = directory.filePath(QStringLiteral("numbers.csv"));
        QString errorMessage;
        QVERIFY2(OcrTableClient::exportCsv(path, cells, &errorMessage),
                 qPrintable(errorMessage));

        QFile file(path);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QByteArray actual = file.readAll();
        // 负数/科学计数/百分比原样；公式样 -1+2 与前导零编号 007 仍被中和。
        const QByteArray expected("\xEF\xBB\xBF-12.5,-1.5e3,-12.5%,'-1+2,'007\r\n");
        QCOMPARE(actual, expected);
    }

    void csvExportRejectsNonRectangularCells()
    {
        QJsonObject cell;
        cell.insert(QStringLiteral("text"), QStringLiteral("A"));
        QJsonArray firstRow;
        firstRow.append(cell);
        QJsonArray secondRow;
        QJsonArray cells;
        cells.append(firstRow);
        cells.append(secondRow);

        QTemporaryDir directory;
        QString errorMessage;
        QVERIFY(!OcrTableClient::exportCsv(
            directory.filePath(QStringLiteral("invalid.csv")),
            cells,
            &errorMessage));
        QVERIFY(!errorMessage.isEmpty());
    }

    void cameraFacadeEditsAppendsAndExportsCurrentTable()
    {
        QTemporaryDir directory;
        QVERIFY(directory.isValid());
        CameraOcrClient client(
            directory.filePath(QStringLiteral("missing-backend.exe")),
            directory.path());
        QJsonObject response = validResponse();
        QVERIFY(QMetaObject::invokeMethod(
            &client,
            "ocrRequestSucceeded",
            Qt::DirectConnection,
            Q_ARG(int, 1),
            Q_ARG(QString, QStringLiteral("recognize")),
            Q_ARG(QJsonObject, response)));

        QString errorMessage;
        QVERIFY2(client.setCellText(0, 0, QStringLiteral("修改值"), &errorMessage),
                 qPrintable(errorMessage));
        QJsonArray added;
        added.append(QStringLiteral("新增值"));
        QVERIFY2(client.appendRow(added, &errorMessage), qPrintable(errorMessage));
        QCOMPARE(client.tableCells().size(), 2);
        QCOMPARE(
            client.tableCells().at(0).toArray().at(0).toObject()
                .value(QStringLiteral("text")).toString(),
            QStringLiteral("修改值"));
        QCOMPARE(
            client.tableCells().at(1).toArray().at(0).toObject()
                .value(QStringLiteral("text")).toString(),
            QStringLiteral("新增值"));

        const QString csv = directory.filePath(QStringLiteral("edited.csv"));
        QVERIFY2(client.exportLastCsv(csv, &errorMessage), qPrintable(errorMessage));
        QFile file(csv);
        QVERIFY(file.open(QIODevice::ReadOnly));
        const QByteArray contents = file.readAll();
        QVERIFY(contents.contains(QStringLiteral("修改值").toUtf8()));
        QVERIFY(contents.contains(QStringLiteral("新增值").toUtf8()));
    }

    void duplicateConfidenceSentinelIsRejectedAsAmbiguous()
    {
        // 后端在同一个格子里重复给了 confidence_sentinel。
        // QJsonDocument 不声不响地保留最后一个值，所以解析结果里 false
        // 覆盖了 true —— 这正是 P0 发布判定被翻掉的那一步。
        const QByteArray raw =
            "{\"protocol\":1,\"status\":\"ok\",\"action\":\"recognize\","
            "\"request_id\":1,\"rows\":1,\"columns\":1,"
            "\"cells\":[[{\"text\":\"A\",\"confidence\":0.77,"
            "\"needs_review\":false,\"confidence_sentinel\":true,"
            "\"confidence_sentinel\":false}]]}";

        // 先确认陷阱确实存在：解析结果只认最后一个值。
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(raw, &parseError);
        QCOMPARE(parseError.error, QJsonParseError::NoError);
        const QJsonObject cell = document.object()
                                     .value(QStringLiteral("cells")).toArray()
                                     .at(0).toArray().at(0).toObject();
        QCOMPARE(cell.value(QStringLiteral("confidence_sentinel")).toBool(), false);

        // SDK 必须能从原始字节里看出这份响应有歧义。
        const QStringList duplicated = OcrTableSdk::duplicatedJsonKeys(raw);
        QVERIFY2(duplicated.contains(QStringLiteral("confidence_sentinel")),
                 qPrintable(duplicated.join(QStringLiteral(", "))));

        // 客户端路径必须拒绝这份响应，而不是默默相信其中一个值。
        OcrTableClient client(QStringLiteral("missing-backend.exe"));
        // 把输出管道打开，避开读取未启动进程时的 Qt 警告。
        client.m_process->start(QStringLiteral("cmd.exe"),
                                QStringList() << QStringLiteral("/c")
                                              << QStringLiteral("ping -n 30 127.0.0.1 > nul"));
        client.m_busy = true;
        client.m_activeRequestId = 1;
        client.m_activeAction = QStringLiteral("recognize");
        client.m_standardOutput = raw + "\n";
        QVERIFY(QMetaObject::invokeMethod(&client, "readStandardOutput", Qt::DirectConnection));
        QCOMPARE(client.m_forcedErrorCode, QStringLiteral("AMBIGUOUS_RESPONSE"));
        QVERIFY(client.m_pendingResponse.isEmpty());
    }

    void verdictReasonSurvivesCellReasonFlood()
    {
        // 200 个坏格子：每格都是低置信度且没有哨兵标记，
        // 逐格原因足以把整个原因额度占满。
        const int rowCount = 20;
        const int columnCount = 10;
        QJsonArray rows;
        for (int row = 0; row < rowCount; ++row) {
            QJsonArray columns;
            for (int column = 0; column < columnCount; ++column) {
                QJsonObject cell;
                cell.insert(QStringLiteral("text"), QStringLiteral("A"));
                cell.insert(QStringLiteral("confidence"), 0.5);
                cell.insert(QStringLiteral("needs_review"), false);
                columns.append(cell);
            }
            rows.append(columns);
        }

        QJsonObject response = validResponse();
        response.insert(QStringLiteral("rows"), rowCount);
        response.insert(QStringLiteral("columns"), columnCount);
        response.insert(QStringLiteral("cells"), rows);
        response.insert(QStringLiteral("recognition_state"), QStringLiteral("needs_review"));
        response.insert(QStringLiteral("publication_blocked"), false);
        QJsonObject certificate =
                response.value(QStringLiteral("structure_certificate")).toObject();
        certificate.insert(QStringLiteral("rows"), rowCount);
        certificate.insert(QStringLiteral("columns"), columnCount);
        response.insert(QStringLiteral("structure_certificate"), certificate);

        QStringList reasons;
        QVERIFY(!OcrTableClient::canAutoPublish(response, &reasons));
        // 解释整体判定的那条原因必须还在，而且排在最前面。
        QVERIFY2(reasons.contains(QStringLiteral("recognition_state is not verified")),
                 qPrintable(reasons.join(QStringLiteral("; "))));
        QCOMPARE(reasons.first(), QStringLiteral("recognition_state is not verified"));
        // 总数仍然有界。
        QVERIFY2(reasons.size() <= 100, qPrintable(QString::number(reasons.size())));
    }

    void cameraFacadeValidatesNormalizedTableRegion()
    {
        QTemporaryDir directory;
        QVERIFY(directory.isValid());
        CameraOcrClient client(
            directory.filePath(QStringLiteral("missing-backend.exe")),
            directory.path());
        QString errorMessage;

        const QRectF region(0.08, 0.12, 0.84, 0.72);
        QVERIFY2(client.setTableRegion(region, &errorMessage), qPrintable(errorMessage));
        QCOMPARE(client.tableRegion(), region);
        // 框选区域按批准口径留边距：左右 2%、上 8%、下 4%。
        // 0.08/0.12/0.84/0.72 → 请求矩形 (80,60,840,360)，
        // 左右各 17 px、上 29 px、下 15 px → (63,31,874,404)。
        QCOMPARE(client.resolvedTableRegion(QSize(1000, 500)),
                 QRect(63, 31, 874, 404));
        QVERIFY(errorMessage.isEmpty());

        QVERIFY(!client.setTableRegion(QRectF(-0.1, 0.1, 0.5, 0.5), &errorMessage));
        QVERIFY(!errorMessage.isEmpty());
        QCOMPARE(client.tableRegion(), region);

        client.clearTableRegion();
        QVERIFY(!client.tableRegion().isValid());
        QVERIFY(!client.resolvedTableRegion(QSize(1000, 500)).isValid());
    }
};

QTEST_APPLESS_MAIN(OcrTableClientTests)
#include "tst_ocrtableclient.moc"
