#include "backendlocator.h"

#include <QDir>
#include <QFileInfo>

namespace
{
QString projectPython(const QDir &projectDirectory)
{
#ifdef Q_OS_WIN
    const QString candidate = projectDirectory.filePath(QStringLiteral(".venv/Scripts/python.exe"));
#else
    const QString candidate = projectDirectory.filePath(QStringLiteral(".venv/bin/python3"));
#endif
    return QFileInfo::exists(candidate) ? QFileInfo(candidate).absoluteFilePath() : QString();
}
}

QString BackendLocator::findPython(const QString &backendScript, const QStringList &searchRoots)
{
    QStringList roots = searchRoots;
    if (!backendScript.isEmpty())
        roots.prepend(QFileInfo(backendScript).absolutePath());

    QString fallback;
    for (int rootIndex = 0; rootIndex < roots.size(); ++rootIndex) {
        const QString originalRoot = QDir::cleanPath(roots.at(rootIndex));
        QDir directory(originalRoot);
        for (int level = 0; level < 7; ++level) {
            const QString direct = projectPython(directory);
            if (!direct.isEmpty())
                return direct;

            const QFileInfoList children = directory.entryInfoList(
                QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
            for (int index = 0; index < children.size(); ++index) {
                const QDir child(children.at(index).absoluteFilePath());
                if (!QFileInfo::exists(child.filePath(QStringLiteral("backend/ocr_backend.py"))))
                    continue;
                const QString candidate = projectPython(child);
                if (candidate.isEmpty())
                    continue;
                if (originalRoot.contains(child.dirName(), Qt::CaseInsensitive))
                    return candidate;
                if (fallback.isEmpty())
                    fallback = candidate;
            }
            if (!directory.cdUp())
                break;
        }
    }
    return fallback;
}
