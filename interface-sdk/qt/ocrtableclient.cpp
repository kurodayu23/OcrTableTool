#include "ocrtableclient.h"

#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QProcess>
#include <QSaveFile>
#include <QSet>
#include <QTimer>
#include <QVector>
#include <QtGlobal>

namespace
{
const int kProtocolVersion = 1;
const int kMaximumRequestId = 2147483647;
const int kMaximumRows = 256;
const int kMaximumColumns = 128;
const int kMaximumCells = 4096;

bool readInteger(const QJsonValue &value, int minimum, int maximum, int *result)
{
    if (!value.isDouble())
        return false;
    const int integer = value.toInt(minimum - 1);
    if (value.toDouble() != static_cast<double>(integer)
        || integer < minimum
        || integer > maximum) {
        return false;
    }
    if (result)
        *result = integer;
    return true;
}

// The reasons are split into two groups.
// Summary reasons explain the overall verdict (protocol, publication state,
// structure, ...). Detail reasons are the per cell and per span findings.
// A flood of detail reasons must never crowd out the summary reasons,
// because the summary reasons are the ones that explain why the result
// cannot be published. The total number of handed out reasons stays bounded.
const int kMaximumReasons = 100;
const int kMaximumSummaryReasons = 40;

void addSummaryReason(QStringList *summaries, const QString &reason)
{
    if (summaries && summaries->size() < kMaximumSummaryReasons)
        summaries->append(reason);
}

void addDetailReason(QStringList *details, const QString &reason)
{
    if (details)
        details->append(reason);
}

// Appends as many entries as the total budget still allows.
void appendBounded(QStringList *target, const QStringList &source)
{
    if (!target)
        return;
    for (int index = 0; index < source.size() && target->size() < kMaximumReasons; ++index)
        target->append(source.at(index));
}

bool jsonArraysEqual(const QJsonArray &left, const QJsonArray &right)
{
    return QJsonDocument(left).toJson(QJsonDocument::Compact)
        == QJsonDocument(right).toJson(QJsonDocument::Compact);
}
}

namespace OcrTableSdk {

// QJsonDocument silently keeps the last occurrence of a duplicated key, so a
// response that carries the same key twice is ambiguous and the SDK has to
// look at the raw bytes to notice it at all.
//
// The walker below is a tolerant minimal JSON reader: it only has to know
// where each object starts and ends. String escapes are decoded, so
// "confidence_sentinel" and "confidence_sentinel" are recognized as
// the same key. Malformed input stops the walk and reports nothing; the
// regular QJsonDocument parse in the caller still rejects such payloads.
// The depth limit keeps a deeply nested payload from exhausting the stack.
class DuplicateKeyWalker
{
public:
    explicit DuplicateKeyWalker(const QByteArray &json)
        : m_json(json)
        , m_position(0)
    {
    }

    void walk()
    {
        parseValue(0);
    }

    QStringList duplicatedKeys() const
    {
        return m_duplicatedKeys;
    }

private:
    enum { kMaximumDepth = 256 };

    bool atEnd() const { return m_position >= m_json.size(); }
    char peek() const { return atEnd() ? '\0' : m_json.at(m_position); }

    void skipWhitespace()
    {
        while (!atEnd()) {
            const char current = m_json.at(m_position);
            if (current == ' ' || current == '\t' || current == '\n' || current == '\r')
                ++m_position;
            else
                break;
        }
    }

    bool parseValue(int depth)
    {
        if (depth > kMaximumDepth)
            return false;
        skipWhitespace();
        if (atEnd())
            return false;
        const char current = peek();
        if (current == '{')
            return parseObject(depth + 1);
        if (current == '[')
            return parseArray(depth + 1);
        if (current == '"')
            return parseString(0);
        return parseScalar();
    }

    bool parseObject(int depth)
    {
        ++m_position; // consume '{'
        QSet<QString> seenKeys;
        skipWhitespace();
        if (!atEnd() && peek() == '}') {
            ++m_position;
            return true;
        }
        while (true) {
            skipWhitespace();
            if (atEnd() || peek() != '"')
                return false;
            QByteArray rawKey;
            if (!parseString(&rawKey))
                return false;
            const QString key = QString::fromUtf8(rawKey);
            skipWhitespace();
            if (atEnd() || peek() != ':')
                return false;
            ++m_position; // consume ':'
            if (seenKeys.contains(key)) {
                if (!m_duplicatedKeys.contains(key))
                    m_duplicatedKeys.append(key);
            } else {
                seenKeys.insert(key);
            }
            if (!parseValue(depth))
                return false;
            skipWhitespace();
            if (atEnd())
                return false;
            if (peek() == ',') {
                ++m_position;
                continue;
            }
            if (peek() == '}') {
                ++m_position;
                return true;
            }
            return false;
        }
    }

    bool parseArray(int depth)
    {
        ++m_position; // consume '['
        skipWhitespace();
        if (!atEnd() && peek() == ']') {
            ++m_position;
            return true;
        }
        while (true) {
            if (!parseValue(depth))
                return false;
            skipWhitespace();
            if (atEnd())
                return false;
            if (peek() == ',') {
                ++m_position;
                continue;
            }
            if (peek() == ']') {
                ++m_position;
                return true;
            }
            return false;
        }
    }

