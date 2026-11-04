# -*- coding: utf-8 -*-
"""空表导出必须显式失败，不能产出"成功但空白"的文件（2026-10-28 修）。

缺陷：``cells`` 是一张"只有空白格"的矩形网格时，``table_pipeline.write_xlsx``
照样把工作簿写盘（实测 4809 B），``export_xlsx`` 返回 ``status=ok``，
客户拿到的是一张空白表；CSV 侧（Qt ``CsvExporter``）更彻底，空表只写 3 字节
UTF-8 BOM。

为什么选择「显式报错」而不是「写一行提示」：``docs/BACKEND_API_V1.md`` §7 明确
"导出成功只表示文件写入成功，不代表 OCR 通过发布门禁"，而空白表一旦加上提示
文字就成了**伪造内容**——客户会把它当成识别结果。契约里已有 ``EXPORT_GRID_INVALID``
（field=cells、retryable=false），空表按同一语义拒绝，客户端不需要新增分支。

判据取「整张网格没有任何非空白文字」，而不是「零行」：``validate_request``
已经拦掉 ``cells=[]``（消息同样以 "cells must" 开头 ⇒ 同一个错误码），
真正漏到 ``write_xlsx`` 的是"有行列、全是空字符串"的网格。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402

EMPTY_GRIDS = {
    "zero_rows": [],
    "one_empty_row": [[]],
    "single_blank_cell": [[""]],
    "whitespace_only": [["   ", "\t"]],
    "newline_only": [["\n"]],
    "blank_matrix": [["", ""], ["", ""]],
    "blank_dict_cells": [
        [{"text": "", "confidence": 0.99}, {"text": "  ", "confidence": 0.0}]
    ],
}


class RejectsEmptyGridTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.output = Path(self._tempdir.name) / "table.xlsx"

    def tearDown(self):
        self._tempdir.cleanup()

    def test_empty_grids_raise_instead_of_writing_a_blank_workbook(self):
        for name, rows in EMPTY_GRIDS.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    table_pipeline.write_xlsx(self.output, rows, [])
                self.assertFalse(
                    self.output.exists(),
                    f"{name}: 拒绝导出时不得留下任何文件",
                )

    def test_error_message_keeps_the_existing_error_code_mapping(self):
        with self.assertRaises(ValueError) as raised:
            table_pipeline.write_xlsx(self.output, [["", ""]], [])
        message = str(raised.exception)
        # 前缀 "cells must" 是 ocr_backend._classify_error 的既有判据，
        # 改动措辞会让空表导出掉进 INVALID_REQUEST（客户端会提示"请重新识别"）。
        self.assertTrue(message.startswith("cells must"), message)
        self.assertEqual(
            ocr_backend._classify_error(ValueError(message)),
            ("EXPORT_GRID_INVALID", "cells", False),
        )

    def test_error_response_uses_export_grid_invalid(self):
        response = ocr_backend._error_response(
            ValueError("cells must contain at least one non-empty row: 空表"),
            {"action": "export_xlsx"},
        )
        self.assertEqual(response["status"], "error")
        self.assertEqual(response["error_code"], "EXPORT_GRID_INVALID")
        self.assertEqual(response["field"], "cells")
        self.assertFalse(response["retryable"])

    def test_protocol_layer_already_rejected_zero_cells(self):
        """`cells=[]` 在协议层就失败（同一个错误码），本修复补的是"全空网格"。"""
        with self.assertRaises(ValueError) as raised:
            table_pipeline.validate_request(
                {
                    "protocol": 1,
                    "action": "export_xlsx",
                    "request_id": 7,
                    "output_path": str(self.output),
                    "cells": [],
                }
            )
        self.assertIn("cells must", str(raised.exception))
        self.assertEqual(
            ocr_backend._classify_error(raised.exception),
            ("EXPORT_GRID_INVALID", "cells", False),
        )


class NonEmptyGridStillExportsTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.output = Path(self._tempdir.name) / "table.xlsx"

    def tearDown(self):
        self._tempdir.cleanup()

    def test_single_populated_cell_is_enough(self):
        table_pipeline.write_xlsx(self.output, [["", ""], ["", "合计"]], [])
        self.assertTrue(self.output.exists())
        self.assertGreater(self.output.stat().st_size, 3000)

        from openpyxl import load_workbook

        sheet = load_workbook(self.output).active
        self.assertEqual(sheet.max_row, 2)
        self.assertEqual(sheet.max_column, 2)
        self.assertEqual(sheet.cell(row=2, column=2).value, "合计")

    def test_normal_grid_and_spans_still_export(self):
        rows = [["标题", "", ""], ["名称", "数值", "备注"], ["闸门", "-12.5", ""]]
        spans = [{"row": 0, "column": 0, "row_span": 1, "column_span": 3, "role": "title"}]
        table_pipeline.write_xlsx(self.output, rows, spans)
        self.assertTrue(self.output.exists())

        from openpyxl import load_workbook

        sheet = load_workbook(self.output).active
        self.assertEqual(sheet.cell(row=3, column=2).value, "-12.5")
        merged = [str(rng) for rng in sheet.merged_cells.ranges]
        self.assertEqual(merged, ["A1:C1"])


if __name__ == "__main__":
    unittest.main()
