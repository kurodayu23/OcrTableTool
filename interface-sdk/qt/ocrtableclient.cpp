#include "ocrtableclient.h"

#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QProcess>
#include <QSaveFile>
#include <QSet>
#include <QTimer>
#include <QVector>
#include <QtGlobal>

namespace
{
const int kProtocolVersion = 1;
const int kMaximumRequestId = 2147483647;
const int kMaximumRows = 256;
const int kMaximumColumns = 128;
const int kMaximumCells = 4096;

bool readInteger(const QJsonValue &value, int minimum, int maximum, int *result)
{
    if (!value.isDouble())
        return false;
    const int integer = value.toInt(minimum - 1);
    if (value.toDouble() != static_cast<double>(integer)
        || integer < minimum
        || integer > maximum) {
        return false;
    }
    if (result)
        *result = integer;
    return true;
}

void addReason(QStringList *reasons, const QString &reason)
{
    if (reasons && reasons->size() < 100)
        reasons->append(reason);
}

bool jsonArraysEqual(const QJsonArray &left, const QJsonArray &right)
{
    return QJsonDocument(left).toJson(QJsonDocument::Compact)
        == QJsonDocument(right).toJson(QJsonDocument::Compact);
}
}

OcrTableClient::OcrTableClient(const QString &backendExecutable, QObject *parent)
    : QObject(parent)
    , m_backendExecutable(QFileInfo(backendExecutable).absoluteFilePath())
    , m_process(new QProcess(this))
    , m_requestTimer(new QTimer(this))
    , m_forcedRetryable(false)
    , m_nextRequestId(1)
    , m_activeRequestId(0)
    , m_busy(false)
{
    m_requestTimer->setSingleShot(true);
    m_process->setProcessChannelMode(QProcess::SeparateChannels);
    connect(m_process, SIGNAL(readyReadStandardOutput()), this, SLOT(readStandardOutput()));
    connect(m_process, SIGNAL(readyReadStandardError()), this, SLOT(readStandardError()));
    connect(m_process, SIGNAL(finished(int,QProcess::ExitStatus)), this, SLOT(processFinished(int)));
    connect(m_process, SIGNAL(errorOccurred(QProcess::ProcessError)), this, SLOT(processError()));
    connect(m_requestTimer, SIGNAL(timeout()), this, SLOT(requestTimedOut()));
}

OcrTableClient::~OcrTableClient()
{
    if (m_process->state() != QProcess::NotRunning) {
        m_process->kill();
        m_process->waitForFinished(1000);
    }
}

bool OcrTableClient::isReady() const
{
    return QFileInfo::exists(m_backendExecutable);
}

bool OcrTableClient::isBusy() const
{
    return m_busy;
}

QString OcrTableClient::backendExecutable() const
{
    return m_backendExecutable;
}

int OcrTableClient::health()
{
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), kProtocolVersion);
    request.insert(QStringLiteral("action"), QStringLiteral("health"));
    return startRequest(QStringLiteral("health"), request, 10000);
}

int OcrTableClient::recognize(const QString &imagePath,
                              const QString &outputDirectory,
                              bool inputRectified)
{
    QJsonObject options;
    options.insert(QStringLiteral("crop_mode"), QStringLiteral("auto"));
    options.insert(QStringLiteral("accuracy_mode"), QStringLiteral("maximum"));
    options.insert(QStringLiteral("deadline_seconds"), 0);
    if (inputRectified)
        options.insert(QStringLiteral("input_rectified"), true);

    QJsonObject request;
    request.insert(QStringLiteral("protocol"), kProtocolVersion);
    request.insert(QStringLiteral("action"), QStringLiteral("recognize"));
    request.insert(QStringLiteral("image_path"), QFileInfo(imagePath).absoluteFilePath());
    request.insert(QStringLiteral("output_directory"), QDir(outputDirectory).absolutePath());
    request.insert(QStringLiteral("options"), options);
    return startRequest(QStringLiteral("recognize"), request, 0);
}

int OcrTableClient::exportXlsx(const QString &outputPath,
                               const QJsonArray &cells,
                               const QJsonArray &spans)
{
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), kProtocolVersion);
    request.insert(QStringLiteral("action"), QStringLiteral("export_xlsx"));
    request.insert(QStringLiteral("output_path"), QFileInfo(outputPath).absoluteFilePath());
    request.insert(QStringLiteral("cells"), cells);
    request.insert(QStringLiteral("spans"), spans);
    return startRequest(QStringLiteral("export_xlsx"), request, 30000);
}

