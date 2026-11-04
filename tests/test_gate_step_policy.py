# -*- coding: utf-8 -*-
"""门禁"验不到"判定策略的行为测试（2026-11-02 新增，F16）。

背景（真实假绿）
----------------
``scripts/verify_all.ps1`` 原来在"要找的东西找不到"时一律 ``Add-Skip``：

* 契约测试文件缺失 → SKIP；
* 点了 ``-IncludeDelivery`` 但交付包 / exe 不存在 → SKIP ×3；
* 点了 ``-IncludeCpp`` 但测试可执行文件没构建出来 → SKIP ×3。

于是**"根本没验"和"验过了"在汇总里长得一模一样**，门禁照样整体 PASS ——
这与项目自己写下的原则直接矛盾（``scripts/verify_delivery_package.py`` 的
2026-10-22 改动说明：「"没查"必须等同于"不通过"」）。

修法：``scripts/gate_step_policy.ps1`` 的 ``Get-MissingStepStatus`` 给出判定，
``verify_all.ps1`` 的三处"找不到"全部改走它。

跑法::

    <venv> -B -m unittest tests/test_gate_step_policy.py

也由 ``scripts\\verify_all.ps1`` 作为 ``gate-policy-unit`` 一项门禁调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "gate_step_policy.ps1"
VERIFY_ALL = ROOT / "scripts" / "verify_all.ps1"


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_powershell(snippet: str) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise unittest.SkipTest("没有 pwsh，跳过（该模块含中文注释，必须 pwsh 7 读）")
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", snippet],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    out = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise AssertionError(
            "pwsh 执行失败（rc=%d）：\n%s\n%s"
            % (completed.returncode, out, completed.stderr.decode("utf-8", "replace"))
        )
    return out.strip()


def _status(reason: str = "", requested: bool = True, explicitly_skipped: bool = False) -> str:
    """调用 Get-MissingStepStatus，返回它给出的判定。

    ⚠ 参数名要**原样**传给 PowerShell：``ExplicitlySkipped`` 是 switch，
    ``Requested`` 是 bool（False 也要显式传 $false）。
    """
    arguments = ["-Reason '%s'" % reason, "-Requested $%s" % ("true" if requested else "false")]
    if explicitly_skipped:
        arguments.append("-ExplicitlySkipped")
    snippet = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        ". %s; "
        "Get-MissingStepStatus %s" % (_ps_quote(MODULE), " ".join(arguments))
    )
    return _run_powershell(snippet)


class MissingStepPolicyTest(unittest.TestCase):
    """Get-MissingStepStatus 的行为断言。"""

    def test_module_exists(self):
        self.assertTrue(
            MODULE.is_file(),
            "判定策略模块缺失：%s（verify_all.ps1 靠它点源引入）" % MODULE,
        )

    def test_requested_check_that_cannot_run_is_a_failure(self):
        """点名要的检查项找不到 ⇒ FAIL（"没查"不等于"通过"）。"""
        self.assertEqual(_status(reason="交付包不存在"), "FAIL")

    def test_explicitly_skipped_stays_skip(self):
        """用户明确要求跳过（-SkipCppBuild）⇒ SKIP。"""
        self.assertEqual(
            _status(reason="未构建", explicitly_skipped=True),
            "SKIP",
        )

    def test_not_requested_stays_skip(self):
        """没点名要的项 ⇒ SKIP（不是所有缺件都该红）。"""
        self.assertEqual(_status(reason="可选", requested=False), "SKIP")

    def test_requested_wins_over_not_requested_only_when_explicit(self):
        """点名 + 明确跳过 ⇒ 跳过优先（尊重用户本意）。"""
        self.assertEqual(
            _status(reason="未构建", explicitly_skipped=True),
            "SKIP",
        )


class VerifyAllWiringTest(unittest.TestCase):
    """verify_all.ps1 的三处"找不到"必须走判定策略，不能再裸 SKIP。"""

    def setUp(self):
        self.assertTrue(VERIFY_ALL.is_file(), "门禁脚本缺失：%s" % VERIFY_ALL)
        self.text = VERIFY_ALL.read_text(encoding="utf-8", errors="replace")

    def test_policy_module_is_dot_sourced(self):
        self.assertIn(
            "gate_step_policy.ps1",
            self.text,
            "门禁没有点源引入判定策略模块",
        )

    def test_contract_test_missing_is_not_a_bare_skip(self):
        self.assertNotIn(
            'Add-Skip -Name $case.name',
            self.text,
            "契约测试文件缺失仍然只标 SKIP（这一步根本没跑，必须 FAIL）",
        )

    def test_delivery_missing_is_not_a_bare_skip(self):
        self.assertNotIn(
            'Add-Skip -Name "delivery-verify"',
            self.text,
            "点名要验的交付位找不到仍然只标 SKIP（必须 FAIL）",
        )
        self.assertNotIn(
            'Add-Skip -Name "delivery-selfcheck"',
            self.text,
            "点名要验的交付位找不到仍然只标 SKIP（必须 FAIL）",
        )

    def test_cpp_binary_missing_is_not_a_bare_skip(self):
        self.assertNotIn(
            "Add-Skip -Name $binary.name",
            self.text,
            "C++ 测试可执行文件缺失仍然只标 SKIP（必须 FAIL，除非用户明确 -SkipCppBuild）",
        )

    def test_all_three_missing_paths_use_the_policy(self):
        self.assertGreaterEqual(
            self.text.count("Get-MissingStepStatus"),
            3,
            "三处'找不到'没有全部改走判定策略",
        )


if __name__ == "__main__":
    unittest.main()
