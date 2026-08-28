#include "backendrunner.h"

#include "backendlocator.h"
#include "guitrace.h"
#include "tabledata.h"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QProcess>
#include <QTimer>

BackendRunner::BackendRunner(QObject *parent)
    : QObject(parent)
    , m_process(new QProcess(this))
    , m_requestTimer(new QTimer(this))
    , m_packagedBackend(findPackagedBackend())
    , m_backendScript(m_packagedBackend.isEmpty() ? findBackendScript() : QString())
    , m_python(m_packagedBackend.isEmpty() ? findPython(m_backendScript) : QString())
    , m_responseReadyElapsedMilliseconds(-1)
    , m_requestActive(false)
    , m_stopping(false)
    , m_nextRequestId(1)
    , m_activeRequestId(0)
{
    m_requestTimer->setSingleShot(true);
    m_process->setProcessChannelMode(QProcess::SeparateChannels);
    connect(m_process, SIGNAL(finished(int,QProcess::ExitStatus)), this, SLOT(processFinished(int)));
    connect(m_process, SIGNAL(errorOccurred(QProcess::ProcessError)), this, SLOT(processError()));
    connect(m_process, SIGNAL(readyReadStandardError()), this, SLOT(readStandardError()));
    connect(m_process, SIGNAL(readyReadStandardOutput()), this, SLOT(readStandardOutput()));
    connect(m_requestTimer, &QTimer::timeout, this, &BackendRunner::requestTimedOut);
}

BackendRunner::~BackendRunner()
{
    if (m_process->state() != QProcess::NotRunning) {
        m_process->kill();
        m_process->waitForFinished(1000);
    }
}

bool BackendRunner::isRunning() const
{
    return m_requestActive || m_stopping;
}

bool BackendRunner::isConfigured() const
{
    return QFileInfo::exists(m_packagedBackend)
        || (!m_python.isEmpty() && QFileInfo::exists(m_backendScript));
}

QString BackendRunner::configurationSummary() const
{
    if (QFileInfo::exists(m_packagedBackend))
        return QStringLiteral("识别组件已准备");
    if (!QFileInfo::exists(m_backendScript))
        return QStringLiteral("未找到识别组件，请检查程序文件是否完整");
    return QStringLiteral("识别组件已准备");
}

QString BackendRunner::findPackagedBackend() const
{
    const QDir applicationDirectory(QCoreApplication::applicationDirPath());
    const QStringList candidates = QStringList()
        << applicationDirectory.filePath(QStringLiteral("ocr-runtime/OcrBackend.exe"))
        << applicationDirectory.filePath(QStringLiteral("ocr-runtime/OcrBackend/OcrBackend.exe"))
        << applicationDirectory.filePath(QStringLiteral("OcrBackend.exe"));
    for (int index = 0; index < candidates.size(); ++index) {
        if (QFileInfo::exists(candidates.at(index)))
            return QFileInfo(candidates.at(index)).absoluteFilePath();
    }
    return QString();
}

QString BackendRunner::findBackendScript() const
{
    const QString environmentPath = QString::fromLocal8Bit(qgetenv("OCR_TABLE_BACKEND"));
    if (QFileInfo::exists(environmentPath))
        return QFileInfo(environmentPath).absoluteFilePath();

    const QFileInfo compiledSourceFile(QString::fromLocal8Bit(__FILE__));
    if (compiledSourceFile.isAbsolute()) {
        const QString sourceCandidate = QDir(compiledSourceFile.absolutePath())
                                            .filePath(QStringLiteral("../../backend/ocr_backend.py"));
        if (QFileInfo::exists(sourceCandidate))
            return QFileInfo(sourceCandidate).absoluteFilePath();
    }

    QStringList roots;
    roots << QCoreApplication::applicationDirPath() << QDir::currentPath();
    for (int rootIndex = 0; rootIndex < roots.size(); ++rootIndex) {
        QDir directory(roots.at(rootIndex));
        for (int level = 0; level < 7; ++level) {
            const QString candidate = directory.filePath(QStringLiteral("backend/ocr_backend.py"));
            if (QFileInfo::exists(candidate))
                return QFileInfo(candidate).absoluteFilePath();
            if (!directory.cdUp())
                break;
        }
    }
    return QString();
}

QString BackendRunner::findPython(const QString &backendScript) const
{
    const QString environmentPath = QString::fromLocal8Bit(qgetenv("OCR_TABLE_PYTHON"));
    if (QFileInfo::exists(environmentPath))
        return QFileInfo(environmentPath).absoluteFilePath();

    const QString discovered = BackendLocator::findPython(
        backendScript,
        QStringList() << QCoreApplication::applicationDirPath() << QDir::currentPath());
    if (!discovered.isEmpty())
        return discovered;
    const QString packagedPython = QDir(QCoreApplication::applicationDirPath())
                                       .filePath(QStringLiteral("runtime/python/python.exe"));
    if (QFileInfo::exists(packagedPython))
        return QFileInfo(packagedPython).absoluteFilePath();
    return QStringLiteral("python");
}

