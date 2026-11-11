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

namespace OcrTableToolGui {

// 后端返回的矫正图路径必须先过这道校验。
// 界面自己的输出目录永远是本机的（AppLocalDataLocation，临时目录兜底），
// 后端却可能给出任意路径：
//   - UNC 路径（\\host\share\x.png）会让进程去认证网络共享，凭据可能外泄；
//   - 带 ".." 的路径会读到输出目录之外的文件；
//   - 空路径说明这份响应不完整。
// 自由函数（外部链接）：单元测试 tests/tst_gui.cpp 直接声明并调用它，
// 绕开 MSVC 把访问级别编进符号名的问题（私有成员函数无法从测试 TU 调用）。
bool isSafeLocalImagePath(const QString &path)
{
    const QString trimmed = path.trimmed();
    if (trimmed.isEmpty())
        return false;
    // UNC：\\host\share 或 //host/share
    if (trimmed.startsWith(QLatin1String("\\\\")) || trimmed.startsWith(QLatin1String("//")))
        return false;
    // 逐段检查，任何 ".." 段都拒绝。
    // 这里不用 QDir::cleanPath：它会把 ".." 直接消掉，越界反而看不出来。
    QString normalized = trimmed;
    normalized.replace(QLatin1Char('\\'), QLatin1Char('/'));
    const QStringList segments = normalized.split(QLatin1Char('/'));
    for (int index = 0; index < segments.size(); ++index) {
        if (segments.at(index) == QLatin1String(".."))
            return false;
    }
    return true;
}

} // namespace OcrTableToolGui

namespace
{
// 一次识别的墙钟预算（毫秒）。这是**界面侧**的镜像口径，对外契约
// 「单次识别 60 秒墙钟」不变：首次请求仍然不传 deadline（不额外收紧）。
// 它的唯一用途是给自动重试划一条总时长上限（findings B1）。
const int kRecognitionWallBudgetMilliseconds = 60000;
// 自动重试与首轮之间留出的空档（原值 350 ms，这里原样保留）。
const int kRetryDelayMilliseconds = 350;
// 后端把 deadline_seconds 收口到至少 5 秒；剩余预算比它还小时重试没有意义，
// 直接按失败呈现，避免"重试必然超时"的假重试。
const int kMinimumRetryBudgetSeconds = 5;

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

// 没有命中任何具体原因时的通用兜底文案。单独取成常量，便于判断"命中的是不是
// 只是兜底"：INVALID_REQUEST / INTERNAL_ERROR / RECOGNITION_BUDGET_EXCEEDED 是
// **桶型**错误码，后端把具体原因写进 message（"图里没检测到表格""网格切分异常…"
// "发生内部错误"…），这些码要优先用更具体的那条，而不是码本身那句通用话。
const QString kGenericBackendError =
    QStringLiteral("识别失败，已保留原始错误信息。请展开“详细信息”查看后重试。");

// 结构性失败的具体原因：命中即返回可行动的具体文案。
//
// 2026-10-24 修正（findings/104）：后端这几类失败的文案都带「图片」或「模型」二字，
// 而下面 model/模型 分支与 image/图片 分支会把它们一律说成
// 「识别模型文件缺失或损坏」或「无法读取图片」—— 于是「60 秒没跑完」
// 「结构过于复杂」「内部错误」「联网被拦截」这些失败在界面上被说成了别的问题。
// 这几条关键词很具体，放在函数最前面，命中即返回，不再往下走。
QString localizedBackendSpecificError(const QString &message)
{
    if (message.contains(QStringLiteral("逐格复核超过"))
        || message.contains(QStringLiteral("判定表格结构过于复杂"))) {
        return QStringLiteral("本次识别未能在 60 秒内完成：这张图的表格结构过于复杂，已中止以免长时间无响应。请用「框选区域」只框住表格本体（去掉桌面背景、条纹或其他网格），或裁剪掉干扰内容后重新识别。");
    }
    if (message.contains(QStringLiteral("复核图片量已超上限"))) {
        return QStringLiteral("本次识别已中止：这张图的网格切分异常，同一批单元格被反复复核，复核图片量已超上限。请用「框选区域」只框住表格本体（去掉桌面背景、条纹或其他网格），或裁剪掉干扰内容后重新识别。");
    }
    if (message.contains(QStringLiteral("发生内部错误"))) {
        return QStringLiteral("识别过程中发生内部错误，请重试；若反复出现，请更换图片或联系技术支持。");
    }
    if (message.contains(QStringLiteral("禁止联网"))) {
        return QStringLiteral("本产品不会进行任何网络访问，请检查本地识别组件后重试。");
    }
    // 2026-10-28（findings/61 F3）：进程退出码非 0 而响应是 ok 时，这份结果
    // 不被采用。文案只陈述事实与可行动作，不承诺"结果一定完整"。
    if (message.contains(QStringLiteral("本次结果未采用"))) {
        return QStringLiteral("识别组件在交出结果后没能正常结束，本次结果未被采用。请重新识别；若反复出现，请重启软件。");
    }
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
        || message.contains(QStringLiteral("crash"), Qt::CaseInsensitive)) {
        return QStringLiteral("识别组件本次运行异常。请再次点击开始识别；若仍失败，请关闭其他正在运行的版本后重启软件。");
    }
    if (message.contains(QStringLiteral("无法可靠"))
        || message.contains(QStringLiteral("跨模型冲突"))
        || message.contains(QStringLiteral("30秒"))) {
        return QStringLiteral("识别流程已正常结束，但不同模型或结构结果不一致。为避免输出错位或拼错列的表格，软件主动没有生成结果；这不是程序崩溃。截图请裁剪到表格区域并保留原始分辨率，照片请改善光线、对焦或靠近后重试。");
    }
    return QString();
}

// 弱猜测：模型 / 图片 / 进程等关键词。只在**没有**更具体原因时使用。
//
// 2026-11-05：这些分支原先和上面的具体分支混在一个函数里，于是
// IMAGE_PATH_REQUIRED / IMAGE_NOT_FOUND 的消息里带「图片」二字就被说成
// 「图片格式或文件损坏」（与真实原因不符）。现在这类码由 error_code 分支
// 接管，弱猜测只作关键词兜底。
QString localizedBackendWeakError(const QString &message)
{
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
    // 2026-10-22 修正：通用英文关键词 "process" 原来排在 "image" **之前**，
    // 于是缺图消息形如 "image does not exist: D:\...\process\xxx.png" 时，
    // 路径里的 process 子串先命中，提示成"识别组件本次运行异常…请重启软件"，
    // 而不是"无法读取图片"。通用关键词放到最后判断。
    if (message.contains(QStringLiteral("process"), Qt::CaseInsensitive)) {
        return QStringLiteral("识别组件本次运行异常。请再次点击开始识别；若仍失败，请关闭其他正在运行的版本后重启软件。");
    }
    return QString();
}

