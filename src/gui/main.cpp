#include "mainwindow.h"

#include <QApplication>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QLocale>
#include <QTimer>
#include <QTranslator>

int main(int argc, char *argv[])
{
    QApplication::setAttribute(Qt::AA_EnableHighDpiScaling);
    QApplication::setAttribute(Qt::AA_UseHighDpiPixmaps);
    QApplication::setAttribute(Qt::AA_SynthesizeMouseForUnhandledTouchEvents, true);
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
    const QStringList arguments = application.arguments();
    if (arguments.size() > 1) {
        const QString imagePath = arguments.at(1);
        QTimer::singleShot(0, &window, [&window, imagePath]() {
            window.loadImageFile(imagePath);
        });
    }
    return application.exec();
}
