#include "mainwindow.h"

#include "backendrunner.h"
#include "cameracapturedialog.h"
#include "guitrace.h"
#include "csvexporter.h"
#include "imagepreview.h"
#include "imageviewerdialog.h"
#include "tabledata.h"

#include <QBrush>
#include <QColor>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QDragEnterEvent>
#include <QDropEvent>
#include <QEvent>
#include <QFileDialog>
#include <QFile>
#include <QFileInfo>
#include <QFontMetrics>
#include <QGuiApplication>
#include <QHeaderView>
#include <QInputMethod>
#include <QHBoxLayout>
#include <QIcon>
#include <QImageReader>
#include <QJsonObject>
#include <QJsonDocument>
#include <QLabel>
#include <QMenu>
#include <QMimeData>
#include <QPainter>
#include <QPixmap>
#include <QProgressBar>
#include <QProcess>
#include <QPushButton>
#include <QApplication>
#include <QScreen>
#include <QSignalBlocker>
#include <QScrollBar>
#include <QScroller>
#include <QScrollerProperties>
#include <QSet>
#include <QStandardPaths>
#include <QStringList>
#include <QSplitter>
#include <QStackedWidget>
#include <QStyle>
#include <QTableWidget>
#include <QTabWidget>
#include <QTimer>
#include <QtMath>
#include <QTouchEvent>
#include <QTouchDevice>
#include <QToolButton>
#include <QUuid>
#include <QUrl>
#include <QVBoxLayout>

namespace
{
const QIcon &reviewMarkerIcon()
{
    static const QIcon icon = []() {
        QPixmap marker(12, 12);
        marker.fill(Qt::transparent);
        QPainter painter(&marker);
        painter.setRenderHint(QPainter::Antialiasing, true);
        painter.setPen(QColor(QStringLiteral("#C88900")));
        painter.setBrush(QColor(QStringLiteral("#F7C948")));
        painter.drawEllipse(1, 1, 9, 9);
        return QIcon(marker);
    }();
    return icon;
}

QString canonicalDisplayedResultHash(const TableData &table,
                                     const QJsonArray &spans,
                                     bool publicationBlocked)
{
    QJsonArray rows;
    for (int row = 0; row < table.rowCount(); ++row) {
        QJsonArray cells;
        for (int column = 0; column < table.columnCount(); ++column) {
            QJsonObject cell;
            cell.insert(QStringLiteral("text"), table.cell(row, column));
            cell.insert(QStringLiteral("needs_review"), table.needsReview(row, column));
            cells.append(cell);
        }
        rows.append(cells);
    }
    QJsonObject payload;
    payload.insert(QStringLiteral("rows"), table.rowCount());
    payload.insert(QStringLiteral("columns"), table.columnCount());
    payload.insert(QStringLiteral("cells"), rows);
    payload.insert(QStringLiteral("spans"), spans);
    payload.insert(QStringLiteral("publication_blocked"), publicationBlocked);
    const QByteArray canonical = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    return QString::fromLatin1(
        QCryptographicHash::hash(canonical, QCryptographicHash::Sha256).toHex());
}

QString localizedBackendError(const QString &message)
{
    if (message.contains(QStringLiteral("严重模糊")))
        return QStringLiteral("照片严重模糊，文字笔画已经丢失，请保持设备稳定、重新对焦后拍摄。");
    if (message.contains(QStringLiteral("边缘贴近照片边界")))
        return QStringLiteral("表格没有完整进入画面，可能已有行列被裁掉，请留出纸张四周边缘后重新拍摄。");
    if (message.contains(QStringLiteral("未检测到可识别")))
        return QStringLiteral("识别流程已正常结束，但没有找到能够可靠对应的完整行列。为避免生成错位表格，软件没有输出结果；这不是程序崩溃。请裁剪到表格区域并保留原始分辨率后重试。");
    if (message.contains(QStringLiteral("首行或左侧字段"))
        || message.contains(QStringLiteral("网格与文字空间结构"))) {
        return QStringLiteral("识别流程已正常结束，但表头、左侧关键字段与网格结构不能互相确认。为避免行列错位，软件主动停止生成结果。请改善对焦、光线或靠近后重试。");
    }
    if (message.contains(QStringLiteral("多级表头")))
        return QStringLiteral("无法可靠确认多级表头的合并关系。请裁剪到完整表格并提高文字清晰度后重试。");
    if (message.contains(QStringLiteral("强反光或模糊")))
        return QStringLiteral("反光或模糊导致相邻表头粘连。请调整拍摄角度、光线并重新对焦后重试。");
    if (message.contains(QStringLiteral("远距离或透视")))
        return QStringLiteral("拍摄距离或透视角度导致相邻记录错行。请正对表格、靠近后重新拍摄。");
    if (message.contains(QStringLiteral("多个物理列被融合")))
        return QStringLiteral("识别流程已完成，但检测到原图中的多个列落进了同一个结果列。为避免导出错位数据，软件已主动停止生成；这不是程序故障。请保留原始分辨率、裁剪到完整表格后重试。");
    if (message.contains(QStringLiteral("多个物理行被融合")))
        return QStringLiteral("识别流程已完成，但检测到原图中的多行记录落进了同一个结果行。为避免导出错位数据，软件已主动停止生成；这不是程序故障。请保留原始分辨率、裁剪到完整表格后重试。");
    if (message.contains(QStringLiteral("发生错位")))
        return QStringLiteral("识别流程已完成，但序号、月份或编号与同行数据没有对齐。为避免把数据放到错误行，软件已主动停止生成；这不是程序故障。请正对表格、靠近并重新拍摄。");
    if (message.contains(QStringLiteral("表头列可能已合并")))
        return QStringLiteral("检测到表格列边界过浅，已阻止输出可能融合的结果。截图请保留原始分辨率并裁剪到表格区域后重试。");
    if (message.contains(QStringLiteral("未纳入网格"))
        || message.contains(QStringLiteral("扩展矫正后仍无法保持"))) {
        return QStringLiteral("识别流程已完成，但检测到表格一侧仍有列没有完整进入结果。为避免整列错位，软件主动没有生成表格；这不是程序故障。请让表格左右边缘完整进入画面，保留原始分辨率后重试。");
    }
    if (message.contains(QStringLiteral("有可见内容但模型无法安全确认")))
        return QStringLiteral("识别流程已完成，并检测到一个或多个单元格中确实有可见内容，但多个模型无法一致确认具体文字。为避免漏格或猜错内容，软件主动没有生成可导出表格；这不是程序故障。请靠近表格、重新对焦或改善光线后重试。");
    if (message.contains(QStringLiteral("融合")))
        return QStringLiteral("识别流程已完成，但相邻行列无法可靠分开。为避免把两行或两列拼到一起，软件主动没有生成表格；这不是程序故障。请保留原始分辨率、正对并靠近表格后重试。");
    if (message.contains(QStringLiteral("另一个任务正在运行"))
        || message.contains(QStringLiteral("正在停止"))) {
        return QStringLiteral("识别组件仍在处理上一项任务，请稍候再试。");
    }
    if (message.contains(QStringLiteral("操作超时"))
        || message.contains(QStringLiteral("timeout"), Qt::CaseInsensitive)
        || message.contains(QStringLiteral("timed out"), Qt::CaseInsensitive)) {
        return QStringLiteral("识别组件响应超时，请重试；若仍失败，请关闭其他正在运行的版本后重启软件。");
    }
    if (message.contains(QStringLiteral("无法启动识别组件"))
        || message.contains(QStringLiteral("无法向识别组件发送任务"))
        || message.contains(QStringLiteral("没有返回有效结果"))
        || message.contains(QStringLiteral("识别组件意外退出"))
        || message.contains(QStringLiteral("crash"), Qt::CaseInsensitive)
        || message.contains(QStringLiteral("process"), Qt::CaseInsensitive)) {
        return QStringLiteral("识别组件本次运行异常。请再次点击开始识别；若仍失败，请关闭其他正在运行的版本后重启软件。");
    }
    if (message.contains(QStringLiteral("无法可靠"))
        || message.contains(QStringLiteral("跨模型冲突"))
        || message.contains(QStringLiteral("30秒"))) {
        return QStringLiteral("识别流程已正常结束，但不同模型或结构结果不一致。为避免输出错位或拼错列的表格，软件主动没有生成结果；这不是程序崩溃。截图请裁剪到表格区域并保留原始分辨率，照片请改善光线、对焦或靠近后重试。");
    }
    if (message.contains(QStringLiteral("model"), Qt::CaseInsensitive)
        || message.contains(QStringLiteral("模型"))) {
        return QStringLiteral("识别模型文件缺失或损坏，请重新配置识别组件。");
    }
    if (message.contains(QStringLiteral("No module named"), Qt::CaseInsensitive))
        return QStringLiteral("识别组件依赖不完整，请重新配置识别组件。");
    if (message.contains(QStringLiteral("image"), Qt::CaseInsensitive)
        || message.contains(QStringLiteral("图片"))) {
        return QStringLiteral("无法读取图片，请检查图片格式或文件是否损坏。");
    }
    return QStringLiteral("识别失败，已保留原始错误信息。请展开“详细信息”查看后重试。");
}

QString localizedBackendDetail(const QString &technicalMessage,
                               const QString &displayMessage)
{
    bool containsChinese = false;
    bool containsLatinLetter = false;
    for (const QChar character : technicalMessage) {
        const ushort value = character.unicode();
        if (value >= 0x3400 && value <= 0x9fff)
            containsChinese = true;
        if ((value >= 'A' && value <= 'Z') || (value >= 'a' && value <= 'z'))
            containsLatinLetter = true;
    }
    if (containsChinese && !containsLatinLetter)
        return technicalMessage;
    return displayMessage;
}

bool validateSpans(const QJsonValue &value,
                   const TableData &table,
                   QJsonArray *validated,
                   QString *errorMessage)
{
    const int rowCount = table.rowCount();
    const int columnCount = table.columnCount();
    if (!value.isArray()) {
        if (errorMessage)
            *errorMessage = QStringLiteral("识别结果缺少合并单元格信息。");
        return false;
    }

    const QJsonArray spans = value.toArray();
    QSet<qint64> occupiedCells;
    for (int index = 0; index < spans.size(); ++index) {
        if (!spans.at(index).isObject()) {
            if (errorMessage)
                *errorMessage = QStringLiteral("识别结果包含无效的合并单元格。");
            return false;
        }
        const QJsonObject span = spans.at(index).toObject();
        const QJsonValue rowValue = span.value(QStringLiteral("row"));
        const QJsonValue columnValue = span.value(QStringLiteral("column"));
        const QJsonValue rowSpanValue = span.value(QStringLiteral("row_span"));
        const QJsonValue columnSpanValue = span.value(QStringLiteral("column_span"));
        if (!rowValue.isDouble()
            || !columnValue.isDouble()
            || (!rowSpanValue.isUndefined() && !rowSpanValue.isDouble())
            || (!columnSpanValue.isUndefined() && !columnSpanValue.isDouble())) {
            if (errorMessage)
                *errorMessage = QStringLiteral("识别结果中的合并单元格坐标格式无效。");
            return false;
        }
        const int row = rowValue.toInt(-1);
        const int column = columnValue.toInt(-1);
        const int rowSpan = rowSpanValue.toInt(1);
        const int columnSpan = columnSpanValue.toInt(1);
        if (rowValue.toDouble() != row
            || columnValue.toDouble() != column
            || (!rowSpanValue.isUndefined() && rowSpanValue.toDouble() != rowSpan)
            || (!columnSpanValue.isUndefined() && columnSpanValue.toDouble() != columnSpan)
            || row < 0
            || column < 0
            || rowSpan <= 0
            || columnSpan <= 0
            || row >= rowCount
            || column >= columnCount
            || rowSpan > rowCount - row
            || columnSpan > columnCount - column) {
            if (errorMessage)
                *errorMessage = QStringLiteral("识别结果中的合并单元格超出表格范围。");
            return false;
        }
        for (int spanRow = row; spanRow < row + rowSpan; ++spanRow) {
            for (int spanColumn = column; spanColumn < column + columnSpan; ++spanColumn) {
                const qint64 cellIndex = qint64(spanRow) * columnCount + spanColumn;
                if (occupiedCells.contains(cellIndex)) {
                    if (errorMessage)
                        *errorMessage = QStringLiteral("识别结果中的合并单元格互相重叠。");
                    return false;
                }
                if ((spanRow != row || spanColumn != column)
                    && !table.cell(spanRow, spanColumn).trimmed().isEmpty()) {
                    if (errorMessage)
                        *errorMessage = QStringLiteral("合并单元格会遮挡已有文字，已停止显示该结果。");
                    return false;
                }
                occupiedCells.insert(cellIndex);
            }
        }
    }
    if (validated)
        *validated = spans;
    if (errorMessage)
        errorMessage->clear();
    return true;
}
}

