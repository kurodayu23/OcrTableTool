#ifndef TABLEDATA_H
#define TABLEDATA_H

#include <QString>
#include <QStringList>
#include <QVector>
#include <QJsonObject>

// 后端解析、界面待核对高亮、人工编辑和 XLSX 导出共同遵守的单元格可信度契约。
// confidence 归一化到 [0, 1]；needsReview 是明确的质量结论，不能只根据显示文字推断。
struct TableCell
{
    TableCell(const QString &value = QString(), double score = 1.0, bool review = false)
        : text(value), confidence(score), needsReview(review)
    {
    }

    QString text;
    double confidence;
    bool needsReview;
};

class TableData
{
public:
    // fromRows 创建可信的本地/用户数据；fromBackendJson 是严格的协议边界，
    // 必须拒绝格式错误或不安全的 OCR 网格结果。
    static TableData fromRows(const QVector<QStringList> &rows);
    static TableData fromBackendJson(const QJsonObject &object, QString *errorMessage = 0);

    int rowCount() const;
    int columnCount() const;
    QString cell(int row, int column) const;
    double confidence(int row, int column) const;
    bool needsReview(int row, int column) const;

    void setCell(int row,
                 int column,
                 const QString &text,
                 double confidence = 1.0,
                 bool needsReview = false);
    void insertRow(int row);
    void removeRow(int row);
    void insertColumn(int column);
    void removeColumn(int column);

private:
    // 存储始终保持矩形：每行都有 m_columnCount 个单元格。
    // 表格显示、结构编辑和导出都依赖这个不变量。
    void ensureSize(int rows, int columns);

    QVector<QVector<TableCell> > m_rows;
    int m_columnCount = 0;
};

#endif // TABLEDATA_H