bool OcrTableClient::exportCsv(const QString &outputPath,
                               const QJsonArray &cells,
                               QString *errorMessage)
{
    const QString invalidData = QString::fromWCharArray(
        L"\u8868\u683c\u6570\u636e\u4e0d\u662f\u5b8c\u6574\u7684\u4e8c\u7ef4\u5355\u5143\u683c\u6570\u7ec4\u3002");
    if (cells.isEmpty()) {
        if (errorMessage)
            *errorMessage = invalidData;
        return false;
    }

    int expectedColumns = -1;
    QByteArray csv("\xEF\xBB\xBF", 3);
    for (int row = 0; row < cells.size(); ++row) {
        if (!cells.at(row).isArray()) {
            if (errorMessage)
                *errorMessage = invalidData;
            return false;
        }
        const QJsonArray columns = cells.at(row).toArray();
        if (expectedColumns < 0)
            expectedColumns = columns.size();
        if (expectedColumns <= 0 || columns.size() != expectedColumns) {
            if (errorMessage)
                *errorMessage = invalidData;
            return false;
        }
        for (int column = 0; column < columns.size(); ++column) {
            if (!columns.at(column).isObject()
                || !columns.at(column).toObject()
                        .value(QStringLiteral("text")).isString()) {
                if (errorMessage)
                    *errorMessage = invalidData;
                return false;
            }
            QString text = columns.at(column).toObject()
                .value(QStringLiteral("text")).toString();
            const bool quote = text.contains(QLatin1Char(','))
                || text.contains(QLatin1Char('"'))
                || text.contains(QLatin1Char('\r'))
                || text.contains(QLatin1Char('\n'));
            if (quote) {
                text.replace(QStringLiteral("\""), QStringLiteral("\"\""));
                text.prepend(QLatin1Char('"'));
                text.append(QLatin1Char('"'));
            }
            if (column > 0)
                csv.append(',');
            csv.append(text.toUtf8());
        }
        csv.append("\r\n", 2);
    }

    const QFileInfo outputInfo(outputPath);
    if (outputInfo.fileName().isEmpty()
        || !QDir().mkpath(outputInfo.absolutePath())) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"\u65e0\u6cd5\u521b\u5efaCSV\u8f93\u51fa\u76ee\u5f55\u3002");
        }
        return false;
    }
    QSaveFile file(outputInfo.absoluteFilePath());
    if (!file.open(QIODevice::WriteOnly)
        || file.write(csv) != csv.size()
        || !file.commit()) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"CSV\u6587\u4ef6\u5199\u5165\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u8def\u5f84\u548c\u5199\u5165\u6743\u9650\u3002");
        }
        return false;
    }
    if (errorMessage)
        errorMessage->clear();
    return true;
}

void OcrTableClient::cancel()
{
    if (!m_busy)
        return;
    m_requestTimer->stop();
    setForcedFailure(QStringLiteral("CANCELLED"),
                     QStringLiteral("The OCR request was cancelled."),
                     true);
    if (m_process->state() != QProcess::NotRunning)
        m_process->kill();
}

int OcrTableClient::startRequest(const QString &action,
                                 QJsonObject request,
                                 int timeoutMilliseconds)
{
    if (m_busy) {
        failLocally(action,
                    QStringLiteral("CLIENT_BUSY"),
                    QString(),
                    QStringLiteral("Another OCR request is already running."),
                    true);
        return 0;
    }
    if (!isReady()) {
        failLocally(action,
                    QStringLiteral("BACKEND_NOT_FOUND"),
                    QStringLiteral("backend_executable"),
                    QStringLiteral("OcrBackend.exe was not found."),
                    false);
        return 0;
    }
    if (m_process->state() != QProcess::NotRunning) {
        failLocally(action,
                    QStringLiteral("BACKEND_STILL_EXITING"),
                    QString(),
                    QStringLiteral("The previous backend process is still exiting."),
                    true);
        return 0;
    }

    const int requestId = m_nextRequestId;
    m_nextRequestId = m_nextRequestId == kMaximumRequestId ? 1 : m_nextRequestId + 1;
    request.insert(QStringLiteral("request_id"), requestId);

    m_process->setProgram(m_backendExecutable);
    m_process->setArguments(QStringList());
    m_process->setWorkingDirectory(QFileInfo(m_backendExecutable).absolutePath());
    m_process->start();
    if (!m_process->waitForStarted(3000)) {
        failLocally(action,
                    QStringLiteral("BACKEND_START_FAILED"),
                    QString(),
                    m_process->errorString(),
                    true);
        return 0;
    }

    m_activeRequestId = requestId;
    m_activeAction = action;
    m_standardOutput.clear();
    m_standardError.clear();
    m_pendingResponse = QJsonObject();
    m_forcedErrorCode.clear();
    m_forcedErrorMessage.clear();
    m_forcedRetryable = false;
    m_busy = true;
    emit busyChanged(true);
    emit requestStarted(requestId, action);

    QByteArray payload = QJsonDocument(request).toJson(QJsonDocument::Compact);
    payload.append('\n');
    if (m_process->write(payload) != payload.size()) {
        setForcedFailure(QStringLiteral("BACKEND_WRITE_FAILED"),
                         QStringLiteral("The OCR request could not be written."),
                         true);
        m_process->kill();
        return requestId;
    }
    // EOF tells the non-persistent backend to finish this single request and exit.
    m_process->closeWriteChannel();
    if (timeoutMilliseconds > 0)
        m_requestTimer->start(timeoutMilliseconds);
    return requestId;
}

