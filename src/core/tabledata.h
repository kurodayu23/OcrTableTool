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
    TableCell(const QString &value = QString(), double score = 1.0, bool review = false,
              bool sentinel = false)
        : text(value), confidence(score), needsReview(review), confidenceSentinel(sentinel)
    {
    }

    QString text;
    double confidence;
    bool needsReview;
    // confidenceSentinel 为真表示这格的 confidence 是识别路径硬写的类别哨兵
    // （0.77 = 没取得双模型一致确认，0.86 = 取得了），不是实测置信度。
    // 老后端不写这个可选字段时一律按 false 处理（= 老行为）。
    bool confidenceSentinel;
};

class TableData
{
public:
    // fromRows 创建可信的本地/用户数据；fromBackendJson 是严格的协议边界，
    // 必须拒绝格式错误或不安全的 OCR 网格结果。
    static TableData fromRows(const QVector<QStringList> &rows);
    static TableData fromBackendJson(const QJsonObject &object, QString *errorMessage = 0);

    // fromBackendJsonLenient 是**兜底解析器**：严格协议校验失败时（status 非 ok、
    // rows/columns 与实际不符、超出行列上限、单元格字段缺失等），只要 cells 是
    // 非空二维数组就把内容读出来，**全部单元格标记为待核对**。
    // 产品策略：扫描到什么就给什么，绝不因契约细节丢掉整张表。
    static TableData fromBackendJsonLenient(const QJsonObject &object, QString *noticeMessage = 0);

    int rowCount() const;
    int columnCount() const;
    QString cell(int row, int column) const;
    double confidence(int row, int column) const;
    bool needsReview(int row, int column) const;
    bool confidenceSentinel(int row, int column) const;

    // 该格是否需要人工核对：后端明确要求复核（needs_review），或者实测置信度
    // 低于 0.78。哨兵格（confidenceSentinel）不是实测置信度，不算低置信告警。
    // 显示层和导出标记都用这一条口径，避免各写一套。
    bool requiresAttention(int row, int column) const;

    void setCell(int row,
                 int column,
                 const QString &text,
                 double confidence = 1.0,
                 bool needsReview = false,
                 bool confidenceSentinel = false);
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