// 单参数版本：**签名与调用形态保持不变**（UI 契约测试 test_ui_contract.py:254
// 锚定了 localizedBackendError(message) 这个调用形态）。
QString localizedBackendError(const QString &message)
{
    const QString specific = localizedBackendSpecificError(message);
    if (!specific.isEmpty())
        return specific;
    const QString weak = localizedBackendWeakError(message);
    if (!weak.isEmpty())
        return weak;
    return kGenericBackendError;
}

// 2026-11-05 修正（缺陷：识别链路的错误提示不按 error_code 分支，19 个错误码
// 里有 8 个给出空泛或错误的提示）。
//
// 契约 docs/BACKEND_API_V1.md §2 明确要求："程序只能根据 error_code、field、
// retryable 分支；message 仅用于显示/日志"，并**禁止**对 message 做关键字匹配。
// 修复前这里只有单参数版本、只能靠关键词猜：
//   · UNSUPPORTED_PROTOCOL / UNSUPPORTED_ACTION / INVALID_REQUEST /
//     INVALID_OPTION / OUTPUT_DIRECTORY_INVALID / OUTPUT_WRITE_FAILED
//     —— 全部落到「识别失败，已保留原始错误信息…」，用户看不出该做什么；
//   · IMAGE_PATH_REQUIRED / IMAGE_NOT_FOUND —— 被说成「图片格式或文件损坏」。
//
// 这里按契约逐个错误码给出**具体、可行动**的中文提示（说清楚"发生了什么 +
// 用户该做什么"），不再用「识别失败」这种空泛话。
// 码表取自 docs/BACKEND_API_V1.md §2 的 19 个码，另加界面侧传输层
// （backendrunner.cpp）与 GUI 自造的 INVALID_RESPONSE。
QString localizedBackendErrorByCode(const QString &errorCode)
{
    if (errorCode.isEmpty())
        return QString();
    // ── 协议层 ────────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("UNSUPPORTED_PROTOCOL"))
        return QStringLiteral("识别组件与当前程序版本不兼容，本次任务没有被受理。请更新或重新安装识别组件后再试。");
    if (errorCode == QStringLiteral("UNSUPPORTED_ACTION"))
        return QStringLiteral("识别组件不支持本次操作，本次任务没有被受理。请更新或重新安装识别组件后再试。");
    // ── 请求层 ────────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("INVALID_REQUEST"))
        return QStringLiteral("识别请求没有被受理：请求内容不合法，或这张图里没有可识别的表格。请裁剪到表格区域并保留原始分辨率后重试。");
    if (errorCode == QStringLiteral("INVALID_OPTION"))
        return QStringLiteral("识别选项不合法（裁剪方式／识别精度／时限），本次任务没有被受理。请重新选择识别选项，或重新安装软件后再试。");
    // ── 图片层 ────────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("IMAGE_PATH_REQUIRED"))
        return QStringLiteral("没有拿到要识别的图片路径，本次任务没有被受理。请重新打开图片后再点开始识别。");
    if (errorCode == QStringLiteral("IMAGE_NOT_FOUND"))
        return QStringLiteral("找不到要识别的图片文件（文件可能已被移动或删除）。请重新打开图片后再点开始识别。");
    if (errorCode == QStringLiteral("IMAGE_READ_FAILED"))
        return QStringLiteral("图片读不出来（文件为空或已损坏，或不是受支持的图片格式／超出尺寸上限）。请换一张完整清晰的图片重试。");
    // ── 输出层 ────────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("OUTPUT_DIRECTORY_INVALID"))
        return QStringLiteral("识别组件拿到的临时输出目录无效，无法写入中间文件。请重启软件后重试；若反复出现，请检查本机临时目录是否可用。");
    if (errorCode == QStringLiteral("OUTPUT_WRITE_FAILED"))
        return QStringLiteral("识别结果写不进临时目录。请检查磁盘剩余空间与临时目录的写入权限后重试。");
    // ── 模型层 ────────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("MODEL_MISSING"))
        return QStringLiteral("识别模型文件缺失，识别无法开始。请重新安装或修复安装目录后重试。");
    if (errorCode == QStringLiteral("MODEL_HASH_MISMATCH"))
        return QStringLiteral("识别模型文件校验不通过（文件损坏或被改动），识别无法开始。请重新安装或修复安装目录后重试。");
    // ── 导出层 ────────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("EXPORT_PATH_REQUIRED"))
        return QStringLiteral("没有指定导出文件路径。请重新选择保存位置后再导出。");
    if (errorCode == QStringLiteral("EXPORT_GRID_INVALID"))
        return QStringLiteral("导出内容不是规整的矩形表格，无法写入 Excel。请重新识别后再导出。");
    if (errorCode == QStringLiteral("EXPORT_SPAN_INVALID"))
        return QStringLiteral("合并单元格信息无效，无法写入 Excel。请重新识别后再导出。");
    if (errorCode == QStringLiteral("EXPORT_WRITE_FAILED"))
        return QStringLiteral("无法写入导出文件。请关闭正在占用该文件的程序（例如 Excel），或换一个保存位置后重试。");
    // ── 其它协议码 ────────────────────────────────────────────────────
    if (errorCode == QStringLiteral("NETWORK_ACCESS_DENIED"))
        return QStringLiteral("本产品不会进行任何网络访问，识别组件的联网行为已被拦截。请检查本地识别组件后重试。");
    if (errorCode == QStringLiteral("RECOGNITION_BUDGET_EXCEEDED"))
        return QStringLiteral("在 60 秒等待预算内没能确认表格结构，本次识别已中止。请用「框选区域」只框住表格本体，或裁掉桌面背景、条纹等干扰内容后重试。");
    if (errorCode == QStringLiteral("INSUFFICIENT_MEMORY"))
        return QStringLiteral("可用内存不足，识别已中止。请关闭占用内存的程序，或用「框选区域」缩小识别范围后重试。");
    if (errorCode == QStringLiteral("INTERNAL_ERROR"))
        return QStringLiteral("识别组件发生内部错误，本次识别未完成。请重试；若反复出现，请更换图片或联系技术支持。");
    // ── 传输层（由界面侧 backendrunner.cpp 产生，后端不会返回）──────────
    if (errorCode == QStringLiteral("EMPTY_RESPONSE"))
        return QStringLiteral("识别组件没有返回有效结果，本次识别未完成。请重试。");
    if (errorCode == QStringLiteral("BACKEND_BUSY"))
        return QStringLiteral("识别组件正在处理上一个任务，请稍候再试。");
    if (errorCode == QStringLiteral("BACKEND_NOT_CONFIGURED"))
        return QStringLiteral("识别组件未配置就绪。请检查安装目录是否完整（识别组件与模型文件）后重试。");
    if (errorCode == QStringLiteral("BACKEND_START_FAILED"))
        return QStringLiteral("识别组件启动失败。请关闭其他正在运行的版本后重试。");
    if (errorCode == QStringLiteral("BACKEND_CRASHED"))
        return QStringLiteral("识别组件异常退出，本次识别未完成。请重试；若反复出现请重启软件。");
    if (errorCode == QStringLiteral("INVALID_RESPONSE"))
        return QStringLiteral("识别组件返回的结果不完整，本次结果未被采用。请重新识别；若反复出现，请重启软件。");
    return QString();
}

