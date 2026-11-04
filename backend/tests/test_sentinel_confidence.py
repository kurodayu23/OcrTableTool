"""哨兵置信度（0.77 / 0.86）不得被当成实测置信度。

背景（见 findings/08-sentinel-confidence-false-alarm.md）：0.77 / 0.86 是各识别
路径硬写的**类别哨兵**（0.77 = 没取得双模型一致确认 / 被截断的上限，
0.86 = 取得了双模型一致确认），**不是实测置信度**。它们和实测值挤在同一个
0~1 数轴上，下游一旦拿 0.78 去比大小就会把绝大多数格子误判成"待复核"：
FC-00005 这张逐格 102/102 全对的表因此被 GUI 标成「102 项待确认」、
XLSX 导出多出 102 条「待人工复核」批注、公开 SDK 判成「非法响应」。

后端的修法（findings 08 的第 0/1/2 步）：
  * 逐格显式写出 `confidence_sentinel`，下游只消费这个字段；
  * `build_result` 不再把哨兵值当成待复核理由；
  * `write_xlsx` 去掉"拿裸 confidence 猜低置信"这个非法来源。

本文件守住这三条，同时守住"真·实测低置信仍然要标"——修误报不能把真告警
一起消掉。
"""
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

import ocr_backend
import table_pipeline


class SentinelValueDetection(unittest.TestCase):
    """0.77 / 0.86 是哨兵，其它数值不是。"""

    def test_sentinel_values_are_detected(self):
        for value in (0.77, 0.86):
            self.assertTrue(
                ocr_backend._is_sentinel_confidence(value),
                msg="后端漏认哨兵值 %r" % value,
            )
            self.assertTrue(
                table_pipeline._is_sentinel_confidence(value),
                msg="pipeline 漏认哨兵值 %r" % value,
            )

    def test_measured_values_are_not_sentinels(self):
        for value in (0.0, 0.5, 0.62, 0.78, 0.79, 0.895, 0.99, 1.0, -1.0, -2.0):
            self.assertFalse(
                ocr_backend._is_sentinel_confidence(value),
                msg="实测值 %r 被误认成哨兵" % value,
            )
            self.assertFalse(
                table_pipeline._is_sentinel_confidence(value),
                msg="实测值 %r 被误认成哨兵" % value,
            )

    def test_illegal_values_are_not_sentinels(self):
        for value in (None, "", "abc", [], {}, object()):
            self.assertFalse(
                ocr_backend._is_sentinel_confidence(value),
                msg="非法值 %r 被误认成哨兵" % value,
            )
            self.assertFalse(
                table_pipeline._is_sentinel_confidence(value),
                msg="非法值 %r 被误认成哨兵" % value,
            )

    def test_numeric_strings_follow_their_number(self):
        # 数值字符串按数值判定，非法字符串不炸。
        self.assertTrue(ocr_backend._is_sentinel_confidence("0.77"))
        self.assertFalse(ocr_backend._is_sentinel_confidence("abc"))

    def test_both_modules_share_the_same_sentinel_set(self):
        # 两个模块各有一份拷贝（pipeline 不能反向 import 后端模块，
        # 那会把离线审计钩子提前装上），必须保持同步。
        self.assertEqual(
            ocr_backend._SENTINEL_CONFIDENCE_VALUES,
            table_pipeline._SENTINEL_CONFIDENCE_VALUES,
        )


