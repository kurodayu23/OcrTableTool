#include "cameraocrclient.h"

#include "ocrtableclient.h"

#include <QCamera>
#include <QCameraExposure>
#include <QCameraFocus>
#include <QCameraImageCapture>
#include <QCameraImageProcessing>
#include <QCameraInfo>
#include <QCameraViewfinder>
#include <QCameraViewfinderSettings>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QImageEncoderSettings>
#include <QImage>
#include <QImageReader>
#include <QStandardPaths>
#include <QTimer>
#include <QtMath>

namespace {

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

QCameraViewfinderSettings preferredPreviewSettings(
    const QList<QCameraViewfinderSettings> &settings)
{
    QCameraViewfinderSettings preferred;
    const qint64 maximumPreviewPixels = 1280LL * 720LL;
    for (int index = 0; index < settings.size(); ++index) {
        const QCameraViewfinderSettings candidate = settings.at(index);
        const QSize resolution = candidate.resolution();
        const qint64 pixels = qint64(resolution.width()) * resolution.height();
        const qint64 currentPixels = qint64(preferred.resolution().width())
            * preferred.resolution().height();
        if (pixels <= maximumPreviewPixels
            && (!preferred.resolution().isValid()
                || pixels > currentPixels
                || (pixels == currentPixels
                    && candidate.maximumFrameRate() > preferred.maximumFrameRate()))) {
            preferred = candidate;
        }
    }
    if (!preferred.resolution().isValid() && !settings.isEmpty()) {
        preferred = settings.first();
        for (int index = 1; index < settings.size(); ++index) {
            const QCameraViewfinderSettings candidate = settings.at(index);
            const qint64 pixels = qint64(candidate.resolution().width())
                * candidate.resolution().height();
            const qint64 currentPixels = qint64(preferred.resolution().width())
                * preferred.resolution().height();
            if (pixels < currentPixels)
                preferred = candidate;
        }
    }
    return preferred;
}

QString normalizedCapturedFilePath(const QString &fileName)
{
    const QFileInfo info(fileName);
    const QString actualSuffix = info.suffix().toLower();
    const QString completeBaseName = info.completeBaseName();
    const QString requestedSuffix = QFileInfo(completeBaseName).suffix().toLower();
    if (actualSuffix.isEmpty()
        || (actualSuffix != QStringLiteral("bmp")
            && actualSuffix != QStringLiteral("jpg")
            && actualSuffix != QStringLiteral("jpeg")
            && actualSuffix != QStringLiteral("png"))
        || (requestedSuffix != QStringLiteral("jpg")
            && requestedSuffix != QStringLiteral("jpeg"))) {
        return info.absoluteFilePath();
    }

    const QString stem = QFileInfo(completeBaseName).completeBaseName();
    const QString normalizedPath = info.dir().filePath(
        stem + QLatin1Char('.') + actualSuffix);
    if (normalizedPath == info.absoluteFilePath()
        || QFileInfo::exists(normalizedPath)
        || !QFile::rename(info.absoluteFilePath(), normalizedPath)) {
        return info.absoluteFilePath();
    }
    return normalizedPath;
}

} // namespace

CameraOcrClient::CameraOcrClient(const QString &backendExecutable,
                                 const QString &workRoot,
                                 QObject *parent)
    : QObject(parent)
    , m_workRoot(QDir::cleanPath(workRoot))
    , m_ocr(new OcrTableClient(backendExecutable, this))
    , m_viewfinder(0)
    , m_camera(0)
    , m_capture(0)
    , m_focusTimer(new QTimer(this))
    , m_cameraIndex(-1)
    , m_ocrRetryCount(0)
    , m_ocrRetryPending(false)
    , m_regionFallbackAttempted(false)
    , m_cameraReady(false)
    , m_capturePending(false)
    , m_captureStarted(false)
{
    if (m_workRoot.isEmpty()) {
        m_workRoot = QStandardPaths::writableLocation(
            QStandardPaths::AppLocalDataLocation);
        m_workRoot = QDir(m_workRoot).filePath(QStringLiteral("camera-ocr"));
    }
    m_focusTimer->setSingleShot(true);
    connect(m_focusTimer, SIGNAL(timeout()), this, SLOT(focusLockFailed()));
    connect(m_ocr, &OcrTableClient::requestSucceeded,
            this, &CameraOcrClient::ocrRequestSucceeded);
    connect(m_ocr, &OcrTableClient::requestFailed,
            this, &CameraOcrClient::ocrRequestFailed);
}

