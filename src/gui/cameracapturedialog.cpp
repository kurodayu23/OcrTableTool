#include "cameracapturedialog.h"

#include <QCamera>
#include <QCameraExposure>
#include <QCameraFocus>
#include <QCameraImageCapture>
#include <QCameraImageProcessing>
#include <QCameraInfo>
#include <QCameraViewfinder>
#include <QCameraViewfinderSettings>
#include <QDateTime>
#include <QDialogButtonBox>
#include <QDir>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QLabel>
#include <QMessageBox>
#include <QPushButton>
#include <QImageEncoderSettings>
#include <QImage>
#include <QImageReader>
#include <QStandardPaths>
#include <QStringList>
#include <QTimer>
#include <QVector>
#include <QVBoxLayout>

namespace {

int preferredCameraIndex(const QList<QCameraInfo> &cameras)
{
    for (int index = 0; index < cameras.size(); ++index) {
        if (cameras.at(index).position() == QCamera::BackFace)
            return index;
    }
    for (int index = 0; index < cameras.size(); ++index) {
        const QString description = cameras.at(index).description().toLower();
        if (description.contains(QStringLiteral("8m"))
            || description.contains(QStringLiteral("rear"))
            || description.contains(QStringLiteral("back"))) {
            return index;
        }
    }
    return 0;
}

QSize preferredStillResolution(const QList<QSize> &resolutions)
{
    QSize preferred;
    const qint64 maximumPixels = 12LL * 1024LL * 1024LL;
    for (int index = 0; index < resolutions.size(); ++index) {
        const QSize candidate = resolutions.at(index);
        const qint64 pixels = qint64(candidate.width()) * candidate.height();
        const qint64 preferredPixels = qint64(preferred.width()) * preferred.height();
        if (pixels <= maximumPixels && pixels > preferredPixels)
            preferred = candidate;
    }
    if (!preferred.isValid()) {
        for (int index = 0; index < resolutions.size(); ++index) {
            const QSize candidate = resolutions.at(index);
            if (!preferred.isValid()
                || qint64(candidate.width()) * candidate.height()
                    > qint64(preferred.width()) * preferred.height()) {
                preferred = candidate;
            }
        }
    }
    return preferred;
}

struct CaptureQuality
{
    QStringList issues;
    double sharpness;

