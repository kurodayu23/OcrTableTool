QT += core gui widgets testlib multimedia multimediawidgets

CONFIG += console testcase c++11
TEMPLATE = app
TARGET = tst_ocrtable_gui
DESTDIR = $$OUT_PWD/bin

# Keep MSVC 2015 from batching UTF-8 BOM sources into one compiler process.
msvc: CONFIG += no_batch

# QFINDTESTDATA needs this macro to locate the test data directory.
DEFINES += QT_TESTCASE_BUILDDIR

INCLUDEPATH += $$PWD/../src/core $$PWD/../src/gui $$PWD/../interface-sdk/qt

SOURCES += \
    $$PWD/tst_gui.cpp \
    $$PWD/../src/core/tabledata.cpp \
    $$PWD/../src/core/csvexporter.cpp \
    $$PWD/../src/gui/mainwindow.cpp \
    $$PWD/../src/gui/cameracapturedialog.cpp \
    $$PWD/../src/gui/backendrunner.cpp \
    $$PWD/../src/gui/guitrace.cpp \
    $$PWD/../src/gui/backendlocator.cpp \
    $$PWD/../src/gui/imagepreview.cpp \
    $$PWD/../src/gui/imageviewerdialog.cpp \
    $$PWD/../interface-sdk/qt/ocrtableclient.cpp

HEADERS += \
    $$PWD/../src/core/tabledata.h \
    $$PWD/../src/core/csvexporter.h \
    $$PWD/../src/gui/mainwindow.h \
    $$PWD/../src/gui/cameracapturedialog.h \
    $$PWD/../src/gui/backendrunner.h \
    $$PWD/../src/gui/guitrace.h \
    $$PWD/../src/gui/backendlocator.h \
    $$PWD/../src/gui/imagepreview.h \
    $$PWD/../src/gui/imageviewerdialog.h \
    $$PWD/../interface-sdk/qt/ocrtableclient.h

# The window icon and the stylesheet both come from the product resources,
# so the test has to keep the same look as the shipped application.
RESOURCES += $$PWD/../src/gui/resources.qrc