CameraOcrClient::~CameraOcrClient()
{
    m_ocr->cancel();
    stopCamera();
}

QStringList CameraOcrClient::availableCameraDescriptions()
{
    QStringList descriptions;
    const QList<QCameraInfo> cameras = QCameraInfo::availableCameras();
    for (int index = 0; index < cameras.size(); ++index)
        descriptions.append(cameras.at(index).description());
    return descriptions;
}

QString CameraOcrClient::preferredStillImageCodec(const QStringList &supportedCodecs)
{
    for (const QString &codec : supportedCodecs) {
        if (codec.compare(QStringLiteral("image/jpeg"), Qt::CaseInsensitive) == 0)
            return codec;
    }
    return supportedCodecs.isEmpty() ? QString() : supportedCodecs.first();
}

bool CameraOcrClient::ensureWritableWorkRoot(const QString &root,
                                              QString *errorMessage)
{
    const QString cleanRoot = QDir::cleanPath(root);
    if (cleanRoot.isEmpty() || cleanRoot == QStringLiteral(".")) {
        if (errorMessage)
            *errorMessage = QString::fromWCharArray(L"拍照工作目录为空。");
        return false;
    }
    if (!QDir().mkpath(cleanRoot)) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"无法创建拍照工作目录：%1").arg(cleanRoot);
        }
        return false;
    }

    const QString probePath = QDir(cleanRoot).filePath(
        QStringLiteral(".camera-ocr-write-test-%1.tmp")
            .arg(QCoreApplication::applicationPid()));
    QFile probe(probePath);
    if (!probe.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"拍照工作目录没有写入权限：%1").arg(cleanRoot);
        }
        return false;
    }
    const bool wrote = probe.write("camera-ocr") == 10;
    probe.close();
    QFile::remove(probePath);
    if (!wrote) {
        if (errorMessage)
            *errorMessage = QString::fromWCharArray(L"无法写入拍照工作目录：%1")
                .arg(cleanRoot);
        return false;
    }
    return true;
}

void CameraOcrClient::setViewfinder(QCameraViewfinder *viewfinder)
{
    m_viewfinder = viewfinder;
    if (m_camera && m_viewfinder)
        m_camera->setViewfinder(m_viewfinder);
}

int CameraOcrClient::preferredCameraIndex() const
{
    const QList<QCameraInfo> cameras = QCameraInfo::availableCameras();
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
    return cameras.isEmpty() ? -1 : 0;
}

bool CameraOcrClient::startCamera(int cameraIndex)
{
    if (isBusy()) {
        emitFailure(QStringLiteral("CAMERA_BUSY"),
                    QString::fromWCharArray(L"\u5f53\u524d\u6b63\u5728\u62cd\u7167\u6216\u8bc6\u522b\uff0c\u8bf7\u5b8c\u6210\u540e\u518d\u5207\u6362\u6444\u50cf\u5934\u3002"),
                    true);
        return false;
    }
    const QList<QCameraInfo> cameras = QCameraInfo::availableCameras();
    if (cameras.isEmpty()) {
        emitFailure(QStringLiteral("CAMERA_NOT_FOUND"),
                    QString::fromWCharArray(L"\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u6444\u50cf\u5934\uff0c\u8bf7\u68c0\u67e5\u76f8\u673a\u6743\u9650\u3001\u9a71\u52a8\u6216\u5360\u7528\u72b6\u6001\u3002"),
                    true);
        return false;
    }
    if (cameraIndex < 0)
        cameraIndex = preferredCameraIndex();
    if (cameraIndex < 0 || cameraIndex >= cameras.size()) {
        emitFailure(QStringLiteral("CAMERA_INDEX_INVALID"),
                    QString::fromWCharArray(L"\u6444\u50cf\u5934\u5e8f\u53f7\u65e0\u6548\u3002"),
                    false);
        return false;
    }

    stopCamera();
    m_cameraIndex = cameraIndex;
    m_cameraDescription = cameras.at(cameraIndex).description();
    emit stageChanged(QStringLiteral("camera_starting"),
                      QString::fromWCharArray(L"\u6b63\u5728\u6253\u5f00\u6444\u50cf\u5934\u3002"));
    m_camera = new QCamera(cameras.at(cameraIndex), this);
    configureCamera();
    m_camera->start();
    return true;
}

