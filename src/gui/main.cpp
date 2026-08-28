#include "mainwindow.h"

#include <QApplication>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QLocale>
#include <QTranslator>

int main(int argc, char *argv[])
{
    QApplication::setAttribute(Qt::AA_EnableHighDpiScaling);
    QApplication::setAttribute(Qt::AA_UseHighDpiPixmaps);
    QApplication application(argc, argv);
    application.setApplicationName(QStringLiteral("OcrTableTool"));
    application.setOrganizationName(QStringLiteral("AP"));
    application.setStyle(QStringLiteral("Fusion"));

    QLocale::setDefault(QLocale(QLocale::Chinese, QLocale::China));
    QTranslator qtTranslator;
    const QString translationDirectory = QDir(QCoreApplication::applicationDirPath())
                                             .filePath(QStringLiteral("translations"));
    if (qtTranslator.load(QStringLiteral("qt_zh_CN"), translationDirectory))
        application.installTranslator(&qtTranslator);

    QFile styleFile(QStringLiteral(":/styles/app.qss"));
    if (styleFile.open(QIODevice::ReadOnly))
        application.setStyleSheet(QString::fromUtf8(styleFile.readAll()));

    MainWindow window;
    window.show();
    return application.exec();
}
