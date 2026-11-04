# -*- coding: utf-8 -*-
"""文本修复启发式的回归契约（2026-10-30 lead 复查 R4）。

这里锁的是「会改错数/改错型号」的五类静默错，它们的共同特征是**不报警、不降置信**，
客户在 Excel 里直接看到错值。每条都给出"不应被改"的反例与"应当被改"的正例。

| 函数 | 旧行为 | 现在 |
|---|---|---|
| `_normalize_proven_reversed_percentages` | `%50` 重复 3 次就被当反转 → `05%`（10 倍错） | 需双向印证；无干净兄弟时只接受真实镜像替换且结果为合法百分比 |
| `_compact_numeric_token` | `1 1/2` → `11/2`（1.5 → 5.5） | 英制混合分数原样保留 |
| `_normalize_dimension_diameter_symbol` | `D25×40` → `Φ25×40`（型号被改写） | 字母前导需列头证据；数字 `0` 前导始终还原 |
| `_looks_like_separated_identifier` | `1~5kg` → `1-5kg`（量程变负号） | 带单位区间保留 `~` |
| `_repair_single_letter_case_from_column_context` | 门限取 `len(Counter)` ⇒ 永不触发 | 按出现次数比较，功能恢复 |
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import ocr_backend  # noqa: E402


def _apply_percent(rows):
    grid = [[value] for value in rows]
    confidence = [[0.99] for _ in rows]
    ocr_backend._apply_consistency_checks(grid, confidence)
    return [row[0] for row in grid]


class ReversedPercentTests(unittest.TestCase):
    def test_legitimate_percent_prefix_is_kept(self):
        """`%50` 是欧陆式合法写法，重复出现不能当"反转证据"。"""
        result = _apply_percent(["编号", "%50", "正常", "%50", "完成", "%50"])
        self.assertEqual(result[1::2].count("%50"), 3, "合法 %50 被改写成了 05%")

    def test_mirrored_percent_still_restored(self):
        """真正镜像的 `%56`（应读作 95%）仍要还原。"""
        result = _apply_percent(["编号", "%56", "正常", "%56", "完成", "%56"])
        self.assertEqual(
            [value for index, value in enumerate(result) if index in (1, 3, 5)],
            ["95%", "95%", "95%"],
        )

    def test_clean_peer_enables_moving_percent_to_tail(self):
        """表里存在干净 `50%` 时，`%50` 应被还原成 `50%`。"""
        result = _apply_percent(["50%", "50%", "50%", "%50"])
        self.assertEqual(result, ["50%", "50%", "50%", "50%"])

    def test_ordinary_percents_untouched(self):
        self.assertEqual(_apply_percent(["50%", "75%", "100%"]), ["50%", "75%", "100%"])


class MixedFractionTests(unittest.TestCase):
    def test_imperial_mixed_fraction_is_preserved(self):
        for value in ("1 1/2", "3 1/4", '1 1/2"', "10 3/8"):
            with self.subTest(value=value):
                self.assertEqual(ocr_backend._compact_numeric_token(value), value,
                                 "英制混合分数被并成了假分数")

    def test_space_artifacts_still_compacted(self):
        self.assertEqual(ocr_backend._compact_numeric_token("944. 985"), "944.985")
        self.assertEqual(ocr_backend._compact_numeric_token("2  026-06-02"), "2026-06-02")

    def test_parallel_integers_not_merged(self):
        self.assertEqual(ocr_backend._compact_numeric_token("12 34"), "12 34")


class DiameterSymbolTests(unittest.TestCase):
    def test_alpha_leader_kept_without_column_evidence(self):
        for value in ("D25×40", "O20×30", "d10×2"):
            with self.subTest(value=value):
                self.assertEqual(ocr_backend._normalize_dimension_diameter_symbol(value), value,
                                 "型号前缀被强行改成了 Φ")

    def test_digit_zero_leader_always_restored(self):
        self.assertEqual(ocr_backend._normalize_dimension_diameter_symbol("025×40"), "Φ25×40")

    def test_alpha_leader_restored_with_diameter_header(self):
        self.assertEqual(
            ocr_backend._normalize_dimension_diameter_symbol("O25×40", allow_alpha_leader=True),
            "Φ25×40",
        )

    def test_plain_numbers_untouched(self):
        for value in ("20×40", "0.5×0.8"):
            with self.subTest(value=value):
                self.assertEqual(ocr_backend._normalize_dimension_diameter_symbol(value), value)


class TildeSeparatorTests(unittest.TestCase):
    def test_range_with_unit_keeps_tilde(self):
        for value in ("1~5kg", "10~15℃", "0~100%", "0.60~0.75 MPa", "1~5"):
            with self.subTest(value=value):
                self.assertEqual(ocr_backend._normalize_identifier_separator(value), value,
                                 "量程/公差被还原成连字符（会被读成减号）")

    def test_identifiers_restored(self):
        cases = {
            "DN~80": "DN-80",
            "6205~2RS": "6205-2RS",
            "SO~260311~07": "SO-260311-07",
            "2026~03~07": "2026-03-07",
            "2026-03~07": "2026-03-07",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ocr_backend._normalize_identifier_separator(raw), expected)


class SingleLetterCaseTests(unittest.TestCase):
    def test_repair_actually_runs_on_dominant_lowercase_column(self):
        """30 个 `v` + 3 个 `V`：旧门限取 len(Counter) 会让此函数永不生效。"""
        grid = [[value] for value in (["v"] * 30 + ["V"] * 3)]
        repaired = ocr_backend._repair_single_letter_case_from_column_context(grid)
        self.assertEqual(len(repaired), 3)
        self.assertTrue(all(row[0] == "v" for row in grid))

    def test_dominant_uppercase_column_is_left_alone(self):
        grid = [[value] for value in (["V"] * 30 + ["v"] * 3)]
        repaired = ocr_backend._repair_single_letter_case_from_column_context(grid)
        self.assertEqual(repaired, set())
        self.assertEqual(sum(1 for row in grid if row[0] == "V"), 30)


if __name__ == "__main__":
    unittest.main()