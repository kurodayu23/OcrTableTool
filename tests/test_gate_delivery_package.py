# -*- coding: utf-8 -*-
"""门禁"当前交付位"解析的行为测试（2026-11-02 新增）。

背景（真实假绿，不是假想）：

``scripts/verify_all.ps1`` 原来把交付位**写死**成 ``package\\backend-delivery``。
产品树里同时存在多个候选包（``backend-delivery`` / ``-20261027`` / ``-20261030``），
而**当前交付位是最新构建的那个**。写死旧包 ⇒ 这一步验的是**客户拿不到、也不是
当前位**的那个包 ⇒ 当前位即使少了模型、少了文件、少了 sidecar，这一步照样 PASS
⇒ 门禁给出一张假绿牌。脚本自己的用法注释还在教人这么用（``-IncludeDelivery``）。

现在改成：``scripts\\gate_delivery_package.ps1`` 里的 ``Resolve-CurrentDeliveryPackage``
按**磁盘实测**解析当前位（exe mtime 最大者），``verify_all.ps1`` 点源引入它。

判据为什么不是 ``max(包目录 mtime, exe mtime)``：
包目录 mtime 会被「往旧包里拷一个文件」顶高 ⇒ 旧包被误判成当前位 ⇒ 假绿重演。
exe 是构建产物，它的时间戳才是"这包什么时候构建出来的"的权威信号。

跑法::

    <venv> -B -m unittest tests/test_gate_delivery_package.py

也由 ``scripts\\verify_all.ps1`` 作为 ``gate-delivery-unit`` 一项门禁调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "gate_delivery_package.ps1"
VERIFY_ALL = ROOT / "scripts" / "verify_all.ps1"


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _run_powershell(snippet: str) -> str:
    """在 pwsh 里跑一段脚本并取回 stdout（UTF-8）。"""
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


def _make_package(root: Path, name: str, exe_mtime: float, dir_mtime: float | None = None) -> Path:
    """在 ``root`` 下造一个候选交付包。

    ⚠ 顺序要紧：先建目录与 exe，**最后**再设包目录 mtime
    （建子目录会顶高父目录 mtime）。
    """
    package = root / name
    runtime = package / "ocr-runtime" / "OcrBackend"
    runtime.mkdir(parents=True, exist_ok=True)
    exe = runtime / "OcrBackend.exe"
    exe.write_bytes(b"MZ")
    os.utime(exe, (exe_mtime, exe_mtime))
    if dir_mtime is not None:
        os.utime(package, (dir_mtime, dir_mtime))
    return package


def _resolve(package_root: Path) -> str:
    """调用解析器，返回它给出的包目录（空串 = 没有候选）。"""
    snippet = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        ". %s; "
        "Resolve-CurrentDeliveryPackage -PackageRoot %s"
        % (_ps_quote(MODULE), _ps_quote(package_root))
    )
    return _run_powershell(snippet)


class DeliveryPackageResolutionTest(unittest.TestCase):
    """Resolve-CurrentDeliveryPackage 的行为断言。"""

    def test_module_exists(self):
        self.assertTrue(
            MODULE.is_file(),
            "当前交付位解析模块缺失：%s（verify_all.ps1 靠它点源引入）" % MODULE,
        )

    def test_newest_built_package_is_selected(self):
        """exe mtime 最新的那个才是当前位。"""
        with tempfile.TemporaryDirectory(prefix="ocr-gate-pkg-") as tmp:
            root = Path(tmp)
            _make_package(root, "backend-delivery", 1000.0)
            _make_package(root, "backend-delivery-20261027", 2000.0)
            _make_package(root, "backend-delivery-20261030", 3000.0)
            resolved = _resolve(root)
            self.assertIn(
                "backend-delivery-20261030",
                resolved,
                "当前位解析错了（应取构建最新的那个）：%s" % resolved,
            )

    def test_package_dir_mtime_does_not_win_over_exe(self):
        """★ 反例：往旧包里拷一个文件会顶高**包目录 mtime**，但它不是当前位。

        这正是旧判据 ``max(包目录 mtime, exe mtime)`` 的洞。
        """
        with tempfile.TemporaryDirectory(prefix="ocr-gate-pkg-") as tmp:
            root = Path(tmp)
            _make_package(root, "backend-delivery", 1000.0, dir_mtime=9999.0)
            _make_package(root, "backend-delivery-20261030", 3000.0, dir_mtime=3000.0)
            resolved = _resolve(root)
            self.assertIn(
                "backend-delivery-20261030",
                resolved,
                "被顶高 mtime 的旧包抢走了当前位（判据应看 exe 构建时刻）：%s" % resolved,
            )

    def test_directories_without_runtime_are_ignored(self):
        """没有 ``ocr-runtime\\OcrBackend`` 的目录不算候选包。"""
        with tempfile.TemporaryDirectory(prefix="ocr-gate-pkg-") as tmp:
            root = Path(tmp)
            _make_package(root, "backend-delivery-20261030", 3000.0)
            (root / "backend-delivery-notes").mkdir(parents=True, exist_ok=True)
            resolved = _resolve(root)
            self.assertIn("backend-delivery-20261030", resolved)

    def test_no_candidate_returns_empty(self):
        """一个候选包都没有时返回空串（不是崩、也不是瞎猜一个包名）。"""
        with tempfile.TemporaryDirectory(prefix="ocr-gate-pkg-") as tmp:
            self.assertEqual(_resolve(Path(tmp)), "")

    def test_missing_package_root_returns_empty(self):
        """目录压根不存在时返回空串。"""
        with tempfile.TemporaryDirectory(prefix="ocr-gate-pkg-") as tmp:
            missing = Path(tmp) / "no-such-package-root"
            self.assertEqual(_resolve(missing), "")


class VerifyAllWiringTest(unittest.TestCase):
    """verify_all.ps1 必须真的用上解析器，不能再写死包名。"""

    def setUp(self):
        self.assertTrue(
            VERIFY_ALL.is_file(),
            "门禁脚本缺失：%s" % VERIFY_ALL,
        )
        self.text = VERIFY_ALL.read_text(encoding="utf-8", errors="replace")

    def test_does_not_hardcode_the_old_package(self):
        self.assertNotIn(
            'package\\backend-delivery"',
            self.text,
            "门禁又把旧交付位写死成默认值了（验的是旧包 ⇒ 假绿）",
        )

    def test_uses_the_shared_resolver(self):
        self.assertIn(
            "gate_delivery_package.ps1",
            self.text,
            "门禁没有点源引入当前交付位解析器",
        )
        self.assertIn(
            "Resolve-CurrentDeliveryPackage",
            self.text,
            "门禁没有调用当前交付位解析器",
        )

    def test_resolver_is_applied_only_when_not_given(self):
        """显式传入的 -DeliveryPackageDir 必须仍然优先（不能被解析器覆盖）。"""
        self.assertIn(
            "if (-not $DeliveryPackageDir)",
            self.text,
            "解析器覆盖了显式指定的交付位",
        )


if __name__ == "__main__":
    unittest.main()
