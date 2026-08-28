#include "guitrace.h"

#include <QCoreApplication>
#include <QDateTime>
#include <QFile>
#include <QJsonDocument>

namespace GuiTrace {

void write(const QString &eventName, const QJsonObject &details)
{
    const QString path = QString::fromLocal8Bit(qgetenv("OCR_TABLE_GUI_TRACE"));
    if (path.isEmpty())
        return;
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text))
        return;
    QJsonObject record = details;
    record.insert(QStringLiteral("event"), eventName);
    record.insert(QStringLiteral("timestamp_utc"),
                  QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
    record.insert(QStringLiteral("process_id"),
                  static_cast<double>(QCoreApplication::applicationPid()));
    file.write(QJsonDocument(record).toJson(QJsonDocument::Compact));
    file.write("\n");
}

}
