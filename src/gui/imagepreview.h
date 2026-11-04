#ifndef IMAGEPREVIEW_H
#define IMAGEPREVIEW_H

#include <QFrame>
#include <QImage>
#include <QPixmap>
#include <QPoint>
#include <QRect>
#include <QSize>

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

    // 控件绘制预览时使用的设备像素比（高 DPI 屏上为 2.0 之类）。
    qreal previewDevicePixelRatio() const;
    // 按给定设备像素比生成一张预览位图。控件自身走的正是这条路径
    // （参数取 previewDevicePixelRatio()），因此这里可以脱离高 DPI 屏幕做断言。
    QPixmap scaledPreviewForDevicePixelRatio(qreal devicePixelRatio) const;
    // 当前预览在**逻辑像素**下的尺寸，框选坐标用的就是这套坐标系。
    QSize previewLogicalSize() const;
    // 缩放源缓存被重建的次数：resize 只重采样，不该再深拷贝整张原图。
    int scaleSourceBuildCount() const;
    // 缩放源（已把设备像素比抹平成 1.0 的原图缓存）。
    const QImage &scaleSource() const;

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
    void rebuildScaleSource();
    void rebuildScaledPreview();
    QPixmap buildScaledPreview(qreal devicePixelRatio) const;
    void updatePreviewLogicalSize();
    QRect displayedImageRect() const;
    void startCropDrag(const QPoint &position);
    void updateCropDrag(const QPoint &position);
    bool finishCropDrag();

    QImage m_image;
    QImage m_scaleSource;
    QPixmap m_scaledPreview;
    QSize m_previewLogicalSize;
    int m_scaleSourceBuildCount;
    QString m_emptyText;
    bool m_cropSelectionActive;
    bool m_cropDragging;
    int m_cropTouchId;
    QPoint m_cropStart;
    QRect m_cropSelection;
};

#endif // IMAGEPREVIEW_H
