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

    def test_empty_row_before_data_rows_is_kept_to_preserve_alignment(self):
        """空行后紧跟**数据行**时不能删：那是"有墨迹没读出文字"的表头行。

        2026-11-05（lead 复查）：实测 FC-00003 的 borderless 网格是
        26x6 = [页眉, 空表头, 24 行数据]。旧逻辑把页眉和空表头都当抬头删掉
        ⇒ 24x6，整表相对真值上移一行，逐格准确率 0.000。
        与 test_empty_leading_row_is_dropped 的区别在于**后续行形态**：
        那边紧跟的是真表头（"序号/物料名称/批次号…"，几乎不含数字），
        这边紧跟的是数据行（含数字 ≥2 格）。
        """
        result = {
            "cells": [
                row("出入库流水账·2026年09月", "", "", "", ""),
                row("", "", "", "", ""),
                row("F7-4437", "接头", "出库", "114.685", "二号楼"),
                row("A1-6474", "接近开关", "出库", "367.259", "一号楼"),
            ],
            "rows": 4, "columns": 5, "spans": [],
        }
        self.assertTrue(ocr_backend._drop_leading_page_caption_rows(result))
        # 只删掉页眉那一行；空表头行保留，保证后面数据行的行号与原件一致
        self.assertEqual(3, result["rows"])
        self.assertEqual("", result["cells"][0][0]["text"])
        self.assertEqual("F7-4437", result["cells"][1][0]["text"])
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


class FullRowIsNeverCaption(unittest.TestCase):
    """锁定 2026-11-09 的最终结论：**满格行一律不当抬头**。

    背景（实测踩坑三次）：为抓"满格页眉行"，先后试过"含 >=2 数字格"、">=2 混合格"、
    ">=2 冒号格"，三次都误删真数据或真表头：
      · 含 >=2 数字格 => FC-00565 少 6 行、FC-00616 少 3 行、FC-00621 少 4 行；
      · 含 >=2 混合格 => FC-00680 少 3 行。
    取证：FC-00723 的满格页眉 = 终盘点单 | 4 | x393 | 20918723，混合格只有 1 个，
    达不到任何 >=2 阈值 => 满格页眉与满格真表头在行内不可区分。
    取舍：宁可漏删（多留一行页眉）也不误删（整表少行、hard 归零）。
    """

    @staticmethod
    def _row(*texts):
        return [{"text": t, "confidence": 0.9, "needs_review": False} for t in texts]

    def test_full_row_with_digits_is_not_caption(self):
        row = self._row("1576", "接近开关", "零星补货", "东区")
        self.assertFalse(ocr_backend._leading_row_is_page_caption_band(row))

    def test_full_row_with_colons_is_not_caption(self):
        row = self._row("A1:500", "B2:700", "C3:900", "D4:100")
        self.assertFalse(ocr_backend._leading_row_is_page_caption_band(row))

    def test_full_row_numeric_cells_are_not_caption(self):
        row = self._row("354.892", "2026-04-28", "85.4%", "12")
        self.assertFalse(ocr_backend._leading_row_is_page_caption_band(row))

    def test_sparse_row_with_two_digits_is_caption(self):
        row = self._row("统计期间 2026-01", "第 6 页", "", "", "", "")
        self.assertTrue(ocr_backend._leading_row_is_page_caption_band(row))


