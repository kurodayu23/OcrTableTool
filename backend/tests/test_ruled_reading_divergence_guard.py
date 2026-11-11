"""多模型分歧兜底：共识把证据权重反转时，格子必须标黄待核对。

背景（2026-11-04 真实翻拍照片回归，PF-00014 / PF-00001）：
真值 ``55`` 被读成 ``552``、真值 ``91`` 被读成 ``912``。
根因是 ``_merge_truncated_reading_groups`` 把"被截短的读法"并进"更完整的超集
版本"—— 这条规则在低分辨率下能救回 ``序号→号``、``4598->45`` 这类真丢字，但
在"背面透印残留被读成多一个字符"这个方向上，它把**票数更多**的短读法并进
**票数更少**的长读法，于是共识反过来选了少数票的错值；置信度还固定写 0.86，
低置信标黄永远不触发 ⇒ 错值静默导出。

这里固定住兜底判据 ``_ruled_cell_reading_needs_confirmation`` 的边界，以及它
在 ``_recognize_complete_ruled_candidate`` 里的接线（不许被悄悄摘掉）。
"""
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import ocr_backend


class RuledReadingDivergenceGuardTests(unittest.TestCase):
    def guard(self, readings, winner):
        return ocr_backend._ruled_cell_reading_needs_confirmation(readings, winner)

    # ---------------------------------------------------------- 少数票胜出
    def test_minority_winner_is_flagged(self):
        # 实测：两个视图读出「55」（0.997 / 0.980），只有一个读出「552」（0.875），
        # 而共识选中了「552」—— 票数被截短合并反转了。
        self.assertTrue(self.guard(
            [("55", 0.997), ("552", 0.875), ("55", 0.980)], "552"))

    def test_minority_winner_with_extra_digit_is_flagged(self):
        # 实测：真值 91 被读成 912，同样是少数票胜出。
        self.assertTrue(self.guard(
            [("91", 0.982), ("912", 0.859), ("91", 0.998)], "912"))

    def test_minority_winner_without_extra_digit_is_also_flagged(self):
        # 证据权重反转本身就不该静默，不管多的是不是数字。
        self.assertTrue(self.guard(
            [("3085", 0.997), ("385", 0.640), ("3085", 0.991)], "385"))

    # -------------------------------------------------- 低分分歧（摇摆格）
    def test_low_confidence_divergence_is_flagged(self):
        # 实测 PF-00014 r22c1：真值「徐刚」被读成「徐丽」，三个模型读成
        # 「徐丽 / 徐丽 / 徐图」，胜出读法自己的最低分只有 0.797。
        # 这类格子在机器负载变化时会在两个读法之间摇摆（OpenVINO 多线程的
        # 浮点归约顺序受调度影响）—— 也就是"同样的照片两次跑结果不同"。
        self.assertTrue(self.guard(
            [("徐丽", 0.797), ("徐丽", 0.996), ("徐图", 0.631)], "徐丽"))

    def test_high_confidence_divergence_of_equal_votes_is_flagged(self):
        # 两票对一票时，少数那一票的存在本身就是"证据不足"。
        self.assertTrue(self.guard(
            [("71", 0.978), ("71", 0.735), ("7", 0.519)], "71"))

    # ------------------------------------------------------------ 不该标黄
    def test_unanimous_reading_is_not_flagged(self):
        self.assertFalse(self.guard(
            [("3085", 1.0), ("3085", 1.0), ("3085", 0.999)], "3085"))

    def test_majority_reading_with_high_score_is_not_flagged(self):
        # 三票里两票是同一个读法，且它的分数都很高 ⇒ 没有证据不足。
        self.assertFalse(self.guard(
            [("3085", 0.997), ("3085", 0.991), ("308", 0.640)], "3085"))

    def test_symbol_only_winner_is_not_flagged(self):
        # 表格里的横线格：三个模型会读出「207 / ST / ''」这种完全不相干的东西
        # （实测 PF-00001 里 7 个横线格全部如此）。拿它们当"证据不足"会把
        # 整张表标黄一大片 —— 实测误伤率会从 0.5% 涨到 10.7%。
        self.assertFalse(self.guard(
            [("207", 0.428), ("ST", 0.570), ("", 0.0)], "-"))

    def test_symbol_only_winner_with_more_symbols_is_not_flagged(self):
        self.assertFalse(self.guard(
            [("57", 0.172), ("pRc", 0.254), ("", 0.0)], "--"))

    def test_empty_and_blank_winners_are_not_flagged(self):
        self.assertFalse(self.guard([("", 0.0), ("", 0.0), ("", 0.0)], ""))
        self.assertFalse(self.guard([], ""))


