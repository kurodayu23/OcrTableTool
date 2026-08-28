#ifndef CAMERAOCRCLIENT_H
#define CAMERAOCRCLIENT_H

#include <QObject>
#include <QCameraImageCapture>
#include <QJsonArray>
#include <QJsonObject>
#include <QRectF>
#include <QSize>
#include <QStringList>

class OcrTableClient;
class QCamera;
class QCameraViewfinder;
class QTimer;

// Camera-only integration facade. It owns camera capture and OCR orchestration,
// but provides no dialog, button, message box, or other product UI.
class CameraOcrClient : public QObject
{
    Q_OBJECT

public:
    explicit CameraOcrClient(const QString &backendExecutable,
                             const QString &workRoot,
                             QObject *parent = 0);
    ~CameraOcrClient();

    static QStringList availableCameraDescriptions();

    // The viewfinder belongs to the caller. Passing 0 enables capture without
    // an SDK-provided preview widget.
    void setViewfinder(QCameraViewfinder *viewfinder);
    bool startCamera(int cameraIndex = -1);
    void stopCamera();
    bool isCameraReady() const;
    bool isBusy() const;

    // Captures one high-resolution JPEG and immediately recognizes its table.
    // The caller never needs to expose an "open image" workflow.
    bool captureAndRecognize();
    // 可选的最终照片归一化区域；同一画面有多个表格时由产品界面选定一个表格。
    bool setTableRegion(const QRectF &normalizedRegion,
                        QString *errorMessage = 0);
    void clearTableRegion();
    QRectF tableRegion() const;
    QRect resolvedTableRegion(const QSize &imageSize) const;
    QJsonObject lastResponse() const;
    QJsonArray tableCells() const;
    bool setCellText(int row,
                     int column,
                     const QString &text,
                     QString *errorMessage = 0);
    bool appendRow(const QJsonArray &values,
                   QString *errorMessage = 0);
    bool exportLastCsv(const QString &outputPath,
                       QString *errorMessage = 0) const;
    // Optional business-safety metadata. It never changes the returned table,
    // creates a yellow style, blocks CSV export, or shows a dialog.
    bool canAutoPublish(QStringList *reasons = 0) const;
    void cancel();

signals:
    void cameraReadyChanged(bool ready,
                            int cameraIndex,
                            const QString &description);
    void stageChanged(const QString &stage, const QString &message);
    void captured(const QString &imagePath);
    void tableRecognized(int rows,
                         int columns,
                         const QJsonArray &cells,
                         const QJsonArray &spans,
                         const QJsonObject &response);
    void tableChanged(const QJsonArray &cells);
    void failed(const QString &errorCode,
                const QString &message,
                bool retryable);

private slots:
    void captureReadyChanged(bool ready);
    void captureAfterFocusLock();
    void focusLockFailed();
    void imageSaved(int captureId, const QString &fileName);
    void cameraError();
    void captureError(int captureId,
                      QCameraImageCapture::Error error,
                      const QString &errorString);
    void ocrRequestSucceeded(int requestId,
                             const QString &action,
                             const QJsonObject &response);
    void ocrRequestFailed(int requestId,
                          const QString &action,
                          const QString &errorCode,
                          const QString &field,
                          const QString &message,
                          bool retryable);

private:
    int preferredCameraIndex() const;
    QString createRequestDirectory();
    void configureCamera();
    void resetCaptureState();
    void emitFailure(const QString &errorCode,
                     const QString &message,
                     bool retryable);

    QString m_workRoot;
    OcrTableClient *m_ocr;
    QCameraViewfinder *m_viewfinder;
    QCamera *m_camera;
    QCameraImageCapture *m_capture;
    QTimer *m_focusTimer;
    QString m_cameraDescription;
    QString m_requestDirectory;
    QString m_capturePath;
    QString m_originalCapturePath;
    QRectF m_tableRegion;
    QJsonObject m_lastResponse;
    int m_cameraIndex;
    int m_ocrRetryCount;
    bool m_ocrRetryPending;
    bool m_regionFallbackAttempted;
    bool m_cameraReady;
    bool m_capturePending;
    bool m_captureStarted;
};

#endif // CAMERAOCRCLIENT_H
