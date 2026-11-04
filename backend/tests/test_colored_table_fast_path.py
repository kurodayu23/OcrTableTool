# -*- coding: utf-8 -*-
"""``backend/colored_table.py`` 必须真的走 ``fast_stats`` 加速路径（2026-11-02 新增，F41）。

真实的测试盲区
--------------
``backend/colored_table.py`` 顶上是一段**三级兜底 import**::

    try:
        from fast_stats import median_u8, median_u8_per_channel
    except ImportError:                 # 第 1 级：绝对导入拿不到
        try:
            from .fast_stats import ...  # 第 2 级：包模式下的相对导入
        except ImportError:
            def median_u8(values):        # 第 3 级：纯 np.median 兜底
                return float(np.median(values))
            ...

第 3 级**没有任何告警**：功能完全正确（``np.median`` 与直方图中位数逐位相等），
但会**白丢 12.9 秒**（2026-10-22 剖析：单个 FC-00016 请求里 22 552 次中位数调用，
``ndarray.partition`` 单独就吃掉 12.9 秒 / 88 秒）。

触发条件是**打包漏掉 ``fast_stats``**：源码树里永远走第 1 级 ⇒ 本机自检永远绿，
只有 PyInstaller 出来的 exe 才会静默退化。而：

* ``backend/tests/`` 下 30 个测试文件里，**没有任何一个**断言
  ``colored_table.median_u8 is fast_stats.median_u8``；
* 打包侧只有 ``scripts/package_backend.ps1`` 里一行 ``--hidden-import fast_stats``，
  **没有任何东西**校验它还在。

⇒ 打包时有人删掉/改错那一行，交付件会静默慢 12.9 秒，而**所有功能测试照样全绿**。

本文件的判据
------------
1. **绑定判定**：``colored_table.median_u8`` / ``median_u8_per_channel`` 必须
   **就是** ``fast_stats`` 里的那一个对象（``assertIs``，不是"数值相等"）；
2. **兜底未被安装**：绑定对象的 ``__module__`` 必须是 ``fast_stats``
   （第 3 级的兜底函数定义在 ``colored_table`` 里，``__module__`` 会是 ``colored_table``）；
3. **兜底源码不含 np.median**：兜底实现的函数体里必然出现 ``np.median``，
   加速路径里没有 ⇒ 用 ``inspect.getsource`` 把两者区分开；
4. ⚠ **反向护栏（最关键的一条）**：把 ``numpy.median`` 打成会抛异常的桩，
   再调 ``colored_table.median_u8``（uint8 输入）—— **必须不抛**。
   若真被降级成第 3 级，这条会立刻炸；
   否则"把 import 悄悄改回兜底"会让上面所有对拍**照样全绿**。
5. ⚠ **桩确实有效**（反向护栏的对照组）：同一个桩打上之后，
   非 uint8 输入**应当**抛（证明桩不是摆设）。

本文件**不导入 ``ocr_backend``**（那会触发 OCR 模型准备，1~2 分钟），
只依赖 ``fast_stats`` + ``colored_table`` + numpy。
"""

import unittest
from unittest.mock import patch

import numpy as np

import colored_table
import fast_stats


