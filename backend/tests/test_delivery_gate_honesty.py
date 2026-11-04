# -*- coding: utf-8 -*-
"""交付闸门不许给破损包发 PASS（2026-10-23 修）。

2026-10-22 的审计发现两处**假阳性**，都是"没查"被当成"查过且通过"：

1. ``_bundled_backend_modules()`` 在 PyInstaller 不可导入时返回
   ``(True, "skipped: ...")`` → 调用方记成 PASS。**一项都没查，却报 12/12 通过。**
2. ``MODEL_HASHES`` 读不到时，两项模型校验被 ``if model_hashes:`` 整段跳过，
   没有任何 FAIL，退出码仍是 0 —— 模型缺失、SHA-256 不匹配都查不出来。

另外 ``--backend-source`` 的默认值在真实交付位上算成一个不存在的目录，
所以"文档推荐用法"正好落在坏路径上。

这里用行为测试锁住语义（不依赖源码排版）。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

# 2026-10-30（lead 复查）：原来写死本机绝对路径 `D:\AP\AP7000\OcrTableTool`，
# 换机器/换克隆目录必挂；若本机另有一份同名镜像树还会拿错树做参照。
# 与同目录 `test_acceptance_runner_metrics.py` 一致，按 `__file__` 推导项目根。
PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts" / "verify_delivery_package.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_delivery_package_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


verify = _load_module()


class DeliveryGateHonestyTests(unittest.TestCase):
    def test_backend_source_is_discovered_by_walking_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend").mkdir()
            (root / "backend" / "ocr_backend.py").write_text("# stub\n", encoding="utf-8")
            package = root / "package" / "backend-delivery" / "ocr-runtime" / "OcrBackend"
            package.mkdir(parents=True)
            resolved = verify._resolve_backend_source(package)
            self.assertIsNotNone(resolved)
            self.assertEqual((resolved / "ocr_backend.py").is_file(), True)

    def test_backend_source_is_none_when_it_cannot_be_found(self):
        """脚本与包都不在源码树里 => 必须返回 None（"没查"必须让闸门 FAIL）。

        2026-10-24（D-06）：``_resolve_backend_source`` 多了一条候选
        ——**本脚本自己所在的源码树**。于是"找不到源码"这件事只在
        **脚本本身**也不在源码树里时才成立。这里把脚本原样复制到临时
        目录再加载，复现的正是交付侧那种情形（客户拿到的包里没有源码树，
        ``verify_delivery_package.py`` 也不随包分发）。

        ⚠ 这条是 2026-10-22「没查 ≠ 通过」加固的锁，任何时候都不能松。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "somewhere" / "OcrBackend"
            package.mkdir(parents=True)
            relocated = root / "scripts" / "verify_delivery_package.py"
            relocated.parent.mkdir(parents=True)
            relocated.write_bytes(SCRIPT.read_bytes())
            spec = importlib.util.spec_from_file_location(
                "verify_delivery_package_relocated_mod", relocated
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertIsNone(module._resolve_backend_source(package))

    def test_backend_source_falls_back_to_the_scripts_own_source_tree(self):
        """D-06：脚本在源码树里、包在任意位置 => 必须能定位到源码。

        2026-10-24（D-06）：交付包一旦被拷到源码树之外（例如给客户做验收、
        或换机复跑），只沿**包路径**向上找必然找不到可比对源码，两项模型
        校验恒 FAIL（原因串 ``backend sources not found``）——看起来像包坏了，
        其实只是找不到参照。补了"本脚本所在源码树"这条候选后即可定位。

        ⚠ 判定语义**没有**放宽：真的找不到源码时依旧返回 None（上条锁住）。
        """
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "somewhere" / "OcrBackend"
            package.mkdir(parents=True)
            resolved = verify._resolve_backend_source(package)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved, (PROJECT / "backend").resolve())
            self.assertEqual((resolved / "ocr_backend.py").is_file(), True)

    def test_pyz_check_is_skip_not_pass_when_pyinstaller_is_missing(self):
        """没装 PyInstaller 时必须报 SKIP，绝不能报 PASS。"""
        import builtins

        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "PyInstaller" or name.startswith("PyInstaller."):
                raise ImportError("simulated: PyInstaller is not installed")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "OcrBackend.exe").write_bytes(b"MZ")
            builtins.__import__ = guarded
            try:
                result, detail = verify._bundled_backend_modules(package)
            finally:
                builtins.__import__ = real_import
        self.assertIsNone(
            result,
            "PyInstaller 不可导入时必须返回 None（跳过），不能返回 True（通过）",
        )
        self.assertIn("skipped", detail)

    def test_missing_model_baseline_makes_the_model_checks_fail(self):
        """拿不到模型基线 → 两项模型校验必须 FAIL，且退出码非 0。"""
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "OcrBackend"
            package.mkdir(parents=True)
            # 没有 exe，main() 会在跑 exe 之前就返回，但模型校验已经先做了。
            stdout = io.StringIO()
            argv = [
                "verify_delivery_package.py",
                "--package-dir",
                str(package),
                "--backend-source",
                str(Path(directory) / "no-such-backend"),
            ]
            saved_argv = sys.argv
            sys.argv = argv
            try:
                with contextlib.redirect_stdout(stdout):
                    code = verify.main()
            finally:
                sys.argv = saved_argv
            output = stdout.getvalue()
        self.assertIn("FAIL all models staged", output)
        self.assertIn("FAIL model hashes match", output)
        self.assertIn("skipped", output)
        self.assertNotEqual(code, 0, "模型基线拿不到时退出码必须非 0")


if __name__ == "__main__":
    unittest.main()