MainWindow::MainWindow(QWidget *parent)
    : QMainWindow(parent)
    , m_backend(new BackendRunner(this))
    , m_recognitionSourceIsRectified(false)
    , m_loadingTable(false)
    , m_touchTracking(false)
    , m_recognitionDisplayRequested(false)
    , m_recognitionActive(false)
    , m_restartRecognitionAfterCancel(false)
    , m_recognitionRetryCount(0)
    , m_publicationBlocked(false)
{
    clearStaleRectifiedImages();
    buildInterface();
    connect(m_backend, &BackendRunner::requestStarted, this, &MainWindow::backendStarted);
    connect(m_backend, &BackendRunner::requestSucceeded, this, &MainWindow::backendSucceeded);
    connect(m_backend, &BackendRunner::requestFailed, this, &MainWindow::backendFailed);
    m_readyStateLabel->setText(QStringLiteral("正在准备"));
    updateActions();
}

MainWindow::~MainWindow()
{
    clearOwnedRectifiedImage();
    clearOwnedCroppedImage();
}

bool MainWindow::loadImageFile(const QString &path)
{
    return loadImage(path);
}

QPushButton *MainWindow::createButton(const QString &text, const QString &objectName)
{
    QPushButton *button = new QPushButton(text, this);
    if (!objectName.isEmpty())
        button->setObjectName(objectName);
    button->setMinimumHeight(48);
    return button;
}

