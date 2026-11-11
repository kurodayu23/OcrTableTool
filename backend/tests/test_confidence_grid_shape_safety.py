"""置信度网格与文字网格**形状不一致**时不得崩溃。

背景（2026-11-05 真实照片全扫）：607 张真实照片里有 1 张直接崩了 ——

    03-外网真实图片\\WC-055-File-United-States-Department-of-Agricul.jpg
    IndexError: list index out of range
      at _guard_unresolved_ascii_ambiguities → confidence_grid[row][column]

不是"识别失败"，是**未捕获异常**：整次识别直接抛出去，用户拿到的是崩溃而不是
"这张图不清晰"这种可行动的结论。

根因：`_guard_unresolved_ascii_ambiguities` 拿 `grid` 的下标去写
`confidence_grid`，但两者**形状不一定一致** —— 有些路径只对部分格做了独立复核，
置信度网格就比文字网格短。越界写就是 IndexError。

修法：用 `_confidence_at` / `_write_confidence` 两个越界安全的读写器；
越界时**跳过写**、不抛异常（该格本来就没有置信度证据，交给下游按"要人工确认"处理）。

本文件守住：
  1. 形状不一致时守卫不抛异常，且已确认的读法仍被登记进 guarded；
  2. 越界读返回默认值、越界写返回 False 且**不改动**原网格；
  3. 守卫函数体内不再出现裸的 `confidence_grid[row][column]` 下标写
     （用 AST 卡住，避免以后再写回裸下标）。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

import ocr_backend


class ConfidenceGridBoundsHelpers(unittest.TestCase):
    """_confidence_at / _write_confidence 的越界行为。"""

    def test_read_returns_default_when_out_of_range(self):
        grid = [[0.9, 0.8], [0.7, 0.6]]
        self.assertEqual(ocr_backend._confidence_at(grid, 0, 0), 0.9)
        self.assertEqual(ocr_backend._confidence_at(grid, 1, 1), 0.6)
        # 行越界 / 列越界 / 负数下标 / 空网格
        self.assertEqual(ocr_backend._confidence_at(grid, 2, 0), 0.0)
        self.assertEqual(ocr_backend._confidence_at(grid, 0, 2), 0.0)
        self.assertEqual(ocr_backend._confidence_at(grid, -1, 0), 0.0)
        self.assertEqual(ocr_backend._confidence_at([], 0, 0), 0.0)
        self.assertEqual(ocr_backend._confidence_at(None, 0, 0), 0.0)

    def test_read_uses_given_default(self):
        grid = [[0.9]]
        self.assertEqual(ocr_backend._confidence_at(grid, 5, 5, 0.77), 0.77)

    def test_write_is_rejected_out_of_range_without_mutating(self):
        grid = [[0.9, 0.8], [0.7, 0.6]]
        self.assertFalse(ocr_backend._write_confidence(grid, 5, 0, 0.1))
        self.assertFalse(ocr_backend._write_confidence(grid, 0, 5, 0.1))
        self.assertFalse(ocr_backend._write_confidence(grid, -1, 0, 0.1))
        self.assertEqual(grid, [[0.9, 0.8], [0.7, 0.6]])
        self.assertTrue(ocr_backend._write_confidence(grid, 1, 1, 0.11))
        self.assertEqual(grid[1][1], 0.11)

    def test_write_handles_none_grid(self):
        self.assertFalse(ocr_backend._write_confidence(None, 0, 0, 0.5))


class GuardRunsOnMismatchedShapes(unittest.TestCase):
    """守卫在 grid 比 confidence_grid 大时不得抛 IndexError。"""

    def test_guard_survives_shorter_confidence_grid(self):
        # 3 行 × 4 列的文字网格，但置信度网格只有 2 行 × 2 列
        grid = [
            ["编号", "金额", "日期", "备注"],
            ["Q6-8498", "1234.5", "2026-11-02", "ok"],
            ["A1-0001", "0", "2026-11-03", "ok"],
        ]
        confidence = [[0.9, 0.9], [0.9, 0.9]]
        guarded = ocr_backend._guard_unresolved_ascii_ambiguities(grid, confidence)
        self.assertIsInstance(guarded, set)
        # 越界的格子不得写进置信度网格（网格形状保持不变）
        self.assertEqual(len(confidence), 2)
        self.assertTrue(all(len(row) == 2 for row in confidence))

    def test_guard_survives_empty_confidence_grid(self):
        grid = [["编号", "金额"], ["Q6-8498", "1234.5"]]
        guarded = ocr_backend._guard_unresolved_ascii_ambiguities(grid, [])
        self.assertIsInstance(guarded, set)

    def test_guard_survives_missing_confidence_grid(self):
        grid = [["编号", "金额"], ["Q6-8498", "1234.5"]]
        guarded = ocr_backend._guard_unresolved_ascii_ambiguities(grid, None)
        self.assertIsInstance(guarded, set)

    def test_guard_still_marks_ambiguous_cell_when_grid_is_big_enough(self):
        # 形状一致时守卫必须照常生效（别把修复做成"整个守卫失效"）：
        # "08" 命中 `0\d{1,}` 这条歧义规则 → 该格降到 ≤0.77 并登记进 guarded。
        grid = [["编号", "金额"], ["08", "1234.5"]]
        confidence = [[0.99, 0.99], [0.99, 0.99]]
        guarded = ocr_backend._guard_unresolved_ascii_ambiguities(grid, confidence)
        self.assertIn((1, 0), guarded)
        self.assertLessEqual(confidence[1][0], 0.77)


class GuardHasNoBareSubscriptWrites(unittest.TestCase):
    """AST 护栏：守卫里不许再出现裸的 confidence_grid[row][column] 写。"""

    def _guard_function(self) -> ast.FunctionDef:
        source = Path(ocr_backend.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_guard_unresolved_ascii_ambiguities":
                return node
        self.fail("源码里找不到 _guard_unresolved_ascii_ambiguities")

    def test_guard_body_has_no_bare_confidence_grid_subscript(self):
        func = self._guard_function()
        offenders = []
        for node in ast.walk(func):
            if not isinstance(node, ast.Subscript):
                continue
            # 只看 "confidence_grid[...]" 这种直接下标访问；
            # 越界安全的 _write_confidence(...) / _confidence_at(...) 不算。
            target = node.value
            if not (isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "confidence_grid"):
                continue
            offenders.append(node.lineno)
        self.assertEqual(
            offenders, [],
            msg="守卫里又出现裸的 confidence_grid[...] 下标访问（第 %s 行），"
                "越界会抛 IndexError" % offenders,
        )