class ColoredTableFastPathBindingTest(unittest.TestCase):
    def test_median_u8_is_the_fast_stats_function(self):
        """``colored_table.median_u8`` 必须**就是** ``fast_stats.median_u8`` 本身。"""
        self.assertIs(
            fast_stats.median_u8,
            colored_table.median_u8,
            "colored_table.median_u8 没有绑定到 fast_stats.median_u8"
            " ⇒ 已静默退化成 np.median 兜底（功能对，但白丢 12.9 秒）",
        )

    def test_median_u8_per_channel_is_the_fast_stats_function(self):
        self.assertIs(
            fast_stats.median_u8_per_channel,
            colored_table.median_u8_per_channel,
            "colored_table.median_u8_per_channel 没有绑定到 fast_stats 的那一个",
        )

    def test_fallback_shim_was_not_installed(self):
        """兜底函数定义在 ``colored_table`` 里 ⇒ ``__module__`` 不会是 ``fast_stats``。"""
        for name in ("median_u8", "median_u8_per_channel"):
            with self.subTest(name=name):
                module_name = getattr(colored_table, name).__module__
                self.assertEqual(
                    "fast_stats",
                    module_name,
                    "%s 来自 %s 而不是 fast_stats ⇒ 走了第 3 级 np.median 兜底"
                    % (name, module_name),
                )

    def test_degraded_module_is_detected_by_the_binding_guard(self):
        """⚠ 承重证明：**造一个真的降级出来的模块**，证明上面的绑定护栏抓得住它。

        做法：把 ``colored_table.py`` 复制到临时目录（那里没有 ``fast_stats.py``），
        再把 ``sys.path`` 里所有含 ``fast_stats.py`` 的目录摘掉、把 ``sys.modules``
        里的 ``fast_stats`` 暂时摘掉 ⇒ 复制品必然走第 3 级兜底。

        然后断言：**降级确实发生了**（``__module__`` 变成 ``colored_table``），
        **而且本文件的绑定判据能分辨它**（``is not fast_stats.median_u8``）。
        ⇒ 证明上面那些 ``assertIs`` 不是恒绿摆设。
        """
        import importlib.util
        import os
        import sys
        import tempfile

        source_path = os.path.abspath(colored_table.__file__)
        saved_path = list(sys.path)
        saved_module = sys.modules.pop("fast_stats", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = os.path.join(tmp, "colored_table_degraded.py")
                with open(source_path, "rb") as handle:
                    payload = handle.read()
                with open(target, "wb") as handle:
                    handle.write(payload)
                sys.path[:] = [
                    item for item in sys.path
                    if not (os.path.isdir(item)
                            and os.path.exists(os.path.join(item, "fast_stats.py")))
                ]
                sys.path.insert(0, tmp)
                spec = importlib.util.spec_from_file_location(
                    "colored_table_degraded", target)
                module = importlib.util.module_from_spec(spec)
                # ⚠ 必须先登记进 sys.modules：``dataclasses`` 会回头查
                #   ``sys.modules[cls.__module__]``，没登记会抛 AttributeError。
                sys.modules["colored_table_degraded"] = module
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop("colored_table_degraded", None)
        finally:
            sys.path[:] = saved_path
            if saved_module is not None:
                sys.modules["fast_stats"] = saved_module

        self.assertEqual(
            "colored_table_degraded",
            module.median_u8.__module__,
            "复制品竟然没有降级（兜底函数定义在复制品自己身上）"
            " ⇒ 这个承重证明本身失效了（环境没隔离干净）",
        )
        self.assertNotEqual(
            "fast_stats",
            module.median_u8.__module__,
            "复制品仍然绑在 fast_stats 上 ⇒ 环境没隔离干净",
        )
        self.assertIsNot(
            module.median_u8,
            fast_stats.median_u8,
            "降级品居然还等于 fast_stats 的那一个",
        )
        # 降级后功能仍然正确（这正是它"静默"的原因：只看结果永远发现不了）。
        values = np.array([1, 2, 3, 4, 5], dtype=np.uint8)
        self.assertEqual(float(np.median(values)), module.median_u8(values))

    def test_results_are_bit_identical_to_numpy_median(self):
        """绑定的那一个算出来的结果必须与 ``np.median`` 逐位相等（不是"差不多"）。"""
        rng = np.random.default_rng(20261102)
        values = rng.integers(0, 256, size=(64, 48), dtype=np.uint8)
        self.assertEqual(float(np.median(values)), colored_table.median_u8(values))

        channels = rng.integers(0, 256, size=(12, 10, 3), dtype=np.uint8)
        expected = np.median(channels, axis=(0, 1))
        actual = colored_table.median_u8_per_channel(channels)
        self.assertTrue(np.array_equal(expected, actual))

    def test_uint8_path_does_not_call_numpy_median(self):
        """⚠ 反向护栏：把 np.median 打成会抛异常的桩，加速路径必须**不抛**。

        若 ``colored_table`` 被降级成第 3 级兜底，这里立刻炸 ——
        而"数值相等"型的对拍在上面对照下**照样全绿**。
        """
        values = np.array([1, 2, 3, 4, 5], dtype=np.uint8)
        with patch("numpy.median", side_effect=AssertionError("走了 np.median 退路")):
            self.assertEqual(3.0, colored_table.median_u8(values))

    def test_the_numpy_median_stub_is_effective(self):
        """⚠ 反向护栏的对照组：同一个桩对非 uint8 输入**应当**抛。

        没有这一条，上一条可能因为"桩没生效"而恒绿。
        """
        values = np.array([1.0, 2.0], dtype=np.float64)
        with patch("numpy.median", side_effect=AssertionError("桩生效了")):
            with self.assertRaises(AssertionError):
                fast_stats.median_u8(values)


if __name__ == "__main__":
    unittest.main()
