# -*- coding: utf-8 -*-
"""静态门禁「空扫描」的行为测试（2026-11-02 新增，F20）。

背景（真实假绿）
----------------
``scripts/gate_checks.py`` 的两个静态门禁在**一个文件都没扫到**时，
以前照样报 ok=True：

* ``bom``：目录改名 / 指错路径 ⇒ 扫到 0 个 ``.cpp/.h`` ⇒ 报
  「0 个 .cpp/.h，含非 ASCII 的全部带 BOM」——**一张真空 PASS**；
* ``identity``：同理 ⇒ 报「0 个测试模块里没有指向本机项目树的绝对路径」。

判据：**没查 ≠ 通过**。现在扫不到任何文件 ⇒ FAIL。

跑法::

    <venv> -B -m unittest tests/test_gate_vacuous_scan.py

也由 ``scripts\\verify_all.ps1`` 作为 ``gate-vacuous-unit`` 一项门禁调用。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "gate_checks.py"


def _load():
    spec = importlib.util.spec_from_file_location("gate_checks_vacuous", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate_checks = _load()

CLEAN_CPP = "// ascii only\nint main() { return 0; }\n"
NON_ASCII_CPP_NO_BOM = '// 中文注释：这张表\nint main() { return 0; }\n'
NON_ASCII_CPP_WITH_BOM = '﻿// 中文注释：这张表\nint main() { return 0; }\n'
CLEAN_TEST = 'import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n'
VIOLATING_TEST = 'ROOT = "D:\\\\AP\\\\AP7000\\\\OcrTableTool\\\\backend"\n'


def _write(root: Path, relative: str, content: str, encoding: str = "utf-8") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode(encoding))
    return path


class BomVacuousScanTest(unittest.TestCase):
    def test_empty_tree_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = gate_checks.check_bom(Path(tmp))
            self.assertFalse(
                result["ok"],
                "一个 .cpp/.h 都没扫到却判通过（真空 PASS）：%s" % result["summary"],
            )
            self.assertEqual(result["scanned"], 0)

    def test_clean_source_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "src/main.cpp", CLEAN_CPP)
            result = gate_checks.check_bom(root)
            self.assertTrue(result["ok"], "干净的源码被判 FAIL：%s" % result["summary"])
            self.assertEqual(result["scanned"], 1)

    def test_non_ascii_without_bom_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "src/main.cpp", NON_ASCII_CPP_NO_BOM)
            result = gate_checks.check_bom(root)
            self.assertFalse(result["ok"], "缺 BOM 的中文源码没有被报出来")
            self.assertEqual(result["missing_bom"], ["src/main.cpp"])

    def test_non_ascii_with_bom_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "src/main.cpp", NON_ASCII_CPP_WITH_BOM)
            result = gate_checks.check_bom(root)
            self.assertTrue(result["ok"], "带 BOM 的中文源码被判 FAIL：%s" % result["summary"])


class IdentityVacuousScanTest(unittest.TestCase):
    def test_tree_without_test_dirs_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir(parents=True, exist_ok=True)
            result = gate_checks.check_test_tree_identity(root)
            self.assertFalse(
                result["ok"],
                "一个测试模块都没扫到却判通过（真空 PASS）：%s" % result["summary"],
            )
            self.assertEqual(result["scanned"], 0)

    def test_clean_test_tree_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "tests/test_clean.py", CLEAN_TEST)
            result = gate_checks.check_test_tree_identity(root)
            self.assertTrue(result["ok"], "干净的测试树被判 FAIL：%s" % result["summary"])
            self.assertEqual(result["scanned"], 1)

    def test_violating_test_still_fails(self):
        """反向护栏：判据不能因为加了"空扫描"就变松。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "tests/test_bad.py", VIOLATING_TEST)
            result = gate_checks.check_test_tree_identity(root)
            self.assertFalse(result["ok"], "写死本机路径没有被报出来")
            self.assertEqual(len(result["violations"]), 1)


if __name__ == "__main__":
    unittest.main()