    bool parseString(QByteArray *decoded)
    {
        if (atEnd() || peek() != '"')
            return false;
        ++m_position;
        QByteArray raw;
        while (true) {
            if (atEnd())
                return false;
            const char current = m_json.at(m_position);
            if (current == '"') {
                ++m_position;
                if (decoded)
                    *decoded = raw;
                return true;
            }
            if (current == '\\') {
                ++m_position;
                if (atEnd())
                    return false;
                const char escaped = m_json.at(m_position);
                ++m_position;
                switch (escaped) {
                case '"': raw.append('"'); break;
                case '\\': raw.append('\\'); break;
                case '/': raw.append('/'); break;
                case 'b': raw.append('\b'); break;
                case 'f': raw.append('\f'); break;
                case 'n': raw.append('\n'); break;
                case 'r': raw.append('\r'); break;
                case 't': raw.append('\t'); break;
                case 'u': {
                    uint codePoint = 0;
                    if (!parseHexQuad(&codePoint))
                        return false;
                    if (codePoint >= 0xD800 && codePoint <= 0xDBFF
                        && m_position + 1 < m_json.size()
                        && m_json.at(m_position) == '\\'
                        && m_json.at(m_position + 1) == 'u') {
                        m_position += 2;
                        uint low = 0;
                        if (!parseHexQuad(&low))
                            return false;
                        if (low >= 0xDC00 && low <= 0xDFFF) {
                            codePoint = 0x10000
                                + ((codePoint - 0xD800) << 10) + (low - 0xDC00);
                        } else {
                            codePoint = low;
                        }
                    }
                    raw.append(QString::fromUcs4(&codePoint, 1).toUtf8());
                    break;
                }
                default:
                    return false;
                }
                continue;
            }
            if (static_cast<unsigned char>(current) < 0x20)
                return false;
            raw.append(current);
            ++m_position;
        }
    }

    bool parseHexQuad(uint *value)
    {
        if (m_position + 4 > m_json.size())
            return false;
        uint result = 0;
        for (int index = 0; index < 4; ++index) {
            const char digit = m_json.at(m_position + index);
            result <<= 4;
            if (digit >= '0' && digit <= '9')
                result |= static_cast<uint>(digit - '0');
            else if (digit >= 'a' && digit <= 'f')
                result |= static_cast<uint>(digit - 'a' + 10);
            else if (digit >= 'A' && digit <= 'F')
                result |= static_cast<uint>(digit - 'A' + 10);
            else
                return false;
        }
        m_position += 4;
        *value = result;
        return true;
    }

    bool parseScalar()
    {
        while (!atEnd()) {
            const char current = m_json.at(m_position);
            if (current == ',' || current == '}' || current == ']'
                || current == ' ' || current == '\t' || current == '\n' || current == '\r')
                break;
            ++m_position;
        }
        return true;
    }

    QByteArray m_json;
    int m_position;
    QStringList m_duplicatedKeys;
};

// Returns every key that appears more than once inside the same JSON object.
// The response object and every nested object (a cell, a span, the structure
// certificate) are inspected.
QStringList duplicatedJsonKeys(const QByteArray &json)
{
    DuplicateKeyWalker walker(json);
    walker.walk();
    return walker.duplicatedKeys();
}

} // namespace OcrTableSdk

// CSV formula injection neutralization: a cell that spreadsheet software would
// evaluate as a formula (or silently rewrite) gets a leading single quote so it
// stays text-only.
//
// 2026-10-28：与产品侧 src/core/csvexporter.cpp 的 needsTextGuard() 逐条对齐。
// 2026-10-22 版对「首字符 ∈ = + - @」一律加撇号，把合法负数 -12.5 写成 '-12.5：
// 导出的 CSV 与 OCR 原文不一致，Excel 里数字列变文本，pandas / SQL LOAD DATA
// 读到的也是带撇号的字符串。负号只是数字的符号，不是公式前缀。
//
// 分三类判：
// ① 真·公式前缀：'=' 与 '@' 一律中和；TAB / CR / LF 开头也中和（OWASP CSV
//    Injection 把 0x09 / 0x0D 与 = + - @ 并列）。
// ② '+' / '-' 开头但后面**不是**能原样读成数字的字面量：-1+2、+SUM(A1) 中和；
//    -12.5 / +1 / -1.5e3 / -12.5% 不中和。
// ③ 无前缀但会被 Excel 改值：>15 位有效数字（订单号尾数被 double 截断）、
//    纯整数前导零编号（"007" → 7）。
//
// The product side implements the same rule in src/core/csvexporter.cpp. The
// public SDK is a standalone deliverable and cannot include src/core, so the
// rule is duplicated here; keep both implementations in sync.
namespace {

int sdkExcelSignificantDigits()
{
    return 15;
}

bool sdkIsAsciiDigit(const QChar &character)
{
    return character >= QLatin1Char('0') && character <= QLatin1Char('9');
}

// 尾数判定：digits[.digits] 或 .digits（不含符号/指数）。
bool sdkParsesAsMantissa(const QString &text, int begin, int end, int *significantDigits)
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
        if (!sdkIsAsciiDigit(character))
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
            if (sdkIsAsciiDigit(text.at(index)))
                ++count;
        }
        *significantDigits = count;
    }
    return true;
}

// Excel 读进来数值不变、显示不失真的数字：可带符号、小数、科学计数法、百分号，
// 且有效位数不超过 15 位。
bool sdkReadsAsFaithfulNumber(const QString &value)
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
            if (!sdkIsAsciiDigit(value.at(exponent)))
                return false;
        }
        break;
    }
    int significantDigits = 0;
    if (!sdkParsesAsMantissa(value, begin, mantissaEnd, &significantDigits))
        return false;
    return significantDigits <= sdkExcelSignificantDigits();
}

