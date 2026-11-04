"""空格不得被当成"有内容却读不出来"的硬证据。

背景（2026-10-14 标准清晰表实测）：`_cell_requires_human_confirmation` 的文档
写着"格里有文字却完全没有置信度证据"，但代码只判了 `confidence <= 0`。
空格子没有可读内容，置信度天然是 0，于是每一格空格都成了"未确认内容"。

代价是实测出来的：标准清晰表 9 例里 9 例被整表阻断，逐格准确率 1.000 的
FC-00047 被判成"15 格未确认"，而那 15 格正是真值第 4 列本来就空的 15 行；
FC-00059 / FC-00162 / FC-00079 的未确认格数 91 / 47 / 21 与各自的空格数
**完全相等**，没有一格例外。而接口返回的 `review_cell_count` 是 0 ——
客户既被禁止导出，又看不到任何黄色标记。

真正的硬证据仍然照旧：豆腐块 / 有字但无置信度 / 结构不可信标记的 -1。
"""
import unittest

import ocr_backend

requires = ocr_backend._cell_requires_human_confirmation


class BlankCellIsNotHardEvidence(unittest.TestCase):
    def test_blank_cell_with_zero_confidence_is_confirmed(self):
        self.assertFalse(requires("", 0.0))
        self.assertFalse(requires("   ", 0.0))

    def test_text_without_confidence_is_still_hard_evidence(self):
        self.assertTrue(requires("联轴器", 0.0))
        self.assertTrue(requires("735.651", 0.0))

    def test_negative_marker_is_still_hard_evidence(self):
        # -1 是结构不可信时的显式标记（例如总计列该有值却空着），不能被当成空格
        self.assertTrue(requires("", -1.0))
        self.assertTrue(requires("合计", -1.0))

    def test_unknown_glyph_is_still_hard_evidence(self):
        self.assertTrue(requires("�", 0.95))
        self.assertTrue(requires("AB�", 0.92))
        # 空串没有字形可判，置信度也够 —— 不该凭空变成硬证据
        self.assertFalse(requires("", 0.9))

    def test_sentinel_confidence_alone_is_not_hard_evidence(self):
        # 0.77 / 0.86 是各路径硬写的保守上限哨兵，不是实测置信度
        for sentinel in (0.77, 0.86):
            self.assertFalse(requires("联轴器", sentinel))
            self.assertFalse(requires("735.651", sentinel))


class BlankCellsDoNotTriggerBlockMessages(unittest.TestCase):
    """整表结论必须由真正标黄的格数驱动，不能由空格数量驱动。"""

    def _response_with_sparse_last_column(self):
        # 与 FC-00047 同型：备注列 35 行里 15 行本来就该空着
        rows = [["工号", "出勤天数", "迟到次数", "备注"],
                ["A001", "5", "0", ""],
                ["A002", "3", "1", "现场复查"],
                ["A003", "4", "0", ""],
                ["A004", "2", "2", ""]]
        cells = [[{"text": text, "confidence": 0.0 if not text else 0.9,
                   "needs_review": False} for text in row] for row in rows]
        return {
            "cells": cells,
            "rows": len(rows),
            "columns": 4,
            "publication_blocked": True,
            "publication_block_reasons": ["存在尚未人工确认的黄色单元格"],
            "structure_review_required": True,
            "structure_warnings": [],
            "processing_notices": [],
        }

    def test_blank_cells_produce_no_unconfirmed_locations(self):
        response = self._response_with_sparse_last_column()
        grid = [[cell["text"] for cell in row] for row in response["cells"]]
        confidence_grid = [[cell["confidence"] for cell in row]
                           for row in response["cells"]]
        unconfirmed = [(r, c)
                       for r, values in enumerate(confidence_grid)
                       for c, value in enumerate(values)
                       if 0.0 < float(value) < 0.78]
        pending = [(r, c)
                   for r, c in unconfirmed
                   if requires(grid[r][c], confidence_grid[r][c])]
        self.assertEqual([], pending)


if __name__ == "__main__":
    unittest.main()