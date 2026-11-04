# -*- coding: utf-8 -*-
"""门禁自身的"夹具隔离"回归用例（2026-11-02 新增，F22）。

背景（真实假红）
----------------
``tests/test_repo_hygiene.py`` 里的
``test_comment_mentions_do_not_fail_and_allow_marker_works`` 原本把**真实产品树**
当第二棵树传给了身份门禁::

    result = gate_checks.check_test_tree_identity(
        root, product=Path(MACHINE_ROOT_FORWARD)     # ← 真实产品树
    )
    self.assertEqual(len(result["comment_mentions"]), 1)

于是"注释提及"的条数取决于**这台机器的产品树里恰好有几处注释提及**，
而不是取决于这条用例自己造的夹具。机器状态一变就假红：

* 2026-11-02 实测：产品树新增 2 处带本机路径的注释提及 ⇒ 期望 1 条、实测 3 条
  ⇒ ``FAILED (failures=2)``，整条 ``hygiene-unit`` 门禁变红。

修法：用临时目录里的第二棵树（内容干净）当 product ⇒ 条数回到夹具自身那 1 条。
本文件把这条不变量**钉住**，防止以后又有人把真实机器目录传进夹具。

跑法::

    <venv> -B -m unittest tests/test_hygiene_fixture_isolation.py
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_checks  # noqa: E402  （必须在 sys.path 调整之后）

HYGIENE = ROOT / "tests" / "test_repo_hygiene.py"

# 与 test_repo_hygiene.py 同款：故意拆开拼，identity 门禁才不会把**本文件**
# 自己判红（本文件也在扫描范围内）。
MACHINE_ROOT = "D:" + "\\" + "AP" + "\\" + "AP7000" + "\\" + "OcrTableTool"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _call_spans(text: str, function_name: str) -> list:
    """取出源码里所有 ``function_name(...)`` 调用的完整实参片段。"""
    spans = []
    for match in re.finditer(re.escape(function_name) + r"\s*\(", text):
        start = match.end()
        depth = 1
        index = start
        while index < len(text) and depth:
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        spans.append(text[start:index - 1])
    return spans


class HygieneFixtureIsolationTest(unittest.TestCase):
    """身份门禁的用例不许拿真实机器目录当夹具。"""

    def test_hygiene_test_file_exists(self):
        self.assertTrue(HYGIENE.is_file(), "仓库卫生用例缺失：%s" % HYGIENE)

    def test_no_identity_call_uses_the_real_machine_tree(self):
        """``check_test_tree_identity`` 的任何调用都不许拿 MACHINE_ROOT 当第二棵树。"""
        text = HYGIENE.read_text(encoding="utf-8", errors="replace")
        spans = _call_spans(text, "check_test_tree_identity")
        self.assertTrue(spans, "没找到 check_test_tree_identity 调用（用例被改没了？）")
        offenders = [span for span in spans if "MACHINE_ROOT" in span]
        self.assertEqual(
            offenders,
            [],
            "身份门禁用例把真实机器目录当夹具传了进去（断言条数会随机器状态漂移）：\n%s"
            % "\n".join(offenders),
        )

    def test_hygiene_test_still_expects_exactly_one_comment_mention(self):
        """钉住"这条用例只认自己夹具里那 1 条注释提及"的本意。"""
        text = HYGIENE.read_text(encoding="utf-8", errors="replace")
        self.assertIn(
            'self.assertEqual(len(result["comment_mentions"]), 1)',
            text,
            "卫生用例不再断言'恰好 1 条注释提及'（本意是只认自己夹具那一条）",
        )


class IdentityGateProductTreeBehaviourTest(unittest.TestCase):
    """身份门禁的 product 分支本身是活的（修 F22 时不能把它关掉）。"""

    def _scan(self, root: Path, product: Path) -> dict:
        return gate_checks.check_test_tree_identity(root, product=product)

    def test_count_depends_only_on_the_trees_passed_in(self):
        """注释提及条数只由传入的两棵树决定，与机器现状无关。"""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            product = base / "product"
            _write(
                root / "backend" / "tests" / "test_a.py",
                "# 原来这里写死 %s\n" % MACHINE_ROOT,
            )
            _write(
                product / "tests" / "test_b.py",
                "# 原来这里写死 %s\n" % MACHINE_ROOT,
            )
            result = self._scan(root, product)
            self.assertEqual(
                len(result["comment_mentions"]),
                2,
                "两棵树各 1 条注释提及，却数出 %d 条" % len(result["comment_mentions"]),
            )

            clean = base / "clean"
            _write(clean / "tests" / "test_c.py", "VALUE = 1\n")
            result = self._scan(root, clean)
            self.assertEqual(
                len(result["comment_mentions"]),
                1,
                "第二棵树干净时，条数应回到夹具自身的 1 条",
            )

    def test_product_tree_comment_mentions_are_still_counted(self):
        """反向护栏：product 分支没有被 F22 的修法顺手关掉。"""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            product = base / "product"
            _write(root / "tests" / "test_a.py", "VALUE = 1\n")
            _write(
                product / "tests" / "test_b.py",
                "# 原来这里写死 %s\n" % MACHINE_ROOT,
            )
            result = self._scan(root, product)
            self.assertEqual(len(result["comment_mentions"]), 1)

    def test_product_tree_violations_are_still_reported(self):
        """反向护栏：product 树里的**真违规**照样报出来（不是只记注释）。"""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            product = base / "product"
            _write(root / "tests" / "test_a.py", "VALUE = 1\n")
            _write(
                product / "tests" / "test_b.py",
                'ROOT = "%s"\n' % MACHINE_ROOT,
            )
            result = self._scan(root, product)
            self.assertFalse(result["ok"], "product 树里的写死路径没被报出来")
            self.assertEqual(len(result["violations"]), 1)
            self.assertTrue(
                result["violations"][0]["path"].startswith("product/"),
                "违规路径应带 product/ 前缀：%s" % result["violations"][0]["path"],
            )


if __name__ == "__main__":
    unittest.main()
