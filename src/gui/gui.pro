QT += core gui widgets multimedia multimediawidgets

CONFIG += c++11
msvc: CONFIG += no_batch

TEMPLATE = app
TARGET = OcrTableTool
DESTDIR = $$OUT_PWD/bin

include(../core/ocrtablecore.pri)

SOURCES += \
    $$PWD/main.cpp \
    $$PWD/mainwindow.cpp \
    $$PWD/cameracapturedialog.cpp \
    $$PWD/backendrunner.cpp \
    $$PWD/guitrace.cpp \
    $$PWD/backendlocator.cpp \
    $$PWD/imagepreview.cpp \
    $$PWD/imageviewerdialog.cpp

HEADERS += \
    $$PWD/mainwindow.h \
    $$PWD/cameracapturedialog.h \
    $$PWD/backendrunner.h \
    $$PWD/guitrace.h \
    $$PWD/backendlocator.h \
    $$PWD/imagepreview.h \
    $$PWD/imageviewerdialog.h

PRE_TARGETDEPS += \
    $$PWD/../../backend/ocr_backend.py \
    $$PWD/../../backend/recognition_scheduler.py \
    $$PWD/../../backend/table_pipeline.py

RESOURCES += $$PWD/resources.qrc

win32 {
    QMAKE_POST_LINK += $$QMAKE_COPY_DIR "$$shell_path($$PWD/../../backend)" "$$shell_path($$DESTDIR/backend)" $$escape_expand(\n\t)
}
