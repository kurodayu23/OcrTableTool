#ifndef OCRTABLECLIENT_H
#define OCRTABLECLIENT_H

#include <QObject>
#include <QJsonArray>
#include <QJsonObject>
#include <QStringList>

class QProcess;
class QTimer;

// Public Qt 5.9.6 client for the OcrBackend V1 process protocol.
// Each request owns one backend process so native model memory is released
// when the process exits. Requests on one client instance are serialized.
class OcrTableClient : public QObject
{
    Q_OBJECT

public:
    explicit OcrTableClient(const QString &backendExecutable, QObject *parent = 0);
    ~OcrTableClient();

    bool isReady() const;
    bool isBusy() const;
    QString backendExecutable() const;

    int health();
    // 摄像头原图或仅做矩形裁剪的图片仍需由后端完成透视矫正。
    int recognizeCameraPhoto(const QString &imagePath,
                             const QString &outputDirectory,
                             bool selectedTableRegion = false);
    // 仅供已经完成透视矫正的图片使用，不能用于普通摄像头照片。
    int recognizeRectifiedTable(const QString &imagePath,
                                const QString &outputDirectory,
                                bool selectedTableRegion = false);
    int recognize(const QString &imagePath,
                  const QString &outputDirectory,
                  bool inputRectified = false,
                  bool selectedTableRegion = false);
    int exportXlsx(const QString &outputPath,
                   const QJsonArray &cells,
                   const QJsonArray &spans = QJsonArray());
    // Writes a rectangular UTF-8 BOM CSV locally. It does not add colors,
    // review columns, or other UI metadata.
    static bool exportCsv(const QString &outputPath,
                          const QJsonArray &cells,
                          QString *errorMessage = 0);
    void cancel();

    // validateRecognitionResponse checks protocol shape and safety metadata.
    // canAutoPublish additionally requires a verified structure and no review cells.
    static bool validateRecognitionResponse(const QJsonObject &response,
                                            QStringList *reasons = 0);
    static bool canAutoPublish(const QJsonObject &response,
                               QStringList *reasons = 0);

signals:
    void requestStarted(int requestId, const QString &action);
    void requestSucceeded(int requestId,
                          const QString &action,
                          const QJsonObject &response);
    void requestFailed(int requestId,
                       const QString &action,
                       const QString &errorCode,
                       const QString &field,
                       const QString &message,
                       bool retryable);
    void logMessage(const QString &message);
    void busyChanged(bool busy);

private slots:
    void readStandardOutput();
    void readStandardError();
    void processFinished(int exitCode);
    void processError();
    void requestTimedOut();

private:
    int startRequest(const QString &action,
                     QJsonObject request,
                     int timeoutMilliseconds);
    void consumeResponseLine(const QByteArray &line);
    void setForcedFailure(const QString &errorCode,
                          const QString &message,
                          bool retryable);
    void finishRequestState();
    void failLocally(const QString &action,
                     const QString &errorCode,
                     const QString &field,
                     const QString &message,
                     bool retryable);

    QString m_backendExecutable;
    QProcess *m_process;
    QTimer *m_requestTimer;
    QByteArray m_standardOutput;
    QByteArray m_standardError;
    QJsonObject m_pendingResponse;
    QString m_activeAction;
    QString m_forcedErrorCode;
    QString m_forcedErrorMessage;
    bool m_forcedRetryable;
    int m_nextRequestId;
    int m_activeRequestId;
    bool m_busy;
};

#endif // OCRTABLECLIENT_H