class BuildResultReviewSemantics(unittest.TestCase):
    """build_result：哨兵格不再 needs_review，真·实测低置信照旧。"""

    def test_fc00005_style_sentinel_cells_are_not_needs_review(self):
        # FC-00005 的口径：17 行 × 6 列 = 102 格，每格 confidence = 0.77。
        result = table_pipeline.build_result(
            [["单元格"] * 6 for _ in range(17)],
            confidence=0.77,
            confidences=[[0.77] * 6 for _ in range(17)],
            engine="sentinel-test",
        )
        cells = [cell for row in result["cells"] for cell in row]
        self.assertEqual(102, len(cells))
        self.assertFalse(
            [cell for cell in cells if cell["needs_review"]],
            "哨兵格不得再被判成待复核",
        )
        self.assertTrue(
            all(cell["confidence_sentinel"] for cell in cells),
            "每格都要显式写明 confidence_sentinel",
        )

    def test_real_low_confidence_still_needs_review(self):
        # 真·实测低置信（不是哨兵字面量）：必须仍然是待复核。
        result = table_pipeline.build_result(
            [["实测低置信"]],
            confidence=0.62,
            confidences=[[0.62]],
            engine="sentinel-test",
        )
        cell = result["cells"][0][0]
        self.assertTrue(cell["needs_review"], "真·低置信必须仍然需要人工确认")
        self.assertFalse(cell["confidence_sentinel"])

    def test_negative_confidence_still_needs_review(self):
        result = table_pipeline.build_result(
            [[""]],
            confidence=0.9,
            confidences=[[-1.0]],
            engine="sentinel-test",
        )
        self.assertTrue(result["cells"][0][0]["needs_review"])

    def test_blank_uncertain_cell_still_needs_review(self):
        # 空格子带非零置信度 = 那里检出过内容却读不出文字，属于真·不确定。
        result = table_pipeline.build_result(
            [[""]],
            confidence=0.9,
            confidences=[[0.77]],
            engine="sentinel-test",
        )
        self.assertTrue(result["cells"][0][0]["needs_review"])

    def test_empty_cell_without_evidence_is_not_needs_review(self):
        result = table_pipeline.build_result(
            [[""]],
            confidence=0.9,
            confidences=[[0.0]],
            engine="sentinel-test",
        )
        self.assertFalse(result["cells"][0][0]["needs_review"])


class SentinelFlagRefresh(unittest.TestCase):
    """发布流程末尾必须按最终 confidence 重算哨兵标记。

    文本大小写修复会把置信度抬到 0.86、公式检查会把它压到 0.77；这两处之后
    标记若不重算，字段就和对外发布的 confidence 对不上。
    """

    def test_flag_follows_the_final_confidence(self):
        result = {
            "cells": [
                [
                    {"text": "甲", "confidence": 0.77, "confidence_sentinel": False},
                    {"text": "乙", "confidence": 0.86, "confidence_sentinel": False},
                    {"text": "丙", "confidence": 0.62, "confidence_sentinel": False},
                    {"text": "丁", "confidence": 0.99, "confidence_sentinel": False},
                ]
            ]
        }
        ocr_backend._refresh_confidence_sentinel_flags(result)
        flags = [cell["confidence_sentinel"] for cell in result["cells"][0]]
        self.assertEqual([True, True, False, False], flags)


class ExportMarkerSemantics(unittest.TestCase):
    """write_xlsx：哨兵格不再带「待人工复核」批注，真·低置信照旧带。"""

    def _marked(self, cells):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xlsx"
            table_pipeline.write_xlsx(path, cells)
            sheet = load_workbook(path).active
            return [
                sheet.cell(row=row, column=column).comment is not None
                for row in range(1, len(cells) + 1)
                for column in range(1, len(cells[0]) + 1)
            ]

    def test_sentinel_cells_are_not_marked(self):
        cells = [[
            {
                "text": "甲",
                "confidence": 0.77,
                "needs_review": False,
                "confidence_sentinel": True,
            }
        ]]
        self.assertEqual([False], self._marked(cells))

    def test_sentinel_cells_without_the_flag_are_not_marked(self):
        # 老 payload 没有 confidence_sentinel 字段：按值判定，同样不能误标。
        cells = [[{"text": "甲", "confidence": 0.77, "needs_review": False}]]
        self.assertEqual([False], self._marked(cells))

    def test_real_low_confidence_is_still_marked(self):
        cells = [[
            {
                "text": "甲",
                "confidence": 0.62,
                "needs_review": False,
                "confidence_sentinel": False,
            }
        ]]
        self.assertEqual([True], self._marked(cells))

    def test_needs_review_is_still_marked(self):
        cells = [[{"text": "甲", "confidence": 0.99, "needs_review": True}]]
        self.assertEqual([True], self._marked(cells))


if __name__ == "__main__":
    unittest.main()