class GuardWiringTests(unittest.TestCase):
    """护栏必须真的接在识别主线上，不能只定义不调用。"""

    def source(self):
        return inspect.getsource(ocr_backend._recognize_complete_ruled_candidate)

    def test_guard_is_called_inside_the_recognition_path(self):
        source = self.source()
        self.assertIn(
            "_ruled_cell_reading_needs_confirmation", source,
            "识别主线上必须调用分歧兜底判据，否则错值会静默导出",
        )

    def test_flagged_cells_are_marked_for_human_confirmation(self):
        source = self.source()
        self.assertIn("minority_cells", source,
                      "判据命中的格子必须被记录下来")
        self.assertIn(
            "confidence_grid[row][column] = -1.0", source,
            "命中的格子必须降到 -1（结构不可信哨兵）才会进 needs_review",
        )

    def test_flagging_happens_after_repairs(self):
        """降级必须放在所有修复之后 —— 修复函数会把置信度重新抬回 0.86。"""
        source = self.source()
        flagged = source.index("confidence_grid[row][column] = -1.0")
        self.assertLess(
            flagged, source.index("review_locations = ["),
            "降级必须早于 review_locations 的计算，否则标黄会失效",
        )

    def test_wide_latin_path_is_covered_too(self):
        """超宽英文表走的是另一条分支，护栏不能在那里失效。"""
        source = inspect.getsource(ocr_backend._read_very_wide_latin_lines)
        self.assertIn("readings_by_cell", source,
                      "超宽英文分支必须把各视图读法带出去供兜底判定")
        main = self.source()
        self.assertIn("readings_by_cell=model_readings_by_cell", main,
                      "超宽英文分支必须接入同一个兜底判定")


class PublishedContractTests(unittest.TestCase):
    """发布出去的每格字段必须符合 GUI / SDK 的契约。

    ⚠ 这条曾经踩过坑：后端内部用 ``-1.0`` 表示"结构不可信"，如果**直接**把它
    当 confidence 发布出去，GUI 的协议边界（``tabledata.cpp`` 的
    ``confidence < 0.0`` 整表拒收）会把**整张表**变成"识别失败"。
    实际链路是 ``table_pipeline.build_result`` 把它钳位成 0.0 再发布，
    并把 needs_review 置真 —— 这里把这条契约钉死，防止将来被改坏。
    """

    def test_negative_confidence_is_clamped_and_flagged(self):
        import table_pipeline

        result = table_pipeline.build_result(
            [["甲", "乙"]],
            confidence=0.86,
            confidences=[[-1.0, 0.86]],
            engine="guard-test",
        )
        flagged = result["cells"][0][0]
        self.assertEqual(flagged["text"], "甲")
        self.assertEqual(flagged["confidence"], 0.0,
                         "负置信度必须钳位到 0.0 再发布，否则 GUI 整表拒收")
        self.assertTrue(flagged["needs_review"], "负置信度必须标成待复核")
        self.assertFalse(flagged["confidence_sentinel"],
                         "钳位后的 0.0 不是类别哨兵")

    def test_every_published_confidence_is_within_range(self):
        import table_pipeline

        result = table_pipeline.build_result(
            [["甲", "乙", "丙"]],
            confidence=0.86,
            confidences=[[-1.0, 0.0, 0.86]],
            engine="guard-test",
        )
        for cell in result["cells"][0]:
            self.assertGreaterEqual(cell["confidence"], 0.0)
            self.assertLessEqual(cell["confidence"], 1.0)

    def test_trimmed_grid_does_not_publish_negative_confidence(self):
        """⚠ 2026-11-05 真实缺陷：结果被裁剪时**绕过钳位**发布原始值。

        `_trim_grid_to_populated_extent` 在裁剪后会用 confidence_grid 重建
        cells，而它原来写的是 ``float(confidence_grid[...])`` —— 没有钳位。
        confidence_grid 里存着 -1.0（"结构不可信"哨兵）⇒ 负值原样发布：
          * 公开 SDK 的 ``confidence < 0.0`` 校验 ⇒ 判成"非法响应"；
          * GUI 的协议边界（tabledata.cpp）⇒ **整张表**变成"识别失败"。
        """
        result = {
            "cells": [
                [{"text": "甲", "confidence": 0.86, "needs_review": True,
                  "confidence_sentinel": False}],
                [{"text": "乙", "confidence": 0.86, "needs_review": False,
                  "confidence_sentinel": True}],
                [{"text": "", "confidence": 0.0, "needs_review": False,
                  "confidence_sentinel": False}],
                [{"text": "", "confidence": 0.0, "needs_review": False,
                  "confidence_sentinel": False}],
            ],
        }
        confidence_grid = [[-1.0], [0.86], [0.0], [0.0]]
        trimmed, _grid = ocr_backend._trim_grid_to_populated_extent(
            result, confidence_grid)
        published = trimmed["cells"][0][0]
        self.assertGreaterEqual(
            published["confidence"], 0.0,
            "裁剪后发布的负置信度会让 SDK 判非法响应、GUI 整表拒收",
        )
        self.assertLessEqual(published["confidence"], 1.0)
        self.assertTrue(
            published["needs_review"],
            "钳位不能丢掉'要人工确认'的结论",
        )


if __name__ == "__main__":
    unittest.main()
