"""「未取得双模型一致确认」不得成为整表阻断理由。

背景（2026-10-14 实测）：43 例真表语料里，confidence_grid 存的 0.77/0.86 是各条
识别路径硬写的**保守上限哨兵**，不是实测置信度。主路径、photo 路径、
borderless_columns 路径、certified ruled 路径这四处都拿它和 0.78 比，
把"没做独立复核"读成"复核失败"，于是几乎每一格都被算成"未确认"，
整表直接 `publication_blocked`。

代价是实测出来的：18 张准确率 ≥0.75 且形状正确的表里，14 张被阻断，
其中 4 张逐格准确率 1.000。客户拿到完全正确的结果，却被告知禁止直接导出。

硬证据（豆腐块 / 无置信度证据）仍然照旧标黄并阻断——那是真正需要人看的。
"""
import unittest

import ocr_backend


def cell(text, confidence):
    return {"text": text, "confidence": confidence, "needs_review": False}


def result_with(cells, confidences):
    rows = len(cells)
    columns = len(cells[0]) if rows else 0
    return {
        "cells": [[cell(text, confidences[r][c])
                   for c, text in enumerate(row)]
                  for r, row in enumerate(cells)],
        "rows": rows,
        "columns": columns,
        "structure_verified": True,
        "structure_certificate": {
            "version": 1, "verified": True, "source": "test",
            "rows": rows, "columns": columns, "spans": [],
        },
        "publication_blocked": False,
        "publication_block_reasons": [],
        "processing_notices": [],
        "structure_warnings": [],
        "structure_review_required": False,
        "review_cell_count": 0,
    }


class SentinelConfidenceTests(unittest.TestCase):
    def test_sentinel_confidence_is_not_human_confirmation(self):
        # 0.77/0.86 是哨兵，不是实测置信度：不得因此要求人工确认。
        for value in (0.77, 0.86, 0.5):
            self.assertFalse(
                ocr_backend._cell_requires_human_confirmation("正常内容", value),
                msg="置信度 %r 被误判成需要人工确认" % value,
            )

    def test_real_hard_evidence_still_requires_confirmation(self):
        # 豆腐块/替换字符：模型没读出这个字，必须人工确认。
        self.assertTrue(ocr_backend._cell_requires_human_confirmation("□号", 0.99))
        self.assertTrue(ocr_backend._cell_requires_human_confirmation("材�料", 0.99))
        # 有文字却完全没有置信度证据。
        self.assertTrue(ocr_backend._cell_requires_human_confirmation("有文字", 0.0))
        self.assertTrue(ocr_backend._cell_requires_human_confirmation("有文字", -1.0))


class BlockingPolicyTests(unittest.TestCase):
    """直接跑 _recognize 里的组装逻辑代价太高，这里对判据本身做断言。

    真正保证"不阻断"的是这四处：unconfirmed_locations 只进 notices，
    只有 review_locations（有硬证据）才进 reasons 并触发 publication_blocked。
    """

    def test_unconfirmed_cells_alone_do_not_block(self):
        grid = [["序号", "数量"], ["A-1", "12"], ["B-2", "34"]]
        confidence = [[0.77, 0.86], [0.77, 0.77], [0.77, 0.86]]
        unconfirmed = [
            (r, c)
            for r in range(len(grid))
            for c in range(len(grid[r]))
            if 0.0 < float(confidence[r][c]) < 0.78
        ]
        review = [
            (r, c)
            for r in range(len(grid))
            for c in range(len(grid[r]))
            if ocr_backend._cell_requires_human_confirmation(
                grid[r][c], confidence[r][c]
            )
        ]
        self.assertEqual(len(unconfirmed), 4)
        self.assertEqual(review, [], "哨兵置信度不应产生任何待复核格")

    def test_unknown_glyph_still_blocks(self):
        grid = [["序号", "数量"], ["□号", "12"]]
        confidence = [[0.99, 0.99], [0.99, 0.99]]
        review = [
            (r, c)
            for r in range(len(grid))
            for c in range(len(grid[r]))
            if ocr_backend._cell_requires_human_confirmation(
                grid[r][c], confidence[r][c]
            )
        ]
        self.assertEqual(review, [(1, 0)])

    def test_source_no_longer_blocks_on_unconfirmed_alone(self):
        """源码级护栏：文案还在，但只能出现在 notices 里。"""
        import inspect
        source = inspect.getsource(ocr_backend)
        marker = "未取得两个模型的一致确认"
        self.assertIn(marker, source)
        # 每处都必须挂在 notices/processing_notices 上，且不得再带
        # "整表不能作为已验证结果直接导出" 的阻断措辞。
        self.assertNotIn(
            marker + '"（{preview}），整表不能作为已验证结果直接导出。"',
            source,
        )
        self.assertNotIn(
            marker + '"（{locations}），整表不能作为已验证结果直接导出；"',
            source,
        )


