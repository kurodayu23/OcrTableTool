#ifndef BACKENDRUNNER_H
#define BACKENDRUNNER_H

#include <QObject>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>

class QProcess;
class QTimer;
class TableData;

namespace OcrTableToolGui {

// 组装一次导出请求。抽成自由函数（外部链接）的理由与 parseBackendResponse 相同：
// tests/tst_gui.cpp 需要直接断言「请求里到底带了什么」，
// 而私有成员函数在 MSVC 下带访问级别修饰，测试 TU 调用必然 LNK2019。
//
// structure_certificate 的处理口径（findings/61 F6）：识别时界面严格校验过结构
// 证书，导出时却整个丢掉，后端 table_pipeline.write_xlsx 里的证书分支因此永远
// 走不到。这里把证书带到请求里，但**只在它满足后端导出侧契约时**才带 ——
// 后端要求 verified 为 true 且 column_boundaries / row_boundaries 恰好是
// 列数+1 / 行数+1 的严格递增数组，不满足会抛
// "invalid XLSX structure certificate boundaries" 让整次导出失败。
// 宁可不带（保持改动前的行为），也不要发一个下游必然拒绝的证书。
QJsonObject buildExportRequest(const QString &outputPath,
                               const TableData &table,
                               const QJsonArray &spans,
                               const QJsonObject &structureCertificate);

// 结构证书是否满足 xlsx 导出侧的边界契约。不满足时调用方不得把它塞进请求。
bool isXlsxCompatibleStructureCertificate(const QJsonObject &structureCertificate,
                                          int rowCount,
                                          int columnCount);

} // namespace OcrTableToolGui

// 管理按需启动的 OCR 后端进程，并通过逐行 JSON 协议串行执行请求。
// 响应交付后结束进程，避免 OpenVINO 模型在平板上长期占用数 GB 内存。
class BackendRunner : public QObject
{
    Q_OBJECT

public:
    explicit BackendRunner(QObject *parent = 0);
    ~BackendRunner();

    // “Running”同时包含活动请求和异步停止阶段；任一状态存在时都不能启动新请求。
    bool isRunning() const;
    // 只表示“正在异步停止上一个 worker”。界面需要把它与“正在跑别的任务”
    // 分开：两种情形都不能立刻开新请求，但用户提示与补偿方式不同
    // （findings/61 F5）。
    bool isStopping() const;
    bool isConfigured() const;
    QString configurationSummary() const;

    // 每个请求使用一个独立 worker；下一次请求会自动拉起新进程。
    // 超时为 0 表示允许最高质量识别完整执行，用户仍可主动取消。
    // budgetSeconds：0 = 不额外收紧（对外口径不变）；
    //                >0 = 本次请求只允许用这么多秒（自动重试用剩余预算，
    //                     避免一轮 60 秒的重试把总时长放大成 ~120 秒）。
    void recognize(const QString &imagePath,
                   const QString &outputDirectory,
                   const QString &cropMode,
                   bool inputRectified = false,
                   bool selectedTableRegion = false,
                   int budgetSeconds = 0);
    void exportXlsx(const QString &outputPath,
                    const TableData &table,
                    const QJsonArray &spans,
                    const QJsonObject &structureCertificate = QJsonObject());
    void cancel();

    // 关闭阶段必须先停掉后端进程，并且**不再向上层发任何信号**。
    // 2026-10-23：界面关闭时点 X 必崩（Windows 0xC0000374 堆损坏，4/4 复现）。
    // 根因是析构顺序：MainWindow 是栈对象，析构时**值成员先于子对象被销毁**，
    // 而 BackendRunner 是 MainWindow 的子 QObject，它析构里的
    // kill() + waitForFinished() 会**同步**触发 finished() 信号 ->
    // processFinished() -> emit requestSucceeded() -> MainWindow::backendSucceeded()
    // 去写 m_pendingRecognitionResponse —— 那时它早已析构 -> use-after-free -> 堆损坏。
    // 这个方法把"停进程"和"断开信号"合成一步，界面在 ~MainWindow 里显式调用，
    // 保证执行时机早于成员析构。
    void shutdown();

signals:
    void requestStarted(const QString &action);
    void requestSucceeded(const QString &action, const QJsonObject &response);
    void requestFailed(const QString &action, const QString &message,
                       const QString &errorCode, bool retryable);
    void logMessage(const QString &message);

private slots:
    void processFinished(int exitCode);
    void processError();
    void readStandardOutput();
    void readStandardError();
    void requestTimedOut();

private:
    QString findPackagedBackend() const;
    QString findBackendScript() const;
    QString findPython(const QString &backendScript) const;
    bool startBackend(QString *errorMessage);
    void startRequest(const QString &action, const QJsonObject &request, int timeoutMilliseconds);
    QJsonObject parseResponse(const QByteArray &output, QString *errorMessage) const;
    // QProcess::kill() 异步完成。这里把停止流程与正常完成流程分开，
    // 避免已取消任务和新任务重叠，也避免同时上报取消与进程退出错误。
    void beginStopping(const QString &action, const QString &message);
    void finishStopping();

    QProcess *m_process;
    QTimer *m_requestTimer;
    QString m_packagedBackend;
    QString m_backendScript;
    QString m_python;
    QString m_action;
    QByteArray m_standardOutput;
    QByteArray m_standardError;
    QJsonObject m_pendingResponse;
    QElapsedTimer m_requestWallTimer;
    qint64 m_responseReadyElapsedMilliseconds;
    // 状态不变量：正常情况下 m_requestActive 与 m_stopping 互斥；
    // isRunning() 会把任一状态都视为忙碌。
    bool m_requestActive;
    bool m_stopping;
    QString m_stoppingAction;
    QString m_stoppingMessage;
    // request_id 是标准输出中的请求关联边界；清空 m_activeRequestId 后，
    // 已取消请求迟到的输出会自动失效。
    int m_nextRequestId;
    int m_activeRequestId;
    // 关闭阶段标志：置位后所有槽函数立即返回，不再向上层发信号。
    bool m_shuttingDown;
};

#endif // BACKENDRUNNER_H
