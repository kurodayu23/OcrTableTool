"""表外抬头说明行剔除的回归测试。

判据的边界来自 611 例真实响应的合成扫描：拿真表响应当主体、前面拼 1~4 行
稀疏抬头，2444 次判定"多吃掉真表自己的行"为 0。这里锁住那些必须成立的行为。

**"没有物理证据就不删"这条前提已被实测推翻并移除。** 标准清晰集 48 例里 7 例
（14.6%）整表凭空多出开头一行，逐格准确率全部归零，其中 6 例因为拿不到物理线框
证书而被旧闸门挡掉；那张拿到证书的 FC-00122 反而证明线框数出来的行里本来就含
着抬头一带（真值 41 行 / 识别 42 行）。现在改用 span 角色当护栏：真值 1200 例里
第 0 行只有 row_header / group_header，从没有 title；表内两级表头一律带
group_header span，删不动。
"""

import unittest

import ocr_backend


def row(*texts):
    return [{"text": text} for text in texts]


class LeadingCaptionBandTests(unittest.TestCase):
    def test_four_row_caption_block_is_removed_whole(self):
        # 实测形态：标题 / 单位 / 统计月份 / 制表人 连着四行，每行只占左边一小段。
        cells = [
            row("费用分摊表", "", "", "", "", ""),
            row("单位：万元", "", "", "", "", ""),
            row("统计月份：2026.10", "", "", "", "", ""),
            row("制表：财务部", "", "", "", "", ""),
            row("编号", "名称", "规格", "数量", "单价", "金额"),
            row("Material", "BB-3753", "77975.58", "90567", "76.2%", "9669.77"),
        ]
        self.assertEqual(ocr_backend._leading_caption_band_length(cells), 4)

    def test_two_level_header_group_row_is_not_a_caption(self):
        # 宽表的两级表头分组行本来就只填少数几列，但它就是表里的内容，不能删。
        cells = [
            row("物料组", "成本构成", "金额汇总", "", "", "", "", "", "", "", ""),
            row("原料", "", "", "人工", "", "", "制造费用", "", "", "", ""),
            row("A1", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"),
        ]
        self.assertEqual(ocr_backend._leading_caption_band_length(cells), 0)

    def test_caption_ending_at_last_row_is_rejected(self):
        cells = [
            row("表格标题", "", "", "", ""),
            row("第二行", "", "", "", ""),
            row("第三行", "", "", "", ""),
        ]
        self.assertEqual(ocr_backend._leading_caption_band_length(cells), 0)

    def test_narrow_grid_is_left_alone(self):
        cells = [
            row("标题", ""),
            row("A", "B"),
            row("1", "2"),
            row("3", "4"),
        ]
        self.assertEqual(ocr_backend._leading_caption_band_length(cells), 0)

    def test_ragged_rows_are_left_alone(self):
        cells = [
            row("标题", "", "", ""),
            row("A", "B", "C", "D"),
            row("1", "2", "3", "D"),
        ]
        cells[2] = cells[2][:2]
        self.assertEqual(ocr_backend._leading_caption_band_length(cells), 0)


class DropLeadingCaptionRowsTests(unittest.TestCase):
    def _result(self, cells):
        return {
            "cells": [list(r) for r in cells],
            "rows": len(cells),
            "columns": len(cells[0]),
            "structure_verified": False,
            "structure_certificate": None,
            "structure_review_required": True,
            "publication_blocked": True,
            "recognition_state": "blocked",
            "spans": [],
            "publication_block_reasons": [],
            "processing_notices": [],
            "structure_warnings": [],
        }

    def test_without_certificate_the_caption_is_still_dropped(self):
        # 没有物理线框证书也照删：标准集里 6/7 例正确的抬头删除全被旧闸门挡掉，
        # 那些表逐格准确率全归零（真值 17 行 / 识别 18 行，整表下移一行）。
        # 护栏换成 span 角色，见 test_leading_caption_drop.py。
        cells = [
            row("费用分摊表", "", "", "", "", ""),
            row("单位：万元", "", "", "", "", ""),
            row("编号", "名称", "规格", "数量", "单价", "金额"),
            row("Material", "BB-3753", "77975.58", "90567", "76.2%", "9669.77"),
        ]
        result = self._result(cells)
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["cells"][0][0]["text"], "编号")

    def test_heuristic_certificate_blocks_shape_change(self):
        # 启发式锁定的网格在降级之前不许改形状：证书是按这些行边界生成的。
        cells = [
            row("标题", "", "", "", "", ""),
            row("单位", "", "", "", "", ""),
            row("编号", "名称", "规格", "数量", "单价", "金额"),
            row("Material", "BB-3753", "77975.58", "90567", "76.2%", "9669.77"),
        ]
        result = self._result(cells)
        result["structure_verified"] = True
        result["structure_certificate"] = {
            "verified": True,
            "source": "photographic_colored_physical_grid",
            "rows": 2,
        }
        self.assertFalse(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(result["rows"], 4)

    def test_physical_certificate_rows_and_spans_are_shifted(self):
        cells = [
            row("标题", "", "", ""),
            row("单位", "", "", ""),
            row("统计月份", "", "", ""),
            row("制表", "", "", ""),
            row("编号", "名称", "数量", "金额"),
            row("Material", "BB-3753", "90567", "9669.77"),
            row("Description", "TTT-2774", "35742.42", "67"),
        ]
        result = self._result(cells)
        result["structure_verified"] = True
        result["structure_certificate"] = {
            "verified": True,
            "source": "screen_ruled_grid",
            "rows": 3,
            "row_offset": 0,
            "columns": 4,
            "column_offset": 0,
            "column_boundaries": [0, 1, 2, 3, 4],
            "spans": [
                {"row": 4, "column": 0, "row_span": 1, "column_span": 4},
                {"row": 2, "column": 1, "row_span": 1, "column_span": 1},
            ],
            "row_column_boundaries": [[0], [1], [2]],
        }
        result["spans"] = [dict(s) for s in result["structure_certificate"]["spans"]]
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(result["rows"], 3)
        certificate = result["structure_certificate"]
        self.assertEqual(certificate["rows"], 3)
        # 线框认定的表格只有 3 行，多出来的 4 行全是表外抬头
        self.assertEqual(certificate["row_offset"], 4)
        # 落在被剔除区域里的合并格随抬头一起消失，剩下的平移到第 0 行
        self.assertEqual(len(certificate["spans"]), 1)
        self.assertEqual(certificate["spans"][0]["row"], 0)
        self.assertEqual(result["spans"], certificate["spans"])
        # 2026-10-30（lead 复查 P0）：裁剪 row_column_boundaries 现在**只由
        # `_align_structure_certificate_to_grid` 一处做**，本函数只负责把 row_offset
        # 累加（旧实现在这里也裁一次，align 再按同一 offset 裁第二次 ⇒ 列表被裁两次、
        # row_column_hash 又没跟着重算，导致带逐行边界的物理证书认证恒被撤销）。
        self.assertEqual(
            certificate["row_column_boundaries"],
            [[0], [1], [2]],
            "本函数不得自行裁剪逐行边界（offset 归一化归 align 管）",
        )
        self.assertTrue(result["publication_blocked"])

    def test_in_table_merged_title_with_group_header_is_kept(self):
        # 表格的网格外本来就有跨列大标题行（Excel A1:M1 合并），首行同样稀疏。
        # 区分它和页边抬头的唯一结构证据是它带 group_header span —— 真值 1200 例
        # 里第 0 行的 span 角色只有 row_header(250) 和 group_header(500)，从来没有
        # title；标准集响应里第 0 行的 7 个 title 角色 span 逐条核对下来全是抬头。
        cells = [
            row("月度成本汇总表", "", "", ""),
            row("2026 年 10 月", "", "", ""),
            row("编号", "名称", "数量", "金额"),
            row("Material", "BB-3753", "90567", "9669.77"),
            row("Description", "TTT-2774", "35742.42", "67"),
        ]
        result = self._result(cells)
        result["spans"] = [
            {"row": 0, "column": 0, "row_span": 1, "column_span": 4,
             "role": "group_header"},
        ]
        result["structure_verified"] = True
        result["structure_certificate"] = {
            "verified": True,
            "source": "photographic_complete_ruled_line_grid",
            "rows": 5,
            "row_offset": 0,
            "columns": 4,
            "column_offset": 0,
            "column_boundaries": [0, 1, 2, 3, 4],
            "spans": [dict(span) for span in result["spans"]],
            "row_column_boundaries": [[0], [1], [2], [3], [4]],
        }
        self.assertFalse(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(result["rows"], 5)

    def test_merged_cells_in_body_do_not_protect_the_caption_row(self):
        # 护栏按行判定：表体里的合并格不牵连开头那一行。
        cells = [
            row("标题", "", "", ""),
            row("编号", "名称", "数量", "金额"),
            row("Material", "BB-3753", "90567", "9669.77"),
            row("Description", "TTT-2774", "35742.42", "67"),
        ]
        result = self._result(cells)
        result["spans"] = [{"row": 1, "column": 0, "row_span": 1, "column_span": 2}]
        result["structure_certificate"] = {
            "verified": True, "source": "screen_ruled_grid", "rows": 3,
            "row_offset": 0, "columns": 4, "column_offset": 0,
            "column_boundaries": [0, 1, 2, 3, 4],
            "spans": [{"row": 1, "column": 0, "row_span": 1, "column_span": 2}],
            "row_column_boundaries": [[0], [1], [2]],
        }
        result["structure_verified"] = True
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(result["rows"], 3)


if __name__ == "__main__":
    unittest.main()