void OcrTableClient::readStandardOutput()
{
    m_standardOutput += m_process->readAllStandardOutput();
    int newline = m_standardOutput.indexOf('\n');
    while (newline >= 0) {
        const QByteArray line = m_standardOutput.left(newline).trimmed();
        m_standardOutput.remove(0, newline + 1);
        if (!line.isEmpty())
            consumeResponseLine(line);
        newline = m_standardOutput.indexOf('\n');
    }
}

void OcrTableClient::consumeResponseLine(const QByteArray &line)
{
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        emit logMessage(QStringLiteral("Ignored a non-JSON backend output line."));
        return;
    }
    if (!m_busy)
        return;

    const QJsonObject response = document.object();
    int responseRequestId = 0;
    if (!readInteger(response.value(QStringLiteral("request_id")),
                     1,
                     kMaximumRequestId,
                     &responseRequestId)
        || responseRequestId != m_activeRequestId) {
        emit logMessage(QStringLiteral("Ignored a stale response with a different request_id."));
        return;
    }
    if (response.value(QStringLiteral("action")).toString() != m_activeAction) {
        setForcedFailure(QStringLiteral("RESPONSE_ACTION_MISMATCH"),
                         QStringLiteral("The backend response action does not match the request."),
                         false);
        return;
    }
    if (m_pendingResponse.isEmpty())
        m_pendingResponse = response;
}

void OcrTableClient::readStandardError()
{
    const QByteArray output = m_process->readAllStandardError();
    m_standardError += output;
    if (m_standardError.size() > 16000)
        m_standardError = m_standardError.right(16000);
    const QString message = QString::fromLocal8Bit(output).trimmed();
    if (!message.isEmpty())
        emit logMessage(message);
}

void OcrTableClient::processFinished(int exitCode)
{
    readStandardOutput();
    readStandardError();
    if (!m_standardOutput.trimmed().isEmpty())
        consumeResponseLine(m_standardOutput.trimmed());
    if (!m_busy)
        return;

    const int requestId = m_activeRequestId;
    const QString action = m_activeAction;
    const QString forcedCode = m_forcedErrorCode;
    const QString forcedMessage = m_forcedErrorMessage;
    const bool forcedRetryable = m_forcedRetryable;
    const QJsonObject response = m_pendingResponse;
    const QString diagnostics = QString::fromLocal8Bit(m_standardError).trimmed().right(4000);
    finishRequestState();

    if (!forcedCode.isEmpty()) {
        emit requestFailed(requestId,
                           action,
                           forcedCode,
                           QString(),
                           forcedMessage,
                           forcedRetryable);
        return;
    }
    if (response.isEmpty()) {
        emit requestFailed(requestId,
                           action,
                           QStringLiteral("BACKEND_EXITED"),
                           QString(),
                           diagnostics.isEmpty()
                               ? QStringLiteral("The backend exited without a JSON response.")
                               : diagnostics,
                           true);
        return;
    }
    if (response.value(QStringLiteral("protocol")).toInt(-1) != kProtocolVersion) {
        emit requestFailed(requestId,
                           action,
                           QStringLiteral("PROTOCOL_MISMATCH"),
                           QStringLiteral("protocol"),
                           QStringLiteral("The backend protocol version is not supported."),
                           false);
        return;
    }
    if (response.value(QStringLiteral("status")).toString() == QStringLiteral("ok")
        && exitCode == 0) {
        emit requestSucceeded(requestId, action, response);
        return;
    }
    emit requestFailed(requestId,
                       action,
                       response.value(QStringLiteral("error_code"))
                           .toString(QStringLiteral("BACKEND_ERROR")),
                       response.value(QStringLiteral("field")).toString(),
                       response.value(QStringLiteral("message"))
                           .toString(QStringLiteral("The OCR request failed.")),
                       response.value(QStringLiteral("retryable")).toBool(false));
}

