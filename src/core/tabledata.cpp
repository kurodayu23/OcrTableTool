#include "tabledata.h"

#include <QtGlobal>
#include <QJsonArray>
#include <QJsonValue>

TableData TableData::fromRows(const QVector<QStringList> &rows)
{
    TableData table;
    int columns = 0;
    for (int row = 0; row < rows.size(); ++row)
        columns = qMax(columns, rows.at(row).size());

    table.ensureSize(rows.size(), columns);
    for (int row = 0; row < rows.size(); ++row) {
        const QStringList values = rows.at(row);
        for (int column = 0; column < values.size(); ++column)
            table.m_rows[row][column] = TableCell(values.at(column), 1.0);
    }
    return table;
}

// 2026-11-10（用户实测反馈）：严格协议校验失败时**不再丢整表**。
// 用户明确要求："图片要是有缺陷的话，先传出来再说；任何情况一定要先传出数据出来，
// 不要出现识别失败的情况。"
// 本函数只要求 `cells` 是非空二维数组；列数按最长行对齐，缺格补空；
// 全部单元格标记 needsReview=true（界面标黄、禁止认证导出），但**数据全部可见可导出**。
// noticeMessage 回填人类可读的降级原因，供界面提示条展示。
TableData TableData::fromBackendJsonLenient(const QJsonObject &object, QString *noticeMessage)
{
    const QJsonValue cellsValue = object.value(QStringLiteral("cells"));
    if (!cellsValue.isArray()) {
        if (noticeMessage)
            *noticeMessage = QStringLiteral("响应里没有表格数据。");
        return TableData();
    }
    const QJsonArray rows = cellsValue.toArray();
    if (rows.isEmpty()) {
        if (noticeMessage)
            *noticeMessage = QStringLiteral("响应里的表格是空的。");
        return TableData();
    }
    // 列数取最长行，保证矩形。
    int columns = 0;
    for (int row = 0; row < rows.size(); ++row) {
        if (rows.at(row).isArray())
            columns = qMax(columns, rows.at(row).toArray().size());
    }
    if (columns <= 0) {
        if (noticeMessage)
            *noticeMessage = QStringLiteral("响应里的表格没有可用单元格。");
        return TableData();
    }
    if (columns > 128)
        columns = 128;
    int rowCount = rows.size();
    if (rowCount > 256)
        rowCount = 256;

    TableData data;
    data.m_columnCount = columns;   // 矩形存储：每行都有 columns 个单元格
    data.m_rows.reserve(rowCount);
    for (int row = 0; row < rowCount; ++row) {
        const QJsonArray source = rows.at(row).toArray();
        QVector<TableCell> cells;
        cells.reserve(columns);
        for (int column = 0; column < columns; ++column) {
            QString text;
            if (column < source.size()) {
                const QJsonValue value = source.at(column);
                if (value.isObject()) {
                    text = value.toObject().value(QStringLiteral("text")).toString();
                } else if (value.isString()) {
                    text = value.toString();
                }
            }
            TableCell cell;
            cell.text = text;
            cell.confidence = 0.0;
            cell.needsReview = true;      // 降级发布：全部标黄待核对
            cell.confidenceSentinel = false;
            cells.append(cell);
        }
        data.m_rows.append(cells);
    }
    if (noticeMessage) {
        *noticeMessage = QStringLiteral(
            "识别组件返回的结果未通过完整性校验，已按可见内容降级展示（%1 行 × %2 列，"
            "全部标为待核对）。请逐格核对后再使用。")
            .arg(data.rowCount())
            .arg(data.columnCount());
    }
    return data;
}