void CameraOcrClient::configureCamera()
{
    m_camera->setCaptureMode(QCamera::CaptureStillImage);
    if (m_viewfinder)
        m_camera->setViewfinder(m_viewfinder);

    QCameraFocus *focus = m_camera->focus();
    if (focus && focus->isFocusModeSupported(QCameraFocus::ContinuousFocus))
        focus->setFocusMode(QCameraFocus::ContinuousFocus);
    QCameraExposure *exposure = m_camera->exposure();
    if (exposure && exposure->isExposureModeSupported(QCameraExposure::ExposureAuto))
        exposure->setExposureMode(QCameraExposure::ExposureAuto);
    QCameraImageProcessing *processing = m_camera->imageProcessing();
    if (processing
        && processing->isWhiteBalanceModeSupported(
            QCameraImageProcessing::WhiteBalanceAuto)) {
        processing->setWhiteBalanceMode(QCameraImageProcessing::WhiteBalanceAuto);
    }

    const QCameraViewfinderSettings preview = preferredPreviewSettings(
        m_camera->supportedViewfinderSettings());
    if (preview.resolution().isValid())
        m_camera->setViewfinderSettings(preview);

    m_capture = new QCameraImageCapture(m_camera, this);
    m_capture->setCaptureDestination(QCameraImageCapture::CaptureToFile);
    QImageEncoderSettings encoder;
    const QString codec = preferredStillImageCodec(
        m_capture->supportedImageCodecs());
    if (!codec.isEmpty())
        encoder.setCodec(codec);
    encoder.setQuality(QMultimedia::VeryHighQuality);
    const QSize resolution = preferredStillResolution(
        m_capture->supportedResolutions(encoder));
    if (resolution.isValid())
        encoder.setResolution(resolution);
    m_capture->setEncodingSettings(encoder);

    connect(m_capture, SIGNAL(readyForCaptureChanged(bool)),
            this, SLOT(captureReadyChanged(bool)));
    connect(m_capture, SIGNAL(imageSaved(int,QString)),
            this, SLOT(imageSaved(int,QString)));
    connect(m_capture,
            SIGNAL(error(int,QCameraImageCapture::Error,QString)),
            this,
            SLOT(captureError(int,QCameraImageCapture::Error,QString)));
    connect(m_camera, SIGNAL(error(QCamera::Error)),
            this, SLOT(cameraError()));
    connect(m_camera, SIGNAL(locked()),
            this, SLOT(captureAfterFocusLock()));
    connect(m_camera, SIGNAL(lockFailed()),
            this, SLOT(focusLockFailed()));
}

void CameraOcrClient::stopCamera()
{
    resetCaptureState();
    m_cameraReady = false;
    if (m_camera)
        m_camera->stop();
    delete m_capture;
    delete m_camera;
    m_capture = 0;
    m_camera = 0;
    if (m_cameraIndex >= 0)
        emit cameraReadyChanged(false, m_cameraIndex, m_cameraDescription);
}

bool CameraOcrClient::isCameraReady() const
{
    return m_cameraReady && m_capture && m_capture->isReadyForCapture();
}

bool CameraOcrClient::isBusy() const
{
    return m_capturePending || m_captureStarted || m_ocrRetryPending || m_ocr->isBusy();
}

QString CameraOcrClient::createRequestDirectory()
{
    QString errorMessage;
    if (!ensureWritableWorkRoot(m_workRoot, &errorMessage))
        return QString();
    const QString stamp = QDateTime::currentDateTime().toString(
        QStringLiteral("yyyyMMdd-hhmmss-zzz"));
    const QString directory = QDir(m_workRoot).filePath(
        QStringLiteral("camera-request-%1").arg(stamp));
    return QDir().mkpath(directory) ? directory : QString();
}

