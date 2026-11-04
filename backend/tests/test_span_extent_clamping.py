# -*- coding: utf-8 -*-
"""裁剪后残留的悬空合并格，必须收进裁剪后的边界内（2026-10-24 修）。

2026-10-24 的两处修复当时**没有配任何单测**：

1. ``_clamp_spans_to_extent`` —— 裁剪网格后，跨出边界的合并区原来只按
   **锚点**过滤（``column <= last_column``），锚点还在表内、范围却指向已被
   裁掉的列，于是 ``table_pipeline.write_xlsx`` 校验跨度时抛
   ``ValueError: invalid XLSX span: outside table bounds``
   ⇒ **导出失败、交付不出表**。常见形态是「标题行整表宽度的合并格」
   + 右侧还有空白网格列。
2. ``_merge_unit_suffix_columns`` 里的锚点 —— ``values``/``scores`` 是按
   kept 的**位置**编号的，不是列号；kept 里有间隔时（>=2 个被并掉的列、
   中间还夹着保留列）两者不等，拿列号当位置会把单位文字写进相邻的无关列。

这两处都在**只在 recognize 路径上**跑到的代码里（单测直调引擎覆盖不到），
正是 2026-10-22 漏掉 ``runtime_profile`` 变量名写错的那一类。
这里用行为测试把语义钉住。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402


def _cell(text: str, confidence: float = 0.95) -> dict:
    return {"text": text, "confidence": confidence}


class ClampSpansToExtentTests(unittest.TestCase):
    """``_clamp_spans_to_extent``：按**跨度**而不是锚点收边界。"""

    def test_span_inside_the_extent_is_kept_unchanged(self):
        spans = [{"row": 0, "column": 1, "row_span": 1, "column_span": 2}]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(kept, [{"row": 0, "column": 1, "row_span": 1, "column_span": 2}])

    def test_anchor_beyond_the_extent_is_dropped(self):
        spans = [{"row": 0, "column": 4, "row_span": 1, "column_span": 2}]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(kept, [])

    def test_span_crossing_the_boundary_is_narrowed_not_dropped(self):
        """锚点在内、范围越界 ⇒ 收窄到剩余部分（标题合并要保得住）。"""
        spans = [{"row": 0, "column": 0, "row_span": 1, "column_span": 5}]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["column"], 0)
        self.assertEqual(kept[0]["column_span"], 4)

    def test_row_axis_is_handled_the_same_way(self):
        spans = [{"row": 1, "column": 0, "row_span": 4, "column_span": 1}]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="row", limit=2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["row"], 1)
        self.assertEqual(kept[0]["row_span"], 2)

    def test_non_dict_entries_are_dropped(self):
        spans = ["not-a-span", {"row": 0, "column": 0, "row_span": 1, "column_span": 1}]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(len(kept), 1)

    def test_missing_zero_or_none_span_size_is_kept(self):
        """跨度缺失/为 0/为 None 时，按**至少 1 格**参与判定，不抛异常、不丢数据。

        注意：函数只把跨度用在**判定**上，不会把归一化后的值写回 span
        （那是 span 生产方的责任：``colored_table.physical_spans`` 里
        ``right - left`` / ``bottom - top`` 恒 >= 1，产不出 0 跨度）。
        """
        spans = [
            {"row": 0, "column": 0},
            {"row": 0, "column": 1, "column_span": 0},
            {"row": 0, "column": 2, "column_span": None},
        ]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(len(kept), 3)

    def test_zero_size_span_beyond_the_extent_is_still_dropped(self):
        """跨度按 1 格算 ⇒ 锚点越界就丢，不会因为跨度是 0 而被留下。"""
        spans = [{"row": 0, "column": 4, "column_span": 0}]
        kept = ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(kept, [])

    def test_input_list_is_not_mutated(self):
        spans = [{"row": 0, "column": 0, "row_span": 1, "column_span": 5}]
        ocr_backend._clamp_spans_to_extent(spans, axis="column", limit=3)
        self.assertEqual(spans[0]["column_span"], 5)


class TrimGridKeepsSpansInsideBoundsTests(unittest.TestCase):
    """裁剪后的合并格必须仍然落在表内 —— 否则导出直接失败。"""

    def _title_over_wide_grid(self) -> tuple[dict, list]:
        # 标题行整表宽度的合并格（0..4），正文只占 0..2，右侧两列是空白网格列。
        rows = [
            ["标题", "", "", "", ""],
            ["A", "B", "C", "", ""],
        ]
        grid = [[_cell(text) for text in row] for row in rows]
        confidence_grid = [[0.95 for _ in row] for row in rows]
        result = {
            "cells": grid,
            "spans": [
                {"row": 0, "column": 0, "row_span": 1, "column_span": 5, "role": "title"}
            ],
        }
        return result, confidence_grid

    def test_trim_narrows_the_span_into_the_trimmed_extent(self):
        result, confidence_grid = self._title_over_wide_grid()
        trimmed, _ = ocr_backend._trim_grid_to_populated_extent(result, confidence_grid)
        self.assertEqual(len(trimmed["cells"][0]), 3)
        self.assertEqual(len(trimmed["spans"]), 1)
        self.assertEqual(trimmed["spans"][0]["column_span"], 3)

    def test_trimmed_title_span_still_exports_to_xlsx(self):
        """这是 2026-10-24 修的交付阻塞项：修之前这里抛 ValueError。"""
        result, confidence_grid = self._title_over_wide_grid()
        trimmed, trimmed_confidence = ocr_backend._trim_grid_to_populated_extent(
            result, confidence_grid
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trimmed.xlsx"
            table_pipeline.write_xlsx(
                output,
                trimmed["cells"],
                spans=trimmed["spans"],
            )
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

    def test_span_outside_the_trimmed_extent_is_rejected_by_write_xlsx(self):
        """反向锁：write_xlsx 仍会拒绝真正越界的跨度（闸门没被放松）。

        5 列的网格上放一个跨 6 列的合并格 => 越界，必须报错。
        """
        rows = [["标题", "", "", "", ""], ["A", "B", "C", "", ""]]
        grid = [[_cell(text) for text in row] for row in rows]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bad.xlsx"
            with self.assertRaises(ValueError) as raised:
                table_pipeline.write_xlsx(
                    output,
                    grid,
                    spans=[
                        {
                            "row": 0,
                            "column": 0,
                            "row_span": 1,
                            "column_span": 6,
                            "role": "title",
                        }
                    ],
                )
            self.assertIn("invalid XLSX span", str(raised.exception))


class MergeUnitSuffixAnchorTests(unittest.TestCase):
    """``_merge_unit_suffix_columns``：锚点要用 kept 的**位置**，不是列号。"""

    def test_unit_lands_in_the_neighbouring_kept_column_not_the_wrong_one(self):
        # kept = [0, 1, 3, 5]（被并掉 2、4 两列，中间夹着保留列 3）
        # 并第 4 列时：列号算出的锚点是 3（第 5 列"余量"），
        # 位置算出的锚点是 2（第 3 列"≤4.5"）—— 后者才是对的。
        rows = [
            ["参数", "数值", "", "备注", "", "余量"],
            ["转速", "≤4.5", "mm/s", "≤4.5", "mm/s", "0.2"],
            ["压力", "≤10", "kPa", "≤10", "kPa", "1.0"],
            ["流量", "≤3.0", "L/min", "≤3.0", "L/min", "0.5"],
        ]
        grid = [[_cell(text) for text in row] for row in rows]
        confidence_grid = [[0.95 for _ in row] for row in rows]
        result = {"cells": grid, "spans": []}

        merged, _ = ocr_backend._merge_unit_suffix_columns(result, confidence_grid)
        merged_rows = [[cell["text"] for cell in row] for row in merged["cells"]]

        self.assertEqual(len(merged_rows[0]), 4)
        # 第 2 列（"≤4.5 mm/s"）与第 3 列（"≤4.5 mm/s"）各自拿到自己左边的单位
        self.assertEqual(merged_rows[1][1], "≤4.5 mm/s")
        self.assertEqual(merged_rows[1][2], "≤4.5 mm/s")
        # 关键断言：最右边那列是**无关列**，绝不能被塞进单位文字
        self.assertEqual(merged_rows[1][3], "0.2")
        self.assertEqual(merged_rows[2][3], "1.0")
        self.assertEqual(merged_rows[3][3], "0.5")


if __name__ == "__main__":
    unittest.main()