class FullRowStopsCaptionBand(unittest.TestCase):
    """锁定领域不变式：**页眉行不可能满格**，所以抬头带遇到满格行必须停。

    2026-11-09 实测：这条不变式原本由 `_leading_row_is_page_caption_band` 内部的
    `populated > max(3, columns // 3) -> return False` 隐式保证；`_leading_caption_band_length`
    里又显式写了一次 `row_filled >= columns: break`，属**冗余但正确**的护栏。
    这里锁定行为（而不是实现），防止将来有人去掉其中一处时悄悄改变结果。
    """

    @staticmethod
    def _row(*texts):
        return [{"text": t, "confidence": 0.9, "needs_review": False} for t in texts]

    def test_caption_band_stops_at_first_full_row(self):
        cells = []
        # 两行抬头（第 1 行满 1 格、第 2 行满 2 格）
        cells.append(self._row("库存台账", "", "", ""))
        cells.append(self._row("仓库：A区", "制表：刘洋", "", ""))
        # 满格真表头（4 列全填）+ 3 行数据
        cells.append(self._row("序号", "物料名称", "库位", "数量"))
        for index in range(3):
            cells.append(self._row(str(index + 1), "显示屏", "东区", "100"))
        lead = ocr_backend._leading_caption_band_length(cells, [])
        # 抬头带必须停在满格表头处（不把表头吃掉），最多删前 2 行
        self.assertEqual(2, lead)

    def test_full_row_as_the_very_first_row_is_never_caption(self):
        cells = [self._row("序号", "物料名称", "库位", "数量")]
        for index in range(4):
            cells.append(self._row(str(index + 1), "显示屏", "东区", "100"))
        self.assertEqual(0, ocr_backend._leading_caption_band_length(cells, []))


class ColumnLetterRowTests(unittest.TestCase):
    """`_drop_column_letter_rows`：删电子表格列标行（整行纯字母），不误删真数据。

    背景（2026-11-10 实测 FC-00982）：输出 R1 = ['A','B','C','D','E','2','G','H','BLES']
    是 Excel 顶部列标栏，真值里没有这行 ⇒ 整表下移、hard 0.006（best 0.941）。
    """

    @staticmethod
    def _cell(text):
        return {"text": text, "confidence": 0.9, "needs_review": False}

    def _table(self, rows):
        cells = [[self._cell(value) for value in row] for row in rows]
        return {"status": "ok", "spans": [], "cells": cells,
                "rows": len(cells), "columns": len(cells[0]) if cells else 0}

    def test_column_letter_row_is_dropped(self):
        result = self._table([
            ["A", "B", "C", "D", "E"],
            ["类别", "客户名称", "产品", "规格", "数量"],
            ["U7-4395", "包装箱", "密封圈", "9701", "786.214"],
            ["V1-2233", "控制板", "齿轮泵", "8449", "153.485"],
        ])
        out = ocr_backend._drop_column_letter_rows(result)
        self.assertEqual(3, out["rows"])
        self.assertEqual("类别", out["cells"][0][0]["text"])

    def test_chinese_header_is_not_dropped(self):
        result = self._table([
            ["类别", "客户名称", "产品", "规格", "数量"],
            ["U7-4395", "包装箱", "密封圈", "9701", "786.214"],
            ["V1-2233", "控制板", "齿轮泵", "8449", "153.485"],
            ["W2-3344", "显示屏", "联轴器", "1024", "963.752"],
        ])
        out = ocr_backend._drop_column_letter_rows(result)
        self.assertEqual(4, out["rows"])

    def test_mixed_letters_and_digits_is_not_dropped(self):
        # 真数据行：字母+数字混排（型号、编号）
        result = self._table([
            ["A1", "B2", "C3", "D4"],
            ["U7-4395", "包装箱", "密封圈", "9701"],
            ["V1-2233", "控制板", "齿轮泵", "8449"],
            ["W2-3344", "显示屏", "联轴器", "1024"],
        ])
        out = ocr_backend._drop_column_letter_rows(result)
        self.assertEqual(4, out["rows"])

    def test_sparse_row_is_not_dropped(self):
        result = self._table([
            ["A", "B"],
            ["类别", "客户名称", "产品"],
            ["U7-4395", "包装箱", "密封圈"],
            ["V1-2233", "控制板", "齿轮泵"],
        ])
        out = ocr_backend._drop_column_letter_rows(result)
        self.assertEqual(4, out["rows"])

    def test_spans_block_the_drop(self):
        result = self._table([
            ["A", "B", "C", "D"],
            ["类别", "客户名称", "产品", "规格"],
            ["U7-4395", "包装箱", "密封圈", "9701"],
            ["V1-2233", "控制板", "齿轮泵", "8449"],
        ])
        result["spans"] = [{"row": 0, "column": 0, "row_span": 1, "column_span": 2, "role": "group_header"}]
        out = ocr_backend._drop_column_letter_rows(result)
        self.assertEqual(4, out["rows"])


