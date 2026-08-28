#include "imagepreview.h"

#include "guitrace.h"

#include <QPainter>
#include <QPaintEvent>
#include <QKeyEvent>
#include <QMouseEvent>
#include <QResizeEvent>
#include <QtMath>

namespace {

QJsonObject rectTrace(const QRect &rect)
{
    QJsonObject value;
    value.insert(QStringLiteral("x"), rect.x());
    value.insert(QStringLiteral("y"), rect.y());
    value.insert(QStringLiteral("width"), rect.width());
    value.insert(QStringLiteral("height"), rect.height());
    return value;
}

}

ImagePreview::ImagePreview(QWidget *parent)
    : QFrame(parent)
    , m_emptyText(QStringLiteral("打开一张手机拍摄的表格照片"))
    , m_cropSelectionActive(false)
    , m_cropDragging(false)
{
    setObjectName(QStringLiteral("ImageCanvas"));
    setMinimumSize(360, 320);
    setFocusPolicy(Qt::StrongFocus);
    setToolTip(QStringLiteral("单击查看大图，滚轮缩放，拖拽移动"));
}

void ImagePreview::setImage(const QImage &image)
{
    cancelCropSelection();
    m_image = image;
    setCursor(image.isNull() ? Qt::ArrowCursor : Qt::PointingHandCursor);
    rebuildScaledPreview();
    update();
}

void ImagePreview::clearImage()
{
    cancelCropSelection();
    m_image = QImage();
    m_scaledPreview = QPixmap();
    setCursor(Qt::ArrowCursor);
    update();
}

QImage ImagePreview::image() const
{
    return m_image;
}

void ImagePreview::beginCropSelection()
{
    if (m_image.isNull())
        return;
    if (m_cropSelectionActive)
        return;
    m_cropSelectionActive = true;
    m_cropDragging = false;
    m_cropSelection = QRect();
    setCursor(Qt::CrossCursor);
    setToolTip(QStringLiteral("按住鼠标左键拖动，框选一个完整表格"));
    emit cropSelectionActiveChanged(true);
    QJsonObject trace;
    trace.insert(QStringLiteral("image_width"), m_image.width());
    trace.insert(QStringLiteral("image_height"), m_image.height());
    trace.insert(QStringLiteral("image_dpr"), m_image.devicePixelRatio());
    trace.insert(QStringLiteral("widget_dpr"), devicePixelRatioF());
    trace.insert(QStringLiteral("widget"), rectTrace(rect()));
    trace.insert(QStringLiteral("display"), rectTrace(displayedImageRect()));
    const QPoint globalTopLeft = mapToGlobal(QPoint(0, 0));
    trace.insert(QStringLiteral("global_x"), globalTopLeft.x());
    trace.insert(QStringLiteral("global_y"), globalTopLeft.y());
    GuiTrace::write(QStringLiteral("crop_selection_started"), trace);
    update();
}

void ImagePreview::cancelCropSelection()
{
    const bool wasActive = m_cropSelectionActive;
    m_cropSelectionActive = false;
    m_cropDragging = false;
    m_cropSelection = QRect();
    setCursor(m_image.isNull() ? Qt::ArrowCursor : Qt::PointingHandCursor);
    setToolTip(QStringLiteral("单击查看大图，滚轮缩放，拖拽移动"));
    if (wasActive)
        emit cropSelectionActiveChanged(false);
    update();
}

bool ImagePreview::isCropSelectionActive() const
{
    return m_cropSelectionActive;
}

void ImagePreview::setEmptyText(const QString &text)
{
    m_emptyText = text;
    update();
}

void ImagePreview::paintEvent(QPaintEvent *event)
{
    QFrame::paintEvent(event);
    QPainter painter(this);
    painter.setRenderHint(QPainter::SmoothPixmapTransform, true);
    const QRect target = rect().adjusted(8, 8, -8, -8);
    if (m_image.isNull()) {
        painter.setPen(QColor(QStringLiteral("#7A8699")));
        painter.drawText(target, Qt::AlignCenter | Qt::TextWordWrap, m_emptyText);
        return;
    }
    if (m_scaledPreview.isNull())
        rebuildScaledPreview();
    const QPoint topLeft(target.center().x() - m_scaledPreview.width() / 2,
                         target.center().y() - m_scaledPreview.height() / 2);
    painter.drawPixmap(topLeft, m_scaledPreview);
    if (m_cropSelectionActive && !m_cropSelection.isEmpty()) {
        painter.setPen(QPen(QColor(QStringLiteral("#2F6BFF")), 2, Qt::DashLine));
        painter.setBrush(QColor(47, 107, 255, 42));
        painter.drawRect(m_cropSelection.normalized());
    }
}

