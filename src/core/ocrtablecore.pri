!contains(DEFINES, OCRTABLECORE_PRI) {
    DEFINES += OCRTABLECORE_PRI
    INCLUDEPATH += $$PWD

    HEADERS += \
        $$PWD/tabledata.h \
        $$PWD/csvexporter.h

    SOURCES += \
        $$PWD/tabledata.cpp \
        $$PWD/csvexporter.cpp
}
