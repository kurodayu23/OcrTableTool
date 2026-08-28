TEMPLATE = subdirs
CONFIG += ordered

SUBDIRS += gui tests

gui.file = src/gui/gui.pro
tests.file = tests/tests.pro
tests.depends = gui
