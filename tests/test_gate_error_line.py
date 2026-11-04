# -*- coding: utf-8 -*-
"""门禁"首要错误行"提取的行为测试（2026-11-02 新增）。

背景（真实缺陷，不是假想）：

``scripts/verify_all.ps1`` 把每一步的 stdout/stderr 交给
``Get-PrimaryErrorLine`` 挑一行"最能说明失败原因"的行打印出来。旧实现命中不到
已知模式时退化成**取最后一行非空行**。而 ``delivery_notes.py verify`` 的输出最后
一行是 facts 清单的末行 ``RUNTIME_MAX_MTIME    2026-10-30 05:52:14`` —— 而这个值
**完全正确**。于是门禁把"首要错误行"报成了它：

    [8/10] delivery-notes   FAIL   [FAIL] 交付说明.md 已过期：第 33 行
            > RUNTIME_MAX_MTIME    2026-10-30 05:52:14      <-- 指错了地方

真实首处差异是 ``DELTA_KB``（模板算出 997 KB，交付位写 995 KB），整条差异被吞掉。
"报错指错地方"比不报错更糟：会把维护者引到本来没问题的地方去查。

所以这里不只测"新实现对不对"，还带一条**承重证明**：把被删掉的旧实现原样留在测试里
跑一遍，证明它确实会指错 —— 也就是这条新用例在旧实现上**必 FAIL**（不是恒绿）。

跑法::

    <venv> -B -m unittest tests/test_gate_error_line.py

也由 ``scripts\\verify_all.ps1`` 作为 ``gate-log-unit`` 一项门禁调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "gate_error_line.ps1"

# 旧实现（原样保留，用于承重证明）：命中不到已知模式时取最后一行非空行。
LEGACY_FUNCTION = r'''
function Get-PrimaryErrorLine {
    param([string[]]$Lines)
    if (-not $Lines -or $Lines.Count -eq 0) { return "" }
    $strong = @(
        '^\s*(FAIL|ERROR):\s',
        '^\s*FAILED\s',
        '^\s*ERROR\s',
        'AssertionError',
        '\berror C\d{4}\b',
        '\bfatal error\b',
        '^\s*Traceback \(most recent call last\)',
        '\bModuleNotFoundError\b',
        '\bImportError\b'
    )
    $detail = @(
        '缺 BOM:',
        '非 UTF-8:',
        '写死本项目路径:',
        '镜像缺文件:',
        '镜像多文件:',
        '内容不同:'
    )
    foreach ($pattern in $strong) {
        foreach ($line in $Lines) {
            if ($line -match $pattern -and $line.Trim()) { return $line.Trim() }
        }
    }
    foreach ($pattern in $detail) {
        foreach ($line in $Lines) {
            if ($line -like ('*' + $pattern + '*')) { return $line.Trim() }
        }
    }
    $tail = @($Lines | Where-Object { $_.Trim() })
    if ($tail.Count -gt 0) { return $tail[-1].Trim() }
    return ""
}
'''

# delivery_notes.py verify 在交付位过期时的**真实输出**（2026-11-02 实测截取）。
DELIVERY_NOTES_OUTPUT = [
    "[FAIL] 交付说明.md 已过期：第 33 行",
    "  模板算出: | **后端运行时本体**（`ocr-runtime\\OcrBackend` 子树） | **369** | 约 536.7 MiB（与整包相差约 997 KB） |",
    "  交付位是: | **后端运行时本体**（`ocr-runtime\\OcrBackend` 子树） | **369** | 约 536.7 MiB（与整包相差约 995 KB） |",
    "  EXE_BYTES            11,055,242",
    "  EXE_MIB              10.5",
    "  PACKAGE_FILES        430",
    "  PACKAGE_MIB          537.7",
    "  RUNTIME_FILES        369",
    "  RUNTIME_BYTES        562,770,686",
    "  RUNTIME_MIB          536.7",
    "  DELTA_KB             997",
    "  EXTRA_FILES          61",
    "  ROOT_DELIVERY_FILES  3",
    "  LICENSE_FILES        57",
    "  RUNTIME_MAX_MTIME    2026-10-30 05:52:14",
]

# unittest 失败输出（unittest -m unittest 的真实格式）。
UNITTEST_OUTPUT = [
    "FAIL: test_product_mirror_matches_repo_worktree (tests.test_repo_hygiene.RepositoryStateTest.test_product_mirror_matches_repo_worktree)",
    "----------------------------------------------------------------------",
    "Traceback (most recent call last):",
    '  File "C:\\...\\tests\\test_repo_hygiene.py", line 378, in test_product_mirror_matches_repo_worktree',
    "    self.assertTrue(result[\"ok\"], msg)",
    "AssertionError: 镜像与工作树不一致",
    "----------------------------------------------------------------------",
    "Ran 16 tests in 0.570s",
    "",
    "FAILED (failures=1)",
]

# gate_checks.py 的明细输出。
GATE_CHECKS_OUTPUT = [
    "[gate] mirror",
    "镜像缺文件: backend/tests/test_protocol_robustness.py",
    "镜像多文件: backend/tests/test_new_module.py",
    "内容不同: backend/ocr_backend.py",
]


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_powershell(snippet: str) -> str:
    """在 pwsh 里跑一段脚本并取回 stdout（UTF-8）。"""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        raise unittest.SkipTest("没有 pwsh，跳过（该模块含中文模式串，必须 pwsh 7 读）")
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


def _extract(lines: list[str], module: Path | None = None) -> str:
    """把输出行喂给模块里的 Get-PrimaryErrorLine，返回它挑中的那一行。"""
    if module is None:
        module = MODULE
    with tempfile.TemporaryDirectory(prefix="ocr-gate-log-") as tmp:
        sample = Path(tmp) / "sample.txt"
        sample.write_text("\n".join(lines) + "\n", encoding="utf-8")
        snippet = (
            "$ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
            ". %s; "
            "$lines = Get-Content -LiteralPath %s -Encoding UTF8; "
            "Get-PrimaryErrorLine -Lines $lines"
            % (_ps_quote(module), _ps_quote(sample))
        )
        return _run_powershell(snippet)


class GateErrorLineTest(unittest.TestCase):
    """Get-PrimaryErrorLine 的行为断言。"""

    def test_module_exists(self):
        self.assertTrue(
            MODULE.is_file(),
            "错误行提取模块缺失：%s（verify_all.ps1 靠它点源引入）" % MODULE,
        )

    def test_delivery_notes_failure_reports_the_real_difference(self):
        """交付说明过期时，必须报到 DELTA_KB（997 vs 995），不能报到 facts 末行。"""
        picked = _extract(DELIVERY_NOTES_OUTPUT)
        self.assertIn("997", picked, "首要错误行没有带上模板算出的 DELTA_KB=997：%r" % picked)
        self.assertIn("995", picked, "首要错误行没有带上交付位写的 DELTA_KB=995：%r" % picked)
        self.assertNotIn(
            "RUNTIME_MAX_MTIME",
            picked,
            "首要错误行又落回 facts 末行（RUNTIME_MAX_MTIME 是对的，不是失败原因）：%r" % picked,
        )

    def test_unittest_failure_line_is_picked(self):
        picked = _extract(UNITTEST_OUTPUT)
        self.assertTrue(
            picked.startswith("FAIL: test_product_mirror_matches_repo_worktree"),
            "unittest 的失败用例名没有被选中：%r" % picked,
        )

    def test_gate_checks_detail_line_is_picked(self):
        picked = _extract(GATE_CHECKS_OUTPUT)
        self.assertIn("镜像缺文件", picked, "镜像明细行没有被选中：%r" % picked)

    def test_empty_output_yields_empty_string(self):
        self.assertEqual(_extract([]), "")

    def test_legacy_implementation_would_have_misreported(self):
        """承重证明：旧实现（取最后一行非空行）确实会指错地方。

        这条用例保证上面的新断言不是"恒绿"：把被删掉的旧实现原样跑一遍，
        它必须拿到那个**正确但无关**的 facts 末行。
        """
        with tempfile.TemporaryDirectory(prefix="ocr-gate-log-legacy-") as tmp:
            legacy = Path(tmp) / "legacy.ps1"
            legacy.write_text(LEGACY_FUNCTION, encoding="utf-8")
            legacy_picked = _extract(DELIVERY_NOTES_OUTPUT, module=legacy)
        self.assertIn(
            "RUNTIME_MAX_MTIME",
            legacy_picked,
            "旧实现没有复现出'取末行'的行为，承重证明失效（旧实现=%r）" % legacy_picked,
        )
        self.assertNotIn(
            "997",
            legacy_picked,
            "旧实现居然拿到了 DELTA_KB，新旧实现没差别，承重证明失效",
        )


if __name__ == "__main__":
    unittest.main()
