# -*- coding: utf-8 -*-
"""Qt 测试源码完整性检查（2026-11-02 新增，F26）。

背景
----
Python 侧的"测试黑洞"（模块级 ``def test_x()``、非 TestCase 类里的 ``test_*``
方法）已由 ``backend/tests/test_suite_completeness.py`` 覆盖；
**C++/Qt 侧一直没有任何护栏**，而 Qt 侧的黑洞比 Python 侧更隐蔽：

* ``QTest`` 只跑 ``private slots:`` 里声明过的函数。测试类里新增了一个
  ``void Foo::bar()`` 却忘了在 ``private slots:`` 里声明 ``void bar();``
  ⇒ 这个用例**一次都不会跑**，而套件汇总照样显示
  ``Totals: N passed, 0 failed`` —— 一张假绿牌。
* 新增了一个测试类却没改 ``QTEST_MAIN(X)`` ⇒ 整个类永远不执行。
* 测试函数里只有 ``QVERIFY(true)`` 之类的平凡断言 ⇒ 看着是测试、实际什么都没断。

本文件把上面三类钉死（纯静态分析，不编译、不跑 Qt）。

跑法::

    <venv> -B -m unittest tests/test_qt_test_integrity.py
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

QT_TEST_FILES = (
    "tests/tst_core.cpp",
    "tests/tst_gui.cpp",
    "interface-sdk/tests/tst_ocrtableclient.cpp",
)

CLASS_RE = re.compile(r"(?m)^\s*class\s+(\w+)\s*:\s*public\s+QObject")
SLOT_BLOCK_RE = re.compile(r"private\s+slots\s*:(.*?)(?=\n\s*(?:public|protected|private|signals)\s*[:;]|\Z)", re.S)
SLOT_DECL_RE = re.compile(r"(?m)^\s*void\s+(\w+)\s*\(\s*\)\s*;")
OUT_OF_LINE_DEF_RE = re.compile(r"(?m)^\s*void\s+(\w+)::(\w+)\s*\(")
INLINE_DEF_RE = re.compile(r"(?m)^\s*void\s+(\w+)\s*\(\s*\)\s*(?:\n\s*)?\{")
QTEST_MAIN_RE = re.compile(r"QTEST_(?:APPLESS_)?MAIN\s*\(\s*(\w+)\s*\)")
ASSERT_RE = re.compile(r"\b(QVERIFY2?|QCOMPARE|QFAIL|QSKIP|QTRY_[A-Z]+|QTEST_ASSERT\w*)\b")
TRIVIAL_ASSERT_RE = re.compile(r"QVERIFY\s*\(\s*(?:true|1)\s*\)")
INIT_CLEANUP = ("initTestCase", "cleanupTestCase")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    out = []
    for line in text.splitlines():
        cut = line.find("//")
        out.append(line if cut < 0 else line[:cut])
    return "\n".join(out)


def _balanced_body(text: str, brace_at: int) -> str:
    depth = 0
    index = brace_at
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_at:index + 1]
        index += 1
    return text[brace_at:]


class QtTestSource:
    """一份 Qt 测试源码的解析结果。"""

    def __init__(self, path: Path):
        self.path = path
        raw = path.read_text(encoding="utf-8", errors="replace")
        self.text = _strip_comments(raw)
        self.classes = CLASS_RE.findall(self.text)
        self.main_classes = QTEST_MAIN_RE.findall(self.text)
        self.slot_blocks = SLOT_BLOCK_RE.findall(self.text)
        self.declared_slots = set()
        self.inline_slots = {}
        for block in self.slot_blocks:
            self.declared_slots.update(SLOT_DECL_RE.findall(block))
            for name, body in self._inline_methods(block):
                self.inline_slots[name] = body
        self.out_of_line = {}
        for cls, name in OUT_OF_LINE_DEF_RE.findall(self.text):
            self.out_of_line.setdefault(cls, set()).add(name)

    @staticmethod
    def _inline_methods(block: str):
        """取出 ``private slots:`` 块里**内联定义**的方法名 → 函数体。"""
        result = []
        for match in INLINE_DEF_RE.finditer(block):
            brace = match.end() - 1
            result.append((match.group(1), _balanced_body(block, brace)))
        return result

    def slot_bodies(self) -> dict:
        """槽方法名 → 函数体（内联的取内联体，外联的按 ``Class::method`` 定位）。"""
        bodies = dict(self.inline_slots)
        for cls, names in self.out_of_line.items():
            for name in names:
                if name in bodies:
                    continue
                pattern = re.compile(
                    r"\bvoid\s+%s::%s\s*\([^)]*\)\s*\{" % (re.escape(cls), re.escape(name))
                )
                match = pattern.search(self.text)
                if match:
                    bodies[name] = _balanced_body(self.text, match.end() - 1)
        return bodies


class QtTestIntegrityTest(unittest.TestCase):
    """Qt 测试源码的结构完整性。"""

    def test_all_qt_test_files_exist(self):
        for rel in QT_TEST_FILES:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "Qt 测试源缺失：%s" % path)

    def test_each_test_class_is_instantiated(self):
        """有 private slots 的测试类必须真的被跑起来，否则整类永不执行。

        两种跑法都算：
          * ``QTEST_MAIN(X)`` / ``QTEST_APPLESS_MAIN(X)`` 宏；
          * 手写 ``int main()`` 里 ``X tests;`` + ``QTest::qExec(&tests, ...)``
            （``tests\\tst_gui.cpp`` 就是后者 —— 它需要自己的 QApplication）。
        """
        for rel in QT_TEST_FILES:
            source = QtTestSource(ROOT / rel)
            with self.subTest(file=rel):
                if not source.classes:
                    self.skipTest("该文件里没有 QObject 测试类")
                for cls in source.classes:
                    instantiated = cls in source.main_classes or re.search(
                        r"\b%s\s+\w+\s*;" % re.escape(cls), source.text
                    )
                    self.assertTrue(
                        instantiated,
                        "%s：测试类 %s 既没被 QTEST_*_MAIN 实例化，也没在 main 里 new 出来"
                        " ⇒ 整类永不执行" % (rel, cls),
                    )

    def test_declared_slots_are_all_defined(self):
        """``private slots:`` 里声明的方法必须有定义（否则链接期才炸）。"""
        for rel in QT_TEST_FILES:
            source = QtTestSource(ROOT / rel)
            bodies = source.slot_bodies()
            with self.subTest(file=rel):
                missing = sorted(source.declared_slots - set(bodies))
                self.assertEqual(
                    missing,
                    [],
                    "%s：声明了却没定义的槽方法：%s" % (rel, missing),
                )

    def test_no_orphan_test_methods(self):
        """类里定义了、却没进 ``private slots:`` 的方法 ⇒ 永不执行（测试黑洞）。"""
        for rel in QT_TEST_FILES:
            source = QtTestSource(ROOT / rel)
            bodies = source.slot_bodies()
            with self.subTest(file=rel):
                orphans = set()
                for cls, names in source.out_of_line.items():
                    for name in names:
                        if name in source.declared_slots:
                            continue
                        # 只看**测试类**里的方法（类名出现在 QTEST_MAIN 里）
                        if cls not in source.main_classes:
                            continue
                        if name in INIT_CLEANUP:
                            continue
                        orphans.add("%s::%s" % (cls, name))
                self.assertEqual(
                    sorted(orphans),
                    [],
                    "%s：这些方法定义了却不是槽 ⇒ 永不执行：%s" % (rel, sorted(orphans)),
                )

    def test_every_slot_has_a_real_assertion(self):
        """每个测试函数至少有一条**非平凡**断言。"""
        for rel in QT_TEST_FILES:
            source = QtTestSource(ROOT / rel)
            bodies = source.slot_bodies()
            with self.subTest(file=rel):
                weak = []
                for name in sorted(source.declared_slots):
                    if name in INIT_CLEANUP:
                        continue
                    body = bodies.get(name, "")
                    if not ASSERT_RE.search(body):
                        weak.append(name + "（没有任何断言）")
                        continue
                    strong = [
                        match
                        for match in ASSERT_RE.finditer(body)
                        if not TRIVIAL_ASSERT_RE.match(body[match.start():match.start() + 40])
                    ]
                    if not strong:
                        weak.append(name + "（只有平凡断言）")
                self.assertEqual(
                    weak,
                    [],
                    "%s：这些测试函数没有有效断言：%s" % (rel, weak),
                )


if __name__ == "__main__":
    unittest.main()