bool sdkLosesPrecisionAsNumber(const QString &value)
{
    int significantDigits = 0;
    if (!sdkParsesAsMantissa(value, 0, value.size(), &significantDigits))
        return false;
    if (significantDigits > sdkExcelSignificantDigits())
        return true;
    return value.size() > 1 && value.at(0) == QLatin1Char('0')
        && !value.contains(QLatin1Char('.'));
}

bool sdkNeedsTextGuard(const QString &value)
{
    // 单个字符（"-"、"+" 这类"无数据"占位符）没有公式可执行，不用管。
    if (value.size() <= 1)
        return false;
    const QChar first = value.at(0);
    if (first == QLatin1Char('\t') || first == QLatin1Char('\r') || first == QLatin1Char('\n'))
        return true;
    if (first == QLatin1Char('=') || first == QLatin1Char('@'))
        return true;
    if (first == QLatin1Char('+') || first == QLatin1Char('-'))
        return !sdkReadsAsFaithfulNumber(value);
    return sdkLosesPrecisionAsNumber(value);
}

} // namespace

bool neutralizeCsvFormula(QString *value)
{
    if (!value || value->isEmpty())
        return false;
    if (!sdkNeedsTextGuard(*value))
        return false;
    value->prepend(QLatin1Char('\''));
    return true;
}
OcrTableClient::OcrTableClient(const QString &backendExecutable, QObject *parent)
    : QObject(parent)
    , m_backendExecutable(QFileInfo(backendExecutable).absoluteFilePath())
    , m_process(new QProcess(this))
    , m_requestTimer(new QTimer(this))
    , m_forcedRetryable(false)
    , m_nextRequestId(1)
    , m_activeRequestId(0)
    , m_busy(false)
    , m_shuttingDown(false)
{
    m_requestTimer->setSingleShot(true);
    m_process->setProcessChannelMode(QProcess::SeparateChannels);
    connect(m_process, SIGNAL(readyReadStandardOutput()), this, SLOT(readStandardOutput()));
    connect(m_process, SIGNAL(readyReadStandardError()), this, SLOT(readStandardError()));
    connect(m_process, SIGNAL(finished(int,QProcess::ExitStatus)), this, SLOT(processFinished(int)));
    connect(m_process, SIGNAL(errorOccurred(QProcess::ProcessError)), this, SLOT(processError()));
    connect(m_requestTimer, SIGNAL(timeout()), this, SLOT(requestTimedOut()));
}

OcrTableClient::~OcrTableClient()
{
    shutdown();
}

void OcrTableClient::shutdown()
{
    // 2026-10-23：公开 SDK 的析构期误导性失败回调（与 GUI BackendRunner 同一类问题）。
    //
    // 析构期 kill() + waitForFinished() 会**同步**触发 QProcess::finished()，
    // 于是 processFinished() 在**本对象正在析构**时跑一遍：若此刻有请求在飞
    // （m_busy 为真），它会向主人 emit 一次
    // requestFailed(..., "BACKEND_EXITED", ..., retryable=true)。
    // 主人若按 retryable 自动重试（本项目 GUI 的既有策略就是
    // `retryAllowed = errorCode.isEmpty() || retryable`），
    // 就会在对象析构过程中又发起新请求 —— 打在正在被销毁的客户端上。
    //
    // 做法与 src/gui/backendrunner.cpp 的 shutdown() 保持一致：
    // 先断开自身所有出向信号（主人不会再收到任何回调），
    // 再置位 m_shuttingDown 让本对象自己的槽函数也直接返回，最后才停进程。
    if (m_shuttingDown)
        return;
    m_shuttingDown = true;
    disconnect(this, nullptr, nullptr, nullptr);
    if (m_process->state() != QProcess::NotRunning) {
        m_process->kill();
        // 等待时间**保持 1000 ms**：与 GUI BackendRunner 同口径，
        // 避免在调用方线程上无谓阻塞更久。
        m_process->waitForFinished(1000);
    }
}

bool OcrTableClient::isReady() const
{
    return QFileInfo::exists(m_backendExecutable);
}

bool OcrTableClient::isBusy() const
{
    return m_busy;
}

QString OcrTableClient::backendExecutable() const
{
    return m_backendExecutable;
}

int OcrTableClient::health()
{
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), kProtocolVersion);
    request.insert(QStringLiteral("action"), QStringLiteral("health"));
    return startRequest(QStringLiteral("health"), request, 10000);
}

int OcrTableClient::recognize(const QString &imagePath,
                              const QString &outputDirectory,
                              bool inputRectified,
                              bool selectedTableRegion)
{
    QJsonObject options;
    options.insert(QStringLiteral("crop_mode"), QStringLiteral("auto"));
    options.insert(QStringLiteral("accuracy_mode"), QStringLiteral("maximum"));
    options.insert(QStringLiteral("deadline_seconds"), 0);
    if (inputRectified)
        options.insert(QStringLiteral("input_rectified"), true);
    if (selectedTableRegion)
        options.insert(QStringLiteral("selected_table_region"), true);

    QJsonObject request;
    request.insert(QStringLiteral("protocol"), kProtocolVersion);
    request.insert(QStringLiteral("action"), QStringLiteral("recognize"));
    request.insert(QStringLiteral("image_path"), QFileInfo(imagePath).absoluteFilePath());
    request.insert(QStringLiteral("output_directory"), QDir(outputDirectory).absolutePath());
    request.insert(QStringLiteral("options"), options);
    return startRequest(QStringLiteral("recognize"), request, 0);
}

int OcrTableClient::recognizeCameraPhoto(const QString &imagePath,
                                         const QString &outputDirectory,
                                         bool selectedTableRegion)
{
    // A camera photo is a raw capture: it is not rectified yet, so
    // input_rectified stays false.
    return recognize(imagePath, outputDirectory, false, selectedTableRegion);
}

