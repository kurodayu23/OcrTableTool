#ifndef CSVEXPORTER_H
#define CSVEXPORTER_H

#include <QByteArray>
#include <QString>

class TableData;

class CsvExporter
{
public:
    static QByteArray encode(const TableData &table);
    static bool writeFile(const QString &filePath, const TableData &table, QString *errorMessage = 0);

private:
    static QString escaped(const QString &value);
};

#endif // CSVEXPORTER_H