void OcrTableClient::processError()
{
    if (!m_busy)
        return;
    if (m_process->state() == QProcess::NotRunning && m_forcedErrorCode.isEmpty()) {
        setForcedFailure(QStringLiteral("BACKEND_PROCESS_ERROR"),
                         m_process->errorString(),
                         true);
    }
}

void OcrTableClient::requestTimedOut()
{
    if (!m_busy)
        return;
    setForcedFailure(QStringLiteral("TIMEOUT"),
                     QStringLiteral("The OCR request timed out."),
                     true);
    if (m_process->state() != QProcess::NotRunning)
        m_process->kill();
}

void OcrTableClient::setForcedFailure(const QString &errorCode,
                                      const QString &message,
                                      bool retryable)
{
    if (!m_forcedErrorCode.isEmpty())
        return;
    m_forcedErrorCode = errorCode;
    m_forcedErrorMessage = message;
    m_forcedRetryable = retryable;
}

void OcrTableClient::finishRequestState()
{
    m_requestTimer->stop();
    m_standardOutput.clear();
    m_standardError.clear();
    m_pendingResponse = QJsonObject();
    m_activeAction.clear();
    m_forcedErrorCode.clear();
    m_forcedErrorMessage.clear();
    m_forcedRetryable = false;
    m_activeRequestId = 0;
    m_busy = false;
    emit busyChanged(false);
}

void OcrTableClient::failLocally(const QString &action,
                                 const QString &errorCode,
                                 const QString &field,
                                 const QString &message,
                                 bool retryable)
{
    emit requestFailed(0, action, errorCode, field, message, retryable);
}