TableData TableData::fromBackendJson(const QJsonObject &object, QString *errorMessage)
{
    // Python 响应属于不可信协议输入。任何结构校验失败都返回空表；
    // 部分接受错误几何可能把数据错放到相邻行列，绝不能进入界面。
    TableData table;
    const auto fail = [errorMessage](const QString &message) {
        if (errorMessage)
            *errorMessage = message;
        return TableData();
    };
    if (object.value(QStringLiteral("protocol")).toInt() != 1) {
        return fail(QStringLiteral("识别组件协议版本不受支持。"));
    }
    if (object.value(QStringLiteral("status")).toString() != QStringLiteral("ok")) {
        return fail(object.value(QStringLiteral("message")).toString(QStringLiteral("识别失败。")));
    }

    const QJsonValue cellsValue = object.value(QStringLiteral("cells"));
    if (!cellsValue.isArray()) {
        return fail(QStringLiteral("识别结果缺少单元格网格。"));
    }

    const QJsonArray rows = cellsValue.toArray();
    const QJsonValue rowCountValue = object.value(QStringLiteral("rows"));
    const QJsonValue columnCountValue = object.value(QStringLiteral("columns"));
    if (!rowCountValue.isDouble() || !columnCountValue.isDouble())
        return fail(QStringLiteral("识别结果缺少网格行列数。"));
    const int expectedRows = rowCountValue.toInt(-1);
    const int expectedColumns = columnCountValue.toInt(-1);
    // 显式行列数必须与矩形数据完全一致。合理上限既覆盖支持的表格规模，
    // 也防止损坏响应触发界面无限制分配内存。
    if (rowCountValue.toDouble() != expectedRows
        || columnCountValue.toDouble() != expectedColumns
        || expectedRows <= 0
        || expectedColumns <= 0
        || expectedRows > 256
        || expectedColumns > 128
        || qint64(expectedRows) * qint64(expectedColumns) > 4096
        || rows.size() != expectedRows) {
        return fail(QStringLiteral("识别结果的网格行列数无效。"));
    }
    for (int row = 0; row < rows.size(); ++row) {
        if (!rows.at(row).isArray())
            return fail(QStringLiteral("识别结果包含格式无效的行。"));
        const QJsonArray columns = rows.at(row).toArray();
        if (columns.size() != expectedColumns)
            return fail(QStringLiteral("识别结果不是完整的矩形网格。"));
        for (int column = 0; column < columns.size(); ++column) {
            if (!columns.at(column).isObject())
                return fail(QStringLiteral("识别结果包含格式无效的单元格。"));
            const QJsonObject cell = columns.at(column).toObject();
            const QJsonValue textValue = cell.value(QStringLiteral("text"));
            const QJsonValue confidenceValue = cell.value(QStringLiteral("confidence"));
            const QJsonValue reviewValue = cell.value(QStringLiteral("needs_review"));
            const double confidence = confidenceValue.toDouble(-1.0);
            // 2026-11-05（lead 复查，数据全部丢失 P0）：后端把"这一格不可信"编码成
            // **负的 confidence**（-1.0 锚点格 / -2.0 判定为空），并同时置
            // needs_review=true。而这里原来一刀 `confidence < 0.0` 就整表拒收
            // ⇒ 客户看到的是一片空白，界面既不显示表也不允许导出。
            // 实测 FC-00024：38 格 confidence=-1.0，整表被丢弃。
            // 现在把负值当作"复核哨兵"接受：存进表格时夹到 0.0，并强制标黄；
            // 真正畸形的读数（NaN/Inf/超界/类型错）仍然照原样拒收。
            const bool negativeReviewSentinel = qIsFinite(confidence)
                && confidence < 0.0
                && confidence >= -2.0;
            if (!textValue.isString()
                || !confidenceValue.isDouble()
                || !qIsFinite(confidence)
                || (confidence < 0.0 && !negativeReviewSentinel)
                || confidence > 1.0
                || !reviewValue.isBool()) {
                return fail(QStringLiteral("识别结果包含无效的单元格字段。"));
            }
            // 低置信但未标黄的格子不整表拒收。
            //
            // 0.77 / 0.86 是各识别路径硬写的保守上限哨兵值，不是实测置信度。
            // 后端对此的结论是“只提示，不阻断”，并记录了实测代价：
            // 43 例里这一条单独就阻断了 14 张准确率 0.73~1.00 的正确表（含 4 张
            // 1.000），客户拿到的是完全对的表却被告禁止发布。
            //
            // 真实语料 47 例里 30 例（63.8%）因此完全不可用：表格不显示、导出禁用、
            // 失败格不可见。本函数不再重复后端已经做过的判断，交给
            // MainWindow::showTable 的既有降级路径：浅黄底 + 待确认提示 + 计入待复核格数。
            //
            // 字段格式校验（类型 / 有限性 / 0~1 区间 / 布尔）保留在上面的 if 里，
            // 真正畸形的响应仍然照常拒收；这里只放行格式合法、仅仅偏低的读数。
            //
            // confidence_sentinel 是后端新增的可选布尔字段：为真表示这格的
            // confidence 是 0.77 / 0.86 这类类别哨兵，不是实测置信度。老后端
            // 不写这个字段时按 false 处理（= 老行为），显示层再据此决定要不要
            // 补黄格。字段本身不参与上面的格式校验，缺失或类型不符一律按
            // false 处理，不改变既有的拒收口径。
            const bool confidenceSentinel = cell.value(QStringLiteral("confidence_sentinel"))
                                                .toBool(false);
            table.setCell(row,
                          column,
                          textValue.toString(),
                          // 负值是"复核哨兵"，落表时夹到 0.0：既满足 [0,1] 的显示约定，
                          // 又靠下面的 needsReview=true 让它保持黄色。
                          negativeReviewSentinel ? 0.0 : confidence,
                          negativeReviewSentinel ? true : reviewValue.toBool(),
                          confidenceSentinel);
        }
    }
    if (errorMessage)
        errorMessage->clear();
    return table;
}

