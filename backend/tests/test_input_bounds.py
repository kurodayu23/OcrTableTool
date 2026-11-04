# -*- coding: utf-8 -*-
"""输入上限契约（2026-10-30 lead 复查）。

两类"输入能把进程撑爆"的路径此前都没有上限：

1. `table_pipeline.parse_html_table`：`rowspan`/`colspan` 既不校验类型也不限幅，
   畸形值（`100000`、非数字、超大整数）会把网格撑到十万行/列，且该函数在识别
   管线内、没有预算检查点。
2. `ocr_backend._read_image`：整文件 `np.fromfile` 读入再 `imdecode`，没有文件
   字节上限也没有像素上限 —— 200 MP 的图光解码就吃 1~2 GB。

这里锁住：畸形/超限输入必须被**安全拒绝或截断**，正常输入不受影响。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402


class HtmlSpanBoundTests(unittest.TestCase):
    def _parse_one_row(self, span_attr: str) -> tuple[list[list[str]], list[dict]]:
        html = '<table><tr><td rowspan="%s">x</td><td>y</td></tr></table>' % span_attr
        return table_pipeline.parse_html_table(html)

    def test_malformed_span_values_fall_back_to_one(self):
        for attr in ("abc", "-5", "", "1e9", "None", "  "):
            with self.subTest(attr=attr):
                grid, spans = self._parse_one_row(attr)
                self.assertTrue(grid, "畸形 rowspan 不应让整表解析失败")
                self.assertEqual(spans, [], "畸形 rowspan 被当成了合并区")

    def test_valid_span_is_preserved(self):
        grid, spans = self._parse_one_row("3")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["row_span"], 3)

    def test_absurd_span_is_clamped_not_allocated(self):
        """rowspan=100000 不能真的开出十万行。"""
        grid, spans = self._parse_one_row("100000")
        self.assertLessEqual(
            len(grid),
            table_pipeline._TableParser._MAX_TABLE_SPAN,
            "网格行数没有被限幅",
        )
        self.assertLessEqual(len(grid[0]), table_pipeline._TableParser._MAX_TABLE_SPAN)

    def test_many_huge_spans_stay_bounded(self):
        """大量大跨度单元格叠加也不能把网格撑爆（总网格上限）。"""
        cells = "".join('<td rowspan="1024" colspan="1024">x</td>' for _ in range(300))
        html = "<table><tr>%s</tr></table>" % cells
        grid, _spans = table_pipeline.parse_html_table(html)
        self.assertLessEqual(len(grid), table_pipeline._TableParser._MAX_TABLE_SPAN)
        self.assertLessEqual(len(grid[0]), table_pipeline._TableParser._MAX_TABLE_SPAN)

    def test_normal_nested_table_unaffected(self):
        html = (
            '<table><tr><td rowspan="2">A</td><td>B</td></tr>'
            '<tr><td>C</td></tr></table>'
        )
        grid, spans = table_pipeline.parse_html_table(html)
        self.assertEqual(grid, [["A", "B"], ["", "C"]])
        self.assertEqual(spans, [{"row": 0, "column": 0, "row_span": 2, "column_span": 1}])


class ImageSizeBoundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault(
            "OCR_TABLE_MODEL_DIR", str(_BACKEND_DIR.parent / "runtime" / "models")
        )
        ocr_backend._load_runtime()

    def test_oversized_file_is_rejected_without_path_leak(self):
        oversized = Path(tempfile.mkdtemp()) / "huge.png"
        with open(oversized, "wb") as stream:
            stream.write(b"\x89PNG\r\n\x1a\n")
            stream.write(b"\0" * (ocr_backend._MAX_IMAGE_FILE_BYTES + 1024))
        with self.assertRaises(ValueError) as caught:
            ocr_backend._read_image(oversized)
        response = ocr_backend._error_response(caught.exception, {"action": "recognize"})
        self.assertEqual(response["error_code"], "IMAGE_READ_FAILED")
        self.assertIn("过大", response["message"])
        self.assertNotIn(":\\", response["message"], "客户可见文案泄漏了绝对路径")


if __name__ == "__main__":
    unittest.main()