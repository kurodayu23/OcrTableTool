#include "cameraocrclient.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QTextStream>
#include <QTimer>

int main(int argc, char *argv[])
{
    QCoreApplication application(argc, argv);
    QTextStream output(stdout);
    const QStringList arguments = application.arguments();
    if (arguments.size() < 3 || arguments.size() > 4) {
        output << "Usage: CameraOcrExample.exe <OcrBackend.exe> <work-root> [camera-index]\n";
        return 2;
    }

    const QString backendExecutable = arguments.at(1);
    const QString workRoot = arguments.at(2);
    const int cameraIndex = arguments.size() == 4 ? arguments.at(3).toInt() : -1;
    CameraOcrClient client(backendExecutable, workRoot);
    bool captureIssued = false;

    QObject::connect(&client, &CameraOcrClient::cameraReadyChanged,
                     [&](bool ready, int index, const QString &description) {
        output << "camera_index=" << index
               << " camera=" << description
               << " ready=" << (ready ? "true" : "false") << "\n";
        output.flush();
        if (ready && !captureIssued) {
            captureIssued = true;
            QTimer::singleShot(500, [&]() {
                if (!client.captureAndRecognize())
                    application.exit(1);
            });
        }
    });
    QObject::connect(&client, &CameraOcrClient::stageChanged,
                     [&](const QString &stage, const QString &message) {
        output << "stage=" << stage << " message=" << message << "\n";
        output.flush();
    });
    QObject::connect(&client, &CameraOcrClient::tableRecognized,
                     [&](int rows,
                         int columns,
                         const QJsonArray &,
                         const QJsonArray &,
                         const QJsonObject &response) {
        const QString responsePath = QDir(workRoot).filePath(
            QStringLiteral("last-camera-response.json"));
        QFile responseFile(responsePath);
        if (!responseFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            output << "error_code=RESPONSE_WRITE_FAILED\n";
            application.exit(1);
            return;
        }
        responseFile.write(QJsonDocument(response).toJson(QJsonDocument::Indented));
        responseFile.close();
        output << "rows=" << rows << " columns=" << columns
               << " response=" << QDir::toNativeSeparators(responsePath) << "\n";
        output.flush();
        application.exit(0);
    });
    QObject::connect(&client, &CameraOcrClient::failed,
                     [&](const QString &errorCode,
                         const QString &message,
                         bool retryable) {
        output << "error_code=" << errorCode
               << " retryable=" << (retryable ? "true" : "false")
               << " message=" << message << "\n";
        output.flush();
        application.exit(1);
    });

    QTimer::singleShot(0, [&]() {
        if (!client.startCamera(cameraIndex))
            application.exit(1);
    });
    return application.exec();
}