class AdvisoryStructureWarningTests(unittest.TestCase):
    """只说"缺少物理证据"的结构告警不得单独成为阻断理由。

    43 例实测：这些告警命中 7 例里 6 例形状正确（逐格 0.93~1.00），
    FC-00022 逐格 1.000 却被告知"整表需复核，禁止导出"。
    真正带信号的那几条（未取得可锁定的物理网格 14 例里 10 例形状错、
    行列边界不够稳定 3 例全错）必须仍然是阻断理由。
    """

    ADVISORY = ocr_backend._ADVISORY_STRUCTURE_WARNINGS

    def test_advisory_warnings_are_removed_from_block_reasons(self):
        structural_warnings = [
            "首行跨列标题未能锁定合并范围，整表需复核。",
            "未取得可锁定的物理网格，合并关系未自动应用，请核对行列。",
        ]
        reasons = [
            w for w in structural_warnings
            if not any(fragment in w for fragment in self.ADVISORY)
        ]
        self.assertEqual(reasons, ["未取得可锁定的物理网格，合并关系未自动应用，请核对行列。"])

    def test_real_evidence_warnings_still_block(self):
        for warning in (
            "未取得可锁定的物理网格，合并关系未自动应用，请核对行列。",
            "行列边界不够稳定，结果已按当前检测位置生成，请重点核对行列。",
            "表格纵向边界证据不足，无法安全确认列结构。",
            "裁剪边缘存在未能稳定复原的表格证据，结果已禁止直接发布。",
            "存在可见但未能安全确认的单元格，禁止直接导出。",
        ):
            self.assertFalse(
                any(fragment in warning for fragment in self.ADVISORY),
                msg="带真实证据的告警被误收进提示清单：%s" % warning,
            )

    def test_advisory_list_only_contains_measured_false_positives(self):
        # 逐条实测命中率：命中案例数 / 其中形状正确数。
        # 门槛是"形状正确必须超过一半"——低于一半说明这条告警仍在抓真错，
        # 不能因为看起来啰嗦就降级成提示。
        measured = {
            "首行跨列标题未能锁定合并范围": (7, 6),
            "物理合并格内部仍有独立字形": (2, 2),
            "已检测网格外仍存在相邻表格横线": (2, 2),
        }
        for fragment in self.ADVISORY:
            if fragment in measured:
                hits, correct = measured[fragment]
                self.assertGreater(
                    correct, hits / 2,
                    msg="%s 命中率 %d/%d 不足以判定为误伤" % (fragment, correct, hits),
                )

    def test_weakly_supported_warning_stays_a_block_reason(self):
        # 实测 2 例只对 1 例，达不到门槛，必须仍然是阻断理由。
        self.assertNotIn(
            "多级表头的合并关系未完全确认", self.ADVISORY,
            msg="命中率不足一半的告警被误收进提示清单",
        )


if __name__ == "__main__":
    unittest.main()