void MainWindow::buildInterface()
{
    setWindowTitle(QStringLiteral("图片转表格"));
    setWindowIcon(QIcon(QStringLiteral(":/icons/image-add.svg")));
    const QRect availableGeometry = QApplication::primaryScreen()->availableGeometry();
    resize(qMin(1180, qMax(760, availableGeometry.width() - 20)),
           qMin(700, qMax(460, availableGeometry.height() - 20)));
    setMinimumSize(760, 460);
    setAcceptDrops(true);

    QWidget *central = new QWidget(this);
    QVBoxLayout *root = new QVBoxLayout(central);
    root->setContentsMargins(14, 12, 14, 10);
    root->setSpacing(10);

    QWidget *actions = new QWidget(central);
    actions->setObjectName(QStringLiteral("Toolbar"));
    QHBoxLayout *actionLayout = new QHBoxLayout(actions);
    actionLayout->setContentsMargins(0, 0, 0, 0);
    actionLayout->setSpacing(12);

    m_openButton = createButton(QStringLiteral("打开图片"));
    m_cropButton = createButton(QStringLiteral("框选表格"));
    m_cameraButton = createButton(QStringLiteral("拍照"));
    m_recognizeButton = createButton(QStringLiteral("开始识别"), QStringLiteral("PrimaryButton"));
    m_cancelButton = createButton(QStringLiteral("取消"));
    m_cancelButton->hide();
    m_csvButton = createButton(QStringLiteral("导出 CSV"));
    m_xlsxButton = createButton(QStringLiteral("导出 XLSX"));
    m_openButton->setIcon(style()->standardIcon(QStyle::SP_DialogOpenButton));
    m_cropButton->setIcon(style()->standardIcon(QStyle::SP_DialogResetButton));
    m_cropButton->setToolTip(QStringLiteral("一张图包含多个表格时，拖动框选一个目标表"));
    m_cameraButton->setIcon(style()->standardIcon(QStyle::SP_FileDialogContentsView));
    m_recognizeButton->setIcon(QIcon(QStringLiteral(":/icons/table-empty.svg")));
    m_cancelButton->setIcon(style()->standardIcon(QStyle::SP_BrowserStop));
    m_csvButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    m_xlsxButton->setIcon(style()->standardIcon(QStyle::SP_DialogSaveButton));
    m_openButton->setFixedWidth(110);
    m_cropButton->setFixedWidth(110);
    m_cameraButton->setFixedWidth(110);
    m_recognizeButton->setFixedWidth(124);
    m_cancelButton->setFixedWidth(124);
    m_csvButton->setFixedWidth(110);
    m_xlsxButton->setFixedWidth(110);
    actionLayout->addWidget(m_openButton);
    actionLayout->addWidget(m_cropButton);
    actionLayout->addWidget(m_recognizeButton);
    actionLayout->addWidget(m_cameraButton);
    actionLayout->addWidget(m_cancelButton);
    actionLayout->addStretch();
    actionLayout->addWidget(m_csvButton);
    actionLayout->addWidget(m_xlsxButton);
    root->addWidget(actions);

    QSplitter *splitter = new QSplitter(Qt::Horizontal, central);
    splitter->setChildrenCollapsible(false);
    splitter->setHandleWidth(10);

    QWidget *previewPanel = new QWidget(splitter);
    previewPanel->setObjectName(QStringLiteral("PreviewPanel"));
    QVBoxLayout *previewLayout = new QVBoxLayout(previewPanel);
    previewLayout->setContentsMargins(14, 12, 14, 14);
    previewLayout->setSpacing(10);
    QHBoxLayout *previewHeader = new QHBoxLayout;
    QLabel *previewTitle = new QLabel(QStringLiteral("图像预览"), previewPanel);
    previewTitle->setObjectName(QStringLiteral("SectionTitle"));
    m_fileNameLabel = new QLabel(QStringLiteral("尚未打开图片"), previewPanel);
    m_fileNameLabel->setObjectName(QStringLiteral("MutedLabel"));
    m_fileNameLabel->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    previewHeader->addWidget(previewTitle);
    previewHeader->addStretch();
    previewHeader->addWidget(m_fileNameLabel);
    previewLayout->addLayout(previewHeader);

    m_previewStack = new QStackedWidget(previewPanel);
    QWidget *dropZone = new QWidget(m_previewStack);
    dropZone->setObjectName(QStringLiteral("DropZone"));
    QVBoxLayout *dropLayout = new QVBoxLayout(dropZone);
    dropLayout->setAlignment(Qt::AlignCenter);
    dropLayout->setSpacing(10);
    QLabel *dropIcon = new QLabel(dropZone);
    dropIcon->setAlignment(Qt::AlignCenter);
    dropIcon->setPixmap(QPixmap(QStringLiteral(":/icons/image-add.svg"))
                            .scaled(84, 84, Qt::KeepAspectRatio, Qt::SmoothTransformation));
    QLabel *dropTitle = new QLabel(QStringLiteral("拖拽图片到这里"), dropZone);
    dropTitle->setObjectName(QStringLiteral("EmptyTitle"));
    dropTitle->setAlignment(Qt::AlignCenter);
    QLabel *dropHint = new QLabel(QStringLiteral("或点击下方按钮打开图片"), dropZone);
    dropHint->setObjectName(QStringLiteral("MutedLabel"));
    dropHint->setAlignment(Qt::AlignCenter);
    QPushButton *dropOpenButton = createButton(QStringLiteral("打开图片"), QStringLiteral("PrimaryButton"));
    dropOpenButton->setIcon(style()->standardIcon(QStyle::SP_DialogOpenButton));
    dropOpenButton->setFixedWidth(136);
    dropLayout->addStretch();
    dropLayout->addWidget(dropIcon);
    dropLayout->addWidget(dropTitle);
    dropLayout->addWidget(dropHint);
    dropLayout->addWidget(dropOpenButton, 0, Qt::AlignHCenter);
    dropLayout->addStretch();

    m_previewTabs = new QTabWidget(previewPanel);
    m_originalPreview = new ImagePreview(m_previewTabs);
    m_rectifiedPreview = new ImagePreview(m_previewTabs);
    m_rectifiedPreview->setEmptyText(QStringLiteral("识别后显示自动矫正的表格区域"));
    m_previewTabs->addTab(m_originalPreview, QStringLiteral("原图"));
    m_previewTabs->addTab(m_rectifiedPreview, QStringLiteral("矫正结果"));
    m_previewStack->addWidget(dropZone);
    m_previewStack->addWidget(m_previewTabs);
    previewLayout->addWidget(m_previewStack, 1);

    QWidget *tablePanel = new QWidget(splitter);
    tablePanel->setObjectName(QStringLiteral("TablePanel"));
    QVBoxLayout *tableLayout = new QVBoxLayout(tablePanel);
    tableLayout->setContentsMargins(10, 12, 10, 14);
    tableLayout->setSpacing(10);
    QHBoxLayout *tableHeader = new QHBoxLayout;
    QLabel *tableTitle = new QLabel(QStringLiteral("识别结果"), tablePanel);
    tableTitle->setObjectName(QStringLiteral("SectionTitle"));
    m_resultSummaryLabel = new QLabel(QStringLiteral("等待识别"), tablePanel);
    m_resultSummaryLabel->setObjectName(QStringLiteral("MutedLabel"));
    QToolButton *structureButton = new QToolButton(tablePanel);
    structureButton->setObjectName(QStringLiteral("MoreButton"));
    structureButton->setText(QStringLiteral("..."));
    structureButton->setToolTip(QStringLiteral("调整表格结构"));
    structureButton->setPopupMode(QToolButton::InstantPopup);
    QMenu *structureMenu = new QMenu(structureButton);
    QAction *addRowAction = structureMenu->addAction(QStringLiteral("添加行"));
    QAction *removeRowAction = structureMenu->addAction(QStringLiteral("删除当前行"));
    structureMenu->addSeparator();
    QAction *addColumnAction = structureMenu->addAction(QStringLiteral("添加列"));
    QAction *removeColumnAction = structureMenu->addAction(QStringLiteral("删除当前列"));
    structureButton->setMenu(structureMenu);
    tableHeader->addWidget(tableTitle);
    tableHeader->addWidget(m_resultSummaryLabel);
    tableHeader->addStretch();
    tableHeader->addWidget(structureButton);
    tableLayout->addLayout(tableHeader);

    m_reviewNotice = new QLabel(
        QStringLiteral("请对照原图核对行列、文字、数字、符号、单位及空白，确认无误后导出。"),
        tablePanel);
    m_reviewNotice->setObjectName(QStringLiteral("ReviewNotice"));
    m_reviewNotice->setWordWrap(true);
    m_reviewNotice->hide();
    tableLayout->addWidget(m_reviewNotice);

    m_resultStack = new QStackedWidget(tablePanel);
    QWidget *resultEmpty = new QWidget(m_resultStack);
    resultEmpty->setObjectName(QStringLiteral("ResultEmpty"));
    QVBoxLayout *emptyLayout = new QVBoxLayout(resultEmpty);
    emptyLayout->setAlignment(Qt::AlignCenter);
    emptyLayout->setSpacing(10);
    QLabel *resultIcon = new QLabel(resultEmpty);
    resultIcon->setAlignment(Qt::AlignCenter);
    resultIcon->setPixmap(QPixmap(QStringLiteral(":/icons/table-empty.svg"))
                              .scaled(96, 96, Qt::KeepAspectRatio, Qt::SmoothTransformation));
    QLabel *resultTitle = new QLabel(QStringLiteral("识别结果将显示在这里"), resultEmpty);
    resultTitle->setObjectName(QStringLiteral("EmptyTitle"));
    resultTitle->setAlignment(Qt::AlignCenter);
    QLabel *resultHint = new QLabel(QStringLiteral("导入图片后将自动识别表格"), resultEmpty);
    resultHint->setObjectName(QStringLiteral("MutedLabel"));
    resultHint->setAlignment(Qt::AlignCenter);
    emptyLayout->addStretch();
    emptyLayout->addWidget(resultIcon);
    emptyLayout->addWidget(resultTitle);
    emptyLayout->addWidget(resultHint);
    emptyLayout->addStretch();

    m_table = new QTableWidget(m_resultStack);
    m_table->setAlternatingRowColors(true);
    m_table->setSelectionMode(QAbstractItemView::ContiguousSelection);
    m_table->setSelectionBehavior(QAbstractItemView::SelectItems);
    m_table->setIconSize(QSize(12, 12));
    m_table->setEditTriggers(QAbstractItemView::SelectedClicked
                             | QAbstractItemView::DoubleClicked
                             | QAbstractItemView::EditKeyPressed);
    m_table->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
    m_table->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
    QScroller::grabGesture(m_table->viewport(), QScroller::TouchGesture);
    QScroller *tableTouchScroller = QScroller::scroller(m_table->viewport());
    QScrollerProperties touchProperties = tableTouchScroller->scrollerProperties();
    touchProperties.setScrollMetric(QScrollerProperties::DragVelocitySmoothingFactor, 0.35);
    touchProperties.setScrollMetric(QScrollerProperties::DecelerationFactor, 0.12);
    touchProperties.setScrollMetric(QScrollerProperties::MaximumVelocity, 0.75);
    touchProperties.setScrollMetric(QScrollerProperties::MousePressEventDelay, 0.08);
    tableTouchScroller->setScrollerProperties(touchProperties);
    m_table->viewport()->setAttribute(Qt::WA_AcceptTouchEvents, true);
    m_table->viewport()->installEventFilter(this);
    m_table->horizontalHeader()->setSectionResizeMode(QHeaderView::Interactive);
    const QFontMetrics tableMetrics(m_table->font());
    const int tableRowHeight = qMax(44, tableMetrics.height() + 20);
    const int tableHeaderHeight = qMax(48, tableMetrics.height() + 24);
    m_table->horizontalHeader()->setFixedHeight(tableHeaderHeight);
    m_table->horizontalHeader()->setMinimumSectionSize(82);
    m_table->verticalHeader()->setDefaultSectionSize(tableRowHeight);
    m_table->verticalHeader()->setFixedWidth(48);
    m_table->setWordWrap(true);
    m_resultStack->addWidget(resultEmpty);
    m_resultStack->addWidget(m_table);
    tableLayout->addWidget(m_resultStack, 1);

    splitter->addWidget(previewPanel);
    splitter->addWidget(tablePanel);
    splitter->setStretchFactor(0, 35);
    splitter->setStretchFactor(1, 65);
    splitter->setSizes(QList<int>() << 400 << 760);
    root->addWidget(splitter, 1);

    QWidget *status = new QWidget(central);
    status->setObjectName(QStringLiteral("StatusBarPanel"));
    QHBoxLayout *statusLayout = new QHBoxLayout(status);
    statusLayout->setContentsMargins(8, 0, 8, 0);
    statusLayout->setSpacing(10);
    m_statusLabel = new QLabel(QStringLiteral("打开一张图片开始"), status);
    m_statusLabel->setObjectName(QStringLiteral("MutedLabel"));
    m_progress = new QProgressBar(status);
    m_progress->setTextVisible(false);
    m_progress->setMaximumWidth(180);
    m_progress->hide();
    statusLayout->addWidget(m_statusLabel);
    statusLayout->addWidget(m_progress);
    statusLayout->addStretch();
    m_readyStateLabel = new QLabel(QStringLiteral("准备就绪"), status);
    m_readyStateLabel->setObjectName(QStringLiteral("ReadyState"));
    statusLayout->addWidget(m_readyStateLabel);
    root->addWidget(status);

    setCentralWidget(central);

    connect(m_openButton, &QPushButton::clicked, this, &MainWindow::openImage);
    connect(m_cropButton, &QPushButton::clicked, this, &MainWindow::selectTableRegion);
    connect(m_cameraButton, &QPushButton::clicked, this, &MainWindow::takePhoto);
    connect(dropOpenButton, &QPushButton::clicked, this, &MainWindow::openImage);
    connect(m_recognizeButton, &QPushButton::clicked, this, &MainWindow::recognizeImage);
    connect(m_originalPreview, &ImagePreview::cropSelected, this, &MainWindow::applyTableCrop);
    connect(m_originalPreview,
            &ImagePreview::cropSelectionActiveChanged,
            this,
            &MainWindow::cropSelectionActiveChanged);
    connect(m_cancelButton, &QPushButton::clicked, m_backend, &BackendRunner::cancel);
    connect(m_csvButton, &QPushButton::clicked, this, &MainWindow::exportCsv);
    connect(m_xlsxButton, &QPushButton::clicked, this, &MainWindow::exportXlsx);
    connect(addRowAction, &QAction::triggered, this, &MainWindow::addRow);
    connect(removeRowAction, &QAction::triggered, this, &MainWindow::removeRow);
    connect(addColumnAction, &QAction::triggered, this, &MainWindow::addColumn);
    connect(removeColumnAction, &QAction::triggered, this, &MainWindow::removeColumn);
    connect(m_table, &QTableWidget::itemChanged, this, &MainWindow::tableItemChanged);
    connect(m_table, &QTableWidget::cellClicked, this, &MainWindow::editTableCell);
    connect(m_originalPreview, &ImagePreview::activated, this, &MainWindow::showOriginalImage);
    connect(m_rectifiedPreview, &ImagePreview::activated, this, &MainWindow::showRectifiedImage);
}