bool CameraOcrClient::captureImage()
{
    if (isBusy()) {
        emitFailure(QStringLiteral("REQUEST_BUSY"),
                    QString::fromWCharArray(L"\u5f53\u524d\u62cd\u7167\u6216\u8bc6\u522b\u5c1a\u672a\u7ed3\u675f\u3002"),
                    true);
        return false;
    }
    if (!isCameraReady()) {
        emitFailure(QStringLiteral("CAMERA_NOT_READY"),
                    QString::fromWCharArray(L"\u6444\u50cf\u5934\u5c1a\u672a\u5c31\u7eea\uff0c\u8bf7\u7a0d\u5019\u540e\u91cd\u8bd5\u3002"),
                    true);
        return false;
    }
    m_lastResponse = QJsonObject();
    m_ocrRetryCount = 0;
    m_ocrRetryPending = false;
    m_regionFallbackAttempted = false;
    m_originalCapturePath.clear();
    clearTableRegion();
    m_requestDirectory = createRequestDirectory();
    if (m_requestDirectory.isEmpty()) {
        emitFailure(QStringLiteral("WORK_DIRECTORY_CREATE_FAILED"),
                    QString::fromWCharArray(L"\u65e0\u6cd5\u521b\u5efa\u672c\u6b21\u8bc6\u522b\u5de5\u4f5c\u76ee\u5f55\u3002"),
                    false);
        return false;
    }
    // 不预设扩展名，避免摄像头驱动实际输出 BMP 时生成 camera.jpg.bmp。
    m_capturePath = QDir(m_requestDirectory).filePath(QStringLiteral("camera"));
    m_capturePending = true;
    m_captureStarted = false;
    emit stageChanged(QStringLiteral("focusing"),
                      QString::fromWCharArray(L"\u6b63\u5728\u9501\u5b9a\u5bf9\u7126\u3001\u66dd\u5149\u548c\u767d\u5e73\u8861\u3002"));

    const QCamera::LockTypes locks = m_camera->supportedLocks()
        & (QCamera::LockFocus | QCamera::LockExposure | QCamera::LockWhiteBalance);
    if (locks != QCamera::NoLock) {
        m_focusTimer->start(3500);
        m_camera->searchAndLock(locks);
    } else {
        QTimer::singleShot(0, this, SLOT(captureAfterFocusLock()));
    }
    return true;
}

bool CameraOcrClient::captureAndRecognize()
{
    return captureImage();
}

QString CameraOcrClient::currentImagePath() const
{
    return m_originalCapturePath;
}

bool CameraOcrClient::prepareRecognitionImage(const QString &imagePath,
                                              QString *errorMessage)
{
    const QString absolutePath = QFileInfo(imagePath).absoluteFilePath();
    m_capturePath = absolutePath;
    if (!m_tableRegion.isValid())
        return true;

    QImageReader reader(absolutePath);
    reader.setAutoTransform(true);
    const QImage sourceImage = reader.read();
    if (sourceImage.isNull()) {
        if (errorMessage)
            *errorMessage = QStringLiteral("无法读取图片，请选择有效的图片文件。");
        return false;
    }

    const QRect cropRect = resolvedTableRegion(sourceImage.size())
        .intersected(sourceImage.rect());
    if (!cropRect.isValid()) {
        if (errorMessage)
            *errorMessage = QStringLiteral("识别区域无效，请重新框选表格区域。");
        return false;
    }

    const QImage tableImage = sourceImage.copy(cropRect);
    const QString croppedPath = QDir(m_requestDirectory).filePath(
        QStringLiteral("camera-table.png"));
    if (tableImage.isNull() || !tableImage.save(croppedPath, "PNG")) {
        if (errorMessage)
            *errorMessage = QStringLiteral("无法保存裁剪后的表格图片，请重新选择区域。");
        return false;
    }
    m_capturePath = croppedPath;
    emit stageChanged(QStringLiteral("table_region_cropped"),
                      QString::fromWCharArray(L"已按选定区域提取表格，正在识别。"));
    return true;
}

bool CameraOcrClient::recognizeImage(const QString &imagePath,
                                     const QRectF &normalizedRegion,
                                     QString *errorMessage)
{
    if (isBusy()) {
        const QString message = QStringLiteral("当前正在拍照或识别，请完成后再试。");
        if (errorMessage)
            *errorMessage = message;
        emitFailure(QStringLiteral("REQUEST_BUSY"), message, true);
        return false;
    }

    const QFileInfo imageInfo(imagePath);
    if (!imageInfo.isFile()) {
        const QString message = QStringLiteral("图片文件不存在，请重新选择图片。");
        if (errorMessage)
            *errorMessage = message;
        emitFailure(QStringLiteral("IMAGE_NOT_FOUND"), message, false);
        return false;
    }

    if (normalizedRegion.isNull()) {
        clearTableRegion();
    } else if (!setTableRegion(normalizedRegion, errorMessage)) {
        const QString message = errorMessage ? *errorMessage
                                              : QStringLiteral("识别区域无效。");
        emitFailure(QStringLiteral("TABLE_REGION_INVALID"), message, false);
        return false;
    }

    const QString absolutePath = imageInfo.absoluteFilePath();
    if (m_requestDirectory.isEmpty() || m_originalCapturePath != absolutePath) {
        m_requestDirectory = createRequestDirectory();
        if (m_requestDirectory.isEmpty()) {
            const QString message = QStringLiteral("无法创建本次识别工作目录。");
            if (errorMessage)
                *errorMessage = message;
            emitFailure(QStringLiteral("WORK_DIRECTORY_CREATE_FAILED"),
                        message, false);
            return false;
        }
    }

    m_lastResponse = QJsonObject();
    m_ocrRetryCount = 0;
    m_ocrRetryPending = false;
    m_regionFallbackAttempted = false;
    m_originalCapturePath = absolutePath;

    if (!prepareRecognitionImage(absolutePath, errorMessage)) {
        const QString message = errorMessage && !errorMessage->isEmpty()
            ? *errorMessage
            : QStringLiteral("无法准备 OCR 识别图片。");
        emitFailure(QStringLiteral("IMAGE_PREPARE_FAILED"), message, true);
        return false;
    }

    emit stageChanged(QStringLiteral("recognizing"),
                      QString::fromWCharArray(L"正在识别表格，请稍候。"));
    m_ocr->recognize(m_capturePath,
                     m_requestDirectory,
                     false,
                     m_tableRegion.isValid());
    return true;
}