    bool needsConfirmation() const { return !issues.isEmpty(); }
};

int percentileFromHistogram(const QVector<int> &histogram, int total, double percentile)
{
    const int target = qMax(1, qRound(total * percentile));
    int accumulated = 0;
    for (int value = 0; value < histogram.size(); ++value) {
        accumulated += histogram.at(value);
        if (accumulated >= target)
            return value;
    }
    return 255;
}

CaptureQuality assessCaptureQuality(const QImage &source)
{
    CaptureQuality result;
    result.sharpness = 0.0;
    if (source.isNull()) {
        result.issues << QStringLiteral("无法读取照片像素");
        return result;
    }
    const QImage image = source.convertToFormat(QImage::Format_RGB32);
    const int marginX = image.width() / 25;
    const int marginY = image.height() / 25;
    const int left = qMax(1, marginX);
    const int right = qMin(image.width() - 1, image.width() - marginX);
    const int top = qMax(1, marginY);
    const int bottom = qMin(image.height() - 1, image.height() - marginY);
    QVector<int> histogram(256, 0);
    int total = 0;
    int darkPixels = 0;
    int highlightPixels = 0;
    for (int y = top; y < bottom; ++y) {
        const QRgb *line = reinterpret_cast<const QRgb *>(image.constScanLine(y));
        for (int x = left; x < right; ++x) {
            const int gray = qGray(line[x]);
            ++histogram[gray];
            ++total;
            if (gray <= 45)
                ++darkPixels;
            if (gray >= 253)
                ++highlightPixels;
        }
    }
    if (total <= 0) {
        result.issues << QStringLiteral("无法分析照片质量");
        return result;
    }
    const int p10 = percentileFromHistogram(histogram, total, 0.10);
    const int median = percentileFromHistogram(histogram, total, 0.50);
    const int p90 = percentileFromHistogram(histogram, total, 0.90);
    const double darkRatio = double(darkPixels) / double(total);
    const double highlightRatio = double(highlightPixels) / double(total);

    double laplacianSum = 0.0;
    double laplacianSquareSum = 0.0;
    int laplacianCount = 0;
    for (int y = top + 1; y < bottom - 1; y += 2) {
        const QRgb *previous = reinterpret_cast<const QRgb *>(image.constScanLine(y - 1));
        const QRgb *line = reinterpret_cast<const QRgb *>(image.constScanLine(y));
        const QRgb *next = reinterpret_cast<const QRgb *>(image.constScanLine(y + 1));
        for (int x = left + 1; x < right - 1; x += 2) {
            const double laplacian = 4.0 * qGray(line[x])
                - qGray(line[x - 1]) - qGray(line[x + 1])
                - qGray(previous[x]) - qGray(next[x]);
            laplacianSum += laplacian;
            laplacianSquareSum += laplacian * laplacian;
            ++laplacianCount;
        }
    }
    if (laplacianCount > 0) {
        const double mean = laplacianSum / laplacianCount;
        result.sharpness = laplacianSquareSum / laplacianCount - mean * mean;
    }
    if (median < 72 || p90 < 112 || darkRatio > 0.32)
        result.issues << QStringLiteral("画面过暗");
    if (p90 - p10 < 42)
        result.issues << QStringLiteral("文字与纸张对比度偏低");
    if (result.sharpness < 38.0)
        result.issues << QStringLiteral("可能失焦或手抖模糊");
    if (highlightRatio > 0.12 && median < 230)
        result.issues << QStringLiteral("局部可能反光过曝");
    return result;
}

} // namespace