void MainWindow::showImageViewer(const QImage &image, const QString &title)
{
    if (image.isNull())
        return;
    ImageViewerDialog dialog(image, title, this);
    dialog.exec();
}

void MainWindow::showOriginalImage()
{
    if (m_originalPreview->isCropSelectionActive())
        return;
    showImageViewer(m_originalPreview->image(), QStringLiteral("原图预览"));
}

void MainWindow::showRectifiedImage()
{
    showImageViewer(m_rectifiedPreview->image(), QStringLiteral("矫正结果预览"));
}

void MainWindow::openImage()
{
    const QString path = QFileDialog::getOpenFileName(
        this,
        QStringLiteral("打开表格照片"),
        QString(),
        QStringLiteral("图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp);;所有文件 (*.*)"));
    if (path.isEmpty())
        return;
    loadImage(path);
}

void MainWindow::selectTableRegion()
{
    if (m_originalPreview->image().isNull()) {
        m_statusLabel->setText(QStringLiteral("请先打开包含表格的图片。"));
        return;
    }
    if (m_originalPreview->isCropSelectionActive()) {
        m_originalPreview->cancelCropSelection();
        m_statusLabel->setText(QStringLiteral("已取消框选，可单击图片查看大图。"));
        return;
    }
    const bool hiddenRecognition = m_backend->isRunning()
        && m_recognitionActive
        && !m_recognitionDisplayRequested;
    if (hiddenRecognition) {
        m_restartRecognitionAfterCancel = false;
        m_backend->cancel();
    }
    m_previewTabs->setCurrentIndex(0);
    m_originalPreview->beginCropSelection();
    m_statusLabel->setText(QStringLiteral("请按住鼠标左键，完整框选一个表格；松开后自动识别。"));
}

void MainWindow::cropSelectionActiveChanged(bool active)
{
    m_cropButton->setText(active ? QStringLiteral("取消框选")
                                 : QStringLiteral("框选表格"));
    m_cropButton->setToolTip(active
        ? QStringLiteral("当前处于框选状态；再次点击或按 Esc 取消")
        : QStringLiteral("一张图包含多个表格时，拖动框选一个目标表"));
    updateActions();
}

void MainWindow::applyTableCrop(const QRect &imageRect)
{
    const QImage source = m_originalPreview->image();
    const QRect bounded = imageRect.normalized().intersected(source.rect());
    if (source.isNull() || bounded.width() < 16 || bounded.height() < 16) {
        m_statusLabel->setText(QStringLiteral("框选区域太小，请重新框选完整表格。"));
        return;
    }
    const int paddingX = qMax(4, qCeil(bounded.width() * 0.015));
    const int paddingY = qMax(4, qCeil(bounded.height() * 0.025));
    const QRect padded = bounded.adjusted(-paddingX,
                                          -paddingY,
                                          paddingX,
                                          paddingY)
                             .intersected(source.rect());
    QJsonObject cropTrace;
    cropTrace.insert(QStringLiteral("source_width"), source.width());
    cropTrace.insert(QStringLiteral("source_height"), source.height());
    cropTrace.insert(QStringLiteral("selected_x"), bounded.x());
    cropTrace.insert(QStringLiteral("selected_y"), bounded.y());
    cropTrace.insert(QStringLiteral("selected_width"), bounded.width());
    cropTrace.insert(QStringLiteral("selected_height"), bounded.height());
    cropTrace.insert(QStringLiteral("padded_x"), padded.x());
    cropTrace.insert(QStringLiteral("padded_y"), padded.y());
    cropTrace.insert(QStringLiteral("padded_width"), padded.width());
    cropTrace.insert(QStringLiteral("padded_height"), padded.height());
    GuiTrace::write(QStringLiteral("crop_saved"), cropTrace);
    QString outputDirectory = QStandardPaths::writableLocation(
        QStandardPaths::AppLocalDataLocation);
    if (outputDirectory.isEmpty())
        outputDirectory = QDir::tempPath() + QStringLiteral("/ocr-table-tool");
    QDir().mkpath(outputDirectory);
    const QString path = QDir(outputDirectory).filePath(
        QStringLiteral("crop-%1.png").arg(
            QUuid::createUuid().toString().remove(QLatin1Char('{')).remove(QLatin1Char('}'))));
    if (!source.copy(padded).save(path, "PNG")) {
        m_statusLabel->setText(QStringLiteral("框选区域保存失败，请重新框选。"));
        return;
    }
    if (!loadImage(path)) {
        QFile::remove(path);
        return;
    }
    m_ownedCroppedImagePath = QFileInfo(path).absoluteFilePath();
    m_fileNameLabel->setText(QStringLiteral("已框选一个表格"));
    m_statusLabel->setText(QStringLiteral("表格已框选，正在按单表模式识别。"));
}

void MainWindow::takePhoto()
{
    const bool hiddenRecognition = m_backend->isRunning()
        && m_recognitionActive
        && !m_recognitionDisplayRequested;
    if (hiddenRecognition) {
        m_restartRecognitionAfterCancel = false;
        m_statusLabel->setText(QStringLiteral("正在释放识别资源并打开摄像头…"));
        m_backend->cancel();
    }
    CameraCaptureDialog dialog(this);
    if (dialog.exec() != QDialog::Accepted)
        return;
    loadImage(dialog.capturedImagePath());
}

bool MainWindow::loadImage(const QString &path)
{
    QImageReader reader(path);
    reader.setAutoTransform(true);
    const QImage image = reader.read();
    if (image.isNull()) {
        m_statusLabel->setText(QStringLiteral("无法打开图片：格式不受支持或文件已损坏。"));
        m_statusLabel->setToolTip(reader.errorString());
        return false;
    }
    if (m_backend->isRunning() && m_recognitionActive) {
        m_restartRecognitionAfterCancel = true;
        m_backend->cancel();
    } else if (!m_backend->isRunning()) {
        m_restartRecognitionAfterCancel = false;
    }
    m_sourceImagePath = QFileInfo(path).absoluteFilePath();
    m_imagePath = m_sourceImagePath;
    if (!m_ownedCroppedImagePath.isEmpty()
        && m_ownedCroppedImagePath != m_sourceImagePath) {
        clearOwnedCroppedImage();
    }
    const QFileInfo sourceInfo(m_sourceImagePath);
    m_recognitionSourceIsRectified = bool(
        sourceInfo.suffix().compare(QStringLiteral("png"), Qt::CaseInsensitive) == 0
        && sourceInfo.completeBaseName().startsWith(
            QStringLiteral("rectified-"),
            Qt::CaseInsensitive));
    m_originalPreview->setImage(image);
    if (m_ownedRectifiedImagePath == m_sourceImagePath)
        m_ownedRectifiedImagePath.clear();
    else
        clearOwnedRectifiedImage();
    m_rectifiedPreview->clearImage();
    m_previewTabs->setCurrentIndex(0);
    m_previewStack->setCurrentWidget(m_previewTabs);
    m_resultStack->setCurrentIndex(0);
    m_fileNameLabel->setText(QFileInfo(path).fileName());
    m_table->clear();
    m_table->setRowCount(0);
    m_table->setColumnCount(0);
    m_spans = QJsonArray();
    m_pendingRecognitionResponse = QJsonObject();
    m_pendingRecognitionError.clear();
    m_recognitionDisplayRequested = false;
    m_recognitionActive = false;
    m_recognitionRetryCount = 0;
    m_publicationBlocked = false;
    m_resultSummaryLabel->setText(QStringLiteral("等待开始识别"));
    m_reviewNotice->hide();
    m_statusLabel->setText(QStringLiteral("图片已打开，后台识别即将开始；点击开始识别可查看进度"));
    m_recognizeButton->setText(QStringLiteral("开始识别"));
    updateActions();
    QTimer::singleShot(0, this, [this]() {
        if (qgetenv("OCR_TABLE_GUI_TEST_AUTO_DISPLAY") == QByteArray("1"))
            recognizeImage();
        else
            startBackgroundRecognition();
    });
    if (qgetenv("OCR_TABLE_GUI_TEST_CROP_FULL") == QByteArray("1")
        && !sourceInfo.completeBaseName().startsWith(
            QStringLiteral("crop-"), Qt::CaseInsensitive)) {
        QTimer::singleShot(100, this, [this]() {
            const QImage current = m_originalPreview->image();
            if (!current.isNull()) {
                GuiTrace::write(QStringLiteral("test_full_crop_triggered"));
                applyTableCrop(current.rect());
            }
        });
    }
    return true;
}

void MainWindow::dragEnterEvent(QDragEnterEvent *event)
{
    if (m_originalPreview->isCropSelectionActive())
        return;
    const bool hiddenRecognition = m_backend->isRunning()
        && m_recognitionActive
        && !m_recognitionDisplayRequested;
    if ((!m_backend->isRunning() || hiddenRecognition) && event->mimeData()->hasUrls())
        event->acceptProposedAction();
}

void MainWindow::dropEvent(QDropEvent *event)
{
    if (m_originalPreview->isCropSelectionActive())
        return;
    const bool hiddenRecognition = m_backend->isRunning()
        && m_recognitionActive
        && !m_recognitionDisplayRequested;
    if (m_backend->isRunning() && !hiddenRecognition)
        return;
    const QList<QUrl> urls = event->mimeData()->urls();
    if (!urls.isEmpty() && urls.first().isLocalFile() && loadImage(urls.first().toLocalFile()))
        event->acceptProposedAction();
}