int OcrTableClient::recognizeRectifiedTable(const QString &imagePath,
                                            const QString &outputDirectory)
{
    // An already rectified table must not be cropped again.
    return recognize(imagePath, outputDirectory, true, false);
}

int OcrTableClient::exportXlsx(const QString &outputPath,
                               const QJsonArray &cells,
                               const QJsonArray &spans)
{
    QJsonObject request;
    request.insert(QStringLiteral("protocol"), kProtocolVersion);
    request.insert(QStringLiteral("action"), QStringLiteral("export_xlsx"));
    request.insert(QStringLiteral("output_path"), QFileInfo(outputPath).absoluteFilePath());
    request.insert(QStringLiteral("cells"), cells);
    request.insert(QStringLiteral("spans"), spans);
    return startRequest(QStringLiteral("export_xlsx"), request, 30000);
}

bool OcrTableClient::exportCsv(const QString &outputPath,
                               const QJsonArray &cells,
                               QString *errorMessage)
{
    const QString invalidData = QString::fromWCharArray(
        L"\u8868\u683c\u6570\u636e\u4e0d\u662f\u5b8c\u6574\u7684\u4e8c\u7ef4\u5355\u5143\u683c\u6570\u7ec4\u3002");
    if (cells.isEmpty()) {
        if (errorMessage)
            *errorMessage = invalidData;
        return false;
    }

    int expectedColumns = -1;
    QByteArray csv("\xEF\xBB\xBF", 3);
    for (int row = 0; row < cells.size(); ++row) {
        if (!cells.at(row).isArray()) {
            if (errorMessage)
                *errorMessage = invalidData;
            return false;
        }
        const QJsonArray columns = cells.at(row).toArray();
        if (expectedColumns < 0)
            expectedColumns = columns.size();
        if (expectedColumns <= 0 || columns.size() != expectedColumns) {
            if (errorMessage)
                *errorMessage = invalidData;
            return false;
        }
        for (int column = 0; column < columns.size(); ++column) {
            if (!columns.at(column).isObject()
                || !columns.at(column).toObject()
                        .value(QStringLiteral("text")).isString()) {
                if (errorMessage)
                    *errorMessage = invalidData;
                return false;
            }
            QString text = columns.at(column).toObject()
                .value(QStringLiteral("text")).toString();
            // Neutralize spreadsheet formula injection first, then decide
            // whether the cell needs quoting: the single quote has to end up
            // inside the quoted text, exactly like the product-side exporter.
            neutralizeCsvFormula(&text);
            // 2026-10-30（lead 复查 P2）：引号判据必须与产品侧 CsvExporter 一致 ——
            // 那边除 , 和 " 之外，对任何 C0 控制字符（unicode < 0x20，含 TAB）也加
            // 引号（裸控制字符会让下游按列切分错位或被静默丢弃）。SDK 原来只判
            // , " \r \n，含 TAB 的单元格两端导出结果不同，违反本文件 keep both in
            // sync 的约定。
            bool quote = false;
            for (int index = 0; index < text.size(); ++index) {
                const QChar character = text.at(index);
                if (character == QLatin1Char(',') || character == QLatin1Char('"')
                    || character.unicode() < 0x20) {
                    quote = true;
                    break;
                }
            }
            if (quote) {
                text.replace(QStringLiteral("\""), QStringLiteral("\"\""));
                text.prepend(QLatin1Char('"'));
                text.append(QLatin1Char('"'));
            }
            if (column > 0)
                csv.append(',');
            csv.append(text.toUtf8());
        }
        csv.append("\r\n", 2);
    }

    const QFileInfo outputInfo(outputPath);
    if (outputInfo.fileName().isEmpty()
        || !QDir().mkpath(outputInfo.absolutePath())) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"\u65e0\u6cd5\u521b\u5efaCSV\u8f93\u51fa\u76ee\u5f55\u3002");
        }
        return false;
    }
    QSaveFile file(outputInfo.absoluteFilePath());
    if (!file.open(QIODevice::WriteOnly)
        || file.write(csv) != csv.size()
        || !file.commit()) {
        if (errorMessage) {
            *errorMessage = QString::fromWCharArray(
                L"CSV\u6587\u4ef6\u5199\u5165\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u8def\u5f84\u548c\u5199\u5165\u6743\u9650\u3002");
        }
        return false;
    }
    if (errorMessage)
        errorMessage->clear();
    return true;
}

void OcrTableClient::cancel()
{
    if (!m_busy)
        return;
    m_requestTimer->stop();
    // 2026-10-30（lead 复查 P1）：这里原来是 retryable=true。调用方（
    // CameraOcrClient::ocrRequestFailed）见到 retryable=true 且重试计数未用完，
    // 会在 300ms 后**自动重跑一次识别** —— 也就是用户点了「取消」、请求反而被
    // 重新发起。主动取消是调用方明确意愿，必须 retryable=false。
    setForcedFailure(QStringLiteral("CANCELLED"),
                     QStringLiteral("The OCR request was cancelled."),
                     false);
    if (m_process->state() != QProcess::NotRunning)
        m_process->kill();
}

