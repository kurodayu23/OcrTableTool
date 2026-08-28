QT -= gui
QT += core

CONFIG += console c++11
CONFIG -= app_bundle
TEMPLATE = app
TARGET = OcrBackendConsoleExample

include(../qt/ocrtableclient.pri)
SOURCES += main.cpp