bool MainWindow::eventFilter(QObject *watched, QEvent *event)
{
    if (watched == m_table->viewport()
        && (event->type() == QEvent::TouchBegin
            || event->type() == QEvent::TouchEnd
            || event->type() == QEvent::TouchCancel)) {
        if (event->type() == QEvent::TouchCancel) {
            m_touchTracking = false;
            return QMainWindow::eventFilter(watched, event);
        }
        QTouchEvent *touchEvent = static_cast<QTouchEvent *>(event);
        if (!touchEvent->touchPoints().isEmpty()) {
            const QPoint position = touchEvent->touchPoints().first().pos().toPoint();
            if (event->type() == QEvent::TouchBegin) {
                m_touchTracking = true;
                m_touchStartPosition = position;
            } else if (m_touchTracking) {
                m_touchTracking = false;
                if ((position - m_touchStartPosition).manhattanLength() <= 14) {
                    const QModelIndex index = m_table->indexAt(position);
                    if (index.isValid())
                        QTimer::singleShot(0, this, [this, index]() {
                            editTableCell(index.row(), index.column());
                        });
                }
            }
        }
    }
    return QMainWindow::eventFilter(watched, event);
}

void MainWindow::recognizeImage()
{
    if (m_sourceImagePath.isEmpty())
        return;

    m_recognitionDisplayRequested = true;
    if (!m_pendingRecognitionResponse.isEmpty()) {
        const QJsonObject response = m_pendingRecognitionResponse;
        m_pendingRecognitionResponse = QJsonObject();
        publishRecognitionResult(response);
        return;
    }
    if (!m_pendingRecognitionError.isEmpty()) {
        m_pendingRecognitionError.clear();
        m_recognitionRetryCount = 0;
        m_statusLabel->setText(QStringLiteral("正在重新尝试识别当前表格…"));
        startRecognitionRequest();
        return;
    }
    if (m_backend->isRunning()) {
        if (m_recognitionActive) {
            setBusy(true, QStringLiteral("正在精确识别，请稍等…"));
        }
        return;
    }
    m_recognitionRetryCount = 0;
    startRecognitionRequest();
}

void MainWindow::startBackgroundRecognition()
{
    if (m_sourceImagePath.isEmpty()
        || m_recognitionDisplayRequested
        || m_backend->isRunning())
        return;
    startRecognitionRequest();
}

void MainWindow::startRecognitionRequest()
{
    if (m_sourceImagePath.isEmpty() || m_backend->isRunning())
        return;
    clearPublishedResult(
        m_recognitionDisplayRequested
            ? QStringLiteral("正在重新识别，本次结果生成前不可导出")
            : QStringLiteral("等待开始识别"));
    QString outputDirectory = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    if (outputDirectory.isEmpty())
        outputDirectory = QDir::tempPath() + QStringLiteral("/ocr-table-tool");
    QDir().mkpath(outputDirectory);
    m_pendingRecognitionResponse = QJsonObject();
    m_pendingRecognitionError.clear();
    m_recognitionActive = true;
    QJsonObject requestTrace;
    requestTrace.insert(QStringLiteral("file_name"),
                        QFileInfo(m_sourceImagePath).fileName());
    requestTrace.insert(QStringLiteral("display_requested"),
                        m_recognitionDisplayRequested);
    requestTrace.insert(QStringLiteral("input_rectified"),
                        m_recognitionSourceIsRectified);
    const bool selectedTableRegion = !m_ownedCroppedImagePath.isEmpty()
        && QFileInfo(m_ownedCroppedImagePath).absoluteFilePath()
            == QFileInfo(m_sourceImagePath).absoluteFilePath();
    requestTrace.insert(QStringLiteral("selected_table_region"),
                        selectedTableRegion);
    GuiTrace::write(QStringLiteral("recognition_requested"), requestTrace);
    m_backend->recognize(m_sourceImagePath,
                         outputDirectory,
                         QStringLiteral("auto"),
                         m_recognitionSourceIsRectified,
                         selectedTableRegion);
}

TableData MainWindow::currentTable() const
{
    TableData table;
    for (int row = 0; row < m_table->rowCount(); ++row) {
        for (int column = 0; column < m_table->columnCount(); ++column) {
            QTableWidgetItem *item = m_table->item(row, column);
            table.setCell(row,
                          column,
                          item ? item->text() : QString(),
                          item ? item->data(Qt::UserRole).toDouble() : 1.0,
                          item ? item->data(Qt::UserRole + 1).toBool() : false);
        }
    }
    return table;
}

QString MainWindow::suggestedOutputPath(const QString &suffix) const
{
    const QFileInfo source(m_sourceImagePath.isEmpty() ? m_imagePath : m_sourceImagePath);
    return source.dir().filePath(source.completeBaseName() + suffix);
}

void MainWindow::exportCsv()
{
    if (m_backend->isRunning() || m_table->rowCount() == 0)
        return;
    const QString path = QFileDialog::getSaveFileName(
        this,
        QStringLiteral("导出 CSV"),
        suggestedOutputPath(QStringLiteral(".csv")),
        QStringLiteral("CSV 文件 (*.csv)"));
    if (path.isEmpty())
        return;
    QString error;
    if (!CsvExporter::writeFile(path, currentTable(), &error)) {
        m_statusLabel->setText(
            QStringLiteral("无法保存 CSV 文件。请检查保存位置、文件名、磁盘空间及文件占用状态。"));
        m_statusLabel->setToolTip(error);
        return;
    }
    m_statusLabel->setText(QStringLiteral("CSV 已保存：%1").arg(QDir::toNativeSeparators(path)));
}

void MainWindow::exportXlsx()
{
    if (m_backend->isRunning() || m_table->rowCount() == 0)
        return;
    const QString path = QFileDialog::getSaveFileName(
        this,
        QStringLiteral("导出 XLSX"),
        suggestedOutputPath(QStringLiteral(".xlsx")),
        QStringLiteral("Excel 工作簿 (*.xlsx)"));
    if (!path.isEmpty())
        m_backend->exportXlsx(path, currentTable(), m_spans);
}

void MainWindow::editTableCell(int row, int column)
{
    if (m_loadingTable || m_backend->isRunning() || row < 0 || column < 0)
        return;
    QTableWidgetItem *item = m_table->item(row, column);
    if (!item) {
        item = new QTableWidgetItem;
        m_table->setItem(row, column, item);
    }
    m_table->setCurrentItem(item);
    m_table->editItem(item);
    QTimer::singleShot(0, this, [this]() { showTouchKeyboard(); });
}

void MainWindow::addRow()
{
    if (m_backend->isRunning())
        return;
    const int row = m_table->currentRow() >= 0 ? m_table->currentRow() + 1 : m_table->rowCount();
    m_table->insertRow(row);
    clearSpansAfterStructureEdit();
}

void MainWindow::removeRow()
{
    if (m_backend->isRunning())
        return;
    if (m_table->currentRow() >= 0) {
        m_table->removeRow(m_table->currentRow());
        clearSpansAfterStructureEdit();
    }
}

void MainWindow::addColumn()
{
    if (m_backend->isRunning())
        return;
    const int column = m_table->currentColumn() >= 0 ? m_table->currentColumn() + 1 : m_table->columnCount();
    m_table->insertColumn(column);
    clearSpansAfterStructureEdit();
}

void MainWindow::removeColumn()
{
    if (m_backend->isRunning())
        return;
    if (m_table->currentColumn() >= 0) {
        m_table->removeColumn(m_table->currentColumn());
        clearSpansAfterStructureEdit();
    }
}

void MainWindow::clearSpansAfterStructureEdit()
{
    m_table->clearSpans();
    m_spans = QJsonArray();
    m_resultSummaryLabel->setText(QStringLiteral("%1 行 × %2 列 · 已调整结构")
                                      .arg(m_table->rowCount())
                                      .arg(m_table->columnCount()));
    updateActions();
}

void MainWindow::backendStarted(const QString &action)
{
    QString message = QStringLiteral("正在生成 XLSX…");
    if (action == QStringLiteral("warmup"))
        message = QStringLiteral("正在准备识别组件…");
    else if (action == QStringLiteral("recognize")) {
        if (!m_recognitionDisplayRequested) {
            m_progress->hide();
            m_recognizeButton->show();
            m_cancelButton->hide();
            m_readyStateLabel->setText(QStringLiteral("后台识别中"));
            m_statusLabel->setText(QStringLiteral("正在后台识别；点击开始识别可查看进度"));
            updateActions();
            return;
        }
        message = QStringLiteral("正在精确识别，请稍等…");
    }
    setBusy(true, message);
}

void MainWindow::backendSucceeded(const QString &action, const QJsonObject &response)
{
    QJsonObject successTrace;
    successTrace.insert(QStringLiteral("action"), action);
    successTrace.insert(QStringLiteral("rows"), response.value(QStringLiteral("rows")));
    successTrace.insert(QStringLiteral("columns"), response.value(QStringLiteral("columns")));
    successTrace.insert(QStringLiteral("recognition_state"),
                        response.value(QStringLiteral("recognition_state")));
    successTrace.insert(QStringLiteral("publication_blocked"),
                        response.value(QStringLiteral("publication_blocked")));
    successTrace.insert(QStringLiteral("display_requested"),
                        m_recognitionDisplayRequested);
    GuiTrace::write(QStringLiteral("backend_succeeded"), successTrace);
    if (action == QStringLiteral("warmup")) {
        setBusy(false);
        m_statusLabel->setText(QStringLiteral("识别组件已准备，导入图片后将自动识别"));
        if (!m_sourceImagePath.isEmpty()) {
            QTimer::singleShot(0, this, [this]() {
                if (m_recognitionDisplayRequested)
                    startRecognitionRequest();
                else
                    startBackgroundRecognition();
            });
        }
        return;
    }
    if (action == QStringLiteral("export_xlsx")) {
        setBusy(false);
        m_statusLabel->setText(QStringLiteral("XLSX 已保存：%1")
                                   .arg(QDir::toNativeSeparators(response.value(QStringLiteral("output_path")).toString())));
        return;
    }
    if (action == QStringLiteral("recognize")
        && m_restartRecognitionAfterCancel) {
        m_recognitionActive = false;
        m_restartRecognitionAfterCancel = false;
        m_pendingRecognitionResponse = QJsonObject();
        m_pendingRecognitionError.clear();
        setBusy(false);
        GuiTrace::write(QStringLiteral("stale_recognition_discarded"));
        QTimer::singleShot(0, this, [this]() {
            if (m_recognitionDisplayRequested)
                startRecognitionRequest();
            else
                startBackgroundRecognition();
        });
        return;
    }
    m_recognitionActive = false;
    if (!m_recognitionDisplayRequested) {
        m_pendingRecognitionResponse = response;
        m_pendingRecognitionError.clear();
        setBusy(false);
        m_statusLabel->setText(QStringLiteral("后台识别已完成，点击开始识别查看结果"));
        return;
    }
    publishRecognitionResult(response);
}

