#ifndef IMAGEVIEWERDIALOG_H
#define IMAGEVIEWERDIALOG_H

#include <QDialog>
#include <QImage>

class ImageViewerView;
class QShowEvent;

class ImageViewerDialog : public QDialog
{
    Q_OBJECT

public:
    explicit ImageViewerDialog(const QImage &image,
                               const QString &title,
                               QWidget *parent = 0);

protected:
    void showEvent(QShowEvent *event) override;

private slots:
    void fitImage();
    void showActualSize();

private:
    ImageViewerView *m_view;
};

#endif // IMAGEVIEWERDIALOG_H
