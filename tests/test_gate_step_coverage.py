# -*- coding: utf-8 -*-
"""门禁"步骤清单"核对的行为测试（2026-11-02 新增，F21）。

背景（真实假绿）
----------------
``scripts/verify_all.ps1`` 里原本只有一个 ``$script:PlannedGates = 12``，
它只是**显示用**的计数器（``[3/12]``），**没有任何东西**去核对
「实际跑了多少步 / 跑了哪些步 / 有没有重名」。于是：

* 从执行列表里删掉一步  → 少跑一项，汇总照样 PASS；
* 加了一步忘了改计数器   → 计数变成 13/12，仍然 PASS；
* 步骤名改了              → 仍然 PASS；
* 同一个名字用了两次      → 仍然 PASS。

这与 F16（"验不到"只标 SKIP）、F20（扫了 0 个文件也 PASS）同源 ——
「"没查"必须等同于"不通过"」被漏掉了最后这一环。

修法：``scripts/gate_step_coverage.ps1`` 持有**独立维护**的步骤清单
（manifest），``verify_all.ps1`` 跑完逐项核对，对不上就判 FAIL。

跑法::

    <venv> -B -m unittest tests/test_gate_step_coverage.py

也由 ``scripts\\verify_all.ps1`` 作为 ``gate-coverage-unit`` 一项门禁调用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "gate_step_coverage.ps1"
VERIFY_ALL = ROOT / "scripts" / "verify_all.ps1"

# 核对步骤自身的名字：它不在 manifest 里（manifest 列的是"必须跑的检查"，
# 而 gate-coverage 是"检查有没有漏跑"的那一步本身）。
COVERAGE_STEP = "gate-coverage"


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
        timeout=180,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    out = completed.stdout.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise AssertionError(
            "pwsh 执行失败（rc=%d）：\n%s\n%s"
            % (completed.returncode, out, completed.stderr.decode("utf-8", "replace"))
        )
    return out.strip()


def _expected_steps(*flags: str) -> list:
    """调用 Get-ExpectedGateSteps，返回步骤名列表。"""
    flag_text = " ".join(flags)
    snippet = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        ". %s; "
        "@(Get-ExpectedGateSteps %s) | ConvertTo-Json -Compress" % (_ps_quote(MODULE), flag_text)
    )
    raw = _run_powershell(snippet)
    data = json.loads(raw)
    if isinstance(data, str):
        data = [data]
    return [str(item) for item in data]


def _ps_array(values: list) -> str:
    """把列表变成 PowerShell 数组字面量。

    ⚠ 不能写成 ``@(["a", "b"])``：PowerShell 会把 ``[`` 当成类型字面量的开头
    （报"缺少类型名称"）。正确写法是 ``@("a", "b")``。
    """
    if not values:
        return "@()"
    return "@(" + ", ".join('"%s"' % value for value in values) + ")"


def _coverage(actual: list, expected: list) -> dict:
    """调用 Test-GateStepCoverage，返回判定对象。"""
    actual_ps = _ps_array(actual)
    expected_ps = _ps_array(expected)
    snippet = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        ". %s; "
        "$r = %s | ForEach-Object { [pscustomobject]@{ name = $_ } }; "
        "$e = %s; "
        "Test-GateStepCoverage -Results $r -Expected $e | ConvertTo-Json -Compress"
        % (_ps_quote(MODULE), actual_ps, expected_ps)
    )
    raw = _run_powershell(snippet)
    return json.loads(raw)


def _strip_ps_comments(text: str) -> str:
    """去掉 PowerShell 注释（块注释 + 行注释），避免注释里的字样被当成代码。"""
    text = re.sub(r"<#.*?#>", " ", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        out = []
        in_single = in_double = False
        for char in line:
            if char == "'" and not in_double:
                in_single = not in_single
            elif char == '"' and not in_single:
                in_double = not in_double
            elif char == "#" and not in_single and not in_double:
                break
            out.append(char)
        lines.append("".join(out))
    return "\n".join(lines)


def _executed_step_names(text: str) -> set:
    """从 verify_all.ps1 的**代码**里解析出"会产生结果的步骤名"。

    解析规则（与 verify_all.ps1 的写法一一对应）：
      * ``-Name "字面量"``            → 直接取；
      * ``foreach ($check in @(...))`` → 取数组里的字面量（bom / identity）；
      * ``foreach ($name in @(...))``  → 取数组里的字面量（交付三步）；
      * ``$contractCases`` / ``$testBinaries`` 数组里的 ``name = "..."``。
    """
    code = _strip_ps_comments(text)
    names = set()
    for match in re.finditer(r'-Name\s+"([A-Za-z0-9_\-]+)"', code):
        names.add(match.group(1))
    for pattern in (
        r'foreach\s*\(\s*\$check\s+in\s*@\(([^)]*)\)',
        r'foreach\s*\(\s*\$name\s+in\s*@\(([^)]*)\)',
    ):
        for match in re.finditer(pattern, code):
            names.update(re.findall(r'"([A-Za-z0-9_\-]+)"', match.group(1)))
    for match in re.finditer(
        r"\$(?:contractCases|testBinaries)\s*=\s*@\((.*?)\n\s*\)\s*\n", code, re.S
    ):
        names.update(re.findall(r'name\s*=\s*"([A-Za-z0-9_\-]+)"', match.group(1)))
    return names


class ExpectedStepManifestTest(unittest.TestCase):
    """Get-ExpectedGateSteps 的行为断言。"""

    def test_module_exists(self):
        self.assertTrue(
            MODULE.is_file(),
            "步骤清单模块缺失：%s（verify_all.ps1 靠它点源引入）" % MODULE,
        )

    def test_base_manifest_has_fifteen_steps(self):
        steps = _expected_steps()
        self.assertEqual(len(steps), 15, "基础步骤清单不是 15 条：%s" % steps)
        for name in ("bom", "identity", "backend-unit", "mirror"):
            self.assertIn(name, steps, "基础清单缺 %s" % name)

    def test_delivery_adds_three_steps(self):
        base = _expected_steps()
        with_delivery = _expected_steps("-IncludeDelivery")
        self.assertEqual(len(with_delivery), len(base) + 3)
        for name in ("delivery-notes", "delivery-verify", "delivery-selfcheck"):
            self.assertIn(name, with_delivery, "交付清单缺 %s" % name)
            self.assertNotIn(name, base, "基础清单不该含交付步骤 %s" % name)

    def test_cpp_adds_four_steps_even_when_skipped(self):
        """cpp-build 在 -SkipCppBuild 下也会记成一条 SKIP 结果 ⇒ 恒为 4 步。"""
        with_cpp = _expected_steps("-IncludeCpp")
        self.assertEqual(len(with_cpp), 19)
        for name in ("cpp-build", "cpp-core-test", "cpp-gui-test", "cpp-sdk-test"):
            self.assertIn(name, with_cpp, "C++ 清单缺 %s" % name)

    def test_photo_adds_one_step(self):
        """真实翻拍照片的端到端准确率门禁（2026-11-04 新增）。

        合成语料长期全绿，真实照片上"真值 55 被读成 552"这类静默瞎编
        一直没人守 —— 缺的就是这一步。
        """
        base = _expected_steps()
        with_photo = _expected_steps("-IncludePhoto")
        self.assertEqual(len(with_photo), len(base) + 1)
        self.assertIn("photo-acceptance", with_photo, "照片清单缺 photo-acceptance")
        self.assertNotIn("photo-acceptance", base, "基础清单不该含照片步骤")

    def test_all_is_the_union(self):
        all_steps = set(_expected_steps("-All"))
        union = set(_expected_steps()) | set(_expected_steps("-IncludeDelivery")) | set(
            _expected_steps("-IncludeCpp")
        ) | set(_expected_steps("-IncludePhoto"))
        self.assertEqual(all_steps, union, "-All 不是四种子集的并集")


class StepCoverageTest(unittest.TestCase):
    """Test-GateStepCoverage 的行为断言。"""

    def test_matching_sets_are_ok(self):
        steps = _expected_steps()
        verdict = _coverage(steps, steps)
        self.assertTrue(verdict["ok"], "清单与实际一致却判 FAIL：%s" % verdict)
        self.assertEqual(verdict["missing"], [])
        self.assertEqual(verdict["unexpected"], [])
        self.assertEqual(verdict["duplicated"], [])

    def test_a_dropped_step_is_detected(self):
        """从执行列表里删掉一步 ⇒ 必须被抓（这正是 F21 要修的洞）。"""
        steps = _expected_steps()
        dropped = list(steps)
        dropped.remove("gate-vacuous-unit")
        verdict = _coverage(dropped, steps)
        self.assertFalse(verdict["ok"], "少跑一步却判 OK")
        self.assertEqual(verdict["missing"], ["gate-vacuous-unit"])

    def test_an_unregistered_step_is_detected(self):
        """加了一步却没登记 ⇒ 必须被抓（新步骤必须先登记）。"""
        steps = _expected_steps()
        verdict = _coverage(steps + ["brand-new-step"], steps)
        self.assertFalse(verdict["ok"], "未登记的步骤没被抓")
        self.assertEqual(verdict["unexpected"], ["brand-new-step"])

    def test_a_duplicated_step_is_detected(self):
        steps = _expected_steps()
        verdict = _coverage(steps + ["mirror"], steps)
        self.assertFalse(verdict["ok"], "重名步骤没被抓")
        self.assertEqual(verdict["duplicated"], ["mirror"])

    def test_empty_results_against_a_real_manifest_is_not_ok(self):
        """一个都没跑 ⇒ 必须判 FAIL（"没查"不等于"通过"）。"""
        steps = _expected_steps()
        verdict = _coverage([], steps)
        self.assertFalse(verdict["ok"], "零步骤却判 OK")
        self.assertEqual(len(verdict["missing"]), len(steps))


class VerifyAllWiringTest(unittest.TestCase):
    """verify_all.ps1 必须真的做核对，而不是只留一个计数器。"""

    def setUp(self):
        self.assertTrue(VERIFY_ALL.is_file(), "门禁脚本缺失：%s" % VERIFY_ALL)
        self.text = VERIFY_ALL.read_text(encoding="utf-8", errors="replace")

    def test_module_is_dot_sourced(self):
        self.assertIn(
            "gate_step_coverage.ps1",
            self.text,
            "门禁没有点源引入步骤清单模块",
        )

    def test_coverage_check_is_invoked(self):
        self.assertIn(
            "Test-GateStepCoverage",
            self.text,
            "门禁没有调用 Test-GateStepCoverage（步骤清单仍然没有强制力）",
        )

    def test_coverage_result_is_recorded(self):
        self.assertIn(
            'Add-Result -Name "%s"' % COVERAGE_STEP,
            self.text,
            "核对结果没有被记进门禁汇总（FAIL 就不会影响退出码）",
        )

    def test_planned_gates_is_not_a_hardcoded_number(self):
        self.assertNotIn(
            "$script:PlannedGates = 12",
            self.text,
            "PlannedGates 仍是写死的数字（少跑一步看不出来）",
        )

    def test_manifest_matches_the_executed_steps(self):
        """静态交叉核对：清单与"代码实际会产生哪些步骤"必须一致。

        这是**独立于运行时**的第二层保险：即使门禁还没跑，
        删掉/改名/新增一个步骤而忘了登记清单，这一层就先 FAIL。
        """
        expected = set(_expected_steps("-All"))
        executed = _executed_step_names(self.text)
        self.assertIn(COVERAGE_STEP, executed, "门禁里找不到核对步骤本身")
        executed.discard(COVERAGE_STEP)
        missing = sorted(expected - executed)
        unexpected = sorted(executed - expected)
        self.assertEqual(
            missing,
            [],
            "清单里有、代码里没有的步骤：%s" % missing,
        )
        self.assertEqual(
            unexpected,
            [],
            "代码里有、清单里没登记的步骤：%s" % unexpected,
        )


if __name__ == "__main__":
    unittest.main()