void ImagePreview::resizeEvent(QResizeEvent *event)
{
    QFrame::resizeEvent(event);
    rebuildScaledPreview();
}

void ImagePreview::mousePressEvent(QMouseEvent *event)
{
    if (m_cropSelectionActive) {
        if (event->button() == Qt::LeftButton
            && displayedImageRect().contains(event->pos())) {
            m_cropDragging = true;
            m_cropStart = event->pos();
            m_cropSelection = QRect(m_cropStart, m_cropStart);
            update();
        }
        event->accept();
        return;
    }
    QFrame::mousePressEvent(event);
}

void ImagePreview::mouseMoveEvent(QMouseEvent *event)
{
    if (m_cropSelectionActive && m_cropDragging) {
        const QPoint bounded(
            qBound(displayedImageRect().left(), event->pos().x(), displayedImageRect().right()),
            qBound(displayedImageRect().top(), event->pos().y(), displayedImageRect().bottom()));
        m_cropSelection = QRect(m_cropStart, bounded).normalized();
        event->accept();
        update();
        return;
    }
    QFrame::mouseMoveEvent(event);
}

void ImagePreview::mouseReleaseEvent(QMouseEvent *event)
{
    if (m_cropSelectionActive) {
        if (m_cropDragging && event->button() == Qt::LeftButton) {
            m_cropDragging = false;
            const QRect display = displayedImageRect();
            const QRect selected = m_cropSelection.normalized().intersected(display);
            if (selected.width() >= 12 && selected.height() >= 12) {
                const double scaleX = double(m_image.width()) / double(qMax(1, display.width()));
                const double scaleY = double(m_image.height()) / double(qMax(1, display.height()));
                const QRect imageRect(
                    qBound(0, qFloor((selected.left() - display.left()) * scaleX), m_image.width() - 1),
                    qBound(0, qFloor((selected.top() - display.top()) * scaleY), m_image.height() - 1),
                    qMax(1, qCeil(selected.width() * scaleX)),
                    qMax(1, qCeil(selected.height() * scaleY)));
                const QRect mapped = imageRect.intersected(m_image.rect());
                QJsonObject trace;
                trace.insert(QStringLiteral("display"), rectTrace(display));
                trace.insert(QStringLiteral("selection"), rectTrace(selected));
                trace.insert(QStringLiteral("image_rect"), rectTrace(mapped));
                trace.insert(QStringLiteral("scale_x"), scaleX);
                trace.insert(QStringLiteral("scale_y"), scaleY);
                GuiTrace::write(QStringLiteral("crop_selection_mapped"), trace);
                cancelCropSelection();
                emit cropSelected(mapped);
                event->accept();
                return;
            }
            m_cropSelection = QRect();
            update();
        }
        event->accept();
        return;
    }
    QFrame::mouseReleaseEvent(event);
    if (event->button() == Qt::LeftButton && !m_image.isNull())
        emit activated();
}

void ImagePreview::keyPressEvent(QKeyEvent *event)
{
    if (m_cropSelectionActive) {
        if (event->key() == Qt::Key_Escape)
            cancelCropSelection();
        event->accept();
        return;
    }
    if (!m_image.isNull()
        && (event->key() == Qt::Key_Return
            || event->key() == Qt::Key_Enter
            || event->key() == Qt::Key_Space)) {
        emit activated();
        event->accept();
        return;
    }
    QFrame::keyPressEvent(event);
}

void ImagePreview::rebuildScaledPreview()
{
    const QSize targetSize = rect().adjusted(8, 8, -8, -8).size();
    if (m_image.isNull() || targetSize.isEmpty()) {
        m_scaledPreview = QPixmap();
        return;
    }
    // 框选坐标使用控件逻辑像素。先清除图片携带的设备像素比，避免高 DPI
    // 截图生成的 QPixmap 尺寸与鼠标坐标分属两套坐标系。
    QImage previewImage = m_image;
    previewImage.setDevicePixelRatio(1.0);
    m_scaledPreview = QPixmap::fromImage(
        previewImage.scaled(targetSize, Qt::KeepAspectRatio, Qt::SmoothTransformation));
    m_scaledPreview.setDevicePixelRatio(1.0);
}

QRect ImagePreview::displayedImageRect() const
{
    if (m_scaledPreview.isNull())
        return QRect();
    const QRect target = rect().adjusted(8, 8, -8, -8);
    return QRect(
        QPoint(target.center().x() - m_scaledPreview.width() / 2,
               target.center().y() - m_scaledPreview.height() / 2),
        m_scaledPreview.size());
}
