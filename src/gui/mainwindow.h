#ifndef MAINWINDOW_H
#define MAINWINDOW_H

#include <QJsonArray>
#include <QJsonObject>
#include <QMainWindow>
#include <QPoint>
#include <QRect>

class BackendRunner;
class ImagePreview;
class QImage;
class QLabel;
class QPushButton;
class QProgressBar;
class QDragEnterEvent;
class QDropEvent;
class QStackedWidget;
class QTableWidget;
class QTableWidgetItem;
class QTabWidget;
class TableData;

class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget *parent = 0);
    ~MainWindow();

protected:
    void dragEnterEvent(QDragEnterEvent *event) override;
    void dropEvent(QDropEvent *event) override;
    bool eventFilter(QObject *watched, QEvent *event) override;

private slots:
    void openImage();
    void selectTableRegion();
    void applyTableCrop(const QRect &imageRect);
    void cropSelectionActiveChanged(bool active);
    void takePhoto();
    void recognizeImage();
    void exportCsv();
    void exportXlsx();
    void editTableCell(int row, int column);
    void addRow();
    void removeRow();
    void addColumn();
    void removeColumn();
    void backendStarted(const QString &action);
    void backendSucceeded(const QString &action, const QJsonObject &response);
    void backendFailed(const QString &action, const QString &message);
    void tableItemChanged(QTableWidgetItem *item);
    void showOriginalImage();
    void showRectifiedImage();

private:
    void buildInterface();
    bool loadImage(const QString &path);
    void startBackgroundRecognition();
    void startRecognitionRequest();
    void publishRecognitionResult(const QJsonObject &response);
    void clearPublishedResult(const QString &summary = QString());
    QPushButton *createButton(const QString &text, const QString &objectName = QString());
    void setBusy(bool busy, const QString &message = QString());
    void updateActions();
    void showTable(const TableData &table, const QJsonArray &spans);
    TableData currentTable() const;
    QString suggestedOutputPath(const QString &suffix) const;
    void clearSpansAfterStructureEdit();
    int pendingReviewCount() const;
    void showTouchKeyboard();
    void showImageViewer(const QImage &image, const QString &title);
    void clearOwnedRectifiedImage();
    void clearOwnedCroppedImage();
    void clearStaleRectifiedImages();

    BackendRunner *m_backend;
    QString m_imagePath;
    QString m_sourceImagePath;
    QString m_ownedRectifiedImagePath;
    QString m_ownedCroppedImagePath;
    // 后端生成的 rectified-*.png 再次导入时禁止重复自动裁剪。
    bool m_recognitionSourceIsRectified;
    QJsonArray m_spans;
    // 后台识别可能先于用户的显示请求完成，因此暂存成功或失败结果，
    // 等用户明确要求显示时再发布，避免后台结果突然覆盖当前界面。
    QJsonObject m_pendingRecognitionResponse;
    QString m_pendingRecognitionError;
    bool m_loadingTable;
    bool m_touchTracking;
    // 这些标志共同组成界面侧识别状态机：后台结果只在用户请求后显示；
    // 更换图片会取消旧任务，并且必须等 BackendRunner 完成异步停止后才能重启。
    bool m_recognitionDisplayRequested;
    bool m_recognitionActive;
    bool m_restartRecognitionAfterCancel;
    int m_recognitionRetryCount;
    // 该状态独立于单元格 needsReview，用于记录后端给出的整表级安全结论，
    // 并在导出确认阶段持续提示风险。
    bool m_publicationBlocked;
    QPoint m_touchStartPosition;

    QLabel *m_fileNameLabel;
    QLabel *m_readyStateLabel;
    QLabel *m_resultSummaryLabel;
    QLabel *m_reviewNotice;
    QLabel *m_statusLabel;
    ImagePreview *m_originalPreview;
    ImagePreview *m_rectifiedPreview;
    QTabWidget *m_previewTabs;
    QStackedWidget *m_previewStack;
    QStackedWidget *m_resultStack;
    QTableWidget *m_table;
    QProgressBar *m_progress;
    QPushButton *m_openButton;
    QPushButton *m_cropButton;
    QPushButton *m_cameraButton;
    QPushButton *m_recognizeButton;
    QPushButton *m_cancelButton;
    QPushButton *m_csvButton;
    QPushButton *m_xlsxButton;
};

#endif // MAINWINDOW_H