bool OcrTableClient::validateRecognitionResponse(const QJsonObject &response,
                                                 QStringList *reasons)
{
    QStringList failures;
    if (response.value(QStringLiteral("protocol")).toInt(-1) != kProtocolVersion)
        addReason(&failures, QStringLiteral("protocol is not 1"));
    if (response.value(QStringLiteral("status")).toString() != QStringLiteral("ok"))
        addReason(&failures, QStringLiteral("status is not ok"));
    if (response.value(QStringLiteral("action")).toString() != QStringLiteral("recognize"))
        addReason(&failures, QStringLiteral("action is not recognize"));
    int requestId = 0;
    if (!readInteger(response.value(QStringLiteral("request_id")),
                     1,
                     kMaximumRequestId,
                     &requestId)) {
        addReason(&failures, QStringLiteral("request_id is invalid"));
    }

    int rows = 0;
    int columns = 0;
    if (!readInteger(response.value(QStringLiteral("rows")), 1, kMaximumRows, &rows))
        addReason(&failures, QStringLiteral("rows is invalid"));
    if (!readInteger(response.value(QStringLiteral("columns")), 1, kMaximumColumns, &columns))
        addReason(&failures, QStringLiteral("columns is invalid"));
    if (rows > 0 && columns > 0 && rows * columns > kMaximumCells)
        addReason(&failures, QStringLiteral("cell count exceeds the V1 limit"));

    const QJsonValue cellsValue = response.value(QStringLiteral("cells"));
    const QJsonArray cellRows = cellsValue.toArray();
    if (!cellsValue.isArray() || cellRows.size() != rows) {
        addReason(&failures, QStringLiteral("cells row count does not match rows"));
    } else {
        for (int row = 0; row < cellRows.size(); ++row) {
            if (!cellRows.at(row).isArray()) {
                addReason(&failures, QStringLiteral("cells[%1] is not an array").arg(row));
                continue;
            }
            const QJsonArray cellColumns = cellRows.at(row).toArray();
            if (cellColumns.size() != columns) {
                addReason(&failures, QStringLiteral("cells[%1] column count is invalid").arg(row));
                continue;
            }
            for (int column = 0; column < cellColumns.size(); ++column) {
                if (!cellColumns.at(column).isObject()) {
                    addReason(&failures,
                              QStringLiteral("cells[%1][%2] is not an object")
                                  .arg(row).arg(column));
                    continue;
                }
                const QJsonObject cell = cellColumns.at(column).toObject();
                const QJsonValue text = cell.value(QStringLiteral("text"));
                const QJsonValue confidence = cell.value(QStringLiteral("confidence"));
                const QJsonValue needsReview = cell.value(QStringLiteral("needs_review"));
                const double confidenceValue = confidence.toDouble(-1.0);
                if (!text.isString()
                    || !confidence.isDouble()
                    || !qIsFinite(confidenceValue)
                    || confidenceValue < 0.0
                    || confidenceValue > 1.0
                    || !needsReview.isBool()) {
                    addReason(&failures,
                              QStringLiteral("cells[%1][%2] fields are invalid")
                                  .arg(row).arg(column));
                } else if (!text.toString().trimmed().isEmpty()
                           && confidenceValue < 0.78
                           && !needsReview.toBool()) {
                    addReason(&failures,
                              QStringLiteral("cells[%1][%2] has unmarked low confidence")
                                  .arg(row).arg(column));
                }
            }
        }
    }

    const QJsonValue spansValue = response.value(QStringLiteral("spans"));
    const QJsonArray spans = spansValue.toArray();
    QVector<bool> occupied(qMax(0, rows * columns), false);
    if (!spansValue.isArray()) {
        addReason(&failures, QStringLiteral("spans is not an array"));
    } else {
        for (int index = 0; index < spans.size(); ++index) {
            if (!spans.at(index).isObject()) {
                addReason(&failures, QStringLiteral("spans[%1] is not an object").arg(index));
                continue;
            }
            const QJsonObject span = spans.at(index).toObject();
            int row = 0;
            int column = 0;
            int rowSpan = 0;
            int columnSpan = 0;
            const bool fieldsValid = readInteger(span.value(QStringLiteral("row")),
                                                 0,
                                                 qMax(0, rows - 1),
                                                 &row)
                && readInteger(span.value(QStringLiteral("column")),
                               0,
                               qMax(0, columns - 1),
                               &column)
                && readInteger(span.value(QStringLiteral("row_span")),
                               1,
                               qMax(1, rows),
                               &rowSpan)
                && readInteger(span.value(QStringLiteral("column_span")),
                               1,
                               qMax(1, columns),
                               &columnSpan)
                && row + rowSpan <= rows
                && column + columnSpan <= columns;
            if (!fieldsValid) {
                addReason(&failures, QStringLiteral("spans[%1] is out of bounds").arg(index));
                continue;
            }
            for (int coveredRow = row; coveredRow < row + rowSpan; ++coveredRow) {
                for (int coveredColumn = column;
                     coveredColumn < column + columnSpan;
                     ++coveredColumn) {
                    const int position = coveredRow * columns + coveredColumn;
                    if (position < 0 || position >= occupied.size())
                        continue;
                    if (occupied.at(position))
                        addReason(&failures,
                                  QStringLiteral("spans[%1] overlaps another span").arg(index));
                    occupied[position] = true;
                    if ((coveredRow != row || coveredColumn != column)
                        && coveredRow < cellRows.size()
                        && cellRows.at(coveredRow).isArray()) {
                        const QJsonArray coveredColumns = cellRows.at(coveredRow).toArray();
                        if (coveredColumn < coveredColumns.size()
                            && coveredColumns.at(coveredColumn).isObject()
                            && !coveredColumns.at(coveredColumn).toObject()
                                    .value(QStringLiteral("text")).toString().trimmed().isEmpty()) {
                            addReason(&failures,
                                      QStringLiteral("spans[%1] hides non-anchor text").arg(index));
                        }
                    }
                }
            }
        }
    }

    const QJsonValue stateValue = response.value(QStringLiteral("recognition_state"));
    const QString state = stateValue.toString();
    if (!stateValue.isString()
        || (state != QStringLiteral("verified")
            && state != QStringLiteral("needs_review")
            && state != QStringLiteral("blocked"))) {
        addReason(&failures, QStringLiteral("recognition_state is invalid"));
    }
    const QJsonValue blockedValue = response.value(QStringLiteral("publication_blocked"));
    const bool blocked = blockedValue.toBool(true);
    if (!blockedValue.isBool())
        addReason(&failures, QStringLiteral("publication_blocked is not a boolean"));
    if (blockedValue.isBool() && ((state == QStringLiteral("blocked")) != blocked))
        addReason(&failures, QStringLiteral("recognition_state and publication_blocked disagree"));
    if (!response.value(QStringLiteral("publication_block_reasons")).isArray())
        addReason(&failures, QStringLiteral("publication_block_reasons is not an array"));

    const QJsonValue imageQualityValue = response.value(QStringLiteral("image_quality"));
    if (!imageQualityValue.isObject()) {
        addReason(&failures, QStringLiteral("image_quality is not an object"));
    } else {
        const QJsonObject imageQuality = imageQualityValue.toObject();
        if (!imageQuality.value(QStringLiteral("issues")).isArray()
            || !imageQuality.value(QStringLiteral("issue_labels")).isArray()
            || !imageQuality.value(QStringLiteral("needs_recapture")).isBool()) {
            addReason(&failures, QStringLiteral("image_quality fields are invalid"));
        }
    }
    if (!response.value(QStringLiteral("rectified_image")).isString()
        || response.value(QStringLiteral("rectified_image")).toString().isEmpty()) {
        addReason(&failures, QStringLiteral("rectified_image is invalid"));
    }

    const QJsonValue structureVerifiedValue = response.value(QStringLiteral("structure_verified"));
    const bool structureVerified = structureVerifiedValue.toBool(false);
    if (!structureVerifiedValue.isBool())
        addReason(&failures, QStringLiteral("structure_verified is not a boolean"));
    const QJsonValue certificateValue = response.value(QStringLiteral("structure_certificate"));
    if (!structureVerified) {
        if (!certificateValue.isNull() && !certificateValue.isUndefined())
            addReason(&failures, QStringLiteral("unverified structure has a certificate"));
        if (blockedValue.isBool() && !blocked)
            addReason(&failures, QStringLiteral("unverified structure is not blocked"));
    } else if (!certificateValue.isObject()) {
        addReason(&failures, QStringLiteral("verified structure has no certificate"));
    } else {
        const QJsonObject certificate = certificateValue.toObject();
        int certificateVersion = 0;
        int certifiedRows = 0;
        int certifiedColumns = 0;
        if (!readInteger(certificate.value(QStringLiteral("version")), 1, 1, &certificateVersion)
            || certificate.value(QStringLiteral("verified")).toBool(false) != true
            || !readInteger(certificate.value(QStringLiteral("rows")),
                            1,
                            kMaximumRows,
                            &certifiedRows)
            || !readInteger(certificate.value(QStringLiteral("columns")),
                            1,
                            kMaximumColumns,
                            &certifiedColumns)
            || certifiedRows != rows
            || certifiedColumns != columns
            || !certificate.value(QStringLiteral("spans")).isArray()
            || !jsonArraysEqual(certificate.value(QStringLiteral("spans")).toArray(), spans)
            || certificate.value(QStringLiteral("geometry_hash")).toString().isEmpty()
            || certificate.value(QStringLiteral("structure_hash")).toString().isEmpty()) {
            addReason(&failures, QStringLiteral("structure_certificate is invalid"));
        }
    }

    if (reasons)
        *reasons = failures;
    return failures.isEmpty();
}

