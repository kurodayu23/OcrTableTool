QT += core gui widgets testlib

CONFIG += console testcase c++11
TEMPLATE = app
TARGET = tst_ocrtable_core

# Keep MSVC 2015 from batching UTF-8 BOM sources into one compiler process.
msvc: CONFIG += no_batch

INCLUDEPATH += $$PWD/../src/core $$PWD/../src/gui

SOURCES += \
    $$PWD/tst_core.cpp \
    $$PWD/../src/gui/backendrunner.cpp \
    $$PWD/../src/gui/guitrace.cpp \
    $$PWD/../src/gui/backendlocator.cpp \
    $$PWD/../src/gui/imagepreview.cpp \
    $$PWD/../src/core/tabledata.cpp \
    $$PWD/../src/core/csvexporter.cpp

HEADERS += \
    $$PWD/../src/core/tabledata.h \
    $$PWD/../src/core/csvexporter.h \
    $$PWD/../src/gui/backendrunner.h \
    $$PWD/../src/gui/guitrace.h \
    $$PWD/../src/gui/imagepreview.h \
    $$PWD/../src/gui/backendlocator.h
