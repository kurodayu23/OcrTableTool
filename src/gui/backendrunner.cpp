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
    , m_shuttingDown(false)
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
    shutdown();
}

void BackendRunner::shutdown()
{
    // 2026-10-23：关闭时点 X 必崩（0xC0000374 堆损坏）的修复点。
    //
    // 析构顺序问题：MainWindow 是栈对象，析构时**值成员先于子对象被销毁**。
    // 本对象是 MainWindow 的子 QObject，于是析构期里 kill() + waitForFinished()
    // 会同步触发 finished() -> processFinished() -> emit requestSucceeded()，
    // 而 MainWindow::backendSucceeded() 会去写 m_pendingRecognitionResponse ——
    // 那个值成员早已析构，写它就是 use-after-free，Windows 直接报堆损坏。
    //
    // 做法：先断开自身所有出向信号（上层槽函数不会再被调用），
    // 再置位 m_shuttingDown 让本对象自己的槽函数也直接返回，最后才停进程。
    if (m_shuttingDown)
        return;
    m_shuttingDown = true;
    disconnect(this, nullptr, nullptr, nullptr);
    if (m_process->state() != QProcess::NotRunning) {
        m_process->kill();
        // kill() 在 Windows 上是 TerminateProcess，子进程立刻死；这里只需要等
        // 进程管理器把状态通知上来。等待时间**保持 1000 ms** —— UI 契约测试
        // （tests/test_ui_contract.py）明文禁止在这个文件里出现三秒档的
        // waitForFinished，那是"不要在界面线程上阻塞 3 秒"的约定，
        // 这里不能为了多等一会儿就破坏它。
        m_process->waitForFinished(1000);
    }
}

bool BackendRunner::isRunning() const
{
    return m_requestActive || m_stopping;
}

bool BackendRunner::isStopping() const
{
    return m_stopping;
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

    // findings：兜底过去硬编码裸名 python。麒麟 / 其它 Linux 发行版上解释器
    // 常常只叫 python3（甚至没有 python 这个别名），于是"找不到 venv"直接变成
    // 起不来的进程。改成按平台候选名逐个探测 PATH，探测不到才退化成候选名本身
    // （由 QProcess 按 PATH 解析，仍是本平台的第一个候选，而不是写死的 python）。
    const QString systemInterpreter = BackendLocator::findSystemInterpreter();
    if (!systemInterpreter.isEmpty())
        return systemInterpreter;
    return BackendLocator::interpreterFileNames().first();
}

void BackendRunner::recognize(const QString &imagePath,
                              const QString &outputDirectory,
                              const QString &cropMode,
                              bool inputRectified,
                              bool selectedTableRegion,
                              int budgetSeconds)
{
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), 1);
    request.insert(QStringLiteral("action"), QStringLiteral("recognize"));
    request.insert(QStringLiteral("image_path"), imagePath);
    request.insert(QStringLiteral("output_directory"), outputDirectory);
    QJsonObject options;
    options.insert(QStringLiteral("crop_mode"), cropMode);
    // 首次识别仍然传 0（不额外收紧，对外「单次识别 60 秒墙钟」口径不变）。
    // 只有自动重试才传剩余预算：后端把 deadline_seconds 解析成
    // 「不传（<=0）⇒ 不额外收紧」「传更小的值 ⇒ 收紧到该值」，
    // 因此重试不会被放大成又一轮完整时长（findings B1：
    // 原先两轮冷启各跑满，总时长 ~120 秒）。
    if (budgetSeconds > 0)
        options.insert(QStringLiteral("deadline_seconds"), budgetSeconds);
    else
        options.insert(QStringLiteral("deadline_seconds"), 0);
    options.insert(QStringLiteral("accuracy_mode"), QStringLiteral("maximum"));
    if (inputRectified)
        options.insert(QStringLiteral("input_rectified"), true);
    if (selectedTableRegion)
        options.insert(QStringLiteral("selected_table_region"), true);
    request.insert(QStringLiteral("options"), options);
    // 重试时同时给出父进程侧的墙钟上限：后端预算只从 worker 内部计时点起算，
    // 不含进程冷启与模型构建，两层都用剩余预算才能保证「总时长不超过一次识别预算」。
    const int timeoutMilliseconds = budgetSeconds > 0 ? budgetSeconds * 1000 : 0;
    if (timeoutMilliseconds > 0)
        startRequest(QStringLiteral("recognize"), request, timeoutMilliseconds);
    else
        startRequest(QStringLiteral("recognize"), request, 0);
}

void BackendRunner::exportXlsx(const QString &outputPath,
                               const TableData &table,
                               const QJsonArray &spans,
                               const QJsonObject &structureCertificate)
{
    startRequest(QStringLiteral("export_xlsx"),
                 OcrTableToolGui::buildExportRequest(outputPath,
                                                     table,
                                                     spans,
                                                     structureCertificate),
                 30000);
}