int OcrTableClient::startRequest(const QString &action,
                                 QJsonObject request,
                                 int timeoutMilliseconds)
{
    if (m_busy) {
        failLocally(action,
                    QStringLiteral("CLIENT_BUSY"),
                    QString(),
                    QStringLiteral("Another OCR request is already running."),
                    true);
        return 0;
    }
    if (!isReady()) {
        failLocally(action,
                    QStringLiteral("BACKEND_NOT_FOUND"),
                    QStringLiteral("backend_executable"),
                    QStringLiteral("OcrBackend.exe was not found."),
                    false);
        return 0;
    }
    if (m_process->state() != QProcess::NotRunning) {
        failLocally(action,
                    QStringLiteral("BACKEND_STILL_EXITING"),
                    QString(),
                    QStringLiteral("The previous backend process is still exiting."),
                    true);
        return 0;
    }

    const int requestId = m_nextRequestId;
    m_nextRequestId = m_nextRequestId == kMaximumRequestId ? 1 : m_nextRequestId + 1;
    request.insert(QStringLiteral("request_id"), requestId);

    m_process->setProgram(m_backendExecutable);
    m_process->setArguments(QStringList());
    m_process->setWorkingDirectory(QFileInfo(m_backendExecutable).absolutePath());
    m_process->start();
    if (!m_process->waitForStarted(3000)) {
        failLocally(action,
                    QStringLiteral("BACKEND_START_FAILED"),
                    QString(),
                    m_process->errorString(),
                    true);
        return 0;
    }

    m_activeRequestId = requestId;
    m_activeAction = action;
    m_standardOutput.clear();
    m_standardError.clear();
    m_pendingResponse = QJsonObject();
    m_forcedErrorCode.clear();
    m_forcedErrorMessage.clear();
    m_forcedRetryable = false;
    m_busy = true;
    emit busyChanged(true);
    emit requestStarted(requestId, action);

    QByteArray payload = QJsonDocument(request).toJson(QJsonDocument::Compact);
    payload.append('\n');
    if (m_process->write(payload) != payload.size()) {
        setForcedFailure(QStringLiteral("BACKEND_WRITE_FAILED"),
                         QStringLiteral("The OCR request could not be written."),
                         true);
        m_process->kill();
        return requestId;
    }
    // EOF tells the non-persistent backend to finish this single request and exit.
    m_process->closeWriteChannel();
    if (timeoutMilliseconds > 0)
        m_requestTimer->start(timeoutMilliseconds);
    return requestId;
}

void OcrTableClient::readStandardOutput()
{
    if (m_shuttingDown)
        return;
    m_standardOutput += m_process->readAllStandardOutput();
    int newline = m_standardOutput.indexOf('\n');
    while (newline >= 0) {
        const QByteArray line = m_standardOutput.left(newline).trimmed();
        m_standardOutput.remove(0, newline + 1);
        if (!line.isEmpty())
            consumeResponseLine(line);
        newline = m_standardOutput.indexOf('\n');
    }
}

void OcrTableClient::consumeResponseLine(const QByteArray &line)
{
    if (m_shuttingDown)
        return;
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(line, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        emit logMessage(QStringLiteral("Ignored a non-JSON backend output line."));
        return;
    }
    if (!m_busy)
        return;

    const QJsonObject response = document.object();
    int responseRequestId = 0;
    if (!readInteger(response.value(QStringLiteral("request_id")),
                     1,
                     kMaximumRequestId,
                     &responseRequestId)
        || responseRequestId != m_activeRequestId) {
        emit logMessage(QStringLiteral("Ignored a stale response with a different request_id."));
        return;
    }
    if (response.value(QStringLiteral("action")).toString() != m_activeAction) {
        setForcedFailure(QStringLiteral("RESPONSE_ACTION_MISMATCH"),
                         QStringLiteral("The backend response action does not match the request."),
                         false);
        return;
    }

    // A duplicated key makes the response ambiguous: QJsonDocument keeps the
    // last occurrence without a word, so the SDK has to look at the raw bytes
    // to notice it at all. A repeated confidence_sentinel flips the
    // publication verdict, so the response is rejected instead of silently
    // trusting one of the two values.
    const QStringList duplicatedKeys = OcrTableSdk::duplicatedJsonKeys(line);
    if (duplicatedKeys.contains(QStringLiteral("confidence_sentinel"))) {
        setForcedFailure(QStringLiteral("AMBIGUOUS_RESPONSE"),
                         QStringLiteral("The backend response repeats the confidence_sentinel key, so the result is ambiguous."),
                         false);
        return;
    }
    if (!duplicatedKeys.isEmpty()) {
        // Other duplicated keys do not change the publication verdict, but the
        // ambiguity is reported instead of being swallowed silently.
        emit logMessage(QStringLiteral("Ignored duplicated backend response keys: %1")
                            .arg(duplicatedKeys.join(QStringLiteral(", "))));
    }

    if (m_pendingResponse.isEmpty()) {
        m_pendingResponse = response;
        // 2026-10-30（lead 复查 P2）：收到**完整响应**后必须停掉请求计时器。
        // GUI 侧（backendrunner.cpp）在同样位置就是 `m_requestTimer->stop()`；
        // SDK 原来不停表，于是 health(10s)/export_xlsx(30s) 这类响应在到期前一刻
        // 到齐、而进程收尾稍慢时，requestTimedOut() 只看 m_busy 就 kill 并把
        // **已经拿到的结果**判成 TIMEOUT(retryable=true) 丢弃重试。
        // 成功/失败的派发仍等 processFinished（保证进程自然退出），但不再被判超时。
        m_requestTimer->stop();
    }
}

void OcrTableClient::readStandardError()
{
    if (m_shuttingDown)
        return;
    const QByteArray output = m_process->readAllStandardError();
    m_standardError += output;
    if (m_standardError.size() > 16000)
        m_standardError = m_standardError.right(16000);
    const QString message = QString::fromLocal8Bit(output).trimmed();
    if (!message.isEmpty())
        emit logMessage(message);
}