namespace OcrTableToolGui
{

QString nextCapturePathIn(const QString &baseDirectory, QString *failureReason)
{
    if (failureReason)
        failureReason->clear();
    if (baseDirectory.isEmpty()) {
        if (failureReason)
            *failureReason = QStringLiteral("找不到可写的照片保存位置，请检查系统存储与文件权限。");
        return QString();
    }
    const QString directory = QDir(baseDirectory).filePath(QStringLiteral("OcrTableTool"));
    // mkpath 返回 false 有两种情况：目录已在（正常）和建不出来（磁盘满 / 无权限 /
    // 同名文件挡路）。只有"建完它仍然不是目录"才是真失败。
    if (!QDir().mkpath(directory) && !QFileInfo(directory).isDir()) {
        if (failureReason) {
            *failureReason = QStringLiteral("无法创建照片保存目录：%1。请检查磁盘空间与文件权限后重试。")
                                 .arg(QDir::toNativeSeparators(directory));
        }
        return QString();
    }
    return QDir(directory).filePath(
        QStringLiteral("camera_%1.jpg")
            .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd_hhmmss_zzz"))));
}

QString nextCapturePath(QString *failureReason)
{
    if (failureReason)
        failureReason->clear();
    QString baseDirectory = QStandardPaths::writableLocation(QStandardPaths::PicturesLocation);
    if (baseDirectory.isEmpty())
        baseDirectory = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    if (baseDirectory.isEmpty()) {
        if (failureReason) {
            *failureReason = QStringLiteral("找不到可写的照片保存位置，请检查系统存储与文件权限。");
        }
        return QString();
    }
    return nextCapturePathIn(baseDirectory, failureReason);
}

} // namespace OcrTableToolGui

CameraCaptureDialog::CameraCaptureDialog(QWidget *parent)
    : QDialog(parent)
    , m_viewfinder(new QCameraViewfinder(this))
    , m_statusLabel(new QLabel(this))
    , m_captureButton(new QPushButton(QStringLiteral("拍摄"), this))
    , m_retryButton(new QPushButton(QStringLiteral("重试打开摄像头"), this))
    , m_camera(0)
    , m_capture(0)
    , m_focusTimer(new QTimer(this))
    , m_capturePending(false)
    , m_captureStarted(false)
    , m_cameraIndex(-1)
    , m_cameraRetryCount(0)
    , m_openWatchdog(0)
{
    setWindowTitle(QStringLiteral("拍照"));
    setModal(true);
    setMinimumSize(520, 400);
    resize(720, 520);

    QVBoxLayout *layout = new QVBoxLayout(this);
    layout->setContentsMargins(16, 14, 16, 16);
    layout->setSpacing(10);

    m_viewfinder->setMinimumHeight(280);
    m_viewfinder->setAspectRatioMode(Qt::KeepAspectRatio);
    layout->addWidget(m_viewfinder, 1);

    m_statusLabel->setWordWrap(true);
    layout->addWidget(m_statusLabel);

    QDialogButtonBox *buttons = new QDialogButtonBox(QDialogButtonBox::Cancel, this);
    m_captureButton->setObjectName(QStringLiteral("PrimaryButton"));
    m_captureButton->setMinimumHeight(48);
    m_captureButton->setMinimumWidth(118);
    m_captureButton->setEnabled(false);
    buttons->addButton(m_captureButton, QDialogButtonBox::AcceptRole);
    // 重试入口：默认隐藏，只在摄像头出错 / 一台都没检测到时出现。
    m_retryButton->setObjectName(QStringLiteral("SecondaryButton"));
    m_retryButton->setMinimumHeight(48);
    m_retryButton->setMinimumWidth(118);
    m_retryButton->setVisible(false);
    buttons->addButton(m_retryButton, QDialogButtonBox::ActionRole);
    layout->addWidget(buttons);

    const QList<QCameraInfo> cameras = QCameraInfo::availableCameras();
    const int defaultCameraIndex = cameras.isEmpty() ? -1 : preferredCameraIndex(cameras);

    connect(m_captureButton, SIGNAL(clicked()), this, SLOT(captureImage()));
    connect(m_retryButton, SIGNAL(clicked()), this, SLOT(retryCamera()));
    connect(buttons, SIGNAL(rejected()), this, SLOT(reject()));
    m_focusTimer->setSingleShot(true);
    connect(m_focusTimer, SIGNAL(timeout()), this, SLOT(focusLockFailed()));

    if (cameras.isEmpty()) {
        m_statusLabel->setText(QStringLiteral("未检测到可用摄像头，请确认平板摄像头未被其他程序占用。"));
        // 一台摄像头都没有时也留一个重试入口：用户插上 / 放开占用后不必关掉对话框重开。
        m_retryButton->setVisible(true);
        return;
    }

    m_statusLabel->setText(QStringLiteral("正在打开摄像头…"));
    startCamera(defaultCameraIndex);
}

CameraCaptureDialog::~CameraCaptureDialog()
{
    stopCamera();
}

QString CameraCaptureDialog::capturedImagePath() const
{
    return m_capturedImagePath;
}

void CameraCaptureDialog::startCamera(int index)
{
    const QList<QCameraInfo> cameras = QCameraInfo::availableCameras();
    if (index < 0 || index >= cameras.size()) {
        m_cameraIndex = -1;
        m_captureButton->setEnabled(false);
        m_retryButton->setVisible(true);
        m_statusLabel->setText(QStringLiteral("未检测到可用摄像头，请确认平板摄像头未被其他程序占用。"));
        return;
    }

    stopCamera();
    m_cameraIndex = index;
    m_retryButton->setVisible(false);
    m_captureButton->setEnabled(false);
    m_statusLabel->setText(QStringLiteral("正在打开摄像头…"));

    // ⚠ 2026-11-02：打开设备是**异步**的，而「设备就绪」（readyForCaptureChanged）
    //   与「设备报错」（QCamera::error）这两个信号**都不来**的情形是真实存在的
    //   （驱动静默失败 / 设备被占用 / 权限被拒但没报错）。
    //   那条路径上 startCamera() 藏起来的重试入口永远不会被恢复
    //   ⇒ 快门禁用 + 状态停在「正在打开摄像头…」+ 用户没有任何重试途径
    //   ⇒ 只能关掉整个对话框重开。
    //   这里加一个兜底定时器：超时（既没就绪也没报错）就把重试入口亮出来。
    //   8 秒远大于真机上摄像头的冷启动时间（通常 < 2 s），
    //   正常路径上不会误触发（就绪信号会先到并停掉它）。
    if (m_openWatchdog) {
        m_openWatchdog->stop();
    }
    m_openWatchdog = new QTimer(this);
    m_openWatchdog->setSingleShot(true);
    m_openWatchdog->setInterval(8000);
    connect(m_openWatchdog, &QTimer::timeout, this, [this]() {
        // 还没就绪 ⇒ 把重试入口亮出来，别让用户卡死。
        if (m_capture && m_capture->isReadyForCapture())
            return;
        m_retryButton->setVisible(true);
        m_retryButton->setEnabled(true);
        if (m_statusLabel->text().contains(QStringLiteral("正在打开摄像头")))
            m_statusLabel->setText(QStringLiteral(
                "摄像头迟迟未就绪，请点「重试打开摄像头」后重试。"));
    });
    m_openWatchdog->start();

    m_camera = new QCamera(cameras.at(index), this);
    m_camera->setCaptureMode(QCamera::CaptureStillImage);
    m_camera->setViewfinder(m_viewfinder);

    QCameraFocus *focus = m_camera->focus();
    if (focus && focus->isFocusModeSupported(QCameraFocus::ContinuousFocus))
        focus->setFocusMode(QCameraFocus::ContinuousFocus);
    QCameraExposure *exposure = m_camera->exposure();
    if (exposure && exposure->isExposureModeSupported(QCameraExposure::ExposureAuto))
        exposure->setExposureMode(QCameraExposure::ExposureAuto);
    QCameraImageProcessing *processing = m_camera->imageProcessing();
    if (processing
        && processing->isWhiteBalanceModeSupported(QCameraImageProcessing::WhiteBalanceAuto)) {
        processing->setWhiteBalanceMode(QCameraImageProcessing::WhiteBalanceAuto);
    }

    const QList<QCameraViewfinderSettings> viewfinderSettings = m_camera->supportedViewfinderSettings();
    QCameraViewfinderSettings preferredViewfinder;
    // The live view only fills a tablet-sized dialog.  Keep it at 720p so the
    // camera driver and Qt do not decode and paint 1080p frames unnecessarily;
    // QCameraImageCapture below still uses the independent 7-12 MP JPEG setting.
    const qint64 maximumPreviewPixels = 1280LL * 720LL;
    for (int settingIndex = 0; settingIndex < viewfinderSettings.size(); ++settingIndex) {
        const QCameraViewfinderSettings candidate = viewfinderSettings.at(settingIndex);
        const QSize resolution = candidate.resolution();
        const qint64 pixels = qint64(resolution.width()) * resolution.height();
        if (pixels > maximumPreviewPixels)
            continue;
        const qint64 preferredPixels = qint64(preferredViewfinder.resolution().width())
            * preferredViewfinder.resolution().height();
        if (!preferredViewfinder.resolution().isValid()
            || pixels > preferredPixels
            || (pixels == preferredPixels
                && candidate.maximumFrameRate() > preferredViewfinder.maximumFrameRate())) {
            preferredViewfinder = candidate;
        }
    }
    if (!preferredViewfinder.resolution().isValid()) {
        for (int settingIndex = 0; settingIndex < viewfinderSettings.size(); ++settingIndex) {
            const QCameraViewfinderSettings candidate = viewfinderSettings.at(settingIndex);
            const QSize resolution = candidate.resolution();
            const qint64 pixels = qint64(resolution.width()) * resolution.height();
            const qint64 preferredPixels = qint64(preferredViewfinder.resolution().width())
                * preferredViewfinder.resolution().height();
            if (!preferredViewfinder.resolution().isValid()
                || pixels < preferredPixels
                || (pixels == preferredPixels
                    && candidate.maximumFrameRate() > preferredViewfinder.maximumFrameRate())) {
                preferredViewfinder = candidate;
            }
        }
    }
    if (preferredViewfinder.resolution().isValid())
        m_camera->setViewfinderSettings(preferredViewfinder);

    m_capture = new QCameraImageCapture(m_camera);
    m_capture->setCaptureDestination(QCameraImageCapture::CaptureToFile);
    QImageEncoderSettings encoderSettings;
    encoderSettings.setCodec(QStringLiteral("image/jpeg"));
    encoderSettings.setQuality(QMultimedia::VeryHighQuality);
    const QSize stillResolution = preferredStillResolution(
        m_capture->supportedResolutions(encoderSettings));
    if (stillResolution.isValid())
        encoderSettings.setResolution(stillResolution);
    m_capture->setEncodingSettings(encoderSettings);
    connect(m_capture, SIGNAL(imageSaved(int,QString)), this, SLOT(imageSaved(int,QString)));
    connect(m_capture, SIGNAL(readyForCaptureChanged(bool)), this, SLOT(captureReadyChanged(bool)));
    connect(m_capture, SIGNAL(error(int,QCameraImageCapture::Error,const QString &)), this, SLOT(captureError()));
    connect(m_camera, SIGNAL(error(QCamera::Error)), this, SLOT(cameraError()));
    connect(m_camera, SIGNAL(locked()), this, SLOT(captureAfterFocusLock()));
    connect(m_camera, SIGNAL(lockFailed()), this, SLOT(focusLockFailed()));
    m_camera->start();
}

void CameraCaptureDialog::stopCamera()
{
    if (m_openWatchdog) {
        m_openWatchdog->stop();
        m_openWatchdog->deleteLater();
        m_openWatchdog = 0;
    }
    m_capturePending = false;
    m_captureStarted = false;
    m_focusTimer->stop();
    if (m_camera)
        m_camera->stop();
    delete m_capture;
    delete m_camera;
    m_capture = 0;
    m_camera = 0;
}

void CameraCaptureDialog::captureImage()
{
    if (m_capturePending || !m_capture || !m_capture->isReadyForCapture()) {
        m_statusLabel->setText(QStringLiteral("摄像头尚未就绪，请稍候后重试。"));
        return;
    }
    m_capturePending = true;
    m_captureStarted = false;
    m_captureButton->setEnabled(false);
    m_statusLabel->setText(QStringLiteral("正在锁定对焦和曝光，请保持平板稳定…"));
    if (m_camera) {
        const QCamera::LockTypes locks = m_camera->supportedLocks()
            & (QCamera::LockFocus | QCamera::LockExposure | QCamera::LockWhiteBalance);
        if (locks != QCamera::NoLock) {
            m_focusTimer->start(3500);
            m_camera->searchAndLock(locks);
            return;
        }
    }
    QTimer::singleShot(0, this, SLOT(captureAfterFocusLock()));
}

void CameraCaptureDialog::captureAfterFocusLock()
{
    if (!m_capturePending || m_captureStarted)
        return;
    m_focusTimer->stop();
    if (!m_capture || !m_capture->isReadyForCapture()) {
        m_capturePending = false;
        m_captureButton->setEnabled(m_capture && m_capture->isReadyForCapture());
        m_statusLabel->setText(QStringLiteral("对焦后摄像头未就绪，请保持稳定后重新拍摄。"));
        if (m_camera)
            m_camera->unlock();
        return;
    }
    m_captureStarted = true;
    m_statusLabel->setText(QStringLiteral("正在保存高清照片…"));
    // findings：路径解析失败（目录建不出来）必须当场如实报错，而不是把一张
    // 指向不存在目录的路径交给 Qt，再让用户看到"照片保存失败，请重试"。
    QString pathFailure;
    const QString targetPath = OcrTableToolGui::nextCapturePath(&pathFailure);
    if (targetPath.isEmpty()) {
        m_capturePending = false;
        m_captureStarted = false;
        m_captureButton->setEnabled(m_capture && m_capture->isReadyForCapture());
        m_statusLabel->setText(pathFailure.isEmpty()
            ? QStringLiteral("无法确定照片保存位置，请检查磁盘空间与文件权限后重试。")
            : pathFailure);
        if (m_camera)
            m_camera->unlock();
        return;
    }
    m_capture->capture(targetPath);
}

void CameraCaptureDialog::focusLockFailed()
{
    if (!m_capturePending || m_captureStarted)
        return;
    m_focusTimer->stop();
    m_capturePending = false;
    m_captureButton->setEnabled(m_capture && m_capture->isReadyForCapture());
    m_statusLabel->setText(QStringLiteral("未能锁定对焦和曝光，请保持平板稳定、增加光线后重试。"));
    if (m_camera)
        m_camera->unlock();
}

void CameraCaptureDialog::imageSaved(int, const QString &fileName)
{
    if (fileName.isEmpty()) {
        m_capturePending = false;
        m_captureStarted = false;
        m_captureButton->setEnabled(true);
        m_statusLabel->setText(QStringLiteral("照片保存失败，请重试。"));
        return;
    }
    QImageReader reader(fileName);
    const QSize actualResolution = reader.size();
    const qint64 actualPixels = qint64(actualResolution.width()) * actualResolution.height();
    const qint64 minimumPixels = 7LL * 1000LL * 1000LL;
    if (!actualResolution.isValid() || actualPixels < minimumPixels) {
        m_capturePending = false;
        m_captureStarted = false;
        m_captureButton->setEnabled(m_capture && m_capture->isReadyForCapture());
        m_statusLabel->setText(actualResolution.isValid()
            ? QStringLiteral("实际照片只有 %1 × %2，未达到7MP，请检查后置摄像头驱动或重新选择摄像头。")
                  .arg(actualResolution.width())
                  .arg(actualResolution.height())
            : QStringLiteral("无法读取保存的照片，请重新拍摄。"));
        if (m_camera)
            m_camera->unlock();
        return;
    }
    reader.setAutoTransform(true);
    reader.setScaledSize(actualResolution.scaled(QSize(1280, 960), Qt::KeepAspectRatio));
    const QImage qualityImage = reader.read();
    const CaptureQuality quality = assessCaptureQuality(qualityImage);
    if (quality.needsConfirmation()) {
        m_capturePending = false;
        m_captureStarted = false;
        if (m_camera)
            m_camera->unlock();
        QMessageBox qualityDialog(QMessageBox::Warning,
                                  QStringLiteral("照片质量需要确认"),
                                  QStringLiteral("检测到：%1。\n为了提高表格识别准确度，建议改善光线、擦净镜头、让表格充满画面并保持稳定后重新拍摄。")
                                      .arg(quality.issues.join(QStringLiteral("、"))),
                                  QMessageBox::NoButton,
                                  this);
        QPushButton *retryButton = qualityDialog.addButton(QStringLiteral("重新拍摄"), QMessageBox::AcceptRole);
        QPushButton *useButton = qualityDialog.addButton(QStringLiteral("仍然使用"), QMessageBox::ActionRole);
        qualityDialog.setDefaultButton(retryButton);
        qualityDialog.exec();
        if (qualityDialog.clickedButton() != useButton) {
            m_captureButton->setEnabled(m_capture && m_capture->isReadyForCapture());
            m_statusLabel->setText(QStringLiteral("请重新构图：避免反光，让文字清晰、纸张平整并尽量充满画面。"));
            return;
        }
    }
    m_capturePending = false;
    m_captureStarted = false;
    if (m_camera)
        m_camera->unlock();
    m_capturedImagePath = fileName;
    accept();
}

void CameraCaptureDialog::captureReadyChanged(bool ready)
{
    if (m_openWatchdog) {
        m_openWatchdog->stop();
        m_openWatchdog->deleteLater();
        m_openWatchdog = 0;
    }
    m_captureButton->setEnabled(ready && !m_capturePending);
    // ⚠ 2026-11-02：原来只在 ready=true 时才碰重试入口，于是「重试之后设备一直没
    //   就绪」这条路径上，入口被 startCamera() 藏掉后再也不会出现
    //   ⇒ 快门禁用 + 状态停在「正在打开摄像头…」+ 没有重试入口
    //   ⇒ 用户被卡死，只能关掉整个对话框重开。
    //   现在按「是否就绪」决定入口的可见性：没就绪就把入口亮出来（能再试），
    //   就绪了才藏起来（此时快门可用，不需要重试）。
    m_retryButton->setVisible(!ready);
    if (ready && !m_capturePending) {
        const QSize resolution = m_capture ? m_capture->encodingSettings().resolution() : QSize();
        m_statusLabel->setText(resolution.isValid()
            ? QStringLiteral("后置摄像头已就绪（%1 × %2），请保持平板稳定并让表格充满画面。")
                  .arg(resolution.width())
                  .arg(resolution.height())
            : QStringLiteral("摄像头已就绪，请保持平板稳定并让表格充满画面。"));
    }
}

void CameraCaptureDialog::cameraError()
{
    if (m_openWatchdog) {
        m_openWatchdog->stop();
        m_openWatchdog->deleteLater();
        m_openWatchdog = 0;
    }
    // findings：原来这里只把快门禁用就结束了 —— 之后既没有任何信号能把快门
    // 重新打开，对话框里也没有重试入口，用户只能关掉整个对话框重开。
    // 现在错误态：快门仍禁用（摄像头不可用），但保留一个醒目的重试入口。
    m_capturePending = false;
    m_captureStarted = false;
    m_focusTimer->stop();
    m_captureButton->setEnabled(false);
    m_retryButton->setVisible(true);
    m_retryButton->setEnabled(true);
    m_statusLabel->setText(
        QStringLiteral("摄像头无法打开，请检查系统相机权限、驱动或是否被其他程序占用；处理后点「重试打开摄像头」。"));
}

void CameraCaptureDialog::retryCamera()
{
    ++m_cameraRetryCount;
    if (m_cameraIndex < 0) {
        const QList<QCameraInfo> cameras = QCameraInfo::availableCameras();
        if (cameras.isEmpty()) {
            m_captureButton->setEnabled(false);
            m_retryButton->setVisible(true);
            m_retryButton->setEnabled(true);
            m_statusLabel->setText(
                QStringLiteral("仍未检测到可用摄像头，请连接或启用摄像头后再次重试。"));
            return;
        }
        m_cameraIndex = preferredCameraIndex(cameras);
    }
    // 重开设备：走与首次启动完全相同的链路，就绪后由 captureReadyChanged
    // 把快门重新打开。
    startCamera(m_cameraIndex);
}

void CameraCaptureDialog::captureError()
{
    m_capturePending = false;
    m_captureStarted = false;
    m_focusTimer->stop();
    if (m_camera)
        m_camera->unlock();
    const bool ready = m_capture && m_capture->isReadyForCapture();
    m_captureButton->setEnabled(ready);
    // 摄像头没回到就绪态时同样给重试入口，别让快门禁用变成死路。
    m_retryButton->setVisible(!ready);
    m_statusLabel->setText(ready
        ? QStringLiteral("拍照保存失败，请重新拍摄。")
        : QStringLiteral("拍照失败，摄像头暂未就绪，请点「重试打开摄像头」后重拍。"));
}