int TableData::rowCount() const
{
    return m_rows.size();
}

int TableData::columnCount() const
{
    return m_columnCount;
}

QString TableData::cell(int row, int column) const
{
    if (row < 0 || row >= m_rows.size() || column < 0 || column >= m_columnCount)
        return QString();
    return m_rows.at(row).at(column).text;
}

double TableData::confidence(int row, int column) const
{
    if (row < 0 || row >= m_rows.size() || column < 0 || column >= m_columnCount)
        return 0.0;
    return m_rows.at(row).at(column).confidence;
}

bool TableData::needsReview(int row, int column) const
{
    if (row < 0 || row >= m_rows.size() || column < 0 || column >= m_columnCount)
        return false;
    return m_rows.at(row).at(column).needsReview;
}

bool TableData::confidenceSentinel(int row, int column) const
{
    if (row < 0 || row >= m_rows.size() || column < 0 || column >= m_columnCount)
        return false;
    return m_rows.at(row).at(column).confidenceSentinel;
}

bool TableData::requiresAttention(int row, int column) const
{
    if (row < 0 || row >= m_rows.size() || column < 0 || column >= m_columnCount)
        return false;
    const TableCell &cell = m_rows.at(row).at(column);
    if (cell.needsReview)
        return true;
    // 哨兵值是类别标记，不是实测置信度：不能拿它和 0.78 比大小，
    // 否则一张逐格全对的表会被整表标成待确认。
    if (cell.confidenceSentinel)
        return false;
    // 「有内容」的判据必须与公开 SDK 一致：那边用的是
    // !text.trimmed().isEmpty()，纯空白格不算内容、判 valid。
    // 这里原来用 !cell.text.isEmpty()，纯空白格（text 只有空格）会被当成
    // 有内容，低置信就刷黄格 —— 同一份数据界面标黄、SDK 判通过。
    // needsReview 的显式标记与 confidenceSentinel 的语义不受影响。
    return cell.confidence < 0.78 && !cell.text.trimmed().isEmpty();
}

void TableData::setCell(int row,
                        int column,
                        const QString &text,
                        double confidence,
                        bool needsReview,
                        bool confidenceSentinel)
{
    if (row < 0 || column < 0)
        return;
    // 结构编辑可能访问新的末尾单元格，必须通过 ensureSize() 同时扩展行列，
    // 防止产生长度不一致的非矩形行。
    ensureSize(row + 1, column + 1);
    m_rows[row][column] = TableCell(text, confidence, needsReview, confidenceSentinel);
}

void TableData::insertRow(int row)
{
    row = qBound(0, row, m_rows.size());
    m_rows.insert(row, QVector<TableCell>(m_columnCount));
}

void TableData::removeRow(int row)
{
    if (row >= 0 && row < m_rows.size())
        m_rows.remove(row);
}

void TableData::insertColumn(int column)
{
    column = qBound(0, column, m_columnCount);
    for (int row = 0; row < m_rows.size(); ++row)
        m_rows[row].insert(column, TableCell());
    ++m_columnCount;
}

void TableData::removeColumn(int column)
{
    if (column < 0 || column >= m_columnCount)
        return;
    for (int row = 0; row < m_rows.size(); ++row)
        m_rows[row].remove(column);
    --m_columnCount;
}

void TableData::ensureSize(int rows, int columns)
{
    if (columns > m_columnCount) {
        for (int row = 0; row < m_rows.size(); ++row)
            m_rows[row].resize(columns);
        m_columnCount = columns;
    }
    while (m_rows.size() < rows)
        m_rows.append(QVector<TableCell>(m_columnCount));
}