void OcrTableClient::processFinished(int exitCode)
{
    if (m_shuttingDown)
        return;
    readStandardOutput();
    readStandardError();
    if (!m_standardOutput.trimmed().isEmpty())
        consumeResponseLine(m_standardOutput.trimmed());
    if (!m_busy)
        return;

    const int requestId = m_activeRequestId;
    const QString action = m_activeAction;
    const QString forcedCode = m_forcedErrorCode;
    const QString forcedMessage = m_forcedErrorMessage;
    const bool forcedRetryable = m_forcedRetryable;
    const QJsonObject response = m_pendingResponse;
    const QString diagnostics = QString::fromLocal8Bit(m_standardError).trimmed().right(4000);
    finishRequestState();

    if (!forcedCode.isEmpty()) {
        emit requestFailed(requestId,
                           action,
                           forcedCode,
                           QString(),
                           forcedMessage,
                           forcedRetryable);
        return;
    }
    if (response.isEmpty()) {
        emit requestFailed(requestId,
                           action,
                           QStringLiteral("BACKEND_EXITED"),
                           QString(),
                           diagnostics.isEmpty()
                               ? QStringLiteral("The backend exited without a JSON response.")
                               : diagnostics,
                           true);
        return;
    }
    if (response.value(QStringLiteral("protocol")).toInt(-1) != kProtocolVersion) {
        emit requestFailed(requestId,
                           action,
                           QStringLiteral("PROTOCOL_MISMATCH"),
                           QStringLiteral("protocol"),
                           QStringLiteral("The backend protocol version is not supported."),
                           false);
        return;
    }
    if (response.value(QStringLiteral("status")).toString() == QStringLiteral("ok")
        && exitCode == 0) {
        emit requestSucceeded(requestId, action, response);
        return;
    }
    emit requestFailed(requestId,
                       action,
                       response.value(QStringLiteral("error_code"))
                           .toString(QStringLiteral("BACKEND_ERROR")),
                       response.value(QStringLiteral("field")).toString(),
                       response.value(QStringLiteral("message"))
                           .toString(QStringLiteral("The OCR request failed.")),
                       response.value(QStringLiteral("retryable")).toBool(false));
}

void OcrTableClient::processError()
{
    if (m_shuttingDown)
        return;
    if (!m_busy)
        return;
    if (m_process->state() == QProcess::NotRunning && m_forcedErrorCode.isEmpty()) {
        setForcedFailure(QStringLiteral("BACKEND_PROCESS_ERROR"),
                         m_process->errorString(),
                         true);
    }
}

void OcrTableClient::requestTimedOut()
{
    if (m_shuttingDown)
        return;
    if (!m_busy)
        return;
    // 2026-10-30（lead 复查 P2）：响应已经完整落地（m_pendingResponse 非空）时不再算超时。
    // 此时只剩"等 worker 自然退出"这一步，超时定时器若抢先 kill 会把已经拿到的结果丢掉。
    if (!m_pendingResponse.isEmpty())
        return;
    setForcedFailure(QStringLiteral("TIMEOUT"),
                     QStringLiteral("The OCR request timed out."),
                     true);
    if (m_process->state() != QProcess::NotRunning)
        m_process->kill();
}

void OcrTableClient::setForcedFailure(const QString &errorCode,
                                      const QString &message,
                                      bool retryable)
{
    if (!m_forcedErrorCode.isEmpty())
        return;
    m_forcedErrorCode = errorCode;
    m_forcedErrorMessage = message;
    m_forcedRetryable = retryable;
}

void OcrTableClient::finishRequestState()
{
    m_requestTimer->stop();
    m_standardOutput.clear();
    m_standardError.clear();
    m_pendingResponse = QJsonObject();
    m_activeAction.clear();
    m_forcedErrorCode.clear();
    m_forcedErrorMessage.clear();
    m_forcedRetryable = false;
    m_activeRequestId = 0;
    m_busy = false;
    emit busyChanged(false);
}

void OcrTableClient::failLocally(const QString &action,
                                 const QString &errorCode,
                                 const QString &field,
                                 const QString &message,
                                 bool retryable)
{
    emit requestFailed(0, action, errorCode, field, message, retryable);
}