bool CameraOcrClient::setTableRegion(const QRectF &normalizedRegion,
                                     QString *errorMessage)
{
    const bool valid = normalizedRegion.isValid()
        && normalizedRegion.left() >= 0.0
        && normalizedRegion.top() >= 0.0
        && normalizedRegion.right() <= 1.0
        && normalizedRegion.bottom() <= 1.0
        && normalizedRegion.width() >= 0.02
        && normalizedRegion.height() >= 0.02;
    if (!valid) {
        if (errorMessage)
            *errorMessage = QString::fromWCharArray(L"表格区域必须位于照片范围内，宽度和高度不能小于照片的 2%。");
        return false;
    }
    m_tableRegion = normalizedRegion;
    if (errorMessage)
        errorMessage->clear();
    return true;
}

void CameraOcrClient::clearTableRegion()
{
    m_tableRegion = QRectF();
}

QRectF CameraOcrClient::tableRegion() const
{
    return m_tableRegion;
}

QRect CameraOcrClient::resolvedTableRegion(const QSize &imageSize) const
{
    if (!m_tableRegion.isValid() || !imageSize.isValid())
        return QRect();
    const QRect requested(
        qFloor(m_tableRegion.left() * imageSize.width()),
        qFloor(m_tableRegion.top() * imageSize.height()),
        qCeil(m_tableRegion.width() * imageSize.width()),
        qCeil(m_tableRegion.height() * imageSize.height()));
    const QRect bounded = requested.intersected(QRect(QPoint(0, 0), imageSize));
    if (!bounded.isValid())
        return QRect();
    const int paddingX = qMax(4, qCeil(bounded.width() * 0.015));
    const int paddingY = qMax(4, qCeil(bounded.height() * 0.025));
    return bounded.adjusted(-paddingX, -paddingY, paddingX, paddingY)
        .intersected(QRect(QPoint(0, 0), imageSize));
}

QJsonObject CameraOcrClient::lastResponse() const
{
    return m_lastResponse;
}

QJsonArray CameraOcrClient::tableCells() const
{
    return m_lastResponse.value(QStringLiteral("cells")).toArray();
}

bool CameraOcrClient::setCellText(int row,
                                  int column,
                                  const QString &text,
                                  QString *errorMessage)
{
    QJsonArray cells = tableCells();
    if (row < 0 || row >= cells.size()) {
        if (errorMessage)
            *errorMessage = QString::fromWCharArray(L"行号超出当前表格范围。");
        return false;
    }
    QJsonArray values = cells.at(row).toArray();
    if (column < 0 || column >= values.size()) {
        if (errorMessage)
            *errorMessage = QString::fromWCharArray(L"列号超出当前表格范围。");
        return false;
    }
    QJsonObject cell = values.at(column).toObject();
    cell.insert(QStringLiteral("text"), text);
    cell.insert(QStringLiteral("confidence"), 1.0);
    cell.insert(QStringLiteral("needs_review"), false);
    values.replace(column, cell);
    cells.replace(row, values);
    m_lastResponse.insert(QStringLiteral("cells"), cells);
    emit tableChanged(cells);
    return true;
}

