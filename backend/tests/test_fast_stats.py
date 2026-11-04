# -*- coding: utf-8 -*-
"""``backend/fast_stats.py`` 的单元测试（2026-11-02 新增，F39）。

背景（真实的测试盲区）
----------------------
``backend/fast_stats.py`` 实现了 **8-bit 图像的精确中位数**（256 桶直方图，
一次计数 + 一次前缀和），用来替掉 ``np.median`` 的 partition 拷贝 ——
2026-10-22 的性能剖析显示：一个 FC-00016 请求里有 22 552 次中位数调用、
约 1 亿个元素，``ndarray.partition`` 单独就吃掉 12.9 秒 / 88 秒。

它的 docstring 明确承诺：结果与 ``np.median`` **bit-identical**（含偶数长度
取中间两数的平均值那条规则），**所以不会有任何几何判定发生变化**。

⚠ 但这个模块此前**一个单元测试都没有**（``backend/tests/`` 下 31 个测试文件
里没有任何一个引用 ``fast_stats``）⇒ 那个"bit-identical"的承诺**没有任何东西
在守**。而且它是**表格线几何测量**的输入 ⇒ 一旦算错，识别质量会静默下降，
不会被任何现有测试发现。

本文件的判据：

1. **对拍**：随机 8-bit 数组上 ``median_u8`` 必须与 ``float(np.median(...))``
   **完全相等**（逐位），奇数/偶数长度都覆盖；
2. **对拍**：``median_u8_per_channel`` 必须与 ``np.median(values, axis=(0, 1))``
   完全相等；
3. **边界**：空数组、单元素、两个元素、全同值、同时含 0 与 255；
4. **退化路径**：非 ``uint8`` dtype、非 3 维输入必须与 ``np.median`` 一致；
5. ⚠ **反向护栏（最关键的一条）**：把 ``np.median`` 打成会抛异常的桩，
   验证 ``median_u8`` 对 ``uint8`` 输入**根本没调用** ``np.median``
   ⇒ 证明走的是**直方图路径**。否则"把实现悄悄改回 ``np.median``"
   会让上面所有对拍**照样全绿**，而性能优化已经没了。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from fast_stats import median_u8, median_u8_per_channel


class MedianU8ParityTest(unittest.TestCase):
    """与 np.median 的逐位对拍。"""

    def test_random_arrays_match_numpy_median(self):
        rng = np.random.default_rng(20261102)
        for size in list(range(1, 40)) + [64, 127, 128, 129, 255, 256, 1000, 4096]:
            values = rng.integers(0, 256, size=size, dtype=np.uint8)
            with self.subTest(size=size):
                self.assertEqual(
                    float(np.median(values)),
                    median_u8(values),
                    "size=%d 的 8-bit 中位数与 np.median 不一致" % size,
                )

    def test_even_length_uses_mean_of_two_middle_values(self):
        """偶数长度必须取中间两数的平均值（不是取下中位数）。"""
        values = np.array([10, 20, 30, 40], dtype=np.uint8)
        self.assertEqual(25.0, median_u8(values))
        self.assertEqual(float(np.median(values)), median_u8(values))

    def test_odd_length_takes_the_middle_value(self):
        values = np.array([10, 20, 30], dtype=np.uint8)
        self.assertEqual(20.0, median_u8(values))

    def test_two_element_array(self):
        values = np.array([7, 9], dtype=np.uint8)
        self.assertEqual(8.0, median_u8(values))

    def test_all_values_identical(self):
        for value in (0, 1, 128, 255):
            values = np.full(11, value, dtype=np.uint8)
            with self.subTest(value=value):
                self.assertEqual(float(value), median_u8(values))

    def test_extremes_present(self):
        values = np.array([0, 255, 0, 255, 0, 255, 128], dtype=np.uint8)
        self.assertEqual(float(np.median(values)), median_u8(values))

    def test_multidimensional_input(self):
        rng = np.random.default_rng(7)
        values = rng.integers(0, 256, size=(9, 13), dtype=np.uint8)
        self.assertEqual(float(np.median(values)), median_u8(values))

    def test_empty_array_falls_back_to_numpy(self):
        """空数组 ⇒ 两边都返回 NaN（NaN != NaN，要单独判）。"""
        empty = np.array([], dtype=np.uint8)
        expected = float(np.median(empty))
        actual = median_u8(empty)
        self.assertTrue(np.isnan(expected) and np.isnan(actual),
                        "空数组两边都应返回 NaN（实际 %r / %r）" % (expected, actual))


class NonUint8FallbackTest(unittest.TestCase):
    """非 uint8 / 非 3 维 ⇒ 退回 np.median，结果仍必须一致。"""

    def test_int32_input(self):
        values = np.array([3, 1, 4, 1, 5, 9, 2, 6], dtype=np.int32)
        self.assertEqual(float(np.median(values)), median_u8(values))

    def test_float64_input(self):
        values = np.array([0.5, 2.5, 1.5], dtype=np.float64)
        self.assertEqual(float(np.median(values)), median_u8(values))

    def test_per_channel_non_uint8_falls_back(self):
        values = np.zeros((4, 5, 3), dtype=np.float64)
        values[0, 0, 1] = 9.0
        expected = np.median(values, axis=(0, 1))
        actual = median_u8_per_channel(values)
        self.assertTrue(np.allclose(expected, actual))


class PerChannelParityTest(unittest.TestCase):
    def test_random_images_match_numpy_axis_median(self):
        rng = np.random.default_rng(31337)
        for shape in [(1, 1, 1), (2, 3, 3), (7, 5, 3), (16, 9, 3), (4, 6, 1)]:
            values = rng.integers(0, 256, size=shape, dtype=np.uint8)
            with self.subTest(shape=shape):
                expected = np.median(values, axis=(0, 1))
                actual = median_u8_per_channel(values)
                self.assertTrue(
                    np.array_equal(expected, actual),
                    "shape=%s 的逐通道中位数与 np.median(axis=(0,1)) 不一致" % (shape,),
                )

    def test_non_3d_falls_back(self):
        values = np.array([[1, 2], [3, 4]], dtype=np.uint8)
        expected = np.median(values, axis=(0, 1))
        actual = median_u8_per_channel(values)
        self.assertTrue(np.allclose(expected, actual))


class HistogramPathGuardTest(unittest.TestCase):
    """⚠ 反向护栏：必须真的走**直方图**路径，不能悄悄退化成 ``np.median``。

    没有这一条，把实现改回 ``np.median`` 会让上面所有对拍**照样全绿**，
    而 2026-10-22 那个 12.9 秒的性能优化就无声地没了。
    """

    def test_uint8_path_does_not_call_numpy_median(self):
        values = np.array([1, 2, 3, 4, 5], dtype=np.uint8)
        with patch("fast_stats.np.median", side_effect=AssertionError("走了 np.median 退路")):
            self.assertEqual(3.0, median_u8(values))

    def test_per_channel_uint8_path_does_not_call_numpy_median(self):
        values = np.arange(3 * 4 * 3, dtype=np.uint8).reshape(3, 4, 3)
        with patch("fast_stats.np.median", side_effect=AssertionError("走了 np.median 退路")):
            result = median_u8_per_channel(values)
        self.assertEqual(3, result.shape[0])

    def test_fallback_still_calls_numpy_median(self):
        """非 uint8 输入**应当**走 np.median（这条证明上面的桩确实有效）。"""
        values = np.array([1.0, 2.0], dtype=np.float64)
        with patch("fast_stats.np.median", side_effect=AssertionError("不该被调用")):
            with self.assertRaises(AssertionError):
                median_u8(values)


if __name__ == "__main__":
    unittest.main()