namespace OcrTableToolGui {

QJsonObject buildExportRequest(const QString &outputPath,
                               const TableData &table,
                               const QJsonArray &spans,
                               const QJsonObject &structureCertificate)
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
            // 导出也要带上哨兵标记：后端 write_xlsx 靠它区分「实测置信度低」
            // 和「类别哨兵值」，否则会给逐格全对的表打上一整片假的复核批注。
            cell.insert(QStringLiteral("confidence_sentinel"),
                        table.confidenceSentinel(row, column));
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
    // findings/61 F6：识别时严格校验过的结构证书必须带到导出侧，
    // 否则后端 write_xlsx 的证书分支（列宽/边界校准）永远跳过。
    // 只在满足后端导出契约时才带，见 isXlsxCompatibleStructureCertificate()。
    if (isXlsxCompatibleStructureCertificate(structureCertificate,
                                             table.rowCount(),
                                             table.columnCount()))
        request.insert(QStringLiteral("structure_certificate"), structureCertificate);
    return request;
}

bool isXlsxCompatibleStructureCertificate(const QJsonObject &structureCertificate,
                                          int rowCount,
                                          int columnCount)
{
    // 与 backend/table_pipeline.py write_xlsx() 的前置校验逐条对齐：
    // 非字典 / verified 不是 True / 边界数组长度不是 行数+1、列数+1 /
    // 元素不是数字 / 不是严格递增 —— 任一不满足，后端都会抛
    // ValueError("invalid XLSX structure certificate boundaries") 让整次导出失败。
    if (structureCertificate.isEmpty())
        return false;
    if (structureCertificate.value(QStringLiteral("verified")).toBool() != true)
        return false;
    if (rowCount <= 0 || columnCount <= 0)
        return false;
    const QJsonValue columnBoundariesValue = structureCertificate.value(QStringLiteral("column_boundaries"));
    const QJsonValue rowBoundariesValue = structureCertificate.value(QStringLiteral("row_boundaries"));
    if (!columnBoundariesValue.isArray() || !rowBoundariesValue.isArray())
        return false;
    const QJsonArray columnBoundaries = columnBoundariesValue.toArray();
    const QJsonArray rowBoundaries = rowBoundariesValue.toArray();
    if (columnBoundaries.size() != columnCount + 1 || rowBoundaries.size() != rowCount + 1)
        return false;
    double previous = 0.0;
    for (int index = 0; index < columnBoundaries.size(); ++index) {
        const QJsonValue value = columnBoundaries.at(index);
        if (!value.isDouble())
            return false;
        const double boundary = value.toDouble();
        // 后端还要求全部是有限值（NaN / inf 会让它抛同一个异常）。
        if (!qIsFinite(boundary))
            return false;
        if (index > 0 && boundary <= previous)
            return false;
        previous = boundary;
    }
    for (int index = 0; index < rowBoundaries.size(); ++index) {
        const QJsonValue value = rowBoundaries.at(index);
        if (!value.isDouble())
            return false;
        const double boundary = value.toDouble();
        if (!qIsFinite(boundary))
            return false;
        if (index > 0 && boundary <= previous)
            return false;
        previous = boundary;
    }
    return true;
}

} // namespace OcrTableToolGui

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
    if (m_shuttingDown)
        return;
    if (!m_stopping)
        return;
    const QString action = m_stoppingAction;
    const QString message = m_stoppingMessage;
    m_stopping = false;
    m_stoppingAction.clear();
    m_stoppingMessage.clear();
    emit requestFailed(action, message, QString(), false);
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
        // 2026-10-30（lead 复查 P1）：超时失败时原来既不 kill 也不关写通道。
        // 慢冷启（首次解压 _internal、杀软扫描）下进程可能仍处于 Starting/Running
        // 且卡在阻塞读 stdin；于是下一次 startBackend() 会被上面那条
        // `state() != NotRunning` 挡成「识别组件正在释放资源，请稍候重试」，
        // 而且永远不会恢复 —— 用户只能重启应用。这里必须把半启动的进程收干净。
        const QString failureDetail = m_process->errorString();
        if (m_process->state() != QProcess::NotRunning) {
            m_process->kill();
            if (!m_process->waitForFinished(500)) {
                // 连 kill 都不返回：至少别让它继续占着「正在释放资源」的状态，
                // 交给下一次 startBackend 重新判定（terminate 是最后手段）。
                m_process->terminate();
                m_process->waitForFinished(2000);
            }
        }
        if (errorMessage)
            *errorMessage = QStringLiteral("无法启动识别组件：%1").arg(failureDetail);
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
        emit requestFailed(action, QStringLiteral("另一个任务正在运行"),
                           QStringLiteral("BACKEND_BUSY"), true);
        return;
    }
    if (!isConfigured()) {
        emit requestFailed(action, configurationSummary(),
                           QStringLiteral("BACKEND_NOT_CONFIGURED"), false);
        return;
    }
    m_requestWallTimer.restart();
    m_responseReadyElapsedMilliseconds = -1;
    QString startError;
    if (!startBackend(&startError)) {
        m_requestWallTimer.invalidate();
        emit requestFailed(action, startError,
                           QStringLiteral("BACKEND_START_FAILED"), true);
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

namespace OcrTableToolGui {

// 后端的标准输出是一行一个 JSON。解析结果必须区分两种失败：
//   - 这一行根本不是 JSON（后端打印了日志、崩溃栈等杂音）；
//   - 这一行是合法 JSON，但内容是空对象 "{}"（后端一个字段都没给）。
// 两者过去都会让界面拿到一条**空的**提示，界面没法告诉用户到底出了什么。
// 这里把两种情况分开，并且任何失败路径都给出非空提示，
// 保证界面永远收不到空日志。
//
// 自由函数（外部链接）：单元测试 tests/tst_gui.cpp 直接声明并调用它，
// 绕开 MSVC 把访问级别编进符号名的问题（私有成员函数无法从测试 TU 调用）。
QJsonObject parseBackendResponse(const QByteArray &output, QString *errorMessage)
{
    // stderr 承载诊断信息，stdout 仍可能夹杂附带输出；从后向前查找，
    // 以最近一个完整 JSON 对象为准。
    const QList<QByteArray> lines = output.trimmed().split('\n');
    bool sawEmptyObject = false;
    for (int index = lines.size() - 1; index >= 0; --index) {
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(lines.at(index).trimmed(), &parseError);
        if (parseError.error != QJsonParseError::NoError || !document.isObject())
            continue;
        const QJsonObject candidate = document.object();
        if (!candidate.isEmpty())
            return candidate;
        // 空对象也是合法 JSON，但它没有任何结果字段。记住它，
        // 提示语与「这一行根本不是 JSON」区分开。
        sawEmptyObject = true;
        break;
    }
    if (errorMessage) {
        *errorMessage = sawEmptyObject
            ? QStringLiteral("识别组件返回了空结果，请重新识别")
            : QStringLiteral("识别组件没有返回有效结果");
    }
    return QJsonObject();
}

} // namespace OcrTableToolGui

QJsonObject BackendRunner::parseResponse(const QByteArray &output, QString *errorMessage) const
{
    return OcrTableToolGui::parseBackendResponse(output, errorMessage);
}

void BackendRunner::processFinished(int exitCode)
{
    if (m_shuttingDown)
        return;
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
        emit requestFailed(action, message, QStringLiteral("EMPTY_RESPONSE"), true);
        return;
    }
    const QJsonValue protocolValue = response.value(QStringLiteral("protocol"));
    if (!protocolValue.isDouble() || protocolValue.toDouble() != 1.0) {
        emit requestFailed(action, QStringLiteral("识别组件协议版本不兼容"),
                           QStringLiteral("UNSUPPORTED_PROTOCOL"), false);
    } else if (response.value(QStringLiteral("status")).toString() != QStringLiteral("ok")) {
        // 2026-10-22 修正：后端一直返回结构化的 error_code / retryable / field，
        // 前端却**整个丢掉**，只取 message，然后靠中文子串猜错误类型 ——
        // 后端消息措辞一改就掉进兜底文案。这里把错误码和可重试标志透出去。
        emit requestFailed(action,
                           response.value(QStringLiteral("message"))
                               .toString(QStringLiteral("识别执行失败")),
                           response.value(QStringLiteral("error_code")).toString(),
                           response.value(QStringLiteral("retryable")).toBool());
    } else if (exitCode != 0) {
        // 2026-10-28 修正（findings/61 F3）：退出码原先只写进 trace，不参与判定，
        // 于是"响应 status=ok、进程随后以非 0 退出"被判成成功。响应只证明
        // 结果被打印出来了，不证明 worker 正常收尾；收尾阶段的崩溃（解释器
        // 析构时 OpenCV/OpenVINO teardown）会留下完整 ok 响应 + 非 0 退出码。
        // 中继口径：这份结果不采用，按可重试的传输层失败上报。
        QJsonObject mismatchTrace;
        mismatchTrace.insert(QStringLiteral("action"), action);
        mismatchTrace.insert(QStringLiteral("exit_code"), exitCode);
        mismatchTrace.insert(QStringLiteral("status"),
                             response.value(QStringLiteral("status")));
        GuiTrace::write(QStringLiteral("backend_exit_code_mismatch"), mismatchTrace);
        emit requestFailed(action,
                           QStringLiteral("识别组件返回结果后异常退出（退出码 %1），本次结果未采用。")
                               .arg(exitCode),
                           QStringLiteral("BACKEND_CRASHED"),
                           true);
    } else {
        emit requestSucceeded(action, response);
    }
}

void BackendRunner::processError()
{
    if (m_shuttingDown)
        return;
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
        emit requestFailed(action, message, QStringLiteral("BACKEND_CRASHED"), true);
    }
}

void BackendRunner::readStandardOutput()
{
    if (m_shuttingDown)
        return;
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
            // 空提示对用户毫无意义，日志里也不该出现空行。
            if (!parseError.isEmpty())
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
    if (m_shuttingDown)
        return;
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
    if (m_shuttingDown)
        return;
    const QByteArray output = m_process->readAllStandardError();
    m_standardError += output;
    if (m_standardError.size() > 16000)
        m_standardError = m_standardError.right(16000);
    const QString message = QString::fromLocal8Bit(output).trimmed();
    if (!message.isEmpty())
        emit logMessage(message);
}