bool OcrTableClient::validateRecognitionResponse(const QJsonObject &response,
                                                 QStringList *reasons)
{
    QStringList summaries;
    QStringList details;
    if (response.value(QStringLiteral("protocol")).toInt(-1) != kProtocolVersion)
        addSummaryReason(&summaries, QStringLiteral("protocol is not 1"));
    if (response.value(QStringLiteral("status")).toString() != QStringLiteral("ok"))
        addSummaryReason(&summaries, QStringLiteral("status is not ok"));
    if (response.value(QStringLiteral("action")).toString() != QStringLiteral("recognize"))
        addSummaryReason(&summaries, QStringLiteral("action is not recognize"));
    int requestId = 0;
    if (!readInteger(response.value(QStringLiteral("request_id")),
                     1,
                     kMaximumRequestId,
                     &requestId)) {
        addSummaryReason(&summaries, QStringLiteral("request_id is invalid"));
    }

    int rows = 0;
    int columns = 0;
    if (!readInteger(response.value(QStringLiteral("rows")), 1, kMaximumRows, &rows))
        addSummaryReason(&summaries, QStringLiteral("rows is invalid"));
    if (!readInteger(response.value(QStringLiteral("columns")), 1, kMaximumColumns, &columns))
        addSummaryReason(&summaries, QStringLiteral("columns is invalid"));
    if (rows > 0 && columns > 0 && rows * columns > kMaximumCells)
        addSummaryReason(&summaries, QStringLiteral("cell count exceeds the V1 limit"));

    const QJsonValue cellsValue = response.value(QStringLiteral("cells"));
    const QJsonArray cellRows = cellsValue.toArray();
    if (!cellsValue.isArray() || cellRows.size() != rows) {
        addSummaryReason(&summaries, QStringLiteral("cells row count does not match rows"));
    } else {
        for (int row = 0; row < cellRows.size(); ++row) {
            if (!cellRows.at(row).isArray()) {
                addDetailReason(&details, QStringLiteral("cells[%1] is not an array").arg(row));
                continue;
            }
            const QJsonArray cellColumns = cellRows.at(row).toArray();
            if (cellColumns.size() != columns) {
                addDetailReason(&details, QStringLiteral("cells[%1] column count is invalid").arg(row));
                continue;
            }
            for (int column = 0; column < cellColumns.size(); ++column) {
                if (!cellColumns.at(column).isObject()) {
                    addDetailReason(&details,
                              QStringLiteral("cells[%1][%2] is not an object")
                                  .arg(row).arg(column));
                    continue;
                }
                const QJsonObject cell = cellColumns.at(column).toObject();
                const QJsonValue text = cell.value(QStringLiteral("text"));
                const QJsonValue confidence = cell.value(QStringLiteral("confidence"));
                const QJsonValue needsReview = cell.value(QStringLiteral("needs_review"));
                const double confidenceValue = confidence.toDouble(-1.0);
                // 2026-11-05（lead 复查）：后端把"这一格不可信"编码成负的 confidence
                // （-1.0 锚点格 / -2.0 判定为空），并同时置 needs_review=true。
                // 这里原来一刀 `confidenceValue < 0.0` 就判非法字段 ⇒ 整份响应不可发布，
                // 而客户其实拿到了一张完全可用的表（只是有几格要人工确认）。
                // 现在把 -2.0 ~ 0.0 当"复核哨兵"接受；比 -2.0 更负、NaN/Inf、类型错照旧判非法。
                const bool negativeReviewSentinel = qIsFinite(confidenceValue)
                    && confidenceValue < 0.0
                    && confidenceValue >= -2.0;
                if (!text.isString()
                    || !confidence.isDouble()
                    || !qIsFinite(confidenceValue)
                    || (confidenceValue < 0.0 && !negativeReviewSentinel)
                    || confidenceValue > 1.0
                    || !needsReview.isBool()) {
                    addDetailReason(&details,
                              QStringLiteral("cells[%1][%2] fields are invalid")
                                  .arg(row).arg(column));
                } else if (!text.toString().trimmed().isEmpty()
                           && confidenceValue < 0.78
                           && !needsReview.toBool()
                           && !cell.value(QStringLiteral("confidence_sentinel")).toBool(false)) {
                    addDetailReason(&details,
                              QStringLiteral("cells[%1][%2] has unmarked low confidence")
                                  .arg(row).arg(column));
                }
            }
        }
    }

    const QJsonValue spansValue = response.value(QStringLiteral("spans"));
    const QJsonArray spans = spansValue.toArray();
    QVector<bool> occupied(qMax(0, rows * columns), false);
    if (!spansValue.isArray()) {
        addSummaryReason(&summaries, QStringLiteral("spans is not an array"));
    } else {
        for (int index = 0; index < spans.size(); ++index) {
            if (!spans.at(index).isObject()) {
                addDetailReason(&details, QStringLiteral("spans[%1] is not an object").arg(index));
                continue;
            }
            const QJsonObject span = spans.at(index).toObject();
            int row = 0;
            int column = 0;
            int rowSpan = 0;
            int columnSpan = 0;
            const bool fieldsValid = readInteger(span.value(QStringLiteral("row")),
                                                 0,
                                                 qMax(0, rows - 1),
                                                 &row)
                && readInteger(span.value(QStringLiteral("column")),
                               0,
                               qMax(0, columns - 1),
                               &column)
                && readInteger(span.value(QStringLiteral("row_span")),
                               1,
                               qMax(1, rows),
                               &rowSpan)
                && readInteger(span.value(QStringLiteral("column_span")),
                               1,
                               qMax(1, columns),
                               &columnSpan)
                && row + rowSpan <= rows
                && column + columnSpan <= columns;
            if (!fieldsValid) {
                addDetailReason(&details, QStringLiteral("spans[%1] is out of bounds").arg(index));
                continue;
            }
            for (int coveredRow = row; coveredRow < row + rowSpan; ++coveredRow) {
                for (int coveredColumn = column;
                     coveredColumn < column + columnSpan;
                     ++coveredColumn) {
                    const int position = coveredRow * columns + coveredColumn;
                    if (position < 0 || position >= occupied.size())
                        continue;
                    if (occupied.at(position))
                        addDetailReason(&details,
                                  QStringLiteral("spans[%1] overlaps another span").arg(index));
                    occupied[position] = true;
                    if ((coveredRow != row || coveredColumn != column)
                        && coveredRow < cellRows.size()
                        && cellRows.at(coveredRow).isArray()) {
                        const QJsonArray coveredColumns = cellRows.at(coveredRow).toArray();
                        if (coveredColumn < coveredColumns.size()
                            && coveredColumns.at(coveredColumn).isObject()
                            && !coveredColumns.at(coveredColumn).toObject()
                                    .value(QStringLiteral("text")).toString().trimmed().isEmpty()) {
                            addDetailReason(&details,
                                      QStringLiteral("spans[%1] hides non-anchor text").arg(index));
                        }
                    }
                }
            }
        }
    }

    const QJsonValue stateValue = response.value(QStringLiteral("recognition_state"));
    const QString state = stateValue.toString();
    if (!stateValue.isString()
        || (state != QStringLiteral("verified")
            && state != QStringLiteral("needs_review")
            && state != QStringLiteral("blocked"))) {
        addSummaryReason(&summaries, QStringLiteral("recognition_state is invalid"));
    }
    const QJsonValue blockedValue = response.value(QStringLiteral("publication_blocked"));
    const bool blocked = blockedValue.toBool(true);
    if (!blockedValue.isBool())
        addSummaryReason(&summaries, QStringLiteral("publication_blocked is not a boolean"));
    if (blockedValue.isBool() && ((state == QStringLiteral("blocked")) != blocked))
        addSummaryReason(&summaries, QStringLiteral("recognition_state and publication_blocked disagree"));
    if (!response.value(QStringLiteral("publication_block_reasons")).isArray())
        addSummaryReason(&summaries, QStringLiteral("publication_block_reasons is not an array"));

    const QJsonValue imageQualityValue = response.value(QStringLiteral("image_quality"));
    if (!imageQualityValue.isObject()) {
        addSummaryReason(&summaries, QStringLiteral("image_quality is not an object"));
    } else {
        const QJsonObject imageQuality = imageQualityValue.toObject();
        if (!imageQuality.value(QStringLiteral("issues")).isArray()
            || !imageQuality.value(QStringLiteral("issue_labels")).isArray()
            || !imageQuality.value(QStringLiteral("needs_recapture")).isBool()) {
            addSummaryReason(&summaries, QStringLiteral("image_quality fields are invalid"));
        }
    }
    if (!response.value(QStringLiteral("rectified_image")).isString()
        || response.value(QStringLiteral("rectified_image")).toString().isEmpty()) {
        addSummaryReason(&summaries, QStringLiteral("rectified_image is invalid"));
    }

    const QJsonValue structureVerifiedValue = response.value(QStringLiteral("structure_verified"));
    const bool structureVerified = structureVerifiedValue.toBool(false);
    if (!structureVerifiedValue.isBool())
        addSummaryReason(&summaries, QStringLiteral("structure_verified is not a boolean"));
    const QJsonValue certificateValue = response.value(QStringLiteral("structure_certificate"));
    if (!structureVerified) {
        if (!certificateValue.isNull() && !certificateValue.isUndefined())
            addSummaryReason(&summaries, QStringLiteral("unverified structure has a certificate"));
        if (blockedValue.isBool() && !blocked)
            addSummaryReason(&summaries, QStringLiteral("unverified structure is not blocked"));
    } else if (!certificateValue.isObject()) {
        addSummaryReason(&summaries, QStringLiteral("verified structure has no certificate"));
    } else {
        const QJsonObject certificate = certificateValue.toObject();
        int certificateVersion = 0;
        int certifiedRows = 0;
        int certifiedColumns = 0;
        if (!readInteger(certificate.value(QStringLiteral("version")), 1, 1, &certificateVersion)
            || certificate.value(QStringLiteral("verified")).toBool(false) != true
            || !readInteger(certificate.value(QStringLiteral("rows")),
                            1,
                            kMaximumRows,
                            &certifiedRows)
            || !readInteger(certificate.value(QStringLiteral("columns")),
                            1,
                            kMaximumColumns,
                            &certifiedColumns)
            || certifiedRows != rows
            || certifiedColumns != columns
            || !certificate.value(QStringLiteral("spans")).isArray()
            || !jsonArraysEqual(certificate.value(QStringLiteral("spans")).toArray(), spans)
            || certificate.value(QStringLiteral("geometry_hash")).toString().isEmpty()
            || certificate.value(QStringLiteral("structure_hash")).toString().isEmpty()) {
            addSummaryReason(&summaries, QStringLiteral("structure_certificate is invalid"));
        }
    }

    QStringList merged = summaries;
    appendBounded(&merged, details);
    if (reasons)
        *reasons = merged;
    return merged.isEmpty();
}