// 带错误码的重载（新增）：**优先按 error_code 精确分支**，关键词匹配作为兜底。
QString localizedBackendError(const QString &errorCode, const QString &message)
{
    // ① 桶型错误码：后端把**具体原因**写进 message，先用它（比码本身那句
    //    通用话更可行动，例如 RECOGNITION_BUDGET_EXCEEDED 的"网格切分异常…
    //    同一批单元格被反复复核"）。
    if (errorCode == QStringLiteral("INVALID_REQUEST")
        || errorCode == QStringLiteral("INTERNAL_ERROR")
        || errorCode == QStringLiteral("RECOGNITION_BUDGET_EXCEEDED")) {
        const QString specific = localizedBackendSpecificError(message);
        if (!specific.isEmpty())
            return specific;
    }
    // ② 按 error_code 精确分支（契约 §2 的主路径）。
    const QString byCode = localizedBackendErrorByCode(errorCode);
    if (!byCode.isEmpty())
        return byCode;
    // ③ 兜底：老后端可能不带 error_code（协议 v1 之前），按消息关键词匹配。
    return localizedBackendError(message);
}

QString localizedExportError(const QString &errorCode, const QString &message)
{
    // 2026-10-22 修正：原来导出失败和识别失败共用一个提示函数，导出错误
    // 落到兜底文案"识别未完成，请重新识别" —— 用户点"Excel"失败却被告知去
    // 重新识别，会白跑一次几十秒的识别，真实原因（路径被占用 / 权限 /
    // 网格不规整）只藏在 tooltip 里。这里按后端 error_code 分支。
    if (errorCode == QStringLiteral("EXPORT_PATH_REQUIRED"))
        return QStringLiteral("没有指定导出文件路径。");
    if (errorCode == QStringLiteral("EXPORT_GRID_INVALID"))
        return QStringLiteral("导出内容不是规整的矩形表格，无法写入 Excel。");
    if (errorCode == QStringLiteral("EXPORT_SPAN_INVALID"))
        return QStringLiteral("合并单元格信息无效，无法写入 Excel。");
    if (errorCode == QStringLiteral("EXPORT_WRITE_FAILED"))
        return QStringLiteral("无法写入导出文件。请关闭正在占用该文件的程序（例如 Excel），或换一个保存位置后重试。");
    if (errorCode == QStringLiteral("INTERNAL_ERROR"))
        return QStringLiteral("导出时发生内部错误，请重试。");
    // 2026-10-23 修正（findings/68 F1）：导出阶段还会走到下面这些后端级错误码，
    // 原先它们不在上面 5 个分支里、message 也不含下面的关键词，于是全部落到
    // 最终兜底 —— 界面一律显示"请关闭正在占用该文件的程序"，与真实原因无关
    // （协议不兼容 / 组件无响应 / 内存不足 / 模型损坏 / 请求非法都会被说成"被占用"）。
    if (errorCode == QStringLiteral("UNSUPPORTED_PROTOCOL"))
        return QStringLiteral("识别组件与当前程序版本不兼容，请更新或重新安装后再试。");
    if (errorCode == QStringLiteral("EMPTY_RESPONSE"))
        return QStringLiteral("识别组件没有返回有效结果，请重试。");
    if (errorCode == QStringLiteral("INSUFFICIENT_MEMORY"))
        return QStringLiteral("可用内存不足，未能完成导出。请关闭占用内存的程序后重试。");
    if (errorCode == QStringLiteral("INVALID_REQUEST"))
        return QStringLiteral("导出请求无效，请重新识别后再导出。");
    if (errorCode == QStringLiteral("INVALID_OPTION"))
        return QStringLiteral("导出选项无效，请在设置里重新选择后再导出。");
    if (errorCode == QStringLiteral("MODEL_MISSING"))
        return QStringLiteral("识别模型缺失或损坏，请重新安装后再试。");
    if (errorCode == QStringLiteral("OUTPUT_WRITE_FAILED"))
        return QStringLiteral("无法写入导出文件，请检查输出路径是否可写。");
    if (errorCode == QStringLiteral("NETWORK_ACCESS_DENIED"))
        return QStringLiteral("本产品不会进行任何网络访问，请检查本地识别组件后重试。");
    // 2026-10-23 第 50 轮修正（findings/109 M3）：上面这些是**后端协议层**的码，
    // 但导出还会先撞上**传输层**的失败（backendrunner.cpp 的 requestFailed）——
    // 这几个码后端从来不会返回，原先全部落进最终兜底，导出超时尤其看不出是超时。
    if (errorCode == QStringLiteral("BACKEND_BUSY"))
        return QStringLiteral("识别组件正在处理上一个任务，请稍后重试。");
    if (errorCode == QStringLiteral("BACKEND_NOT_CONFIGURED"))
        return QStringLiteral("识别组件未配置就绪，请检查安装目录是否完整后重试。");
    if (errorCode == QStringLiteral("BACKEND_START_FAILED"))
        return QStringLiteral("识别组件启动失败，请关闭其他正在运行的版本后重试。");
    if (errorCode == QStringLiteral("BACKEND_CRASHED"))
        return QStringLiteral("识别组件异常退出，导出未完成。请重试；若反复出现请重启软件。");
    // 超时与取消走的是**空错误码**（backendrunner.cpp:244），导出超时必须单独认出来。
    if (errorCode.isEmpty() && message.contains(QStringLiteral("超时")))
        return QStringLiteral("操作超时，导出未完成。请稍后重试。");
    // 没有结构化错误码时按后端原文兜底判断。
    if (message.contains(QStringLiteral("output_path is required"), Qt::CaseInsensitive))
        return QStringLiteral("没有指定导出文件路径。");
    if (message.contains(QStringLiteral("cells must"), Qt::CaseInsensitive))
        return QStringLiteral("导出内容不是规整的矩形表格，无法写入 Excel。");
    if (message.contains(QStringLiteral("invalid XLSX"), Qt::CaseInsensitive))
        return QStringLiteral("合并单元格信息无效，无法写入 Excel。");
    if (message.contains(QStringLiteral("permission"), Qt::CaseInsensitive)
        || message.contains(QStringLiteral("denied"), Qt::CaseInsensitive))
        return QStringLiteral("没有写入权限。请换一个保存位置后重试。");
    // 最终兜底：不要再断言"被占用"（2026-10-23 修正，见 findings/68 F1）。
    // 真实原因可能与文件占用无关。
    // 2026-10-23 第 50 轮修正（findings/109 M2）：**不要承诺「展开详细信息」**——
    // 2026-11-05 之前 localizedBackendDetail() 只在原文纯中文时把原文放进
    // tooltip，多数情况下 tooltip 与状态栏逐字相同 ⇒ 那个提示是假承诺，会把用户
    // 引到空处。现在 localizedBackendDetail() 一律保留后端原文（见该函数），
    // tooltip 里确实能看到原始信息。
    return QStringLiteral("导出失败。请重试；若仍失败，请重新识别后再导出。");
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
    // 2026-11-05 修正（缺陷：后端原文被丢弃）：原文原先只在「纯中文」时留下，
    // 含英文/数字的后端原话被**整个丢掉** ⇒ tooltip 里看不到后端到底说了什么，
    // 真实原因在界面上无处可查（只落在未开启环境变量就不写任何内容的 GuiTrace
    // 里）。现在一律保留原文，排在可读文案后面；截断到 1000 字符，与
    // backendFailed 写 GuiTrace 的截断口径一致。
    if (technicalMessage.trimmed().isEmpty())
        return displayMessage;
    if (technicalMessage == displayMessage)
        return displayMessage;
    return displayMessage + QStringLiteral("\n原始信息：") + technicalMessage.left(1000);
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

namespace OcrTableToolGui {

int remainingRecognitionBudgetSeconds(qint64 usedMilliseconds)
{
    // findings B1：自动重试用剩余预算，不允许再开一轮完整时长。
    // 已用时长里已经包含了第一次请求的进程冷启与模型构建，所以这里算出来的
    // 秒数就是「这一次自动重试最多还能跑多久」；重试间隔先扣掉，
    // 保证 已用 + 间隔 + 重试预算 ≤ 一次识别预算。
    const qint64 remainingMilliseconds =
        qint64(kRecognitionWallBudgetMilliseconds)
        - qMax<qint64>(0, usedMilliseconds)
        - qint64(kRetryDelayMilliseconds);
    if (remainingMilliseconds <= 0)
        return 0;
    return int(remainingMilliseconds / 1000);
}

} // namespace OcrTableToolGui

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
    , m_startRequestedWhileStopping(false)
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
    // 2026-10-23：必须**先**停掉后端进程再让成员析构。
    // MainWindow 是栈对象，析构时值成员先于子对象被销毁；而 BackendRunner 是
    // 本窗口的子 QObject，它析构里的 kill()+waitForFinished() 会同步触发
    // finished() -> processFinished() -> emit requestSucceeded()，
    // MainWindow::backendSucceeded() 随后写 m_pendingRecognitionResponse ——
    // 那个成员此时已经析构，use-after-free，Windows 报 0xC0000374 堆损坏，
    // 表现为"识别过一次之后点 X 关闭必崩"（4/4 复现）。
    // 在这里显式调用 shutdown()，把时点提到成员析构之前。
    if (m_backend)
        m_backend->shutdown();
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
    // 2026-10-30（lead 复查）：只记住**用户选的**那张图；框选后重新 loadImage 传进来的是
    // 我们自己生成的 crop-*.png（AppData 临时文件），不能拿它当导出默认名。
    if (m_ownedCroppedImagePath.isEmpty()
        || m_sourceImagePath != m_ownedCroppedImagePath) {
        m_userVisibleImagePath = m_sourceImagePath;
    }
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
    m_structureCertificate = QJsonObject();
    m_pendingRecognitionResponse = QJsonObject();
    m_pendingRecognitionError.clear();
    m_pendingRecognitionErrorCode.clear();
    // 换图 ⇒ 上一次失败的原因不再相关（新图片会有自己的失败原因）。
    m_lastRecognitionFailureDisplay.clear();
    m_lastRecognitionFailureMessage.clear();
    m_recognitionDisplayRequested = false;
    m_recognitionActive = false;
    m_recognitionRetryCount = 0;
    m_startRequestedWhileStopping = false;
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
    if (m_sourceImagePath.isEmpty()) {
        // 按钮在没有图片时是禁用的，正常点不到；留一条可读提示
        // 以免自动触发路径（测试钩子 / 拖放）里出现"点了什么都没发生"。
        m_statusLabel->setText(QStringLiteral("请先打开一张表格图片，再开始识别。"));
        return;
    }

    m_recognitionDisplayRequested = true;
    if (!m_pendingRecognitionResponse.isEmpty()) {
        const QJsonObject response = m_pendingRecognitionResponse;
        m_pendingRecognitionResponse = QJsonObject();
        publishRecognitionResult(response);
        return;
    }
    if (!m_pendingRecognitionError.isEmpty()) {
        // 2026-11-05 修正（缺陷：后台识别失败的真实原因被丢弃）：原来这里
        // **第一件事**就是 clear() 掉原因、然后直接重新识别 —— 用户点
        // 「开始识别」既看不到上一次为什么失败，界面上也没有任何地方能查
        // （原因只在 GuiTrace 里，而未设环境变量时它一条都不写）。
        // 现在：先把上一次失败的真实原因摆出来（状态栏 + tooltip + 结果区
        // 提示条），再开始新一轮识别。
        const QString previousMessage = m_pendingRecognitionError;
        const QString previousCode = m_pendingRecognitionErrorCode;
        const QString previousDisplay = m_lastRecognitionFailureDisplay.isEmpty()
            ? localizedBackendError(previousCode, previousMessage)
            : m_lastRecognitionFailureDisplay;
        m_pendingRecognitionError.clear();
        m_pendingRecognitionErrorCode.clear();
        m_recognitionRetryCount = 0;
        m_statusLabel->setText(QStringLiteral("上一次识别未完成：%1 正在重新识别…")
                                   .arg(previousDisplay));
        m_statusLabel->setToolTip(localizedBackendDetail(previousMessage, previousDisplay));
        showRecognitionFailureNotice();
        startRecognitionRequest();
        return;
    }
    if (m_backend->isRunning()) {
        // findings/61 F5：这里过去只有"正在识别"一种状态有反馈，另外两种状态
        // 会把用户的点击**静默吞掉**（用户点了没反应，或者更糟：停止完成后
        // 状态栏改写成"识别已取消"，等于告诉用户"你取消了"）。
        // 现在按状态分开提示，并且不丢点击意图：
        if (m_backend->isStopping()) {
            // 停止窗口：进程还在回收。记住意图，等停止完成（backendFailed 收到
            // 取消/超时上报时）立刻补跑，而不是让用户再点一次。
            m_startRequestedWhileStopping = true;
            m_statusLabel->setText(
                QStringLiteral("识别组件正在结束上一个任务，稍后会自动开始识别…"));
            m_readyStateLabel->setText(QStringLiteral("正在处理"));
            updateActions();
            return;
        }
        if (m_recognitionActive) {
            setBusy(true, QStringLiteral("正在精确识别，请稍等…"));
            return;
        }
        // 识别组件被其它任务占用（例如导出 XLSX）。
        m_statusLabel->setText(
            QStringLiteral("识别组件正在处理另一项任务，请等它完成后再点开始识别。"));
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

void MainWindow::startRecognitionRequest(int budgetSeconds)
{
    if (m_sourceImagePath.isEmpty()) {
        m_statusLabel->setText(QStringLiteral("请先打开一张表格图片，再开始识别。"));
        return;
    }
    if (m_backend->isRunning()) {
        // 同样不允许静默返回：走到这里说明点击被"组件忙"挡住了（findings/61 F5）。
        m_statusLabel->setText(
            m_backend->isStopping()
                ? QStringLiteral("识别组件正在结束上一个任务，稍后会自动开始识别…")
                : QStringLiteral("识别组件正在处理另一项任务，请等它完成后再点开始识别。"));
        if (m_backend->isStopping())
            m_startRequestedWhileStopping = true;
        return;
    }
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
    m_pendingRecognitionErrorCode.clear();
    m_recognitionActive = true;
    m_startRequestedWhileStopping = false;
    // 一次识别的预算从**首个**请求发出时开始计；自动重试沿用同一个计时器，
    // 所以只有全新一轮（重试计数为 0）才重置（findings B1）。
    if (m_recognitionRetryCount == 0)
        m_recognitionBudgetTimer.start();
    QJsonObject requestTrace;
    requestTrace.insert(QStringLiteral("file_name"),
                        QFileInfo(m_sourceImagePath).fileName());
    requestTrace.insert(QStringLiteral("display_requested"),
                        m_recognitionDisplayRequested);
    requestTrace.insert(QStringLiteral("input_rectified"),
                        m_recognitionSourceIsRectified);
    requestTrace.insert(QStringLiteral("budget_seconds"), budgetSeconds);
    requestTrace.insert(QStringLiteral("attempt"), m_recognitionRetryCount + 1);
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
                         selectedTableRegion,
                         budgetSeconds);
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
                          item ? item->data(Qt::UserRole + 1).toBool() : false,
                          item ? item->data(Qt::UserRole + 3).toBool() : false);
        }
    }
    return table;
}

