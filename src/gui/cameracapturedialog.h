#ifndef CAMERACAPTUREDIALOG_H
#define CAMERACAPTUREDIALOG_H

#include <QDialog>
#include <QTimer>
#include <QString>

class QCamera;
class QCameraImageCapture;
class QCameraViewfinder;
class QLabel;
class QPushButton;
class QTimer;

namespace OcrTableToolGui
{
// 在 baseDirectory 下准备 OcrTableTool/ 目录并给出本次拍照的保存路径。
// 目录建不出来时返回空串并把可读原因写进 failureReason —— findings：
// 过去 mkpath 的返回值被整个丢掉，照样返回一个指向不存在目录的路径，
// 用户看到的是"照片保存失败，请重试"这种误导性提示。
QString nextCapturePathIn(const QString &baseDirectory, QString *failureReason);
// 用系统图片目录（PicturesLocation，缺失时退到 AppLocalDataLocation）的同义接口。
QString nextCapturePath(QString *failureReason);
}

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
    // 摄像头报错 / 没检测到设备后的重试入口（findings：过去报错后快门永久禁用、
    // 对话框内没有任何重试入口，用户只能关掉整个对话框重开）。
    void retryCamera();

private:
    void startCamera(int index);
    void stopCamera();

    QCameraViewfinder *m_viewfinder;
    QLabel *m_statusLabel;
    QPushButton *m_captureButton;
    QPushButton *m_retryButton;
    QCamera *m_camera;
    QCameraImageCapture *m_capture;
    QTimer *m_focusTimer;
    QString m_capturedImagePath;
    bool m_capturePending;
    bool m_captureStarted;
    int m_cameraIndex;
    int m_cameraRetryCount;
    // 打开设备的兜底定时器：既没就绪也没报错时，靠它把「重试打开摄像头」亮出来，
    // 避免用户被卡在「正在打开摄像头…」上无路可走（2026-11-02）。
    QTimer *m_openWatchdog;
};

#endif // CAMERACAPTUREDIALOG_H