bool CameraOcrClient::appendRow(const QJsonArray &values,
                                QString *errorMessage)
{
    QJsonArray cells = tableCells();
    const int columns = m_lastResponse.value(QStringLiteral("columns")).toInt();
    if (cells.isEmpty() || columns <= 0) {
        if (errorMessage)
            *errorMessage = QString::fromWCharArray(L"尚无可编辑的识别结果。");
        return false;
    }
    if (values.size() != columns) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"新增行必须包含 %1 个单元格。").arg(columns);
        }
        return false;
    }
    QJsonArray row;
    for (int column = 0; column < values.size(); ++column) {
        QJsonObject cell;
        if (values.at(column).isObject())
            cell = values.at(column).toObject();
        else
            cell.insert(QStringLiteral("text"), values.at(column).toVariant().toString());
        if (!cell.contains(QStringLiteral("text")))
            cell.insert(QStringLiteral("text"), QString());
        cell.insert(QStringLiteral("confidence"), 1.0);
        cell.insert(QStringLiteral("needs_review"), false);
        row.append(cell);
    }
    cells.append(row);
    m_lastResponse.insert(QStringLiteral("cells"), cells);
    m_lastResponse.insert(QStringLiteral("rows"), cells.size());
    emit tableChanged(cells);
    return true;
}

bool CameraOcrClient::exportLastCsv(const QString &outputPath,
                                    QString *errorMessage) const
{
    if (m_lastResponse.isEmpty()) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"\u5c1a\u65e0\u53ef\u5bfc\u51fa\u7684\u8bc6\u522b\u7ed3\u679c\u3002");
        }
        return false;
    }
    return OcrTableClient::exportCsv(outputPath,
                                     m_lastResponse.value(QStringLiteral("cells")).toArray(),
                                     errorMessage);
}

bool CameraOcrClient::canAutoPublish(QStringList *reasons) const
{
    return OcrTableClient::canAutoPublish(m_lastResponse, reasons);
}

void CameraOcrClient::captureAfterFocusLock()
{
    if (!m_capturePending || m_captureStarted)
        return;
    m_focusTimer->stop();
    if (!m_capture || !m_capture->isReadyForCapture()) {
        resetCaptureState();
        if (m_camera)
            m_camera->unlock();
        emitFailure(QStringLiteral("CAMERA_NOT_READY_AFTER_FOCUS"),
                    QString::fromWCharArray(L"\u5bf9\u7126\u5b8c\u6210\u540e\u6444\u50cf\u5934\u672a\u5c31\u7eea\uff0c\u8bf7\u4fdd\u6301\u8bbe\u5907\u7a33\u5b9a\u540e\u91cd\u8bd5\u3002"),
                    true);
        return;
    }
    m_captureStarted = true;
    emit stageChanged(QStringLiteral("capturing"),
                      QString::fromWCharArray(L"\u6b63\u5728\u4fdd\u5b58\u9ad8\u6e05\u7167\u7247\u3002"));
    m_capture->capture(m_capturePath);
}

void CameraOcrClient::focusLockFailed()
{
    if (!m_capturePending || m_captureStarted)
        return;
    m_focusTimer->stop();
    if (m_camera)
        m_camera->unlock();
    emit stageChanged(QStringLiteral("focus_fallback"),
                      QString::fromWCharArray(L"\u672a\u80fd\u9501\u5b9a\u5bf9\u7126\uff0c\u5c06\u4f7f\u7528\u5f53\u524d\u753b\u9762\u7ee7\u7eed\u62cd\u7167\u548c\u8bc6\u522b\u3002"));
    QTimer::singleShot(0, this, SLOT(captureAfterFocusLock()));
}