void MainWindow::publishRecognitionResult(const QJsonObject &response)
{
    setBusy(false);
    QString error;
    const TableData table = TableData::fromBackendJson(response, &error);
    if (!error.isEmpty()) {
        backendFailed(QStringLiteral("recognize"), error);
        return;
    }
    const QJsonValue publicationBlockedValue = response.value(QStringLiteral("publication_blocked"));
    if (!publicationBlockedValue.isBool()) {
        backendFailed(QStringLiteral("recognize"),
                      QStringLiteral("识别结果缺少风险发布状态。"));
        return;
    }
    const QJsonValue recognitionStateValue = response.value(QStringLiteral("recognition_state"));
    const QString recognitionState = recognitionStateValue.toString();
    const bool publicationBlocked = publicationBlockedValue.toBool();
    if (!recognitionStateValue.isString()
        || (recognitionState != QStringLiteral("verified")
            && recognitionState != QStringLiteral("needs_review")
            && recognitionState != QStringLiteral("blocked"))
        || (recognitionState == QStringLiteral("blocked")) != publicationBlocked) {
        backendFailed(QStringLiteral("recognize"),
                      QStringLiteral("识别结果的风险状态前后不一致。"));
        return;
    }
    const QJsonValue structureVerifiedValue = response.value(QStringLiteral("structure_verified"));
    const QJsonValue structureCertificateValue = response.value(QStringLiteral("structure_certificate"));
    const bool hasStructureCertificate = !structureCertificateValue.isUndefined()
        && !structureCertificateValue.isNull();
    if (!structureVerifiedValue.isBool()) {
        backendFailed(QStringLiteral("recognize"),
                      QStringLiteral("识别结果缺少表格结构状态。"));
        return;
    }
    const bool structureVerified = structureVerifiedValue.toBool();
    QJsonArray validatedSpans;
    bool unsafeSpansDiscarded = false;
    if (!validateSpans(response.value(QStringLiteral("spans")),
                       table,
                       &validatedSpans,
                       &error)) {
        if (structureVerified) {
            backendFailed(QStringLiteral("recognize"), error);
            return;
        }
        validatedSpans = QJsonArray();
        const QJsonValue rawSpansValue = response.value(QStringLiteral("spans"));
        if (rawSpansValue.isArray()) {
            const QJsonArray rawSpans = rawSpansValue.toArray();
            for (int spanIndex = 0; spanIndex < rawSpans.size(); ++spanIndex) {
                QJsonArray candidate = validatedSpans;
                candidate.append(rawSpans.at(spanIndex));
                QJsonArray checked;
                QString candidateError;
                if (validateSpans(candidate,
                                  table,
                                  &checked,
                                  &candidateError)) {
                    validatedSpans = checked;
                }
            }
        }
        unsafeSpansDiscarded = true;
    }
    if (!structureVerified) {
        if (hasStructureCertificate || !publicationBlocked) {
            backendFailed(QStringLiteral("recognize"),
                          QStringLiteral("结构未确认的结果包含不安全的合并关系或发布状态。"));
            return;
        }
    } else {
        if (!structureCertificateValue.isObject()) {
            backendFailed(QStringLiteral("recognize"),
                          QStringLiteral("已确认的表格结构缺少校验证书。"));
            return;
        }
        const QJsonObject certificate = structureCertificateValue.toObject();
        const QJsonValue certificateVersion = certificate.value(QStringLiteral("version"));
        const QJsonValue certifiedRows = certificate.value(QStringLiteral("rows"));
        const QJsonValue certifiedColumns = certificate.value(QStringLiteral("columns"));
        const QJsonValue certifiedSpans = certificate.value(QStringLiteral("spans"));
        if (!certificateVersion.isDouble()
            || certificateVersion.toDouble() != 1.0
            || certificate.value(QStringLiteral("verified")).toBool() != true
            || !certifiedRows.isDouble()
            || !certifiedColumns.isDouble()
            || certifiedRows.toDouble() != static_cast<double>(certifiedRows.toInt(-1))
            || certifiedColumns.toDouble() != static_cast<double>(certifiedColumns.toInt(-1))
            || certifiedRows.toInt(-1) != table.rowCount()
            || certifiedColumns.toInt(-1) != table.columnCount()
            || !certifiedSpans.isArray()
            || certifiedSpans.toArray() != validatedSpans
            || certificate.value(QStringLiteral("geometry_hash")).toString().isEmpty()
            || certificate.value(QStringLiteral("structure_hash")).toString().isEmpty()) {
            backendFailed(QStringLiteral("recognize"),
                          QStringLiteral("表格结构校验证书与识别结果不一致。"));
            return;
        }
    }
    if (recognitionState == QStringLiteral("verified")) {
        for (int row = 0; row < table.rowCount(); ++row) {
            for (int column = 0; column < table.columnCount(); ++column) {
                if (table.needsReview(row, column)) {
                    backendFailed(QStringLiteral("recognize"),
                                  QStringLiteral("已确认结果中仍存在待核对单元格。"));
                    return;
                }
            }
        }
    }
    m_publicationBlocked = publicationBlocked;
    m_spans = validatedSpans;
    showTable(table, m_spans);
    QJsonObject publishedTrace;
    publishedTrace.insert(QStringLiteral("rows"), table.rowCount());
    publishedTrace.insert(QStringLiteral("columns"), table.columnCount());
    publishedTrace.insert(QStringLiteral("span_count"), m_spans.size());
    publishedTrace.insert(QStringLiteral("publication_blocked"), m_publicationBlocked);
    publishedTrace.insert(QStringLiteral("review_cell_count"), pendingReviewCount());
    publishedTrace.insert(QStringLiteral("canonical_result_hash"),
                          canonicalDisplayedResultHash(table,
                                                       m_spans,
                                                       m_publicationBlocked));
    publishedTrace.insert(QStringLiteral("unsafe_spans_discarded"),
                          unsafeSpansDiscarded);
    GuiTrace::write(QStringLiteral("result_published"), publishedTrace);
    m_recognitionRetryCount = 0;
    m_recognizeButton->setText(QStringLiteral("重新识别"));
    const double elapsedSeconds = response.value(QStringLiteral("worker_wall_seconds"))
                                      .toDouble(response.value(QStringLiteral("elapsed_seconds")).toDouble());
    const int withheldNumbers = response.value(QStringLiteral("withheld_numeric_segments")).toInt();
    QString statusText;
    if (m_publicationBlocked) {
        statusText = QStringLiteral("精确识别完成 · 有风险待核对 · 用时 %1 秒")
                         .arg(elapsedSeconds, 0, 'f', 1);
    } else if (withheldNumbers > 0) {
        statusText = QStringLiteral("精确识别完成 · 用时 %1 秒 · %2 处数字仍缺少可靠视觉证据，已留待核对")
                         .arg(elapsedSeconds, 0, 'f', 1)
                         .arg(withheldNumbers);
    } else {
        statusText = QStringLiteral("精确识别完成 · 用时 %1 秒")
                         .arg(elapsedSeconds, 0, 'f', 1);
    }
    const QJsonArray qualityLabels = response.value(QStringLiteral("image_quality"))
                                         .toObject()
                                         .value(QStringLiteral("issue_labels"))
                                         .toArray();
    QStringList qualityWarnings;
    for (int index = 0; index < qualityLabels.size(); ++index)
        qualityWarnings.append(qualityLabels.at(index).toString());
    QStringList detailTexts;
    if (unsafeSpansDiscarded)
        detailTexts.append(QStringLiteral("合并关系未通过安全校验，已按独立单元格显示，数据仍保留待核对。"));
    if (!qualityWarnings.isEmpty())
        detailTexts.append(QStringLiteral("图像提示：%1").arg(qualityWarnings.join(QStringLiteral("、"))));
    const QJsonArray processingNotices = response.value(QStringLiteral("processing_notices")).toArray();
    QStringList processingNoticeTexts;
    for (int index = 0; index < processingNotices.size(); ++index)
        processingNoticeTexts.append(processingNotices.at(index).toString());
    processingNoticeTexts.removeDuplicates();
    if (!processingNoticeTexts.isEmpty())
        detailTexts.append(QStringLiteral("处理说明：%1").arg(processingNoticeTexts.join(QStringLiteral("；"))));
    const QJsonArray publicationBlockReasons = response.value(QStringLiteral("publication_block_reasons")).toArray();
    QStringList publicationBlockReasonTexts;
    for (int index = 0; index < publicationBlockReasons.size(); ++index)
        publicationBlockReasonTexts.append(publicationBlockReasons.at(index).toString());
    publicationBlockReasonTexts.removeDuplicates();
    if (!publicationBlockReasonTexts.isEmpty()) {
        detailTexts.append(QStringLiteral("风险原因：%1")
                               .arg(publicationBlockReasonTexts.join(QStringLiteral("；"))));
        m_reviewNotice->setText(QStringLiteral("结果存在风险；仍可导出当前结果，带黄色标记的单元格请重点核对。"));
    } else {
        m_reviewNotice->setText(QStringLiteral("请核对带黄色标记的单元格，确认后导出。"));
    }
    const QJsonArray structureWarnings = response.value(QStringLiteral("structure_warnings")).toArray();
    QStringList structureWarningTexts;
    for (int index = 0; index < structureWarnings.size(); ++index)
        structureWarningTexts.append(structureWarnings.at(index).toString());
    structureWarningTexts.removeDuplicates();
    if (publicationBlockReasonTexts.isEmpty() && !structureWarningTexts.isEmpty())
        detailTexts.append(QStringLiteral("结构待核对：%1").arg(structureWarningTexts.join(QStringLiteral("；"))));
    const QString detailText = detailTexts.join(QStringLiteral("\n"));
    m_statusLabel->setText(statusText);
    m_statusLabel->setToolTip(detailText.isEmpty() ? statusText : detailText);
    m_reviewNotice->setToolTip(detailText);
    m_resultStack->setCurrentWidget(m_table);
    const QString rectifiedPath = response.value(QStringLiteral("rectified_image")).toString();
    const QImage rectified(rectifiedPath);
    if (!rectified.isNull()) {
        clearOwnedRectifiedImage();
        m_ownedRectifiedImagePath = QFileInfo(rectifiedPath).absoluteFilePath();
        m_rectifiedPreview->setImage(rectified);
        m_previewTabs->setCurrentIndex(1);
    }
}

