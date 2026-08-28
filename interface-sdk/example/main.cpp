#include "ocrtableclient.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTextStream>
#include <QTimer>

int main(int argc, char *argv[])
{
    QCoreApplication application(argc, argv);
    QTextStream output(stdout);
    const QStringList arguments = application.arguments();
    const bool healthOnly = arguments.size() == 3
        && arguments.at(1) == QStringLiteral("--health");
    if (!healthOnly && arguments.size() != 4) {
        output << "Usage:\n"
               << "  OcrBackendConsoleExample.exe --health <OcrBackend.exe>\n"
               << "  OcrBackendConsoleExample.exe <OcrBackend.exe> <image> <output-directory>\n";
        return 2;
    }

    const QString backend = healthOnly ? arguments.at(2) : arguments.at(1);
    const QString image = healthOnly ? QString() : arguments.at(2);
    const QString outputDirectory = healthOnly ? QString() : arguments.at(3);
    if (!healthOnly && !QDir().mkpath(outputDirectory)) {
        output << "error_code=OUTPUT_DIRECTORY_INVALID\n";
        return 2;
    }

    OcrTableClient client(backend);
    QObject::connect(&client, &OcrTableClient::requestSucceeded,
                     [&](int requestId,
                         const QString &action,
                         const QJsonObject &response) {
        output << "request_id=" << requestId << " action=" << action << " status=ok\n";
        output.flush();
        if (action == QStringLiteral("health")) {
            if (healthOnly) {
                application.exit(0);
                return;
            }
            client.recognize(image, outputDirectory);
            return;
        }

        const QString responsePath = QDir(outputDirectory).filePath(QStringLiteral("response.json"));
        QFile responseFile(responsePath);
        if (!responseFile.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            output << "error_code=RESPONSE_WRITE_FAILED\n";
            application.exit(1);
            return;
        }
        responseFile.write(QJsonDocument(response).toJson(QJsonDocument::Indented));
        responseFile.close();

        QStringList reasons;
        const bool canAutoPublish = OcrTableClient::canAutoPublish(response, &reasons);
        output << "rows=" << response.value(QStringLiteral("rows")).toInt()
               << " columns=" << response.value(QStringLiteral("columns")).toInt()
               << " can_auto_publish=" << (canAutoPublish ? "true" : "false") << "\n";
        if (!reasons.isEmpty())
            output << "review_reasons=" << reasons.join(QStringLiteral("; ")) << "\n";
        output << "response=" << QDir::toNativeSeparators(responsePath) << "\n";
        output.flush();
        application.exit(0);
    });
    QObject::connect(&client, &OcrTableClient::requestFailed,
                     [&](int requestId,
                         const QString &action,
                         const QString &errorCode,
                         const QString &field,
                         const QString &message,
                         bool retryable) {
        output << "request_id=" << requestId
               << " action=" << action
               << " error_code=" << errorCode
               << " field=" << field
               << " retryable=" << (retryable ? "true" : "false")
               << " message=" << message << "\n";
        output.flush();
        application.exit(1);
    });
    QObject::connect(&client, &OcrTableClient::logMessage,
                     [&](const QString &message) {
        output << "[backend] " << message << "\n";
        output.flush();
    });

    QTimer::singleShot(0, [&]() {
        if (client.health() == 0)
            application.exit(1);
    });
    return application.exec();
}