QString MainWindow::suggestedOutputPath(const QString &suffix) const
{
    // 2026-10-30（lead 复查）：优先用**用户可见的原图路径**。框选/拍照后
    // m_sourceImagePath 会变成 AppData 里的 crop-<uuid>.png，用它拼默认名会让
    // 客户在「文档/图片」里找不到导出文件（实测默认落到
    // C:\Users\…\AppData\Local\AP\OcrTableTool\crop-<uuid>.xlsx）。
    const QString visible = !m_userVisibleImagePath.isEmpty()
                                ? m_userVisibleImagePath
                                : (m_sourceImagePath.isEmpty() ? m_imagePath
                                                               : m_sourceImagePath);
    const QFileInfo source(visible);
    if (source.fileName().isEmpty())
        return QStringLiteral("识别结果") + suffix;
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
    // 2026-11-05：导出的 CSV 在文件末尾标出了待确认单元格的位置（见
    // CsvExporter::encode），这里也把同样的信息说一遍 —— 客户只看状态栏时
    // 也不会以为这份 CSV 是"全部已确认"的。
    const int pending = pendingReviewCount();
    if (pending > 0) {
        m_statusLabel->setText(
            QStringLiteral("CSV 已保存：%1（%2 项待确认，已在文件末尾标出位置，请对照原图核对后再使用）")
                .arg(QDir::toNativeSeparators(path))
                .arg(pending));
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
    // findings/61 F6：把识别阶段严格校验通过的结构证书一起交给后端，
    // 后端 write_xlsx 才能用它校准列宽与物理边界（GUI 不裁剪、不覆盖该字段）。
    if (!path.isEmpty())
        m_backend->exportXlsx(path, currentTable(), m_spans, m_structureCertificate);
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
    // 行列结构被人工改过之后，原证书的边界与当前网格不再对应，
    // 继续带到导出会被后端判为非法边界（findings/61 F6 的"宁可不发"）。
    m_structureCertificate = QJsonObject();
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
        m_pendingRecognitionErrorCode.clear();
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
    // 2026-11-05：新结果发布成功 ⇒ 上一次识别失败的提示条不再适用。
    // （本函数里任何校验失败都会走 backendFailed，由它写回新的失败原因。）
    m_lastRecognitionFailureDisplay.clear();
    m_lastRecognitionFailureMessage.clear();
    QString error;
    TableData table = TableData::fromBackendJson(response, &error);
    if (!error.isEmpty()) {
        // 2026-11-10（用户实测反馈）：**绝不因契约细节丢掉整张表**。
        // 用户要求："任何情况一定要先传出数据出来，不要出现识别失败的情况。"
        // 严格校验失败时改用宽松解析；只要能读出单元格就降级发布
        // （全部标黄、禁止认证导出），把已识别的内容完整展示给用户。
        QString lenientNotice;
        TableData lenient = TableData::fromBackendJsonLenient(response, &lenientNotice);
        if (lenient.rowCount() > 0 && lenient.columnCount() > 0) {
            m_structureCertificate = QJsonObject();
            m_publicationBlocked = true;
            m_spans = QJsonArray();
            showTable(lenient, m_spans);
            QJsonObject fallbackTrace;
            fallbackTrace.insert(QStringLiteral("reason"), error);
            fallbackTrace.insert(QStringLiteral("rows"), lenient.rowCount());
            fallbackTrace.insert(QStringLiteral("columns"), lenient.columnCount());
            GuiTrace::write(QStringLiteral("result_published_lenient_fallback"), fallbackTrace);
            m_lastRecognitionFailureDisplay.clear();
            m_lastRecognitionFailureMessage.clear();
            m_recognitionRetryCount = 0;
            m_recognizeButton->setText(QStringLiteral("重新识别"));
            m_reviewNotice->setText(lenientNotice);
            m_statusLabel->setText(
                QStringLiteral("识别完成（结果未通过完整性校验，已降级展示，请逐格核对）"));
            return;
        }
        backendFailed(QStringLiteral("recognize"), error,
                      QStringLiteral("INVALID_RESPONSE"), false);
        return;
    }
    const QJsonValue publicationBlockedValue = response.value(QStringLiteral("publication_blocked"));
    if (!publicationBlockedValue.isBool()) {
        backendFailed(QStringLiteral("recognize"),
                      QStringLiteral("识别结果缺少风险发布状态。"),
                      QStringLiteral("INVALID_RESPONSE"), false);
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
                      QStringLiteral("识别结果的风险状态前后不一致。"),
                      QStringLiteral("INVALID_RESPONSE"), false);
        return;
    }
    const QJsonValue structureVerifiedValue = response.value(QStringLiteral("structure_verified"));
    const QJsonValue structureCertificateValue = response.value(QStringLiteral("structure_certificate"));
    const bool hasStructureCertificate = !structureCertificateValue.isUndefined()
        && !structureCertificateValue.isNull();
    if (!structureVerifiedValue.isBool()) {
        backendFailed(QStringLiteral("recognize"),
                      QStringLiteral("识别结果缺少表格结构状态。"),
                      QStringLiteral("INVALID_RESPONSE"), false);
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
            backendFailed(QStringLiteral("recognize"), error,
                      QStringLiteral("INVALID_RESPONSE"), false);
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
                          QStringLiteral("结构未确认的结果包含不安全的合并关系或发布状态。"),
                          QStringLiteral("INVALID_RESPONSE"), false);
            return;
        }
    } else {
        if (!structureCertificateValue.isObject()) {
            backendFailed(QStringLiteral("recognize"),
                          QStringLiteral("已确认的表格结构缺少校验证书。"),
                          QStringLiteral("INVALID_RESPONSE"), false);
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
                          QStringLiteral("表格结构校验证书与识别结果不一致。"),
                          QStringLiteral("INVALID_RESPONSE"), false);
            return;
        }
    }
    if (recognitionState == QStringLiteral("verified")) {
        for (int row = 0; row < table.rowCount(); ++row) {
            for (int column = 0; column < table.columnCount(); ++column) {
                if (table.needsReview(row, column)) {
                    backendFailed(QStringLiteral("recognize"),
                                  QStringLiteral("已确认结果中仍存在待核对单元格。"),
                                  QStringLiteral("INVALID_RESPONSE"), false);
                    return;
                }
            }
        }
    }
    // findings/61 F6：校验通过的证书必须留到导出。原先它只被校验、不留存，
    // 导出请求里没有它 ⇒ 后端 write_xlsx 的证书分支（列宽/边界校准）永远跳过。
    // 未认证（结构未确认）的结果一律清空，避免把上一张表的证书带出去。
    m_structureCertificate = structureVerified ? structureCertificateValue.toObject()
                                               : QJsonObject();
    m_publicationBlocked = publicationBlocked;
    m_spans = validatedSpans;
    showTable(table, m_spans);
    QJsonObject publishedTrace;
    publishedTrace.insert(QStringLiteral("rows"), table.rowCount());
    publishedTrace.insert(QStringLiteral("columns"), table.columnCount());
    publishedTrace.insert(QStringLiteral("span_count"), m_spans.size());
    publishedTrace.insert(QStringLiteral("publication_blocked"), m_publicationBlocked);
    // review_cell_count 是界面自己的待确认格数（UserRole+1 或 UserRole+2），
    // 与后端同名字段口径不同（后端只认 needs_review）。两个口径各写一个
    // 名字，避免验收时把 GUI 的数当成后端的数读。
    publishedTrace.insert(QStringLiteral("review_cell_count"), pendingReviewCount());
    publishedTrace.insert(QStringLiteral("ui_pending_count"), pendingReviewCount());
    publishedTrace.insert(QStringLiteral("backend_review_cell_count"),
                          response.value(QStringLiteral("review_cell_count")).toInt());
    publishedTrace.insert(QStringLiteral("canonical_result_hash"),
                          canonicalDisplayedResultHash(table,
                                                       m_spans,
                                                       m_publicationBlocked));
    publishedTrace.insert(QStringLiteral("unsafe_spans_discarded"),
                          unsafeSpansDiscarded);
    GuiTrace::write(QStringLiteral("result_published"), publishedTrace);
    m_recognitionRetryCount = 0;
    m_recognizeButton->setText(QStringLiteral("重新识别"));
    // findings F4：状态栏「用时」过去取 worker_wall_seconds —— 那是 worker 从进程
    // 启动到退出（含解释器启动、模型卸载、进程回收）的全生命周期，比识别组件自己
    // 计时的 elapsed_seconds 大，和"单次识别 60 秒墙钟"这条契约**不能相减**，
    // 显示的秒数也系统性偏大。现在优先显示 elapsed_seconds（后端口径），
    // 退出开销单独在 tooltip 里点明；识别组件没上报自身计时（桩后端 / 老版本）时，
    // 退回「worker 全生命周期 − 响应到退出那段」的估算。
    const double workerWallSeconds = response.value(QStringLiteral("worker_wall_seconds")).toDouble(-1.0);
    const double responseExitSeconds = qMax(0.0, response.value(QStringLiteral("response_exit_seconds")).toDouble(0.0));
    const QJsonValue elapsedSecondsValue = response.value(QStringLiteral("elapsed_seconds"));
    const bool elapsedFromWorker = !elapsedSecondsValue.isDouble();
    const double elapsedSeconds = elapsedFromWorker
        ? qMax(0.0, workerWallSeconds >= 0.0 ? workerWallSeconds - responseExitSeconds : 0.0)
        : response.value(QStringLiteral("elapsed_seconds")).toDouble();
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
    // 用时口径（findings F4）：状态栏上的数字是识别组件自身计时，可与单次识别
    // 60 秒上限直接比较；进程启动 / 退出与模型卸载的开销单独在这里交代，
    // 免得验收时把两个不同口径的秒数拿去相减。
    if (elapsedFromWorker) {
        detailTexts.append(
            QStringLiteral("用时口径：识别组件未上报自身计时，显示的 %1 秒是识别进程全生命周期减去退出开销后的估算。")
                .arg(elapsedSeconds, 0, 'f', 1));
    } else if (responseExitSeconds > 0.0) {
        detailTexts.append(
            QStringLiteral("用时口径：%1 秒为识别组件自身计时，可与单次识别 60 秒上限直接比较；另有 %2 秒用于进程启动与退出、模型卸载。")
                .arg(elapsedSeconds, 0, 'f', 1)
                .arg(responseExitSeconds, 0, 'f', 1));
    }
    const QString detailText = detailTexts.join(QStringLiteral("\n"));
    m_statusLabel->setText(statusText);
    m_statusLabel->setToolTip(detailText.isEmpty() ? statusText : detailText);
    m_reviewNotice->setToolTip(detailText);
    m_resultStack->setCurrentWidget(m_table);
    const QString rectifiedPath = response.value(QStringLiteral("rectified_image")).toString();
    if (!OcrTableToolGui::isSafeLocalImagePath(rectifiedPath)) {
        // 路径不可信（UNC、上级目录或为空）：不打开它，只在状态栏说明。
        const QString warning = rectifiedPath.trimmed().isEmpty()
            ? QStringLiteral("识别组件没有返回矫正图，已跳过矫正图预览。")
            : QStringLiteral("识别组件返回的矫正图路径不是本地安全路径，已忽略该图片。");
        m_statusLabel->setText(statusText + QStringLiteral(" · ") + warning);
        m_statusLabel->setToolTip(detailText.isEmpty()
                                      ? warning
                                      : detailText + QStringLiteral("\n") + warning);
    } else {
        const QImage rectified(rectifiedPath);
        if (!rectified.isNull()) {
            clearOwnedRectifiedImage();
            m_ownedRectifiedImagePath = QFileInfo(rectifiedPath).absoluteFilePath();
            m_rectifiedPreview->setImage(rectified);
            m_previewTabs->setCurrentIndex(1);
        }
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

void MainWindow::backendFailed(const QString &action, const QString &message,
                               const QString &errorCode, bool retryable)
{
    QJsonObject failureTrace;
    failureTrace.insert(QStringLiteral("action"), action);
    failureTrace.insert(QStringLiteral("message"), message.left(1000));
    failureTrace.insert(QStringLiteral("error_code"), errorCode);
    failureTrace.insert(QStringLiteral("retryable"), retryable);
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
        // findings/61 F5：用户在"正在停止"窗口里点的「开始识别」不能被吞掉。
        // 停止完成时后端上报的是"操作已取消"/"操作超时"，而这一刻 m_stopping
        // 已复位（isRunning() == false）—— 正是补跑时机。顺带避免状态栏把
        // 用户的"重新开始"改写成"识别已取消"。
        if (m_startRequestedWhileStopping && !m_backend->isRunning()) {
            m_startRequestedWhileStopping = false;
            m_restartRecognitionAfterCancel = false;
            m_pendingRecognitionResponse = QJsonObject();
            m_pendingRecognitionError.clear();
            m_pendingRecognitionErrorCode.clear();
            // 这是用户新发起的一轮，预算重新从 0 计（重试计数归零）。
            m_recognitionRetryCount = 0;
            setBusy(false);
            m_statusLabel->setText(
                QStringLiteral("识别组件已就绪，正在按刚才的点击重新识别…"));
            GuiTrace::write(QStringLiteral("recognition_resumed_after_stop"));
            const QString resumeSource = m_sourceImagePath;
            QTimer::singleShot(0, this, [this, resumeSource]() {
                if (m_sourceImagePath == resumeSource && !m_backend->isRunning())
                    startRecognitionRequest();
            });
            return;
        }
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
        // 2026-10-23 第 50 轮修正（findings/109 M1）：原判定只看 message 是否以
        // 「可用内存不足」开头，但后端**只对原生 OOM** 改写 message
        // （ocr_backend.py:62602-62608）；Python 的 MemoryError 支（:62545-62546）
        // 透的是空串/原文 ⇒ 那一支落通用兜底，用户看不到内存不足提示。
        // 改为**优先按 error_code 精确匹配**（公开 SDK 的 cameraocrclient.cpp:820 同款做法），
        // 前缀判断保留为兼容（后端文案变化时不至于失效）。
        const bool insufficientMemory =
            errorCode == QStringLiteral("INSUFFICIENT_MEMORY")
            || message.startsWith(QStringLiteral("可用内存不足"));
        // 2026-10-22 修正：原来**任何**失败都会自动重试一次，包括"识别模型
        // 文件缺失或损坏"、协议不兼容这类必然再失败的错误 —— 用户要白等两轮
        // 完整失败（每轮重载数 GB OpenVINO 工作集）才看到提示，平板上多耗
        // 一分钟以上。现在只有后端明确标记 retryable（或没有结构化错误码，
        // 例如进程崩溃、传输层失败）时才重试。
        const bool retryAllowed = errorCode.isEmpty() || retryable;
        // 2026-10-28 修正（findings B1）：自动重试**不允许**再开一轮完整时长。
        // 原先每轮都冷启进程、各跑满一次预算 ⇒ 一次识别实际最长 ~120 秒，
        // 与对外"单次识别 60 秒墙钟"的口径不符。这里按首个请求发出后的
        // 墙钟已用时长算剩余预算：够用才重试，且只把**剩余秒数**传给后端与
        // 父进程计时器（backendrunner.cpp 的 recognize/timeout）。
        // 口径：预算常量是界面侧镜像，不改后端契约、不改首次请求（仍传 0）。
        const qint64 usedMilliseconds = m_recognitionBudgetTimer.isValid()
            ? m_recognitionBudgetTimer.elapsed()
            : qint64(kRecognitionWallBudgetMilliseconds);
        const int remainingBudgetSeconds =
            OcrTableToolGui::remainingRecognitionBudgetSeconds(usedMilliseconds);
        const bool retryBudgetExhausted =
            remainingBudgetSeconds < kMinimumRetryBudgetSeconds;
        // 「本该重试、但预算不允许」——只有这种情形才需要在提示里说明没重试。
        const bool retrySkippedByBudget = retryAllowed
            && !insufficientMemory
            && m_recognitionRetryCount < 1
            && retryBudgetExhausted
            && !m_sourceImagePath.isEmpty();
        if (retrySkippedByBudget) {
            QJsonObject budgetTrace;
            budgetTrace.insert(QStringLiteral("action"), action);
            budgetTrace.insert(QStringLiteral("used_milliseconds"),
                               static_cast<double>(usedMilliseconds));
            budgetTrace.insert(QStringLiteral("remaining_seconds"), remainingBudgetSeconds);
            budgetTrace.insert(QStringLiteral("skipped_reason"),
                               QStringLiteral("budget_exhausted"));
            GuiTrace::write(QStringLiteral("recognition_retry_skipped"), budgetTrace);
        }
        if (!insufficientMemory
            && m_recognitionRetryCount < 1
            && retryAllowed
            && !m_sourceImagePath.isEmpty()
            && !retryBudgetExhausted) {
            ++m_recognitionRetryCount;
            m_pendingRecognitionResponse = QJsonObject();
            m_pendingRecognitionError.clear();
            m_pendingRecognitionErrorCode.clear();
            setBusy(false);
            const QString retrySource = m_sourceImagePath;
            m_statusLabel->setText(QStringLiteral("首次识别未完成，正在自动恢复并重试…"));
            QJsonObject retryTrace;
            retryTrace.insert(QStringLiteral("budget_seconds"), remainingBudgetSeconds);
            retryTrace.insert(QStringLiteral("used_milliseconds"),
                              static_cast<double>(usedMilliseconds));
            GuiTrace::write(QStringLiteral("recognition_retry_scheduled"), retryTrace);
            QTimer::singleShot(kRetryDelayMilliseconds, this,
                               [this, retrySource, remainingBudgetSeconds]() {
                if (m_sourceImagePath == retrySource && !m_backend->isRunning())
                    startRecognitionRequest(remainingBudgetSeconds);
            });
            return;
        }
        if (!m_recognitionDisplayRequested) {
            m_pendingRecognitionResponse = QJsonObject();
            m_pendingRecognitionError = message;
            m_pendingRecognitionErrorCode = errorCode;
            // 2026-11-05 修正（缺陷：后台失败的真实原因被丢弃）：原来这里只写
            // 「后台识别未完成，点击开始识别查看原因」，而用户点「开始识别」后
            // 原因被 clear() 掉、直接重新识别 ⇒ 真实原因只留在 GuiTrace，
            // 未设环境变量时一条都不写，生产环境无处可查。
            // 现在：状态栏直接给出可行动的原因，tooltip 给出后端原文，
            // 结果区上方的提示条持续显示（直到下一次成功结果或换图）。
            const QString failureDisplay = insufficientMemory
                ? QStringLiteral("可用内存不足，已暂停识别；释放内存后点击开始识别重试")
                : localizedBackendError(errorCode, message);
            m_lastRecognitionFailureDisplay = failureDisplay;
            m_lastRecognitionFailureMessage = message;
            setBusy(false);
            m_statusLabel->setText(QStringLiteral("后台识别未完成：%1（点击开始识别重试）")
                                       .arg(failureDisplay));
            m_statusLabel->setToolTip(localizedBackendDetail(message, failureDisplay));
            showRecognitionFailureNotice();
            return;
        }
        clearPublishedResult(QStringLiteral("本次识别未生成可导出结果"));
        setBusy(false);
        // 2026-10-23 修正（findings/68 F2）：前台路径原先对"可用内存不足"也走
        // localizedBackendError()，该函数没有内存不足分支 ⇒ 落到通用兜底
        // "识别失败，已保留原始错误信息"，后端那条可操作建议（关闭占内存程序 /
        // 用框选区域减少识别量）只落在 tooltip 里。前台与后台（:1524）保持一致。
        // 2026-11-05：改成按 error_code 精确分支（新增重载），关键词匹配兜底。
        const QString displayMessage = insufficientMemory
            ? QStringLiteral("可用内存不足，已中止识别；释放内存后点击开始识别重试")
            : localizedBackendError(errorCode, message);
        m_lastRecognitionFailureDisplay = displayMessage;
        m_lastRecognitionFailureMessage = message;
        m_statusLabel->setText(QStringLiteral("识别未完成：%1 可重新识别或重新框选表格。")
                                   .arg(displayMessage)
                               + (retrySkippedByBudget
                                      ? QStringLiteral("（本次识别已用完 60 秒等待预算，未再自动重试。）")
                                      : QString()));
        m_statusLabel->setToolTip(localizedBackendDetail(message, displayMessage));
        showRecognitionFailureNotice();
        return;
    }
    setBusy(false);
    const QString displayMessage = localizedBackendError(errorCode, message);
    if (action == QStringLiteral("warmup")) {
        m_statusLabel->setText(QStringLiteral("识别组件准备失败：%1").arg(displayMessage));
        return;
    }
    if (action == QStringLiteral("export_xlsx")) {
        // 导出失败要单独提示，不能说成"识别未完成"（2026-10-22 修正）。
        const QString exportMessage = localizedExportError(errorCode, message);
        m_statusLabel->setText(QStringLiteral("导出失败：%1").arg(exportMessage));
        m_statusLabel->setToolTip(localizedBackendDetail(message, exportMessage));
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
    m_structureCertificate = QJsonObject();
    m_resultStack->setCurrentIndex(0);
    // 2026-11-05：失败提示条不能跟着结果一起被清掉 —— 上一次识别失败的**真实
    // 原因**必须一直可见，直到新结果发布成功或用户换图。
    if (m_lastRecognitionFailureDisplay.isEmpty())
        m_reviewNotice->hide();
    else
        showRecognitionFailureNotice();
    if (!summary.isEmpty())
        m_resultSummaryLabel->setText(summary);
    m_loadingTable = false;
    updateActions();
}

// 2026-11-05（缺陷：后台识别失败的真实原因被丢弃）：把最近一次失败原因显示成
// 结果区上方的提示条 —— 状态栏会被"正在识别…"覆盖，提示条不会。
void MainWindow::showRecognitionFailureNotice()
{
    if (m_lastRecognitionFailureDisplay.isEmpty()) {
        m_reviewNotice->hide();
        return;
    }
    m_reviewNotice->setText(
        QStringLiteral("上一次识别未完成：%1").arg(m_lastRecognitionFailureDisplay));
    m_reviewNotice->setToolTip(
        localizedBackendDetail(m_lastRecognitionFailureMessage,
                               m_lastRecognitionFailureDisplay));
    m_reviewNotice->show();
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
            // 0.77 / 0.86 是各识别路径硬写的类别哨兵，不是实测置信度。
            // 后端用可选字段 confidence_sentinel 显式表达这件事，界面只消费
            // 这个字段，不再自己拿 0.78 去和哨兵比大小。
            const bool confidenceSentinel = table.confidenceSentinel(row, column);
            const bool requiresAttention = table.requiresAttention(row, column);
            item->setData(Qt::UserRole, confidence);
            item->setData(Qt::UserRole + 1, needsReview);
            // Qt::UserRole + 3：单元格是不是类别哨兵。只读不改显示语义，
            // 导出时还要把它带回后端，否则导出会重新按裸 confidence 猜复核。
            item->setData(Qt::UserRole + 3, confidenceSentinel);
            // Qt::UserRole + 2：界面自己的"低置信待确认"标记。
            // 2026-10-22 修正：低置信格（confidence < 0.78 且非空）原来只刷
            // 标记、只加局部 reviewCount，却**不写任何计数位**，于是
            // pendingReviewCount() 数不到它 —— 两处口径不一致：提示条说
            // "N 项待确认"，状态栏却说"人工核对完成，可以正式导出"，而且
            // 用户手工改一格后提示条忽隐忽现。
            // 这里单独用 +2 承载界面标记，UserRole+1 仍然是后端的 needs_review
            // 语义（导出文件的复核标记由它决定，不能混）。
            item->setData(Qt::UserRole + 2, false);
            if (needsReview) {
                item->setToolTip(QStringLiteral("待确认：请对照原图核对文字、数字、符号和位置"));
                item->setIcon(reviewMarkerIcon());
                ++reviewCount;
            } else if (confidenceSentinel) {
                // 哨兵值不是置信度：不能显示成百分比，也不能算待确认。
                item->setToolTip(QStringLiteral("置信度未知（未取得双模型一致确认）"));
            } else {
                item->setToolTip(QStringLiteral("OCR 置信度：%1%").arg(qRound(confidence * 100.0)));
            }
            // 只有实测置信度真的偏低才补黄格；哨兵格不补。
            if (requiresAttention && !needsReview) {
                item->setToolTip(QStringLiteral("待确认：请对照原图核对文字、数字、符号和位置"));
                item->setIcon(reviewMarkerIcon());
                item->setData(Qt::UserRole + 2, true);
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
        item->setData(Qt::UserRole + 2, false);
        // 人工改过的单元格置信度按 1.0 计，不再是类别哨兵。
        item->setData(Qt::UserRole + 3, false);
        item->setBackground(QBrush());
        // 2026-10-30（lead 复查）：原来只清了背景与 tooltip，没清**图标**。
        // showTable() 会给待核对格 setIcon(reviewMarkerIcon())，于是人工改完后
        // 黄点还留着，摘要却已变成「0 项待确认 / 人工核对完成，可以正式导出」——
        // 界面自相矛盾。
        item->setIcon(QIcon());
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
            if (item && (item->data(Qt::UserRole + 1).toBool()
                          || item->data(Qt::UserRole + 2).toBool()))
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
    // findings/61 F5：组件忙时按钮被禁用，此时点击根本无法到达槽函数，
    // 用户同样"点了没反应"。用提示说明原因，避免把"按钮灰了"读成软件卡死。
    m_recognizeButton->setToolTip(busy && !hiddenRecognition
        ? QStringLiteral("识别组件正在处理另一项任务，完成前不能开始新的识别")
        : QString());
}
