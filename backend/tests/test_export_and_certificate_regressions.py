# -*- coding: utf-8 -*-
"""2026-10-30 lead 复查修复的两个 P0 的回归契约。

1. **导出被整表拒绝**（`table_pipeline.write_xlsx`）：合并区覆盖到非空格时，旧实现
   直接 `raise ValueError("invalid XLSX span: merge would hide cell content")` ⇒ 客户
   点导出就失败、没有退路（两级表头 + 覆盖格有 OCR 文字即命中）。现在改为丢弃那一条
   合并、保留每格文字，并把锚点与冲突格标 needs_review，导出照常成功。

2. **结构认证自毁**（`_drop_leading_page_caption_rows` 与
   `_align_structure_certificate_to_grid`）：抬头行删除时两处各按同一 offset 裁剪
   `row_column_boundaries` 一次，列表被裁两次、`row_column_hash` 又没跟着重算 ⇒
   `_structure_certificate_matches` 恒 False ⇒ `structure_verified` 被撤销、
   `publication_blocked=True`。现在 offset 归一化只在 align 一处做，并同步重算 hash。
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402


def _row_column_hash(row_columns) -> str:
    return hashlib.blake2b(
        json.dumps(row_columns, separators=(",", ":")).encode("utf-8"), digest_size=16
    ).hexdigest()


class XlsxMergeCoveringTextTests(unittest.TestCase):
    """合并区覆盖到有文字的格子：导出必须成功，且文字一格都不能丢。"""

    def test_span_covering_text_is_dropped_not_fatal(self):
        cells = [
            [
                {"text": "分组标题", "confidence": 0.9, "needs_review": False},
                {"text": "", "confidence": 0.0, "needs_review": False},
                {"text": "意外文字", "confidence": 0.9, "needs_review": False},
                {"text": "", "confidence": 0.0, "needs_review": False},
            ],
            [{"text": "a", "confidence": 0.9, "needs_review": False}] * 4,
            [{"text": "b", "confidence": 0.9, "needs_review": False}] * 4,
        ]
        spans = [{"row": 0, "column": 0, "row_span": 1, "column_span": 4, "role": "title"}]
        out = Path(tempfile.mkdtemp()) / "merge-cover.xlsx"
        # 旧实现这里会抛 ValueError，整个导出失败
        table_pipeline.write_xlsx(str(out), cells, spans)
        self.assertTrue(out.is_file() and zipfile.is_zipfile(out))

        import openpyxl

        sheet = openpyxl.load_workbook(str(out)).active
        first_row = [cell.value for cell in sheet[1]]
        self.assertEqual(first_row[0], "分组标题")
        self.assertEqual(first_row[2], "意外文字", "覆盖格里的文字被合并吞掉了")
        # 该条合并应被丢弃（否则会把"意外文字"藏起来）
        self.assertEqual([str(r) for r in sheet.merged_cells.ranges], [])

    def test_pure_empty_cover_still_merges(self):
        """覆盖格全空时合并照旧生效（别把正常合并也一起废掉）。"""
        cells = [
            [{"text": "标题", "confidence": 0.9, "needs_review": False},
             {"text": "", "confidence": 0.0, "needs_review": False},
             {"text": "", "confidence": 0.0, "needs_review": False}],
            [{"text": "a", "confidence": 0.9, "needs_review": False}] * 3,
        ]
        spans = [{"row": 0, "column": 0, "row_span": 1, "column_span": 3}]
        out = Path(tempfile.mkdtemp()) / "merge-ok.xlsx"
        table_pipeline.write_xlsx(str(out), cells, spans)
        import openpyxl

        sheet = openpyxl.load_workbook(str(out)).active
        self.assertEqual(len(list(sheet.merged_cells.ranges)), 1)


class CertificateOffsetTests(unittest.TestCase):
    """带逐行边界的结构证书在 row_offset/column_offset 归一化后必须仍能通过校验。"""

    def _build(self, physical_rows: int, columns: int) -> dict:
        row_bounds = [index * 10 for index in range(physical_rows + 1)]
        col_bounds = [index * 5 for index in range(columns + 1)]
        row_columns = [list(col_bounds) for _ in range(physical_rows)]
        certificate = {
            "source": "unit-test",
            "source_shape": [row_bounds[-1], col_bounds[-1]],
            "rows": physical_rows,
            "columns": columns,
            "verified": True,
            "spans": [],
            "row_boundaries": row_bounds,
            "column_boundaries": col_bounds,
            "row_column_boundaries": [list(item) for item in row_columns],
            "row_column_hash": _row_column_hash(row_columns),
            "row_offset": 0,
            "column_offset": 0,
        }
        payload = json.dumps(
            {
                "source": "unit-test",
                "source_shape": [row_bounds[-1], col_bounds[-1]],
                "columns": col_bounds,
                "rows": row_bounds,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        certificate["geometry_hash"] = hashlib.blake2b(
            payload.encode("utf-8"), digest_size=16
        ).hexdigest()
        return certificate

    def test_single_truncation_when_row_offset_present(self):
        """模拟「删掉 2 行抬头」后 align：只能裁一次，且 hash 必须跟着重算。"""
        certificate = self._build(physical_rows=5, columns=3)
        certificate["row_offset"] = 2
        certificate["rows"] = 3
        grid = [["a"] * 3 for _ in range(3)]
        self.assertTrue(
            ocr_backend._align_structure_certificate_to_grid(certificate, grid),
            "align 应当成功（边界数量足够）",
        )
        row_columns = certificate["row_column_boundaries"]
        self.assertEqual(len(row_columns), 3, "row_columns 被裁了不止一次")
        self.assertEqual([len(item) for item in row_columns], [4, 4, 4])
        self.assertEqual(
            certificate["row_column_hash"], _row_column_hash(row_columns),
            "裁剪后必须重算 row_column_hash，否则认证恒失败",
        )
        self.assertTrue(
            ocr_backend._structure_certificate_matches(certificate, grid, []),
            "归一化后的证书必须能通过结构校验",
        )

    def test_column_offset_trims_each_row_boundaries(self):
        """column_offset 也要应用到逐行竖线边界，否则行长恒多 offset 项。"""
        certificate = self._build(physical_rows=3, columns=4)
        certificate["column_offset"] = 1
        certificate["rows"] = 3
        grid = [["a"] * 3 for _ in range(3)]
        self.assertTrue(ocr_backend._align_structure_certificate_to_grid(certificate, grid))
        row_columns = certificate["row_column_boundaries"]
        self.assertEqual([len(item) for item in row_columns], [4, 4, 4],
                         "每行边界应被裁到 columns+1 = 4 项")
        self.assertEqual(certificate["row_column_hash"], _row_column_hash(row_columns))
        self.assertTrue(ocr_backend._structure_certificate_matches(certificate, grid, []))


class ExportInputShapeTests(unittest.TestCase):
    """导出输入形状的两个数据污染缺陷（2026-10-30 lead 复查）。"""

    def test_none_cell_exports_as_empty_not_literal_none(self):
        """null 格要导出成空白格，不能导出成字面字符串 "None"。

        2026-11-04：契约 docs/BACKEND_API_V1.md §7 规定"整个网格全是空白"
        （含所有 text 均为 null）⇒ EXPORT_GRID_INVALID，所以这里用**含内容的
        混合网格**验证 null 格；全 null 网格的拒绝行为由
        test_empty_table_export.py 覆盖。
        """
        for cells in (
            [[{"text": "A"}, None]],
            [[{"text": "A"}, {"text": None}]],
        ):
            with self.subTest(cells=cells):
                out = Path(tempfile.mkdtemp()) / "none.xlsx"
                table_pipeline.write_xlsx(str(out), cells, [])
                import openpyxl

                sheet = openpyxl.load_workbook(str(out)).active
                self.assertEqual(sheet["A1"].value, "A")
                self.assertIn(
                    sheet["B1"].value,
                    (None, ""),
                    "空值被写成了 %r" % (sheet["B1"].value,),
                )

    def test_non_numeric_confidence_is_export_grid_invalid(self):
        """confidence 非数值应归 EXPORT_GRID_INVALID，而不是 INTERNAL_ERROR。"""
        with self.assertRaises(ValueError) as caught:
            table_pipeline.write_xlsx(
                str(Path(tempfile.mkdtemp()) / "conf.xlsx"),
                [[{"text": "a", "confidence": "high"}]],
                [],
            )
        self.assertIn("cells must", str(caught.exception))
        error_code, field, retryable = ocr_backend._classify_error(
            caught.exception, {"action": "export_xlsx"}
        )
        self.assertEqual(error_code, "EXPORT_GRID_INVALID")
        self.assertEqual(field, "cells")
        self.assertFalse(retryable)

    def test_nan_confidence_does_not_fail_export(self):
        """nan/inf 置信度只影响复核批注，不该让导出失败。"""
        out = Path(tempfile.mkdtemp()) / "nan.xlsx"
        table_pipeline.write_xlsx(
            str(out),
            [[{"text": "a", "confidence": "nan"}, {"text": "b", "confidence": float("inf")}]],
            [],
        )
        self.assertTrue(out.is_file())


if __name__ == "__main__":
    unittest.main()