class BlankRowNearHeaderTests(unittest.TestCase):
    """`_drop_blank_rows_near_header`：删表头区整行空行，并同步平移 spans 行号。

    背景（2026-11-10 实测 FC-00276）：输出 R0=表头、R1=整行全空、R2=数据，
    真值无此空行 ⇒ 30×9 vs 29×9、hard 0.088（best 0.966）。
    该例 `spans=1`，所以上一版"有 spans 就不动"的护栏把它整个跳过了。
    """

    @staticmethod
    def _cell(text):
        return {"text": text, "confidence": 0.9, "needs_review": False}

    def _table(self, rows, spans=None):
        cells = [[self._cell(value) for value in row] for row in rows]
        return {"status": "ok", "spans": list(spans or []), "cells": cells,
                "rows": len(cells), "columns": len(cells[0]) if cells else 0}

    def test_blank_row_between_header_and_data_is_dropped(self):
        result = self._table([
            ["产品编号", "产品名称", "材料费"],
            ["", "", ""],
            ["M2-3344", "铁芯", "964.046"],
            ["U9-9336", "盖板", "319.131"],
        ])
        out = ocr_backend._drop_blank_rows_near_header(result)
        self.assertEqual(3, out["rows"])
        self.assertEqual("产品编号", out["cells"][0][0]["text"])
        self.assertEqual("M2-3344", out["cells"][1][0]["text"])

    def test_spans_row_index_is_shifted(self):
        # span 锚在数据行（原第 2 行），删掉第 1 行空行后应移到第 1 行
        spans = [{"row": 2, "column": 0, "row_span": 1, "column_span": 2, "role": "group_header"}]
        result = self._table([
            ["产品编号", "产品名称", "材料费"],
            ["", "", ""],
            ["M2-3344", "铁芯", "964.046"],
            ["U9-9336", "盖板", "319.131"],
        ], spans=spans)
        out = ocr_backend._drop_blank_rows_near_header(result)
        self.assertEqual(3, out["rows"])
        self.assertEqual(1, out["spans"][0]["row"])

    def test_span_anchored_on_dropped_row_is_removed(self):
        spans = [{"row": 1, "column": 0, "row_span": 1, "column_span": 2, "role": "group_header"}]
        result = self._table([
            ["产品编号", "产品名称", "材料费"],
            ["", "", ""],
            ["M2-3344", "铁芯", "964.046"],
            ["U9-9336", "盖板", "319.131"],
        ], spans=spans)
        out = ocr_backend._drop_blank_rows_near_header(result)
        self.assertEqual([], out["spans"])

    def test_blank_row_at_table_edge_is_kept(self):
        # 首行/末行是空行 ⇒ 不删（只删"前后都非空"的内部空行）
        result = self._table([
            ["", "", ""],
            ["产品编号", "产品名称", "材料费"],
            ["M2-3344", "铁芯", "964.046"],
            ["", "", ""],
        ])
        out = ocr_backend._drop_blank_rows_near_header(result)
        self.assertEqual(4, out["rows"])

    def test_no_blank_row_means_no_change(self):
        result = self._table([
            ["产品编号", "产品名称", "材料费"],
            ["M2-3344", "铁芯", "964.046"],
            ["U9-9336", "盖板", "319.131"],
        ])
        out = ocr_backend._drop_blank_rows_near_header(result)
        self.assertEqual(3, out["rows"])


if __name__ == "__main__":
    unittest.main()