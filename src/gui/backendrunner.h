#ifndef BACKENDRUNNER_H
#define BACKENDRUNNER_H

#include <QObject>
#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>

class QProcess;
class QTimer;
class TableData;

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
    bool isConfigured() const;
    QString configurationSummary() const;

    // 每个请求使用一个独立 worker；下一次请求会自动拉起新进程。
    // 超时为 0 表示允许最高质量识别完整执行，用户仍可主动取消。
    void recognize(const QString &imagePath,
                   const QString &outputDirectory,
                   const QString &cropMode,
                   bool inputRectified = false,
                   bool selectedTableRegion = false);
    void exportXlsx(const QString &outputPath, const TableData &table, const QJsonArray &spans);
    void cancel();

signals:
    void requestStarted(const QString &action);
    void requestSucceeded(const QString &action, const QJsonObject &response);
    void requestFailed(const QString &action, const QString &message);
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
};

#endif // BACKENDRUNNER_H