bool OcrTableClient::canAutoPublish(const QJsonObject &response, QStringList *reasons)
{
    QStringList validationReasons;
    validateRecognitionResponse(response, &validationReasons);

    // The verdict reasons come first: they explain why the result cannot be
    // published, and they must survive a flood of per cell details.
    QStringList summaries;
    if (response.value(QStringLiteral("recognition_state")).toString()
        != QStringLiteral("verified")) {
        addSummaryReason(&summaries, QStringLiteral("recognition_state is not verified"));
    }
    if (response.value(QStringLiteral("publication_blocked")).toBool(true))
        addSummaryReason(&summaries, QStringLiteral("publication_blocked is true"));
    if (!response.value(QStringLiteral("structure_verified")).toBool(false))
        addSummaryReason(&summaries, QStringLiteral("structure_verified is false"));

    QStringList details;
    const QJsonArray rows = response.value(QStringLiteral("cells")).toArray();
    for (int row = 0; row < rows.size(); ++row) {
        const QJsonArray columns = rows.at(row).toArray();
        for (int column = 0; column < columns.size(); ++column) {
            if (columns.at(column).isObject()
                && columns.at(column).toObject()
                       .value(QStringLiteral("needs_review")).toBool(true)) {
                addDetailReason(&details,
                                QStringLiteral("cells[%1][%2] needs review").arg(row).arg(column));
            }
        }
    }

    QStringList merged = summaries;
    appendBounded(&merged, validationReasons);
    appendBounded(&merged, details);
    merged.removeDuplicates();
    if (reasons)
        *reasons = merged;
    return merged.isEmpty();
}
