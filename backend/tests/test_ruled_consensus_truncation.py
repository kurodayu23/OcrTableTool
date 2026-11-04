"""截短的读法不得压过它自己的完整版本。

丢字版本常常票数更多（`金` 被两个视图读出就能压过只出现一次的 `金额`），
于是整列表头集体丢字而且完全静默。这里固定住合并规则的三条边界：
超集唯一、超集自身有分数、代表文本必须真的拼得出规范键。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

import ocr_backend


class RuledConsensusTruncationTests(unittest.TestCase):
    def select(self, readings):
        return ocr_backend._select_ruled_line_consensus(readings)

    def select_family(self, families):
        return ocr_backend._select_ruled_family_consensus(families)

    def test_truncated_reading_loses_to_its_extension(self):
        # 两个视图读出「金」，只有一个读出「金额」：票数 2 > 1，
        # 但丢字没有独立证据，必须取完整版本。
        self.assertEqual(self.select([("金", 0.90), ("金", 0.88), ("金额", 0.85)])[0], "金额")
        self.assertEqual(self.select([("号", 0.92), ("号", 0.90), ("序号", 0.88)])[0], "序号")
        self.assertEqual(self.select([("热片", 0.91), ("热片", 0.90), ("散热片", 0.83)])[0], "散热片")

    def test_truncation_detected_beyond_plain_prefix(self):
        # 「0」不是「20」的前缀或后缀，但仍然是丢字。
        self.assertEqual(self.select([("0", 0.60), ("20", 0.93)])[0], "20")
        self.assertEqual(self.select([("45", 0.91), ("45", 0.90), ("4598", 0.86)])[0], "4598")

    def test_symbol_only_extension_does_not_override(self):
        # 多出来的只是一个杂符号时，完整版本并不更完整：模型多认了一个墨点
        # 而已。合并过去会把原本正确的格子改坏（实测 1200 例语料里
        # `972.961→|972.961`、`2025-08-27→2025-08-27°` 都是这样坏的）。
        self.assertEqual(self.select([("972.961", 0.90), ("972.951", 0.89),
                                     ("|972.961", 0.82)])[0], "972.961")
        self.assertEqual(self.select([("2025-08-27", 0.90),
                                     ("2025-08-27\u00b0", 0.86)])[0], "2025-08-27")
        self.assertEqual(self.select([("14.4", 0.90), ("14.4%", 0.84)])[0], "14.4")

    def test_real_truncation_still_wins_over_majority_shortening(self):
        # 超集唯一且多出的是实义字时仍然要合并：`联器` 两票压过 `联轴器` 一票。
        self.assertEqual(self.select([("联轴器", 0.64), ("联器", 0.87),
                                      ("联器", 0.69)])[0], "联轴器")

    def test_chain_of_extensions_collapses_to_longest(self):
        self.assertEqual(self.select([("1", 0.90), ("12", 0.88), ("123", 0.86)])[0], "123")

    def test_ambiguous_superset_stays_undecided(self):
        # 「2」既可能是 20 也可能是 25，缺的那一位本来就该保持分歧，不能猜。
        text, confidence = self.select([("2", 0.90), ("20", 0.40), ("25", 0.90)])
        self.assertEqual(text, "2")
        self.assertLess(confidence, 0.86)

    def test_two_text_supersets_stay_undecided(self):
        # 「工」既能补成「外协加工」也能补成「外协第工」，两条都是真读法时
        # 不能猜，保持分歧才是对的。
        text, _ = self.select([("外协第工", 0.58), ("外协加工", 0.82), ("工", 0.93)])
        self.assertEqual(text, "工")

    def test_low_confidence_superset_does_not_override(self):
        # 用一个没把握的长串覆盖有把握的短串等于放大不确定。
        text, _ = self.select([("ABC", 0.95), ("ABC", 0.94), ("ABCD", 0.30)])
        self.assertEqual(text, "ABC")

    def test_unrelated_readings_are_untouched(self):
        self.assertEqual(self.select([("8", 0.95), ("8", 0.94)])[0], "8")
        text, confidence = self.select([("A", 0.95), ("B", 0.95)])
        self.assertIn(text, {"A", "B"})
        self.assertLess(confidence, 0.86)

    def test_merged_group_becomes_confirmed(self):
        # 合并后组里不再有竞争对手，这一位应当拿到「已确认」而不是留在 0.77。
        _, confidence = self.select([("金", 0.90), ("金", 0.88), ("金额", 0.85)])
        self.assertEqual(confidence, 0.86)

    def test_family_consensus_applies_the_same_rule(self):
        text, confidence = self.select_family([
            [("金", 0.90)], [("金", 0.88)], [("金额", 0.85)],
        ])
        self.assertEqual(text, "金额")
        self.assertEqual(confidence, 0.86)

    def test_family_consensus_keeps_disagreement_when_ambiguous(self):
        text, _ = self.select_family([
            [("2", 0.90)], [("20", 0.40)], [("25", 0.90)],
        ])
        self.assertEqual(text, "2")


if __name__ == "__main__":
    unittest.main()