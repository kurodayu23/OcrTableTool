#include "csvexporter.h"

#include "tabledata.h"

#include <QSaveFile>

namespace {

// Excel 单元格一次能精确显示的有效数字上限：超过 15 位，Excel 会把文本当
// double 存并改掉尾数（18 位订单号 123456789012345678 显示成 123456789012345670）。
int excelSignificantDigits()
{
    return 15;
}

bool isAsciiDigit(const QChar &character)
{
    return character >= QLatin1Char('0') && character <= QLatin1Char('9');
}

// 纯数字字面量的「尾数」判定：digits[.digits] 或 .digits（不含符号/指数）。
// significantDigits 回传去掉前导零、去掉小数点和尾随零之后的有效位数。
bool parsesAsMantissa(const QString &text, int begin, int end, int *significantDigits)
{
    int decimalPoints = 0;
    int digits = 0;
    int firstSignificant = -1;
    int lastSignificant = -1;
    for (int index = begin; index < end; ++index) {
        const QChar character = text.at(index);
        if (character == QLatin1Char('.')) {
            if (++decimalPoints > 1)
                return false;
            continue;
        }
        if (!isAsciiDigit(character))
            return false;
        ++digits;
        if (character != QLatin1Char('0')) {
            if (firstSignificant < 0)
                firstSignificant = index;
            lastSignificant = index;
        }
    }
    if (digits == 0)
        return false;
    if (significantDigits) {
        int count = 0;
        for (int index = firstSignificant; firstSignificant >= 0 && index <= lastSignificant; ++index) {
            if (isAsciiDigit(text.at(index)))
                ++count;
        }
        *significantDigits = count;
    }
    return true;
}

// Excel 读进来「数值不变且显示不失真」的数字：可带正负号、小数、科学计数法、
// 尾随百分号，且有效位数不超过 15 位。
bool readsAsFaithfulNumber(const QString &value)
{
    if (value.isEmpty())
        return false;
    int begin = 0;
    if (value.at(0) == QLatin1Char('+') || value.at(0) == QLatin1Char('-'))
        begin = 1;
    int end = value.size();
    if (end > begin && value.at(end - 1) == QLatin1Char('%'))
        --end;
    if (begin >= end)
        return false;
    int mantissaEnd = end;
    for (int index = begin; index < end; ++index) {
        const QChar character = value.at(index);
        if (character != QLatin1Char('e') && character != QLatin1Char('E'))
            continue;
        mantissaEnd = index;
        int exponent = index + 1;
        if (exponent < end
            && (value.at(exponent) == QLatin1Char('+') || value.at(exponent) == QLatin1Char('-'))) {
            ++exponent;
        }
        if (exponent >= end)
            return false;
        for (; exponent < end; ++exponent) {
            if (!isAsciiDigit(value.at(exponent)))
                return false;
        }
        break;
    }
    int significantDigits = 0;
    if (!parsesAsMantissa(value, begin, mantissaEnd, &significantDigits))
        return false;
    return significantDigits <= excelSignificantDigits();
}

// Excel 会把这段「看起来是数字」的文本改值：
// ① 有效位数超过 15 位（尾数被 double 截断）；
// ② 带前导零的整数编号（"007" 变成 7、"0123" 变成 123，前导零丢失）。
bool losesPrecisionAsNumber(const QString &value)
{
    int significantDigits = 0;
    if (!parsesAsMantissa(value, 0, value.size(), &significantDigits))
        return false;
    if (significantDigits > excelSignificantDigits())
        return true;
    // 只对纯整数判前导零："0.5" 小数点的那个 0 是数值本身的一部分，不算丢失。
    return value.size() > 1 && value.at(0) == QLatin1Char('0')
        && !value.contains(QLatin1Char('.'));
}

// 2026-10-28 修正：只中和「真会被 Excel 解释成公式」或「真会丢精度」的形态。
//
// 2026-10-22 那版对任何以 = + - @ 开头的文本都前置单引号，把合法负数
// "-12.5" 写成 "'-12.5"：CSV 与 OCR 原文不一致，Excel 里数字列变成文本
// （绿三角、"以文本形式存储的数字"），下游 pandas / SQL LOAD DATA 读到的
// 也是带撇号的字符串。负号只是数字的符号，不是公式前缀。
//
// 现在分三类判：
// ① 真·公式前缀：= 和 @ 一律中和；TAB / CR / LF 开头也中和（它们会把
//    后续字节推开一格，消费方去掉空白后仍按公式解析，OWASP CSV Injection
//    把 0x09 / 0x0D 与 = + - @ 并列）。
// ② +/- 开头但后面**不是**能原样读成数字的字面量：-1+2、+SUM(A1)、
//    -cmd|' /c calc'!A0 打开就是公式；-12.5 / +1 / -1.5e3 / -12.5% 不是。
// ③ 无前缀但会被 Excel 改值：>15 位长数字（订单号/身份证号尾数被改写）、
//    前导零编号（"007"→7）。
//
// 已知不在本函数职责内（与 2026-10-22 之前的行为一致，不是本轮引入的回归）：
// 带千分位的数（"1,234.56"）、日期样式文本（"10-20"→10月20日）Excel 仍会
// 自行解释，需要产品侧另行决定是否中和。
bool needsTextGuard(const QString &value)
{
    // 单个字符（"-"、"+"、"、" 这类"无数据"占位符）没有公式可执行，不用管。
    if (value.size() <= 1)
        return false;
    const QChar first = value.at(0);
    if (first == QLatin1Char('\t') || first == QLatin1Char('\r') || first == QLatin1Char('\n'))
        return true;
    if (first == QLatin1Char('=') || first == QLatin1Char('@'))
        return true;
    if (first == QLatin1Char('+') || first == QLatin1Char('-'))
        return !readsAsFaithfulNumber(value);
    return losesPrecisionAsNumber(value);
}

// 整张表没有任何文字（0 行 / 0 列，或者全是空白格）就不算一张表。
bool hasExportableContent(const TableData &table)
{
    if (table.rowCount() == 0 || table.columnCount() == 0)
        return false;
    for (int row = 0; row < table.rowCount(); ++row) {
        for (int column = 0; column < table.columnCount(); ++column) {
            if (!table.cell(row, column).trimmed().isEmpty())
                return true;
        }
    }
    return false;
}

} // namespace

