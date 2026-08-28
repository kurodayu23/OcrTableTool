QT -= gui
QT += core testlib

CONFIG += console testcase c++11
CONFIG -= app_bundle
TEMPLATE = app
TARGET = tst_ocrtableclient

include(../qt/ocrtableclient.pri)
SOURCES += tst_ocrtableclient.cpp