void BackendRunner::recognize(const QString &imagePath,
                              const QString &outputDirectory,
                              const QString &cropMode,
                              bool inputRectified)
{
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), 1);
    request.insert(QStringLiteral("action"), QStringLiteral("recognize"));
    request.insert(QStringLiteral("image_path"), imagePath);
    request.insert(QStringLiteral("output_directory"), outputDirectory);
    QJsonObject options;
    options.insert(QStringLiteral("crop_mode"), cropMode);
    options.insert(QStringLiteral("deadline_seconds"), 0);
    options.insert(QStringLiteral("accuracy_mode"), QStringLiteral("maximum"));
    if (inputRectified)
        options.insert(QStringLiteral("input_rectified"), true);
    request.insert(QStringLiteral("options"), options);
    startRequest(QStringLiteral("recognize"), request, 0);
}

void BackendRunner::exportXlsx(const QString &outputPath,
                               const TableData &table,
                               const QJsonArray &spans)
{
    // 导出时保留核对元数据。它属于后端数据契约，不只是界面装饰；
    // XLSX 需要用它保存不确定内容的标记。
    QJsonArray rows;
    for (int row = 0; row < table.rowCount(); ++row) {
        QJsonArray columns;
        for (int column = 0; column < table.columnCount(); ++column) {
            QJsonObject cell;
            cell.insert(QStringLiteral("text"), table.cell(row, column));
            cell.insert(QStringLiteral("confidence"), table.confidence(row, column));
            cell.insert(QStringLiteral("needs_review"), table.needsReview(row, column));
            columns.append(cell);
        }
        rows.append(columns);
    }
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), 1);
    request.insert(QStringLiteral("action"), QStringLiteral("export_xlsx"));
    request.insert(QStringLiteral("output_path"), outputPath);
    request.insert(QStringLiteral("cells"), rows);
    request.insert(QStringLiteral("spans"), spans);
    startRequest(QStringLiteral("export_xlsx"), request, 30000);
}

void BackendRunner::cancel()
{
    if (!m_requestActive || m_stopping)
        return;
    const QString action = m_action;
    m_action.clear();
    m_requestActive = false;
    m_activeRequestId = 0;
    m_requestTimer->stop();
    m_standardOutput.clear();
    m_pendingResponse = QJsonObject();
    // 后端同步调用模型，执行期间不能安全接收第二条命令。
    // 因此取消操作会停止整个 worker，下一次请求重新按需启动。
    beginStopping(action, QStringLiteral("操作已取消"));
}

void BackendRunner::beginStopping(const QString &action, const QString &message)
{
    m_requestWallTimer.invalidate();
    m_responseReadyElapsedMilliseconds = -1;
    m_stopping = true;
    m_stoppingAction = action;
    m_stoppingMessage = message;
    m_process->kill();
    if (m_process->state() == QProcess::NotRunning)
        QTimer::singleShot(0, this, [this]() { finishStopping(); });
}

void BackendRunner::finishStopping()
{
    if (!m_stopping)
        return;
    const QString action = m_stoppingAction;
    const QString message = m_stoppingMessage;
    m_stopping = false;
    m_stoppingAction.clear();
    m_stoppingMessage.clear();
    emit requestFailed(action, message);
}

bool BackendRunner::startBackend(QString *errorMessage)
{
    if (m_stopping) {
        if (errorMessage)
            *errorMessage = QStringLiteral("识别组件正在停止，请稍候重试");
        return false;
    }
    // 一个 worker 只处理一个请求。进程退出就是 OpenVINO 原生工作集可靠
    // 归还给 Windows 的资源边界，避免空闲时影响同机开发软件。
    if (m_process->state() != QProcess::NotRunning) {
        if (errorMessage)
            *errorMessage = QStringLiteral("识别组件正在释放资源，请稍候重试");
        return false;
    }
    if (QFileInfo::exists(m_packagedBackend)) {
        m_process->setProgram(m_packagedBackend);
        m_process->setArguments(QStringList());
        m_process->setWorkingDirectory(QFileInfo(m_packagedBackend).absolutePath());
    } else {
        m_process->setProgram(m_python);
        m_process->setArguments(QStringList() << m_backendScript);
        m_process->setWorkingDirectory(QFileInfo(m_backendScript).absolutePath());
    }
    m_process->start();
    if (!m_process->waitForStarted(3000)) {
        if (errorMessage)
            *errorMessage = QStringLiteral("无法启动识别组件：%1").arg(m_process->errorString());
        return false;
    }
    return true;
}

