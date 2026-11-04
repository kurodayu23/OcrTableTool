"""XLSX 导出文本安全：XML 1.0 禁止的码位必须在写入前清掉。

一个单元格里的一个控制字符就会让 openpyxl 抛 IllegalCharacterError，
整张表导不出来 —— 已实测 openpyxl 直接写 \x0b 就是这个异常。
"""

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

import table_pipeline


class XmlSafeTextTests(unittest.TestCase):
    def test_control_characters_are_removed(self):
        self.assertEqual(table_pipeline.xml_safe_text("编号\x00\x08"), "编号")
        self.assertEqual(table_pipeline.xml_safe_text("竖制表\x0b换页\x0c"), "竖制表换页")
        self.assertEqual(table_pipeline.xml_safe_text("单元\x1f格\x0e"), "单元格")

    def test_non_characters_are_removed(self):
        self.assertEqual(table_pipeline.xml_safe_text("正常￾文字￿"), "正常文字")

    def test_tab_newline_and_return_are_preserved(self):
        # 单元格内换行是本工具明确支持的能力，不能当噪声删掉
        self.assertEqual(table_pipeline.xml_safe_text("行一\n行二"), "行一\n行二")
        self.assertEqual(table_pipeline.xml_safe_text("制表\t符"), "制表\t符")
        self.assertEqual(table_pipeline.xml_safe_text("回车\r"), "回车\r")

    def test_lone_surrogates_are_removed(self):
        self.assertEqual(table_pipeline.xml_safe_text("代理\ud800项"), "代理项")

    def test_plain_text_and_empty_are_unchanged(self):
        self.assertEqual(table_pipeline.xml_safe_text(""), "")
        self.assertEqual(table_pipeline.xml_safe_text("正常文字 123 %"), "正常文字 123 %")

    def test_dict_cells_are_read_from_text_key(self):
        self.assertEqual(table_pipeline.xml_safe_text({"text": "值\x0b"}), "值")


class WriteXlsxTextSafetyTests(unittest.TestCase):
    def test_export_succeeds_with_illegal_characters(self):
        rows = [
            [{"text": "编号"}, {"text": "规格/型号"}],
            [{"text": "编号\x0b错"}, {"text": "12\x00345\x0c"}],
            [{"text": "换行\n保留"}, {"text": "正常"}],
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clean.xlsx"
            table_pipeline.write_xlsx(path, rows)
            sheet = load_workbook(path).active
            values = [tuple(row) for row in sheet.iter_rows(values_only=True)]
        self.assertEqual(values, [
            ("编号", "规格/型号"),
            ("编号错", "12345"),
            ("换行\n保留", "正常"),
        ])


if __name__ == "__main__":
    unittest.main()