QString CsvExporter::escaped(const QString &value)
{
    // 公式注入中和：见 needsTextGuard 的注释（2026-10-22 首次加，2026-10-28
    // 收紧触发条件，不再把合法负数当成公式）。前置单引号让 Excel 按文本显示。
    // XLSX 路径不受影响（openpyxl 写成文本）。
    QString safe = value;
    if (needsTextGuard(safe))
        safe.prepend(QLatin1Char('\''));

    // RFC4180：分隔符、引号、CR/LF 必须加引号。TAB 与其它 C0 控制字符一并
    // 加引号 —— 裸控制字符会让下游按列切分时错位或被静默丢弃。
    bool needsQuotes = false;
    for (int index = 0; index < safe.size(); ++index) {
        const QChar character = safe.at(index);
        if (character == QLatin1Char(',') || character == QLatin1Char('"')
            || character.unicode() < 0x20) {
            needsQuotes = true;
            break;
        }
    }
    if (!needsQuotes)
        return safe;

    QString quoted = safe;
    quoted.replace(QStringLiteral("\""), QStringLiteral("\"\""));
    return QLatin1Char('"') + quoted + QLatin1Char('"');
}

QByteArray CsvExporter::encode(const TableData &table)
{
    // 空表不产出任何字节（连 BOM 也不写）：只有 BOM 的 3 字节文件在 Excel 里
    // 是「一个空单元格」，客户拿到的是"成功但空白"。真正写盘的调用方见
    // writeFile 的显式拒绝。
    if (!hasExportableContent(table))
        return QByteArray();

    QString output;
    for (int row = 0; row < table.rowCount(); ++row) {
        QStringList fields;
        for (int column = 0; column < table.columnCount(); ++column)
            fields.append(escaped(table.cell(row, column)));
        output += fields.join(QLatin1Char(','));
        output += QStringLiteral("\r\n");
    }
    return QByteArray::fromHex("EFBBBF") + output.toUtf8();
}

bool CsvExporter::writeFile(const QString &filePath, const TableData &table, QString *errorMessage)
{
    // 空表明确失败：契约规定 CSV 由客户端从 cells[].text 生成
    // （docs/BACKEND_API_V1.md §7），没有内容的网格不是一张表，写一个空壳文件
    // 比直接报错更容易被误当成"导出成功"。
    if (!hasExportableContent(table)) {
        if (errorMessage)
            *errorMessage = QStringLiteral("没有可导出的表格数据（空表不导出空白文件）。");
        return false;
    }

    QSaveFile file(filePath);
    if (!file.open(QIODevice::WriteOnly)) {
        if (errorMessage)
            *errorMessage = file.errorString();
        return false;
    }

    const QByteArray data = encode(table);
    if (file.write(data) != data.size()) {
        if (errorMessage)
            *errorMessage = file.errorString();
        file.cancelWriting();
        return false;
    }
    if (!file.commit()) {
        if (errorMessage)
            *errorMessage = file.errorString();
        return false;
    }
    return true;
}
