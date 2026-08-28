QT += core multimedia multimediawidgets
CONFIG += console c++11
CONFIG -= app_bundle
TEMPLATE = app
TARGET = CameraOcrExample

include(../qt/ocrtableclient.pri)

SOURCES += camera-main.cpp
