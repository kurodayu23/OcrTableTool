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
            if (!textValue.isString()
                || !confidenceValue.isDouble()
                || !qIsFinite(confidence)
                || confidence < 0.0
                || confidence > 1.0
                || !reviewValue.isBool()) {
                return fail(QStringLiteral("识别结果包含无效的单元格字段。"));
            }
            // 非空低置信内容只有在后端明确标为人工核对时才允许展示。
            // 该阈值必须与界面核对语义一致；静默接受会破坏证据与确认的边界。
            if (!textValue.toString().trimmed().isEmpty()
                && confidence < 0.78
                && !reviewValue.toBool()) {
                return fail(QStringLiteral("识别结果包含未标黄的低置信度单元格。"));
            }
            table.setCell(row,
                          column,
                          textValue.toString(),
                          confidence,
                          reviewValue.toBool());
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

void TableData::setCell(int row,
                        int column,
                        const QString &text,
                        double confidence,
                        bool needsReview)
{
    if (row < 0 || column < 0)
        return;
    // 结构编辑可能访问新的末尾单元格，必须通过 ensureSize() 同时扩展行列，
    // 防止产生长度不一致的非矩形行。
    ensureSize(row + 1, column + 1);
    m_rows[row][column] = TableCell(text, confidence, needsReview);
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