void CameraOcrClient::imageSaved(int, const QString &fileName)
{
    resetCaptureState();
    if (m_camera)
        m_camera->unlock();
    const QString capturedFilePath = normalizedCapturedFilePath(fileName);
    QImageReader reader(capturedFilePath);
    const QSize resolution = reader.size();
    const qint64 pixels = qint64(resolution.width()) * resolution.height();
    if (!resolution.isValid()) {
        emitFailure(QStringLiteral("CAMERA_RESOLUTION_TOO_LOW"),
                    QString::fromWCharArray(L"\u65e0\u6cd5\u8bfb\u53d6\u6444\u50cf\u5934\u4fdd\u5b58\u7684\u7167\u7247\u3002"),
                    true);
        return;
    }
    if (pixels < 7LL * 1000LL * 1000LL) {
        emit stageChanged(
            QStringLiteral("image_quality_warning"),
            QString::fromWCharArray(L"\u7167\u7247\u5206\u8fa8\u7387\u4e3a %1 \u00d7 %2\uff0c\u5c06\u7ee7\u7eed\u8bc6\u522b\uff0c\u4f4e\u7f6e\u4fe1\u5ea6\u5185\u5bb9\u4f1a\u4fdd\u7559\u98ce\u9669\u72b6\u6001\u3002")
                .arg(resolution.width()).arg(resolution.height()));
    }
    m_originalCapturePath = capturedFilePath;
    m_regionFallbackAttempted = false;
    QString recognitionPath = capturedFilePath;
    if (m_tableRegion.isValid()) {
        reader.setAutoTransform(true);
        const QImage capturedImage = reader.read();
        if (capturedImage.isNull()) {
            emitFailure(QStringLiteral("CAMERA_IMAGE_READ_FAILED"),
                        QString::fromWCharArray(L"无法读取摄像头照片，请重新拍摄。"),
                        true);
            return;
        }
        const QRect cropRect = resolvedTableRegion(capturedImage.size());
        const QImage tableImage = capturedImage.copy(
            cropRect.intersected(capturedImage.rect()));
        recognitionPath = QDir(m_requestDirectory).filePath(
            QStringLiteral("camera-table.png"));
        if (tableImage.isNull() || !tableImage.save(recognitionPath, "PNG")) {
            emitFailure(QStringLiteral("TABLE_REGION_SAVE_FAILED"),
                        QString::fromWCharArray(L"无法保存框选的表格区域，请重新框选后再试。"),
                        true);
            return;
        }
        emit stageChanged(QStringLiteral("table_region_cropped"),
                          QString::fromWCharArray(L"已按框选区域提取单个表格，正在识别。"));
    }
    m_capturePath = recognitionPath;
    emit captured(recognitionPath);
    emit stageChanged(
        QStringLiteral("captured"),
        QString::fromWCharArray(L"\u62cd\u7167\u5b8c\u6210\uff0c\u8bf7\u9009\u62e9\u8bc6\u522b\u6574\u56fe\u6216\u8bc6\u522b\u9009\u533a\u3002"));
}

void CameraOcrClient::captureReadyChanged(bool ready)
{
    m_cameraReady = ready;
    emit cameraReadyChanged(ready, m_cameraIndex, m_cameraDescription);
    if (ready) {
        emit stageChanged(QStringLiteral("camera_ready"),
                          QString::fromWCharArray(L"\u6444\u50cf\u5934\u5df2\u5c31\u7eea\u3002"));
    }
}

void CameraOcrClient::cameraError()
{
    m_cameraReady = false;
    emit cameraReadyChanged(false, m_cameraIndex, m_cameraDescription);
    emitFailure(QStringLiteral("CAMERA_OPEN_FAILED"),
                QString::fromWCharArray(L"\u6444\u50cf\u5934\u65e0\u6cd5\u6253\u5f00\uff0c\u8bf7\u68c0\u67e5\u7cfb\u7edf\u76f8\u673a\u6743\u9650\u3001\u9a71\u52a8\u6216\u5360\u7528\u72b6\u6001\u3002"),
                true);
}

void CameraOcrClient::captureError(int,
                                   QCameraImageCapture::Error,
                                   const QString &errorString)
{
    resetCaptureState();
    if (m_camera)
        m_camera->unlock();
    QString message = QString::fromWCharArray(
        L"\u62cd\u7167\u4fdd\u5b58\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\u3002");
    if (!errorString.trimmed().isEmpty())
        message += QString::fromWCharArray(L"\n\u9a71\u52a8\u4fe1\u606f：%1")
            .arg(errorString.trimmed());
    emitFailure(QStringLiteral("CAMERA_CAPTURE_FAILED"),
                message,
                true);
}

void CameraOcrClient::ocrRequestSucceeded(int,
                                          const QString &action,
                                          const QJsonObject &response)
{
    if (action != QStringLiteral("recognize"))
        return;
    m_ocrRetryCount = 0;
    m_ocrRetryPending = false;
    QJsonObject publishedResponse = response;
    if (m_regionFallbackAttempted)
        publishedResponse.insert(QStringLiteral("table_region_fallback"), true);
    m_lastResponse = publishedResponse;
    emit stageChanged(QStringLiteral("completed"),
                      QString::fromWCharArray(L"\u8868\u683c\u8bc6\u522b\u5b8c\u6210\u3002"));
    emit tableRecognized(publishedResponse.value(QStringLiteral("rows")).toInt(),
                         publishedResponse.value(QStringLiteral("columns")).toInt(),
                         publishedResponse.value(QStringLiteral("cells")).toArray(),
                         publishedResponse.value(QStringLiteral("spans")).toArray(),
                         publishedResponse);
}

