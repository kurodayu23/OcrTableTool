#ifndef BACKENDLOCATOR_H
#define BACKENDLOCATOR_H

#include <QString>
#include <QStringList>

namespace BackendLocator
{
QString findPython(const QString &backendScript, const QStringList &searchRoots);
}

#endif // BACKENDLOCATOR_H
