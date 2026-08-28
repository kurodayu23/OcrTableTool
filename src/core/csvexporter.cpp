#include "csvexporter.h"

#include "tabledata.h"

#include <QSaveFile>

QString CsvExporter::escaped(const QString &value)
{
    if (!value.contains(QLatin1Char(',')) &&
        !value.contains(QLatin1Char('"')) &&
        !value.contains(QLatin1Char('\n')) &&
        !value.contains(QLatin1Char('\r'))) {
        return value;
    }

    QString quoted = value;
    quoted.replace(QStringLiteral("\""), QStringLiteral("\"\""));
    return QLatin1Char('"') + quoted + QLatin1Char('"');
}

QByteArray CsvExporter::encode(const TableData &table)
{
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
