QT += core multimedia multimediawidgets

# Qt 5.9/MSVC 2015 may batch several large translation units into one cl.exe
# invocation. Disable that build optimization so the SDK also builds reliably
# in older product projects.
CONFIG += c++11 no_batch

win32-msvc*: QMAKE_CXXFLAGS += /utf-8

INCLUDEPATH += $$PWD
DEPENDPATH += $$PWD

HEADERS += \
    $$PWD/ocrtableclient.h \
    $$PWD/cameraocrclient.h

SOURCES += \
    $$PWD/ocrtableclient.cpp \
    $$PWD/cameraocrclient.cpp
