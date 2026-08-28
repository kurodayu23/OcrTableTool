#include <QtTest>

#include "ocrtableclient.h"
#include "cameraocrclient.h"

#include <QJsonArray>
#include <QJsonObject>
#include <QFile>
#include <QTemporaryDir>

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
        QCOMPARE(client.resolvedTableRegion(QSize(1000, 500)),
                 QRect(67, 51, 866, 378));
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
