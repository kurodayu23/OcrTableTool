#ifndef MAINWINDOW_H
#define MAINWINDOW_H

#include <QJsonArray>
#include <QJsonObject>
#include <QElapsedTimer>
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

namespace OcrTableToolGui {

// 自动重试可用的剩余预算（秒）。用首个请求发出后的墙钟已用毫秒数算：
//   剩余秒 = floor((60000 - 已用毫秒 - 重试间隔) / 1000)，不足 0 记 0。
// 抽成自由函数（外部链接）是为了让 tests/tst_gui.cpp 能穷举断言
// 「已用 + 间隔 + 重试预算 ≤ 一次识别预算」这条不变量（findings B1）。
int remainingRecognitionBudgetSeconds(qint64 usedMilliseconds);

} // namespace OcrTableToolGui

class MainWindow : public QMainWindow
{
    Q_OBJECT

public:
    explicit MainWindow(QWidget *parent = 0);
    ~MainWindow();
    bool loadImageFile(const QString &path);

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
    void backendFailed(const QString &action, const QString &message,
                       const QString &errorCode, bool retryable);
    void tableItemChanged(QTableWidgetItem *item);
    void showOriginalImage();
    void showRectifiedImage();

private:
    void buildInterface();
    bool loadImage(const QString &path);
    void startBackgroundRecognition();
    // budgetSeconds：0 = 完整预算（与改动前一致）；
    //               >0 = 只用剩余预算（自动重试专用，见 backendFailed 的预算检查）。
    void startRecognitionRequest(int budgetSeconds = 0);
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
    // 2026-10-30（lead 复查）：用户**真正选的那张图**的路径。
    // 框选/拍照后再 loadImage() 会把 m_sourceImagePath 换成 AppData 里的
    // crop-<uuid>.png，导致「导出」的默认文件名落到临时目录的随机名上。
    // 这一份只记录用户可见的原图，供 suggestedOutputPath 使用。
    QString m_userVisibleImagePath;
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
    // findings/61 F5：停止窗口内点的「开始识别」原先被静默丢弃（还会被改写
    // 成"识别已取消"）。这里记住用户的点击意图，等停止真正完成后补跑一次。
    bool m_startRequestedWhileStopping;
    // findings B1：一次识别的墙钟预算从首个请求发出时开始计，自动重试只能用**剩余**
    // 预算，不允许再开一轮完整时长（原先两轮冷启 ⇒ ~120 秒）。
    QElapsedTimer m_recognitionBudgetTimer;
    // findings/61 F6：识别时严格校验过的表格结构证书。校验通过后必须留到导出，
    // 否则后端 write_xlsx 的列宽/边界校准分支永远拿不到它。
    QJsonObject m_structureCertificate;
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
