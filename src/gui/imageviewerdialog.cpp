#include "imageviewerdialog.h"

#include <QApplication>
#include <QDesktopWidget>
#include <QEvent>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QKeyEvent>
#include <QLineF>
#include <QMouseEvent>
#include <QPainter>
#include <QResizeEvent>
#include <QScrollBar>
#include <QShowEvent>
#include <QTouchEvent>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <QtMath>

class ImageViewerView : public QGraphicsView
{
public:
    explicit ImageViewerView(QWidget *parent = 0)
        : QGraphicsView(parent)
        , m_scene(new QGraphicsScene(this))
        , m_pixmapItem(0)
        , m_fitMode(true)
        , m_touchActive(false)
        , m_lastTouchDistance(0.0)
    {
        setScene(m_scene);
        setFrameShape(QFrame::NoFrame);
        setBackgroundBrush(QColor(QStringLiteral("#101318")));
        setDragMode(QGraphicsView::ScrollHandDrag);
        setTransformationAnchor(QGraphicsView::AnchorUnderMouse);
        setResizeAnchor(QGraphicsView::AnchorViewCenter);
        setRenderHint(QPainter::SmoothPixmapTransform, true);
        setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
        setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
        viewport()->setAttribute(Qt::WA_AcceptTouchEvents, true);
    }

    void setImage(const QImage &image)
    {
        m_scene->clear();
        m_pixmapItem = m_scene->addPixmap(QPixmap::fromImage(image));
        m_scene->setSceneRect(m_pixmapItem->boundingRect());
        m_fitMode = true;
    }

    void fitImage()
    {
        if (!m_pixmapItem)
            return;
        resetTransform();
        fitInView(m_pixmapItem, Qt::KeepAspectRatio);
        m_fitMode = true;
    }

    void showActualSize()
    {
        resetTransform();
        centerOn(m_pixmapItem);
        m_fitMode = false;
    }

protected:
    bool viewportEvent(QEvent *event) override
    {
        if (m_pixmapItem
            && (event->type() == QEvent::TouchBegin
                || event->type() == QEvent::TouchUpdate
                || event->type() == QEvent::TouchEnd
                || event->type() == QEvent::TouchCancel)) {
            QTouchEvent *touchEvent = static_cast<QTouchEvent *>(event);
            const QList<QTouchEvent::TouchPoint> points = touchEvent->touchPoints();
            if (event->type() == QEvent::TouchBegin && !points.isEmpty()) {
                m_touchActive = true;
                m_fitMode = false;
                m_lastTouchCenter = touchCenter(points);
                m_lastTouchDistance = touchDistance(points);
            } else if (event->type() == QEvent::TouchUpdate
                       && m_touchActive
                       && !points.isEmpty()) {
                const QPointF center = touchCenter(points);
                const QPointF movement = center - m_lastTouchCenter;
                horizontalScrollBar()->setValue(
                    horizontalScrollBar()->value() - qRound(movement.x() * 1.15));
                verticalScrollBar()->setValue(
                    verticalScrollBar()->value() - qRound(movement.y() * 1.15));

                if (touchEvent->touchPoints().size() >= 2) {
                    const qreal distance = touchDistance(points);
                    if (m_lastTouchDistance > 1.0 && distance > 1.0) {
                        const qreal currentScale = transform().m11();
                        qreal factor = qPow(distance / m_lastTouchDistance, 1.18);
                        const qreal targetScale = currentScale * factor;
                        if (targetScale < 0.08)
                            factor = 0.08 / currentScale;
                        else if (targetScale > 16.0)
                            factor = 16.0 / currentScale;
                        const QPointF sceneAnchor = mapToScene(center.toPoint());
                        scale(factor, factor);
                        const QPointF shiftedAnchor = mapFromScene(sceneAnchor);
                        horizontalScrollBar()->setValue(
                            horizontalScrollBar()->value()
                            + qRound(shiftedAnchor.x() - center.x()));
                        verticalScrollBar()->setValue(
                            verticalScrollBar()->value()
                            + qRound(shiftedAnchor.y() - center.y()));
                    }
                    m_lastTouchDistance = distance;
                } else {
                    m_lastTouchDistance = 0.0;
                }
                m_lastTouchCenter = center;
            } else if (event->type() == QEvent::TouchEnd
                       || event->type() == QEvent::TouchCancel) {
                m_touchActive = false;
                m_lastTouchDistance = 0.0;
            }
            touchEvent->accept();
            return true;
        }
        return QGraphicsView::viewportEvent(event);
    }