void MainWindow::clearOwnedRectifiedImage()
{
    if (m_ownedRectifiedImagePath.isEmpty())
        return;

    QString outputDirectory = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    if (outputDirectory.isEmpty())
        outputDirectory = QDir::tempPath() + QStringLiteral("/ocr-table-tool");
    const QFileInfo fileInfo(m_ownedRectifiedImagePath);
    const QString ownedDirectory = QDir(outputDirectory).absolutePath();
    if (fileInfo.absolutePath() == ownedDirectory
        && fileInfo.fileName().startsWith(QStringLiteral("rectified-"))
        && fileInfo.suffix().compare(QStringLiteral("png"), Qt::CaseInsensitive) == 0) {
        QFile::remove(fileInfo.absoluteFilePath());
    }
    m_ownedRectifiedImagePath.clear();
}

void MainWindow::clearOwnedCroppedImage()
{
    if (m_ownedCroppedImagePath.isEmpty())
        return;

    QString outputDirectory = QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    if (outputDirectory.isEmpty())
        outputDirectory = QDir::tempPath() + QStringLiteral("/ocr-table-tool");
    const QFileInfo fileInfo(m_ownedCroppedImagePath);
    const QString ownedDirectory = QDir(outputDirectory).absolutePath();
    if (fileInfo.absolutePath() == ownedDirectory
        && fileInfo.fileName().startsWith(QStringLiteral("crop-"))
        && fileInfo.suffix().compare(QStringLiteral("png"), Qt::CaseInsensitive) == 0) {
        QFile::remove(fileInfo.absoluteFilePath());
    }
    m_ownedCroppedImagePath.clear();
}

void MainWindow::clearStaleRectifiedImages()
{
    QStringList outputDirectories;
    const QString applicationData = QStandardPaths::writableLocation(
        QStandardPaths::AppLocalDataLocation);
    if (!applicationData.isEmpty())
        outputDirectories.append(QDir(applicationData).absolutePath());
    outputDirectories.append(
        QDir(QDir::tempPath() + QStringLiteral("/ocr-table-tool")).absolutePath());
    outputDirectories.removeDuplicates();

    const QDateTime cutoff = QDateTime::currentDateTimeUtc().addDays(-1);
    for (int directoryIndex = 0;
         directoryIndex < outputDirectories.size();
         ++directoryIndex) {
        const QDir directory(outputDirectories.at(directoryIndex));
        const QFileInfoList files = directory.entryInfoList(
            QStringList() << QStringLiteral("rectified-*.png") << QStringLiteral("crop-*.png"),
            QDir::Files | QDir::NoSymLinks);
        for (int fileIndex = 0; fileIndex < files.size(); ++fileIndex) {
            const QFileInfo fileInfo = files.at(fileIndex);
            if (fileInfo.lastModified().toUTC() < cutoff)
                QFile::remove(fileInfo.absoluteFilePath());
        }
    }
}

void MainWindow::backendFailed(const QString &action, const QString &message)
{
    QJsonObject failureTrace;
    failureTrace.insert(QStringLiteral("action"), action);
    failureTrace.insert(QStringLiteral("message"), message.left(1000));
    failureTrace.insert(QStringLiteral("display_requested"),
                        m_recognitionDisplayRequested);
    GuiTrace::write(QStringLiteral("backend_failed"), failureTrace);
    if (message == QStringLiteral("操作已取消")
        && action != QStringLiteral("recognize")) {
        setBusy(false);
        m_statusLabel->setText(action == QStringLiteral("warmup")
            ? QStringLiteral("识别组件准备已取消")
            : QStringLiteral("XLSX 导出已取消"));
        return;
    }
    if (action == QStringLiteral("recognize")) {
        m_recognitionActive = false;
        if (message == QStringLiteral("操作已取消")) {
            m_pendingRecognitionResponse = QJsonObject();
            m_pendingRecognitionError.clear();
            setBusy(false);
            if (m_restartRecognitionAfterCancel) {
                m_restartRecognitionAfterCancel = false;
                m_statusLabel->setText(QStringLiteral("图片已打开，后台识别即将重新开始；点击开始识别可查看进度"));
                QTimer::singleShot(0, this, [this]() {
                    if (m_recognitionDisplayRequested)
                        startRecognitionRequest();
                    else
                        startBackgroundRecognition();
                });
                return;
            }
            m_statusLabel->setText(QStringLiteral("识别已取消"));
            return;
        }
        const bool insufficientMemory = message.startsWith(
            QStringLiteral("可用内存不足"));
        if (!insufficientMemory
            && m_recognitionRetryCount < 1
            && !m_sourceImagePath.isEmpty()) {
            ++m_recognitionRetryCount;
            m_pendingRecognitionResponse = QJsonObject();
            m_pendingRecognitionError.clear();
            setBusy(false);
            const QString retrySource = m_sourceImagePath;
            m_statusLabel->setText(QStringLiteral("首次识别未完成，正在自动恢复并重试…"));
            QTimer::singleShot(350, this, [this, retrySource]() {
                if (m_sourceImagePath == retrySource && !m_backend->isRunning())
                    startRecognitionRequest();
            });
            return;
        }
        if (!m_recognitionDisplayRequested) {
            m_pendingRecognitionResponse = QJsonObject();
            m_pendingRecognitionError = message;
            setBusy(false);
            m_statusLabel->setText(insufficientMemory
                ? QStringLiteral("可用内存不足，已暂停识别；释放内存后点击开始识别重试")
                : QStringLiteral("后台识别未完成，点击开始识别查看原因"));
            return;
        }
        clearPublishedResult(QStringLiteral("本次识别未生成可导出结果"));
        setBusy(false);
        const QString displayMessage = localizedBackendError(message);
        m_statusLabel->setText(QStringLiteral("识别未完成：%1 可重新识别或重新框选表格。")
                                   .arg(displayMessage));
        m_statusLabel->setToolTip(localizedBackendDetail(message, displayMessage));
        return;
    }
    setBusy(false);
    const QString displayMessage = localizedBackendError(message);
    if (action == QStringLiteral("warmup")) {
        m_statusLabel->setText(QStringLiteral("识别组件准备失败：%1").arg(displayMessage));
        return;
    }
    m_statusLabel->setText(QStringLiteral("操作失败：%1").arg(displayMessage));
    m_statusLabel->setToolTip(localizedBackendDetail(message, displayMessage));
}

void MainWindow::clearPublishedResult(const QString &summary)
{
    m_loadingTable = true;
    m_table->clear();
    m_table->setRowCount(0);
    m_table->setColumnCount(0);
    m_spans = QJsonArray();
    m_publicationBlocked = false;
    m_resultStack->setCurrentIndex(0);
    m_reviewNotice->hide();
    if (!summary.isEmpty())
        m_resultSummaryLabel->setText(summary);
    m_loadingTable = false;
    updateActions();
}

