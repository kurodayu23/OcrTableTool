# -*- coding: utf-8 -*-
"""身份门禁**扫描范围**的行为测试（2026-11-02 新增，F18）。

背景（真实漏检）
----------------
``scripts/gate_checks.py`` 的 ``check_test_tree_identity()`` 以前**只扫 root
（仓库树）**。但产品树才是新测试的产地：新写的用例先落在产品树，**同步**进仓库
之后才会被这道门禁盯上 ⇒ 产品树里写死本机路径的测试文件，在同步之前**没人管**。

修法：给了 ``--product`` 时，**两棵树都扫**，违规路径带 ``product/`` 前缀。

跑法::

    <venv> -B -m unittest tests/test_gate_identity_scope.py

也由 ``scripts\\verify_all.ps1`` 作为 ``gate-identity-unit`` 一项门禁调用。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "gate_checks.py"
# ⚠ 刻意**不**写死本机路径：身份门禁会抓「写死本项目本机路径」的测试文件，
#   而本文件自己就在被扫范围里（tests/）⇒ 写死就是自曝。
#   产品树 = 本文件所在树的上一级（产品树本身就是真源）。
PRODUCT = ROOT

VIOLATION = 'ROOT = "D:\\\\AP\\\\AP7000\\\\OcrTableTool\\\\backend"\n'
ALLOWED = 'ROOT = Path("D:\\\\AP\\\\AP7000\\\\OcrTableTool") / "x"  # gate:allow-machine-path\n'
COMMENT_ONLY = '# 2026-10-28：原来这里写死 D:\\\\AP\\\\AP7000\\\\OcrTableTool\n'
OTHER_PATH = 'IMAGE = "C:/input.png"\n'


def _load():
    spec = importlib.util.spec_from_file_location("gate_checks_scope", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate_checks = _load()


def _tree(base: Path, name: str, content: str) -> Path:
    directory = base / name / "backend" / "tests"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "test_sample.py").write_text(content, encoding="utf-8")
    return base / name


class IdentityScopeTest(unittest.TestCase):
    def test_violation_in_repo_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _tree(base, "repo", VIOLATION)
            product = _tree(base, "product", OTHER_PATH)
            result = gate_checks.check_test_tree_identity(repo, product=product)
            self.assertFalse(result["ok"], "仓库里的写死路径没有被报出来")
            self.assertEqual(len(result["violations"]), 1)

    def test_violation_in_product_tree_is_reported(self):
        """★ 产品树里的写死路径同样必须被抓（同步之前就有人管）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _tree(base, "repo", OTHER_PATH)
            product = _tree(base, "product", VIOLATION)
            result = gate_checks.check_test_tree_identity(repo, product=product)
            self.assertFalse(
                result["ok"],
                "产品树里的写死路径没有被报出来（同步前是盲区）",
            )
            self.assertEqual(len(result["violations"]), 1)
            self.assertTrue(
                result["violations"][0]["path"].startswith("product/"),
                "产品树的违规没有标记出来：%s" % result["violations"][0]["path"],
            )

    def test_both_trees_are_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _tree(base, "repo", VIOLATION)
            product = _tree(base, "product", VIOLATION)
            result = gate_checks.check_test_tree_identity(repo, product=product)
            self.assertEqual(len(result["violations"]), 2)
            self.assertEqual(result["scanned"], 2)

    def test_same_tree_is_not_scanned_twice(self):
        """root 与 product 指向同一棵树时不能重复扫（否则违规数翻倍）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _tree(base, "repo", VIOLATION)
            result = gate_checks.check_test_tree_identity(repo, product=repo)
            self.assertEqual(result["scanned"], 1)
            self.assertEqual(len(result["violations"]), 1)

    def test_allow_marker_and_comments_are_not_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _tree(base, "repo", ALLOWED + COMMENT_ONLY + OTHER_PATH)
            product = _tree(base, "product", OTHER_PATH)
            result = gate_checks.check_test_tree_identity(repo, product=product)
            self.assertTrue(
                result["ok"],
                "豁免标记/注释里的路径被误判成违规：%s" % result["violations"],
            )
            self.assertTrue(result["comment_mentions"])

    def test_other_machine_paths_are_not_violations(self):
        """C:/input.png 这类测试数据路径不算违规（与仓库树同口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = _tree(base, "repo", OTHER_PATH)
            product = _tree(base, "product", OTHER_PATH)
            result = gate_checks.check_test_tree_identity(repo, product=product)
            self.assertTrue(result["ok"], "测试数据路径被误判成违规")


@unittest.skipUnless(
    (PRODUCT / "backend" / "tests").is_dir(), "本机没有产品树，跳过"
)
class RealTreeTest(unittest.TestCase):
    def test_real_tree_has_no_violation(self):
        """真实产品树必须仍然是绿的 —— 判据不能变成恒红。

        这里用 ``product=产品树自身``：两棵树指向同一处时只扫一次
        （不会把违规数翻倍），而产品树的测试文件仍然**在扫描范围内**。
        仓库树那一侧由门禁的 identity 步负责（它扫的就是仓库树）。
        """
        result = gate_checks.check_test_tree_identity(PRODUCT, product=PRODUCT)
        self.assertTrue(
            result["ok"],
            "真实树上出现写死路径：%s" % result["violations"][:5],
        )
        self.assertGreater(result["scanned"], 0, "一个文件都没扫到")


if __name__ == "__main__":
    unittest.main()
