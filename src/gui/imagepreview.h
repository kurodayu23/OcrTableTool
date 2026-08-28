#ifndef IMAGEPREVIEW_H
#define IMAGEPREVIEW_H

#include <QFrame>
#include <QImage>
#include <QPixmap>
#include <QPoint>
#include <QRect>

class QResizeEvent;
class QKeyEvent;
class QMouseEvent;
class QEvent;

class ImagePreview : public QFrame
{
    Q_OBJECT

public:
    explicit ImagePreview(QWidget *parent = 0);

    void setImage(const QImage &image);
    void clearImage();
    void setEmptyText(const QString &text);
    QImage image() const;
    void beginCropSelection();
    void cancelCropSelection();
    bool isCropSelectionActive() const;

signals:
    void activated();
    void cropSelected(const QRect &imageRect);
    void cropSelectionActiveChanged(bool active);

protected:
    bool event(QEvent *event) override;
    void paintEvent(QPaintEvent *event) override;
    void resizeEvent(QResizeEvent *event) override;
    void mousePressEvent(QMouseEvent *event) override;
    void mouseMoveEvent(QMouseEvent *event) override;
    void mouseReleaseEvent(QMouseEvent *event) override;
    void keyPressEvent(QKeyEvent *event) override;

private:
    void rebuildScaledPreview();
    QRect displayedImageRect() const;
    void startCropDrag(const QPoint &position);
    void updateCropDrag(const QPoint &position);
    bool finishCropDrag();

    QImage m_image;
    QPixmap m_scaledPreview;
    QString m_emptyText;
    bool m_cropSelectionActive;
    bool m_cropDragging;
    int m_cropTouchId;
    QPoint m_cropStart;
    QRect m_cropSelection;
};

#endif // IMAGEPREVIEW_H