void BackendRunner::startRequest(const QString &action,
                                 const QJsonObject &request,
                                 int timeoutMilliseconds)
{
    // 后端循环为同步执行，Runner 也只有一个响应缓冲区；并发写入会导致请求归属不明确。
    if (m_requestActive) {
        emit requestFailed(action, QStringLiteral("另一个任务正在运行"));
        return;
    }
    if (!isConfigured()) {
        emit requestFailed(action, configurationSummary());
        return;
    }
    m_requestWallTimer.restart();
    m_responseReadyElapsedMilliseconds = -1;
    QString startError;
    if (!startBackend(&startError)) {
        m_requestWallTimer.invalidate();
        emit requestFailed(action, startError);
        return;
    }
    QJsonObject identifiedRequest = request;
    const int requestId = m_nextRequestId++;
    identifiedRequest.insert(QStringLiteral("request_id"), requestId);
    m_action = action;
    m_requestActive = true;
    m_activeRequestId = requestId;
    QJsonObject requestTrace;
    requestTrace.insert(QStringLiteral("action"), action);
    requestTrace.insert(QStringLiteral("request_id"), requestId);
    GuiTrace::write(QStringLiteral("backend_request_started"), requestTrace);
    m_standardOutput.clear();
    m_standardError.clear();
    m_pendingResponse = QJsonObject();
    // 单次 worker 以 EOF 作为请求结束边界；后端返回完整响应后自然退出。
    QByteArray payload = QJsonDocument(identifiedRequest).toJson(QJsonDocument::Compact);
    if (m_process->write(payload) != payload.size()) {
        m_action.clear();
        m_requestActive = false;
        m_activeRequestId = 0;
        // 写入失败说明当前常驻进程的协议通道已经不可信，必须先停止它；
        // 否则下一次任务会继续复用损坏的进程并重复失败。
        beginStopping(action, QStringLiteral("无法向识别组件发送任务"));
        return;
    }
    m_process->closeWriteChannel();
    if (timeoutMilliseconds > 0)
        m_requestTimer->start(timeoutMilliseconds);
    emit requestStarted(action);
}

QJsonObject BackendRunner::parseResponse(const QByteArray &output, QString *errorMessage) const
{
    // stderr 承载诊断信息，stdout 仍可能夹杂附带输出；从后向前查找，
    // 以最近一个完整 JSON 对象为准。
    const QList<QByteArray> lines = output.trimmed().split('\n');
    for (int index = lines.size() - 1; index >= 0; --index) {
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(lines.at(index).trimmed(), &parseError);
        if (parseError.error == QJsonParseError::NoError && document.isObject())
            return document.object();
    }
    if (errorMessage)
        *errorMessage = QStringLiteral("识别组件没有返回有效结果");
    return QJsonObject();
}

void BackendRunner::processFinished(int exitCode)
{
    readStandardOutput();
    readStandardError();
    // 取消或超时导致的进程退出属于停止状态；finishStopping() 只上报一次已保存的用户提示。
    if (m_stopping) {
        finishStopping();
        return;
    }
    if (!m_requestActive)
        return;
    const QString action = m_action;
    QJsonObject response = m_pendingResponse;
    const qint64 workerWallMilliseconds = m_requestWallTimer.isValid()
        ? m_requestWallTimer.elapsed()
        : -1;
    QJsonObject finishTrace;
    finishTrace.insert(QStringLiteral("action"), action);
    finishTrace.insert(QStringLiteral("exit_code"), exitCode);
    finishTrace.insert(QStringLiteral("response_present"), !response.isEmpty());
    finishTrace.insert(QStringLiteral("worker_wall_ms"),
                       static_cast<double>(workerWallMilliseconds));
    GuiTrace::write(QStringLiteral("backend_process_finished"), finishTrace);
    if (!response.isEmpty() && workerWallMilliseconds >= 0) {
        response.insert(QStringLiteral("worker_wall_seconds"),
                        workerWallMilliseconds / 1000.0);
        const qint64 responseExitMilliseconds = m_responseReadyElapsedMilliseconds >= 0
            ? qMax<qint64>(0, workerWallMilliseconds - m_responseReadyElapsedMilliseconds)
            : 0;
        response.insert(QStringLiteral("response_exit_seconds"),
                        responseExitMilliseconds / 1000.0);
    }
    m_action.clear();
    m_requestActive = false;
    m_activeRequestId = 0;
    m_requestTimer->stop();
    const QString technicalDetails = QString::fromLocal8Bit(m_standardError).trimmed().right(4000);
    m_standardOutput.clear();
    m_standardError.clear();
    m_pendingResponse = QJsonObject();
    m_requestWallTimer.invalidate();
    m_responseReadyElapsedMilliseconds = -1;
    if (response.isEmpty()) {
        const QString message = technicalDetails.isEmpty()
            ? QStringLiteral("识别组件没有返回有效结果")
            : QStringLiteral("识别组件没有返回有效结果\n%1").arg(technicalDetails);
        emit requestFailed(action, message);
        return;
    }
    const QJsonValue protocolValue = response.value(QStringLiteral("protocol"));
    if (!protocolValue.isDouble() || protocolValue.toDouble() != 1.0) {
        emit requestFailed(action, QStringLiteral("识别组件协议版本不兼容"));
    } else if (response.value(QStringLiteral("status")).toString() != QStringLiteral("ok")) {
        emit requestFailed(action,
                           response.value(QStringLiteral("message"))
                               .toString(QStringLiteral("识别执行失败")));
    } else {
        emit requestSucceeded(action, response);
    }
}