void MainWindow::showTable(const TableData &table, const QJsonArray &spans)
{
    m_loadingTable = true;
    QScroller *tableScroller = QScroller::scroller(m_table->viewport());
    if (tableScroller)
        tableScroller->stop();
    int reviewCount = 0;
    m_table->clear();
    m_table->setRowCount(table.rowCount());
    m_table->setColumnCount(table.columnCount());
    const QFontMetrics rowHeaderMetrics(m_table->verticalHeader()->font());
    const int rowHeaderWidth = rowHeaderMetrics.width(QString::number(qMax(1, table.rowCount()))) + 24;
    m_table->verticalHeader()->setFixedWidth(qMax(48, rowHeaderWidth));
    QStringList headers;
    for (int column = 0; column < table.columnCount(); ++column)
        headers.append(QStringLiteral("列 %1").arg(column + 1));
    m_table->setHorizontalHeaderLabels(headers);
    for (int row = 0; row < table.rowCount(); ++row) {
        for (int column = 0; column < table.columnCount(); ++column) {
            QTableWidgetItem *item = new QTableWidgetItem(table.cell(row, column));
            const double confidence = table.confidence(row, column);
            const bool needsReview = table.needsReview(row, column);
            item->setData(Qt::UserRole, confidence);
            item->setData(Qt::UserRole + 1, needsReview);
            if (needsReview) {
                item->setToolTip(QStringLiteral("待确认：请对照原图核对文字、数字、符号和位置"));
                item->setIcon(reviewMarkerIcon());
                ++reviewCount;
            } else {
                item->setToolTip(QStringLiteral("OCR 置信度：%1%").arg(qRound(confidence * 100.0)));
            }
            if (!needsReview && confidence < 0.78 && !item->text().isEmpty()) {
                item->setToolTip(QStringLiteral("待确认：请对照原图核对文字、数字、符号和位置"));
                item->setIcon(reviewMarkerIcon());
                ++reviewCount;
            }
            m_table->setItem(row, column, item);
        }
    }
    QSet<int> titleRows;
    QSet<int> documentHeaderRows;
    QList<QTableWidgetItem *> centeredHeaderItems;
    for (int index = 0; index < spans.size(); ++index) {
        const QJsonObject span = spans.at(index).toObject();
        const int spanRow = span.value(QStringLiteral("row")).toInt();
        const int spanColumn = span.value(QStringLiteral("column")).toInt();
        const int rowSpan = span.value(QStringLiteral("row_span")).toInt(1);
        const int columnSpan = span.value(QStringLiteral("column_span")).toInt(1);
        if (rowSpan > 1 || columnSpan > 1) {
            m_table->setSpan(spanRow,
                             spanColumn,
                             rowSpan,
                             columnSpan);
        }
        const QString role = span.value(QStringLiteral("role")).toString();
        const int titleRow = spanRow;
        const int titleColumn = spanColumn;
        const bool isTitle = role == QStringLiteral("title")
            || (titleRow == 0 && titleColumn == 0 && columnSpan == table.columnCount());
        if (isTitle) {
            titleRows.insert(titleRow);
            if (titleRow + rowSpan < table.rowCount())
                documentHeaderRows.insert(titleRow + rowSpan);
            QTableWidgetItem *titleItem = m_table->item(titleRow, titleColumn);
            if (titleItem) {
                QFont titleFont = titleItem->font();
                titleFont.setFamily(QStringLiteral("Microsoft YaHei UI"));
                titleFont.setPixelSize(18);
                titleFont.setWeight(QFont::DemiBold);
                titleItem->setFont(titleFont);
                titleItem->setForeground(QColor(QStringLiteral("#183B5B")));
                titleItem->setBackground(QColor(QStringLiteral("#E8F0F8")));
                titleItem->setTextAlignment(Qt::AlignCenter);
            }
        } else if (role == QStringLiteral("subtitle")) {
            documentHeaderRows.insert(spanRow);
            QTableWidgetItem *subtitleItem = m_table->item(spanRow, spanColumn);
            if (subtitleItem)
                centeredHeaderItems.append(subtitleItem);
        } else if (role == QStringLiteral("group_header")) {
            documentHeaderRows.insert(spanRow);
            if (spanRow + rowSpan < table.rowCount())
                documentHeaderRows.insert(spanRow + rowSpan);
            QTableWidgetItem *headerItem = m_table->item(spanRow, spanColumn);
            if (headerItem)
                centeredHeaderItems.append(headerItem);
        } else if (role == QStringLiteral("row_header")) {
            documentHeaderRows.insert(spanRow);
            QTableWidgetItem *headerItem = m_table->item(spanRow, spanColumn);
            if (headerItem)
                centeredHeaderItems.append(headerItem);
        }
    }
    for (int headerRow : documentHeaderRows) {
        if (headerRow < 0 || headerRow >= table.rowCount() || titleRows.contains(headerRow))
            continue;
        for (int column = 0; column < table.columnCount(); ++column) {
            QTableWidgetItem *headerItem = m_table->item(headerRow, column);
            if (!headerItem)
                continue;
            QFont headerFont = headerItem->font();
            headerFont.setFamily(QStringLiteral("Microsoft YaHei UI"));
            headerFont.setPixelSize(14);
            headerFont.setWeight(QFont::DemiBold);
            headerItem->setFont(headerFont);
            headerItem->setForeground(QColor(QStringLiteral("#344054")));
            if (headerItem->background().style() == Qt::NoBrush)
                headerItem->setBackground(QColor(QStringLiteral("#F3F6FA")));
            headerItem->setTextAlignment(Qt::AlignLeft | Qt::AlignVCenter);
        }
    }
    for (QTableWidgetItem *headerItem : centeredHeaderItems)
        headerItem->setTextAlignment(Qt::AlignCenter);
    for (int column = 0; column < table.columnCount(); ++column) {
        int contentWidth = 110;
        for (int row = 0; row < table.rowCount(); ++row) {
            if (titleRows.contains(row) || m_table->columnSpan(row, column) > 1)
                continue;
            const QTableWidgetItem *item = m_table->item(row, column);
            if (!item)
                continue;
            const QFontMetrics metrics(item->font());
            const QStringList lines = item->text().split(QLatin1Char('\n'));
            for (const QString &line : lines)
                contentWidth = qMax(contentWidth, metrics.width(line) + 28);
        }
        m_table->setColumnWidth(column, qBound(110, contentWidth, 360));
    }
    m_table->resizeRowsToContents();
    for (int titleRow : titleRows)
        m_table->setRowHeight(titleRow, qMax(m_table->rowHeight(titleRow), 56));
    for (int headerRow : documentHeaderRows) {
        if (!titleRows.contains(headerRow))
            m_table->setRowHeight(headerRow, qMax(m_table->rowHeight(headerRow), 48));
    }
    const auto resetScrollPosition = [this]() {
        m_table->horizontalScrollBar()->setSliderPosition(
            m_table->horizontalScrollBar()->minimum());
        m_table->verticalScrollBar()->setSliderPosition(
            m_table->verticalScrollBar()->minimum());
    };
    resetScrollPosition();
    QTimer::singleShot(0, this, resetScrollPosition);
    m_loadingTable = false;
    m_reviewNotice->show();
    m_resultSummaryLabel->setText(QStringLiteral("%1 行 × %2 列 · %3 项待确认")
                                      .arg(table.rowCount())
                                      .arg(table.columnCount())
                                      .arg(reviewCount));
    if (m_publicationBlocked)
        m_statusLabel->setText(QStringLiteral("识别完成 · 结果有风险，仍可导出当前结果"));
    else if (reviewCount > 0)
        m_statusLabel->setText(QStringLiteral("识别完成 · 黄色标记项待核对，可随时导出"));
    else
        m_statusLabel->setText(QStringLiteral("识别完成 · 可以正式导出"));
    updateActions();
}

void MainWindow::tableItemChanged(QTableWidgetItem *item)
{
    if (m_loadingTable)
        return;
    if (item) {
        const QSignalBlocker blocker(m_table);
        item->setData(Qt::UserRole, 1.0);
        item->setData(Qt::UserRole + 1, false);
        item->setBackground(QBrush());
        item->setToolTip(QStringLiteral("已人工修改"));
        const int pending = pendingReviewCount();
        m_resultSummaryLabel->setText(QStringLiteral("%1 行 × %2 列 · %3 项待确认")
                                          .arg(m_table->rowCount())
                                          .arg(m_table->columnCount())
                                          .arg(pending));
        if (m_publicationBlocked)
            m_statusLabel->setText(QStringLiteral("结果仍有风险，可导出当前结果"));
        else if (pending == 0)
            m_statusLabel->setText(QStringLiteral("人工核对完成，可以正式导出"));
        updateActions();
    }
}

int MainWindow::pendingReviewCount() const
{
    int count = 0;
    for (int row = 0; row < m_table->rowCount(); ++row) {
        for (int column = 0; column < m_table->columnCount(); ++column) {
            const QTableWidgetItem *item = m_table->item(row, column);
            if (item && item->data(Qt::UserRole + 1).toBool())
                ++count;
        }
    }
    return count;
}

void MainWindow::showTouchKeyboard()
{
    QGuiApplication::inputMethod()->show();
#ifdef Q_OS_WIN
    // Some Windows tablet drivers are not exposed through QTouchDevice even
    // though a user is editing this table by touch.  Always request TabTip;
    // it is harmless when the system keyboard is already visible.
    const QStringList candidates = QStringList()
        << QStringLiteral("C:/Program Files/Common Files/microsoft shared/ink/TabTip.exe")
        << QStringLiteral("C:/Program Files (x86)/Common Files/microsoft shared/ink/TabTip.exe");
    for (int index = 0; index < candidates.size(); ++index) {
        if (QFileInfo::exists(candidates.at(index))) {
            QProcess::startDetached(candidates.at(index));
            break;
        }
    }
#endif
}

void MainWindow::setBusy(bool busy, const QString &message)
{
    m_progress->setRange(0, 0);
    m_progress->setVisible(busy);
    m_recognizeButton->setVisible(!busy);
    m_cancelButton->setVisible(busy);
    if (!message.isEmpty())
        m_statusLabel->setText(message);
    m_readyStateLabel->setText(busy ? QStringLiteral("正在处理") : QStringLiteral("准备就绪"));
    updateActions();
}

void MainWindow::updateActions()
{
    const bool busy = m_backend->isRunning();
    const bool hiddenRecognition = busy
        && m_recognitionActive
        && !m_recognitionDisplayRequested;
    const bool hasImage = !m_imagePath.isEmpty();
    const bool cropSelectionActive = m_originalPreview->isCropSelectionActive();
    const bool hasTable = m_table->rowCount() > 0 && m_table->columnCount() > 0;
    const int pending = pendingReviewCount();
    const bool exportReady = !busy && hasTable;
    m_openButton->setEnabled(!cropSelectionActive && (!busy || hiddenRecognition));
    m_cropButton->setEnabled((!busy || hiddenRecognition) && hasImage);
    m_cameraButton->setEnabled(!cropSelectionActive && (!busy || hiddenRecognition));
    m_recognizeButton->setEnabled(!cropSelectionActive
                                  && (!busy || hiddenRecognition)
                                  && hasImage
                                  && m_backend->isConfigured());
    m_cancelButton->setEnabled(busy);
    m_csvButton->setEnabled(!cropSelectionActive && exportReady);
    m_xlsxButton->setEnabled(!cropSelectionActive
                             && exportReady
                             && m_backend->isConfigured());
    const QString exportTip = pending > 0
        ? QStringLiteral("可导出当前结果；其中 %1 个黄色标记项仍需核对").arg(pending)
        : QString();
    m_csvButton->setToolTip(exportTip);
    m_xlsxButton->setToolTip(exportTip);
}
