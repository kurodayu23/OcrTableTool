#ifndef GUITRACE_H
#define GUITRACE_H

#include <QJsonObject>
#include <QString>

namespace GuiTrace {

void write(const QString &eventName, const QJsonObject &details = QJsonObject());

}

#endif // GUITRACE_H