    void wheelEvent(QWheelEvent *event) override
    {
        if (!m_pixmapItem || event->angleDelta().y() == 0) {
            QGraphicsView::wheelEvent(event);
            return;
        }
        const qreal currentScale = transform().m11();
        const qreal factor = event->angleDelta().y() > 0 ? 1.18 : 1.0 / 1.18;
        const qreal nextScale = currentScale * factor;
        if (nextScale >= 0.08 && nextScale <= 16.0)
            scale(factor, factor);
        m_fitMode = false;
        event->accept();
    }

    void mouseDoubleClickEvent(QMouseEvent *event) override
    {
        if (m_fitMode)
            showActualSize();
        else
            fitImage();
        event->accept();
    }

    void resizeEvent(QResizeEvent *event) override
    {
        QGraphicsView::resizeEvent(event);
        if (m_fitMode)
            fitImage();
    }

private:
    static QPointF touchCenter(const QList<QTouchEvent::TouchPoint> &points)
    {
        if (points.size() < 2)
            return points.first().pos();
        return (points.at(0).pos() + points.at(1).pos()) / 2.0;
    }

    static qreal touchDistance(const QList<QTouchEvent::TouchPoint> &points)
    {
        return points.size() >= 2
            ? QLineF(points.at(0).pos(), points.at(1).pos()).length()
            : 0.0;
    }

    QGraphicsScene *m_scene;
    QGraphicsPixmapItem *m_pixmapItem;
    bool m_fitMode;
    bool m_touchActive;
    QPointF m_lastTouchCenter;
    qreal m_lastTouchDistance;
};

ImageViewerDialog::ImageViewerDialog(const QImage &image,
                                     const QString &title,
                                     QWidget *parent)
    : QDialog(parent)
    , m_view(new ImageViewerView(this))
{
    Q_UNUSED(title);
    setWindowTitle(QString());
    setModal(true);
    setWindowFlags(windowFlags() | Qt::WindowMaximizeButtonHint);
    const QRect available = QApplication::desktop()->availableGeometry(parent);
    const int usableWidth = qMax(1, available.width() - 32);
    const int usableHeight = qMax(1, available.height() - 32);
    const int preferredWidth = qMin(usableWidth, qMin(1100, qMax(520, available.width() * 4 / 5)));
    const int preferredHeight = qMin(usableHeight, qMin(720, qMax(320, available.height() * 4 / 5)));
    setMinimumSize(qMin(520, usableWidth), qMin(320, usableHeight));
    resize(preferredWidth, preferredHeight);
    setStyleSheet(QStringLiteral(
        "QDialog { background: #101318; color: #F8FAFC; }"));

    QVBoxLayout *layout = new QVBoxLayout(this);
    layout->setContentsMargins(0, 0, 0, 0);
    layout->setSpacing(0);
    layout->addWidget(m_view, 1);
    m_view->setImage(image);
}

void ImageViewerDialog::showEvent(QShowEvent *event)
{
    QDialog::showEvent(event);
    const QRect available = QApplication::desktop()->availableGeometry(parentWidget());
    const QPoint centered = parentWidget()
        ? parentWidget()->frameGeometry().center() - rect().center()
        : available.center() - rect().center();
    const int maximumX = available.right() - width() + 1;
    const int maximumY = available.bottom() - height() + 1;
    move(qBound(available.left(), centered.x(), maximumX),
         qBound(available.top(), centered.y(), maximumY));
    fitImage();
}

void ImageViewerDialog::fitImage()
{
    m_view->fitImage();
}

void ImageViewerDialog::showActualSize()
{
    m_view->showActualSize();
}
