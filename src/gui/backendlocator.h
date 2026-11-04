#ifndef BACKENDLOCATOR_H
#define BACKENDLOCATOR_H

#include <QString>
#include <QStringList>

namespace BackendLocator
{
// 本平台解释器可能的可执行文件名，按优先级排列。
// Windows 只有 python.exe；麒麟 / 其它 Linux 发行版上解释器常常只叫 python3，
// 过去找不到 venv 时兜底落成裸名 python，在那些机器上直接起不来。
QStringList interpreterFileNames();

// 按候选名 × 目录逐个探测，返回第一个真实存在的解释器绝对路径。
// 目录里什么都没有时返回空串 —— 绝不编出一个裸名字当解释器。
QString findInterpreter(const QStringList &candidateNames,
                        const QStringList &searchDirectories);

// 按 interpreterFileNames() 在系统 PATH 上逐个探测；找不到返回空串。
QString findSystemInterpreter();

// 该虚拟环境里是否装了本产品的依赖标记。用来区分「本工程的 venv」和
// 同机其它工程的 venv：过去用路径子串匹配判首选，会把别人的 venv 选中。
bool providesProductDependencies(const QString &virtualEnvironmentRoot);

QString findPython(const QString &backendScript, const QStringList &searchRoots);
}

#endif // BACKENDLOCATOR_H
