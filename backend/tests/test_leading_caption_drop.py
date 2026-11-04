"""页边抬头行必须剔除，表内两级表头必须保留。

标准清晰集 48 例里 7 例（占 14.6%）整表凭空多出开头一行——线框检测把纸张上边缘
当成表格顶边，于是"库存台账・2025年12月""车间：B区 计划员：李建国"这种页边抬头
被算成表格第 0 行，表头被挤到第 1 行，后面每一行整体错位，逐格准确率直接归零。

原来的剔除函数要求"必须有物理线框证书"，7 例里只有 FC-00122 拿到了
photographic_complete_ruled_line_grid，另外 6 例正确的删除全被闸门挡掉了；而
FC-00122 那张表恰恰带着真值 41 行 / 识别 42 行的证据，说明线框数出来的行里
**本来就含着**抬头那一带，拿它当上界反而删不掉。

现在的护栏是 span 角色：真值 1200 例里第 0 行的 span 角色只有 row_header（250）
和 group_header（500），从来没有 title；响应里第 0 行的 7 个 title 角色 span
逐条核对下来全是页边抬头。group_header 才是表内两级表头，一律不动。
"""
import unittest

import ocr_backend


def cell(text):
    return {"text": text, "confidence": 0.9, "needs_review": False}


def row(*texts):
    return [cell(t) for t in texts]


HEADER = row("序号", "物料名称", "批次号", "数量", "库位")
DATA = row("8746", "数据线", "C7-3894", "97.114", "二号楼")


def table(header_prefix=None, spans=None, rows=4):
    cells = []
    if header_prefix is not None:
        cells.append(header_prefix + [cell("")] * (5 - len(header_prefix)))
    cells.append(HEADER)
    for _ in range(rows):
        cells.append([dict(c) for c in DATA])
    return {"cells": cells, "rows": len(cells), "columns": 5, "spans": spans or []}


class LeadingCaptionRows(unittest.TestCase):
    def test_page_caption_row_is_dropped_without_certificate(self):
        result = table(header_prefix=row("库存台账・2025年12月"))
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(["序号", "物料名称", "批次号", "数量", "库位"],
                         [c["text"] for c in result["cells"][0]])
        self.assertEqual(5, result["rows"])

    def test_two_level_group_header_is_kept(self):
        result = table(
            header_prefix=row("物料组", "", "属性信息", "", ""),
            spans=[{"row": 0, "column": 0, "row_span": 1, "column_span": 2,
                    "role": "group_header"}],
        )
        self.assertFalse(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual("物料组", result["cells"][0][0]["text"])

    def test_empty_leading_row_is_dropped(self):
        result = table(header_prefix=row("", "", "", "", ""))
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual("序号", result["cells"][0][0]["text"])

    def test_table_starting_with_real_header_is_untouched(self):
        result = table()
        self.assertFalse(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(5, result["rows"])

    def test_sparse_header_followed_by_sparse_row_is_not_dropped(self):
        # 第 1 行没有填满 → 说明抬头带后面不是真表头，宁可留着让人复核
        result = {
            "cells": [row("标题"), row("甲", "乙", "", "", ""),
                      row("1", "2", "3", "4", "5"), row("1", "2", "3", "4", "5")],
            "rows": 4, "columns": 5, "spans": [],
        }
        self.assertFalse(ocr_backend._drop_leading_page_caption_rows(result))

    def test_certificate_never_allows_dropping_below_its_own_row_count(self):
        # 线框认定 4 行而内容 5 行 → 最多只能删 1 行
        result = table(header_prefix=row("抬头"), rows=3)
        result["structure_certificate"] = {
            "verified": True, "source": "screen_ruled_grid", "rows": 4,
            "columns": 5, "row_offset": 0, "column_offset": 0,
            "spans": [], "row_column_boundaries": [], "row_column_hash": "x",
        }
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        self.assertEqual(4, result["rows"])


if __name__ == "__main__":
    unittest.main()