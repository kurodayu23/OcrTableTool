#include "backendlocator.h"

#include <QDir>
#include <QFileInfo>
#include <QStandardPaths>

namespace
{
// 本产品依赖（backend/requirements.txt）的顶层模块名。venv 里至少装了其中
// 一个，才可能是给本产品准备的虚拟环境。
//
// findings：原来的「首选」判定是 `originalRoot.contains(child.dirName())`，
// 也就是拿搜索根的**路径子串**去猜哪个是"本工程"。同一台机器上放着的
// 其它工程（名字里带同样的项目名，或者构建目录名里带项目名）很容易命中，
// 于是拿别人的解释器去跑本产品后端，依赖对不上就只看到 ImportError。
QStringList productDependencyMarkers()
{
    return QStringList()
        << QStringLiteral("cv2")            // opencv-python
        << QStringLiteral("openvino")       // openvino
        << QStringLiteral("onnxruntime")    // onnxruntime
        << QStringLiteral("rapidocr");      // rapidocr
}

QStringList sitePackageDirectories(const QString &virtualEnvironmentRoot)
{
    const QDir root(virtualEnvironmentRoot);
    QStringList directories;
#ifdef Q_OS_WIN
    directories << root.filePath(QStringLiteral("Lib/site-packages"));
#else
    // Linux 的 venv 把包放在 lib/pythonX.Y/site-packages，版本号不能写死。
    const QFileInfoList versions = QDir(root.filePath(QStringLiteral("lib"))).entryInfoList(
        QStringList() << QStringLiteral("python*"),
        QDir::Dirs | QDir::NoDotAndDotDot,
        QDir::Name);
    for (int index = 0; index < versions.size(); ++index) {
        directories << QDir(versions.at(index).absoluteFilePath())
                           .filePath(QStringLiteral("site-packages"));
    }
    directories << root.filePath(QStringLiteral("lib/python3/site-packages"));
#endif
    return directories;
}

QString projectPython(const QDir &projectDirectory)
{
#ifdef Q_OS_WIN
    const QString candidate = projectDirectory.filePath(QStringLiteral(".venv/Scripts/python.exe"));
#else
    const QString candidate = projectDirectory.filePath(QStringLiteral(".venv/bin/python3"));
#endif
    return QFileInfo::exists(candidate) ? QFileInfo(candidate).absoluteFilePath() : QString();
}

QStringList pathDirectories()
{
#ifdef Q_OS_WIN
    const QChar separator = QLatin1Char(';');
#else
    const QChar separator = QLatin1Char(':');
#endif
    const QStringList entries = QString::fromLocal8Bit(qgetenv("PATH"))
                                    .split(separator, QString::SkipEmptyParts);
    QStringList directories;
    for (int index = 0; index < entries.size(); ++index) {
        const QString directory = QDir::fromNativeSeparators(entries.at(index)).trimmed();
        if (!directory.isEmpty())
            directories << directory;
    }
    return directories;
}
}

QStringList BackendLocator::interpreterFileNames()
{
#ifdef Q_OS_WIN
    return QStringList() << QStringLiteral("python.exe");
#else
    // 麒麟的默认安装里 python 往往不存在，只有 python3（甚至有机器只有 python3.x）。
    return QStringList() << QStringLiteral("python3") << QStringLiteral("python");
#endif
}

QString BackendLocator::findInterpreter(const QStringList &candidateNames,
                                        const QStringList &searchDirectories)
{
    for (int nameIndex = 0; nameIndex < candidateNames.size(); ++nameIndex) {
        const QString name = candidateNames.at(nameIndex).trimmed();
        if (name.isEmpty())
            continue;
        for (int directoryIndex = 0; directoryIndex < searchDirectories.size(); ++directoryIndex) {
            const QString directory = searchDirectories.at(directoryIndex).trimmed();
            if (directory.isEmpty())
                continue;
            const QFileInfo candidate(QDir(directory).filePath(name));
            // 不用 isExecutable()：Windows 上它只认 .exe/.com/.bat 后缀，
            // 而 Linux 侧我们探测的正是无后缀的 python3。
            if (candidate.isFile() && candidate.isReadable())
                return candidate.absoluteFilePath();
        }
    }
    return QString();
}

QString BackendLocator::findSystemInterpreter()
{
    const QStringList names = interpreterFileNames();
    for (int index = 0; index < names.size(); ++index) {
        const QString found = QStandardPaths::findExecutable(names.at(index));
        if (!found.isEmpty())
            return QFileInfo(found).absoluteFilePath();
    }
    // 再按 PATH 的目录顺序手工探测一遍：部分麒麟镜像上解释器没有可执行位，
    // QStandardPaths::findExecutable 会漏掉真实存在的 python3。
    return findInterpreter(names, pathDirectories());
}

bool BackendLocator::providesProductDependencies(const QString &virtualEnvironmentRoot)
{
    if (virtualEnvironmentRoot.isEmpty())
        return false;

    const QStringList siteDirectories = sitePackageDirectories(virtualEnvironmentRoot);
    const QStringList markers = productDependencyMarkers();
    for (int directoryIndex = 0; directoryIndex < siteDirectories.size(); ++directoryIndex) {
        const QDir siteDirectory(siteDirectories.at(directoryIndex));
        if (!siteDirectory.exists())
            continue;
        for (int markerIndex = 0; markerIndex < markers.size(); ++markerIndex) {
            const QString marker = markers.at(markerIndex);
            // 装好的包要么是模块目录（cv2/、openvino/），要么至少留下
            // <名字>-<版本>.dist-info。
            if (QFileInfo::exists(siteDirectory.filePath(marker)))
                return true;
            const QStringList distributions = siteDirectory.entryList(
                QStringList() << (marker + QStringLiteral("-*")),
                QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System);
            if (!distributions.isEmpty())
                return true;
        }
    }
    return false;
}

QString BackendLocator::findPython(const QString &backendScript, const QStringList &searchRoots)
{
    QStringList roots = searchRoots;
    if (!backendScript.isEmpty())
        roots.prepend(QFileInfo(backendScript).absolutePath());

    QString fallbackCandidate;
    for (int rootIndex = 0; rootIndex < roots.size(); ++rootIndex) {
        QDir directory(QDir::cleanPath(roots.at(rootIndex)));
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
                // 严格判定：venv 里装了本产品依赖才算「本工程的解释器」。
                // 命中的第一个（按目录名顺序）直接返回，保证确定且不依赖路径子串。
                if (providesProductDependencies(child.filePath(QStringLiteral(".venv"))))
                    return candidate;
                if (fallbackCandidate.isEmpty())
                    fallbackCandidate = candidate;
            }
            if (!directory.cdUp())
                break;
        }
    }
    // 一个带依赖标记的 venv 都没找到时，退回第一个结构上像本工程 venv 的候选，
    // 保持"能跑就先用起来"的老行为（测试用的空 venv 也依赖这条兜底）。
    return fallbackCandidate;
}