void CameraOcrClient::ocrRequestFailed(int,
                                       const QString &action,
                                       const QString &errorCode,
                                       const QString &field,
                                       const QString &message,
                                       bool retryable)
{
    if (action != QStringLiteral("recognize"))
        return;
    Q_UNUSED(field);
    const bool insufficientMemory = errorCode == QStringLiteral("INSUFFICIENT_MEMORY");
    if (!insufficientMemory
        && retryable
        && m_ocrRetryCount < 1
        && QFileInfo::exists(m_capturePath)) {
        ++m_ocrRetryCount;
        m_ocrRetryPending = true;
        emit stageChanged(QStringLiteral("recognizing_retry"),
                          QString::fromWCharArray(L"\u8bc6\u522b\u7ec4\u4ef6\u672c\u6b21\u8fd0\u884c\u5f02\u5e38\uff0c\u6b63\u5728\u81ea\u52a8\u6062\u590d\u5e76\u91cd\u8bd5\u4e00\u6b21\u3002"));
        QTimer::singleShot(300, this, [this]() {
            if (!m_ocrRetryPending)
                return;
            m_ocrRetryPending = false;
            if (!m_ocr->isBusy())
                m_ocr->recognize(
                    m_capturePath,
                    m_requestDirectory,
                    false,
                    m_tableRegion.isValid()
                        && m_capturePath != m_originalCapturePath);
        });
        return;
    }
    if (!insufficientMemory
        && m_tableRegion.isValid()
        && !m_regionFallbackAttempted
        && !m_originalCapturePath.isEmpty()
        && QFileInfo::exists(m_originalCapturePath)
        && m_capturePath != m_originalCapturePath) {
        QImageReader reader(m_originalCapturePath);
        reader.setAutoTransform(true);
        const QImage originalImage = reader.read();
        if (!originalImage.isNull()) {
            const QRect cropRect = resolvedTableRegion(originalImage.size());
            const int extraX = qMax(8, qCeil(cropRect.width() * 0.04));
            const int extraY = qMax(8, qCeil(cropRect.height() * 0.06));
            const QRect expandedCropRect = cropRect.adjusted(
                -extraX, -extraY, extraX, extraY).intersected(originalImage.rect());
            const QImage expandedTable = originalImage.copy(expandedCropRect);
            const QString retryPath = QDir(m_requestDirectory).filePath(
                QStringLiteral("camera-table-retry.png"));
            if (!expandedTable.isNull() && expandedTable.save(retryPath, "PNG")) {
                m_regionFallbackAttempted = true;
                m_ocrRetryCount = 0;
                m_capturePath = retryPath;
                emit stageChanged(
                    QStringLiteral("table_region_fallback"),
                    QString::fromWCharArray(L"框选区域首次识别未形成可靠结果，正在扩大少量边缘后重试。"));
                QTimer::singleShot(300, this, [this]() {
                    if (!m_ocr->isBusy())
                        m_ocr->recognize(
                            m_capturePath,
                            m_requestDirectory,
                            false,
                            true);
                });
                return;
            }
        }
    }
    m_ocrRetryPending = false;
    if (insufficientMemory) {
        emitFailure(errorCode,
                    QString::fromWCharArray(L"设备可用内存不足，识别尚未启动；请释放内存后重试。"),
                    true);
        return;
    }
    const QString backendMessage = message.trimmed();
    const QString displayMessage = backendMessage.isEmpty()
        ? QString::fromWCharArray(L"表格识别失败，请根据错误码 %1 重试或联系维护人员。")
              .arg(errorCode)
        : QString::fromWCharArray(L"表格识别失败（%1）：%2")
              .arg(errorCode, backendMessage);
    emitFailure(errorCode,
                displayMessage,
                retryable);
}

void CameraOcrClient::cancel()
{
    m_ocrRetryPending = false;
    m_regionFallbackAttempted = false;
    m_focusTimer->stop();
    if (m_camera)
        m_camera->unlock();
    resetCaptureState();
    m_ocr->cancel();
    emit stageChanged(QStringLiteral("cancelled"),
                      QString::fromWCharArray(L"\u672c\u6b21\u62cd\u7167\u8bc6\u522b\u5df2\u53d6\u6d88\u3002"));
}

void CameraOcrClient::resetCaptureState()
{
    m_focusTimer->stop();
    m_capturePending = false;
    m_captureStarted = false;
}

void CameraOcrClient::emitFailure(const QString &errorCode,
                                  const QString &message,
                                  bool retryable)
{
    emit stageChanged(QStringLiteral("failed"), message);
    emit failed(errorCode, message, retryable);
}