void BackendRunner::processError()
{
    if (m_stopping && m_process->state() == QProcess::NotRunning) {
        finishStopping();
        return;
    }
    if (m_process->state() == QProcess::NotRunning
        && m_requestActive
        && m_pendingResponse.isEmpty()) {
        readStandardError();
        const QString action = m_action;
        m_action.clear();
        m_requestActive = false;
        m_activeRequestId = 0;
        m_requestTimer->stop();
        m_standardOutput.clear();
        m_pendingResponse = QJsonObject();
        m_requestWallTimer.invalidate();
        m_responseReadyElapsedMilliseconds = -1;
        const QString technicalDetails = QString::fromLocal8Bit(m_standardError).trimmed().right(4000);
        m_standardError.clear();
        const QString message = technicalDetails.isEmpty()
            ? m_process->errorString()
            : QStringLiteral("%1\n%2").arg(m_process->errorString(), technicalDetails);
        emit requestFailed(action, message);
    }
}

void BackendRunner::readStandardOutput()
{
    m_standardOutput += m_process->readAllStandardOutput();
    // 最后一段数据若不完整则继续缓存，等换行到达后再作为独立 JSONL 记录处理。
    int newline = m_standardOutput.indexOf('\n');
    while (newline >= 0) {
        const QByteArray line = m_standardOutput.left(newline).trimmed();
        m_standardOutput.remove(0, newline + 1);
        newline = m_standardOutput.indexOf('\n');
        if (line.isEmpty())
            continue;

        QString parseError;
        const QJsonObject response = parseResponse(line, &parseError);
        if (response.isEmpty()) {
            emit logMessage(parseError);
            continue;
        }
        if (!m_requestActive)
            continue;
        const QJsonValue responseRequestIdValue = response.value(QStringLiteral("request_id"));
        const int responseRequestId = responseRequestIdValue.toInt();
        // QProcess 的终止、readyRead 和 finished 信号都是异步的。
        // 请求关联校验可防止旧任务的迟到响应被误当成新任务结果发布。
        if (!responseRequestIdValue.isDouble()
            || responseRequestIdValue.toDouble() != responseRequestId
            || responseRequestId != m_activeRequestId) {
            emit logMessage(QStringLiteral("已忽略过期识别结果"));
            continue;
        }

        if (m_pendingResponse.isEmpty()) {
            // 完整响应仍要等待一次性 worker 自然退出后再发布。这样 GUI 计时、
            // 进程回收和批量验收使用同一边界，也不会把正常析构误判为崩溃。
            m_pendingResponse = response;
            QJsonObject responseTrace;
            responseTrace.insert(QStringLiteral("action"), m_action);
            responseTrace.insert(QStringLiteral("request_id"), responseRequestId);
            responseTrace.insert(QStringLiteral("status"),
                                 response.value(QStringLiteral("status")));
            responseTrace.insert(QStringLiteral("rows"),
                                 response.value(QStringLiteral("rows")));
            responseTrace.insert(QStringLiteral("columns"),
                                 response.value(QStringLiteral("columns")));
            GuiTrace::write(QStringLiteral("backend_response_received"), responseTrace);
            m_responseReadyElapsedMilliseconds = m_requestWallTimer.isValid()
                ? m_requestWallTimer.elapsed()
                : -1;
            m_requestTimer->stop();
        }
    }
}

void BackendRunner::requestTimedOut()
{
    if (!m_requestActive)
        return;
    const QString action = m_action;
    m_action.clear();
    m_requestActive = false;
    m_activeRequestId = 0;
    m_standardOutput.clear();
    m_pendingResponse = QJsonObject();
    beginStopping(action, QStringLiteral("操作超时，请重试。"));
}

void BackendRunner::readStandardError()
{
    const QByteArray output = m_process->readAllStandardError();
    m_standardError += output;
    if (m_standardError.size() > 16000)
        m_standardError = m_standardError.right(16000);
    const QString message = QString::fromLocal8Bit(output).trimmed();
    if (!message.isEmpty())
        emit logMessage(message);
}
