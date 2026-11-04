# -*- coding: utf-8 -*-
"""测试套件自身的守卫（2026-10-30 lead 复查）。

背景：`test_backend.py` 里 `TablePipelineTests` 曾有 4 组**重名测试**
（`test_letter_leading_code_column_flags_digit_lookalike` 等）——后定义覆盖前定义，
前 4 个从未执行，而 `unittest discover` 照常报"全绿"。这类问题会让门禁失真，
且不看 AST 根本发现不了。

这里用 AST 静态扫描守住两件事：
1. 同一个 TestCase 类内不允许出现重名测试方法（后定义会静默覆盖前定义）；
2. 所有测试方法名必须唯一可定位（同名跨类允许，同类内不允许）。
"""
from __future__ import annotations

import ast
import collections
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent


def _duplicate_tests(path: Path) -> dict[str, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        seen: dict[str, int] = collections.Counter()
        lines: dict[str, list[int]] = {}
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                seen[item.name] += 1
                lines.setdefault(item.name, []).append(item.lineno)
        for name, count in seen.items():
            if count > 1:
                problems["%s::%s" % (node.name, name)] = lines[name]
    return problems


class TestSuiteIntegrityTests(unittest.TestCase):
    def test_no_shadowed_duplicate_test_methods(self):
        """同类内重名测试会让前面的定义永不执行 —— 这是"假绿"的常见来源。"""
        offenders: list[str] = []
        for path in sorted(_TESTS_DIR.glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            for key, lines in _duplicate_tests(path).items():
                offenders.append("%s: %s 行号 %s" % (path.name, key, lines))
        self.assertEqual(offenders, [], "存在重名测试（后定义会覆盖前定义，前者永不执行）")

    def test_every_test_file_parses_and_has_tests(self):
        """每个测试文件都应至少包含一个测试方法（防止空壳文件。"""
        empty: list[str] = []
        for path in sorted(_TESTS_DIR.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = 0
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    found += sum(
                        1
                        for item in node.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith("test")
                    )
            if found == 0:
                empty.append(path.name)
        self.assertEqual(empty, [], "这些测试文件里没有任何测试方法")


if __name__ == "__main__":
    unittest.main()