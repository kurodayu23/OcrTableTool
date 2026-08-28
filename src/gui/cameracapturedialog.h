#ifndef CAMERACAPTUREDIALOG_H
#define CAMERACAPTUREDIALOG_H

#include <QDialog>

class QCamera;
class QCameraImageCapture;
class QCameraViewfinder;
class QLabel;
class QPushButton;
class QTimer;

class CameraCaptureDialog : public QDialog
{
    Q_OBJECT

public:
    explicit CameraCaptureDialog(QWidget *parent = 0);
    ~CameraCaptureDialog();

    QString capturedImagePath() const;

private slots:
    void captureImage();
    void captureAfterFocusLock();
    void imageSaved(int id, const QString &fileName);
    void captureReadyChanged(bool ready);
    void focusLockFailed();
    void cameraError();
    void captureError();

private:
    void startCamera(int index);
    void stopCamera();
    QString nextCapturePath() const;

    QCameraViewfinder *m_viewfinder;
    QLabel *m_statusLabel;
    QPushButton *m_captureButton;
    QCamera *m_camera;
    QCameraImageCapture *m_capture;
    QTimer *m_focusTimer;
    QString m_capturedImagePath;
    bool m_capturePending;
    bool m_captureStarted;
};

#endif // CAMERACAPTUREDIALOG_H
