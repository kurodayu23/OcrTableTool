# -*- coding: utf-8 -*-
"""测试套件完整性元测试（2026-11-02 新增，F19）。

防的是什么
-----------
``unittest`` 只收集 **``unittest.TestCase`` 子类**里的 ``test_*`` 方法。
于是下面两种写法**看着是测试、实际从不执行**：

    def test_something():            # 模块级函数 —— pytest 风格
        ...

    class FooTests:                 # 不继承 TestCase
        def test_something(self): ...

本机 venv 没有 pytest ⇒ 这些用例**一次都没跑过**，而套件汇总照样显示
「Ran N tests, OK」⇒ 一张**假绿牌**。这类"测试黑洞"最难发现：文件在、函数在、
名字以 test_ 开头，只是**永远不会被执行**。

本模块对**整个测试树**做静态核对：
  * 模块级 ``test_*`` 函数 ⇒ 必须没有（unittest 不收集）；
  * 类里的 ``test_*`` 方法，若所在类**最终不是** TestCase 子类 ⇒ 必须没有；
  * 例外：基类来自其它模块、无法在本文件内判定 ⇒ 只统计不报错（避免误伤）。

跑法::

    <venv> -B -m unittest discover -s backend\\tests -t backend\\tests
"""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PRODUCT = BACKEND.parent
# 扫描目录可用环境变量覆盖（变异证明脚本用它指向临时树，避免动真仓库）。
_override = os.environ.get("OCRTABLE_TEST_TREES", "").strip()
if _override:
    TEST_TREES = tuple(Path(part) for part in _override.split(os.pathsep) if part.strip())
else:
    TEST_TREES = (
        BACKEND / "tests",
        PRODUCT / "tests",
    )

# 来自其它模块、无法在本文件内判定来源的基类 —— 只统计，不报错。
_UNRESOLVED_BASES = {
    "TestCase",
    "IsolatedAsyncioTestCase",
}


def _is_unittest_case(class_node: ast.ClassDef, module: ast.Module) -> bool:
    """这个类是否最终继承自 unittest.TestCase（只在本模块内追基类链）。"""
    by_name = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.ClassDef)
    }
    seen: set[str] = set()

    def resolve(node: ast.ClassDef) -> bool:
        for base in node.bases:
            if isinstance(base, ast.Attribute) and base.attr in _UNRESOLVED_BASES:
                return True
            if isinstance(base, ast.Name):
                if base.id in _UNRESOLVED_BASES:
                    return True
                parent = by_name.get(base.id)
                if parent is None:
                    continue  # 外部基类：判不了
                if base.id in seen:
                    continue
                seen.add(base.id)
                if resolve(parent):
                    return True
        return False

    return resolve(class_node)


def _module_level_test_functions(module: ast.Module) -> list[str]:
    return [
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]


def _classes_with_tests(module: ast.Module) -> list[tuple[str, int]]:
    found = []
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        count = sum(
            1
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name.startswith("test")
        )
        if count:
            found.append((node.name, count))
    return found


class SuiteCompletenessTest(unittest.TestCase):
    def _test_files(self) -> list[Path]:
        files: list[Path] = []
        for tree in TEST_TREES:
            if tree.is_dir():
                files.extend(sorted(tree.glob("test_*.py")))
        return files

    def test_there_are_test_files(self):
        files = self._test_files()
        self.assertGreater(
            len(files),
            0,
            "一个测试文件都没找到（扫描目录配错了？）：%s" % [str(t) for t in TEST_TREES],
        )

    def test_no_module_level_test_functions(self):
        """模块级 test_* 函数在 unittest 下**不会被执行** ⇒ 必须没有。"""
        offenders = []
        for path in self._test_files():
            module = ast.parse(path.read_text(encoding="utf-8-sig"))
            for name in _module_level_test_functions(module):
                offenders.append("%s: %s" % (path.name, name))
        self.assertEqual(
            offenders,
            [],
            "这些模块级 test_* 函数永远不会被 unittest 执行（看着是测试、其实没跑）：\n"
            + "\n".join(offenders),
        )

    def test_no_test_methods_in_non_testcase_classes(self):
        """类里的 test_* 方法，若类不是 TestCase 子类 ⇒ 永远不会被执行。"""
        offenders = []
        for path in self._test_files():
            module = ast.parse(path.read_text(encoding="utf-8-sig"))
            for class_name, count in _classes_with_tests(module):
                class_node = next(
                    node
                    for node in module.body
                    if isinstance(node, ast.ClassDef) and node.name == class_name
                )
                if _is_unittest_case(class_node, module):
                    continue
                offenders.append("%s: %s（%d 个 test_ 方法）" % (path.name, class_name, count))
        self.assertEqual(
            offenders,
            [],
            "这些类不是 unittest.TestCase 子类 ⇒ 里面的 test_* 方法永远不会被执行：\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