bool OcrTableClient::canAutoPublish(const QJsonObject &response, QStringList *reasons)
{
    QStringList failures;
    validateRecognitionResponse(response, &failures);
    if (response.value(QStringLiteral("recognition_state")).toString()
        != QStringLiteral("verified")) {
        addReason(&failures, QStringLiteral("recognition_state is not verified"));
    }
    if (response.value(QStringLiteral("publication_blocked")).toBool(true))
        addReason(&failures, QStringLiteral("publication_blocked is true"));
    if (!response.value(QStringLiteral("structure_verified")).toBool(false))
        addReason(&failures, QStringLiteral("structure_verified is false"));

    const QJsonArray rows = response.value(QStringLiteral("cells")).toArray();
    for (int row = 0; row < rows.size(); ++row) {
        const QJsonArray columns = rows.at(row).toArray();
        for (int column = 0; column < columns.size(); ++column) {
            if (columns.at(column).isObject()
                && columns.at(column).toObject()
                       .value(QStringLiteral("needs_review")).toBool(true)) {
                addReason(&failures,
                          QStringLiteral("cells[%1][%2] needs review").arg(row).arg(column));
            }
        }
    }
    failures.removeDuplicates();
    if (reasons)
        *reasons = failures;
    return failures.isEmpty();
}
