# -*- coding: utf-8 -*-
"""复核提示的坐标与格数必须与**交付出去的那张表**一致。

背景（2026-11-05，真实照片 PF-00002）
-------------------------------------
识别阶段按 18×4 的中间网格算出：

* "存在尚未人工确认的黄色单元格（…等 40 格）"
* "有 72 格未取得两个模型的一致确认"
* "有 34 格检测到墨迹但未能读出内容"
* "存在跨模型冲突或内容证据不足的单元格（R6C1、R9C4、R10C1、R12C4、R14C4、R17C1）"

表格随后被「并回多出的一列」「删表外抬头行」「折回竖排文字行」「裁尾部空白」
一路改成 14×3，于是：

* 提示说 40 格黄色，界面上实际只有 **1** 个黄点（review_cell_count=1）；
* 提示里出现 R9C4、R17C1 这种**根本不存在**的坐标；
* "34 格墨迹读不出"说的正是被当成表外抬头删掉的那几行 —— 格子已经不在表里，
  提示还在让用户去核对。

本模块守住三条：

1. 黄色格提示的格数与坐标 == 交付表里真正标黄的格子；
2. 所有提示里的格坐标都落在交付表的范围内；
3. 网格变形（删行、并列、裁剪）后坐标跟着平移，越界的一律作废。
"""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ocr_backend  # noqa: E402

_COORDINATE = re.compile(r"R(\d+)C(\d+)")


def _cell(text: str = "", confidence: float = 0.0, needs_review: bool = False) -> dict:
    return {"text": text, "confidence": confidence, "needs_review": needs_review}


def _coordinates(items) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for item in items:
        found.extend(
            (int(row), int(column)) for row, column in _COORDINATE.findall(str(item))
        )
    return found


class ReviewMessageMatchesFinalGrid(unittest.TestCase):
    """提示的格数与坐标必须和最终表格对得上。"""

    def _result(self, rows: int = 3, columns: int = 3) -> dict:
        cells = [
            [_cell("值%d%d" % (row, column), 0.86) for column in range(columns)]
            for row in range(rows)
        ]
        return {
            "cells": cells,
            "rows": rows,
            "columns": columns,
            "publication_block_reasons": [],
            "processing_notices": [],
            "structure_warnings": [],
            "publication_blocked": False,
            "recognition_state": "verified",
        }

    def test_yellow_cells_match_the_delivered_cells(self):
        result = self._result()
        result["cells"][1][2] = _cell("待核", 0.0, needs_review=True)
        result["publication_block_reasons"].append(
            "存在尚未人工确认的黄色单元格（R1C1、R2C3 等 40 格），禁止作为已验证结果直接导出。"
        )
        result["processing_notices"].append(
            "有 72 格未取得两个模型的一致确认（R1C1、R1C2、R2C1 等 72 格），请对照原图确认。"
        )

        ocr_backend._render_review_messages(result)

        reasons = result["publication_block_reasons"]
        yellow_reasons = [
            reason for reason in reasons if "尚未人工确认的黄色单元格" in reason
        ]
        self.assertEqual(len(yellow_reasons), 1, msg="旧的那条必须被替换掉，不能留两份")
        self.assertEqual(_coordinates(yellow_reasons), [(2, 3)])
        self.assertNotIn("40 格", yellow_reasons[0])
        self.assertNotIn("R1C1", yellow_reasons[0])

        notices = result["processing_notices"]
        unconfirmed = [
            notice for notice in notices if "未取得两个模型的一致确认" in notice
        ]
        # 最终表里 0 < confidence < 0.78 的格子一格都没有（全是 0.86），
        # 所以这条提示必须消失，而不是留着"72 格"。
        self.assertEqual(unconfirmed, [])

    def test_no_reason_when_nothing_needs_review(self):
        result = self._result()
        result["publication_block_reasons"].append(
            "存在尚未人工确认的黄色单元格（R1C1 等 40 格），禁止作为已验证结果直接导出。"
        )
        result["publication_blocked"] = True
        result["recognition_state"] = "blocked"

        ocr_backend._render_review_messages(result)

        self.assertEqual(
            [reason for reason in result["publication_block_reasons"]
             if "尚未人工确认的黄色单元格" in reason],
            [],
        )
        self.assertFalse(result["publication_blocked"])
        self.assertEqual(result["recognition_state"], "verified")

    def test_coordinates_never_point_outside_the_delivered_grid(self):
        result = self._result(rows=2, columns=2)
        result["publication_block_reasons"].append(
            "存在跨模型冲突或内容证据不足的单元格（R6C1、R9C4、R17C1），禁止直接导出。"
        )
        ocr_backend._carry_review_locations(
            result,
            {"conflicts": [(5, 0), (8, 3), (16, 0)], "visible_blanks": []},
            18,
            4,
        )

        ocr_backend._render_review_messages(result)

        for reason in result["publication_block_reasons"]:
            for row, column in _coordinates(reason):
                self.assertLessEqual(row, 2)
                self.assertLessEqual(column, 2)


class ReviewCoordinatesFollowGridChanges(unittest.TestCase):
    """网格变形后坐标必须跟着走。"""

    def test_dropping_leading_rows_shifts_the_coordinates(self):
        result = {"cells": []}
        ocr_backend._carry_review_locations(
            result, {"conflicts": [(6, 0), (7, 1)], "visible_blanks": []}, 10, 3
        )

        ocr_backend._remap_review_axis(
            result, "rows", lambda index: None if index < 4 else index - 4
        )

        rows = result[ocr_backend._REVIEW_AXIS_KEY]["rows"]
        self.assertIsNone(rows[3], msg="被删掉的行上的坐标必须作废")
        self.assertEqual(rows[6], 2)
        self.assertEqual(rows[7], 3)

    def test_merging_a_column_moves_the_coordinates_to_the_anchor(self):
        result = {"cells": []}
        ocr_backend._carry_review_locations(
            result, {"conflicts": [(0, 2)], "visible_blanks": []}, 4, 4
        )
        kept = [0, 1, 3]
        kept_positions = {column: position for position, column in enumerate(kept)}

        def merged_position(column: int) -> int:
            if column in kept_positions:
                return kept_positions[column]
            anchors = [
                position for position, index in enumerate(kept) if index < column
            ]
            return anchors[-1] if anchors else 0

        ocr_backend._remap_review_axis(result, "columns", merged_position)

        columns = result[ocr_backend._REVIEW_AXIS_KEY]["columns"]
        self.assertEqual(columns[2], 1, msg="被并掉的列要挪到锚点列")
        self.assertEqual(columns[3], 2)

    def test_collapsed_row_moves_onto_the_previous_row(self):
        result = {"cells": []}
        ocr_backend._carry_review_locations(
            result, {"conflicts": [(4, 0)], "visible_blanks": []}, 8, 3
        )

        ocr_backend._remap_review_axis(
            result,
            "rows",
            lambda current: (
                current - 1 if current > 5
                else 4 if current == 5
                else current
            ),
        )

        rows = result[ocr_backend._REVIEW_AXIS_KEY]["rows"]
        self.assertEqual(rows[4], 4)
        self.assertEqual(rows[5], 4, msg="被折回的行要并到上一行")


class ReviewMessageRenderStaysInPlace(unittest.TestCase):
    """重写逻辑必须留在**最终网格**之后，不能提前。"""

    def setUp(self):
        source = Path(os.path.abspath(ocr_backend.__file__)).read_text(encoding="utf-8")
        self.tree = ast.parse(source)
        self.functions = {
            node.name: node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.calls = {
            node.name: [
                child.lineno
                for child in ast.walk(node)
                if isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == node.name
            ]
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef)
        }

    def test_render_is_called_from_the_request_handler(self):
        node = self.functions.get("_handle_request_body")
        self.assertIsNotNone(node, msg="源码里找不到 _handle_request_body")
        calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_render_review_messages"
        ]
        self.assertEqual(len(calls), 1, msg="_handle_request_body 必须重写一次提示")
        # 重写必须发生在 review_cell_count 对账之后、return 之前。
        counter_lines = [
            child.lineno
            for child in ast.walk(node)
            if isinstance(child, ast.Assign)
            and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "result"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "review_cell_count"
                for target in child.targets
            )
        ]
        return_lines = [
            child.lineno
            for child in ast.walk(node)
            if isinstance(child, ast.Return)
        ]
        self.assertTrue(counter_lines, msg="找不到 review_cell_count 的对账赋值")
        self.assertTrue(return_lines, msg="找不到 return")
        self.assertGreater(calls[0].lineno, max(counter_lines))
        self.assertLess(calls[0].lineno, max(return_lines))

    def test_render_replaces_stale_messages_instead_of_appending(self):
        node = self.functions.get("_render_review_messages")
        self.assertIsNotNone(node)
        source = ast.dump(node)
        self.assertIn("尚未人工确认的黄色单元格", source)
        self.assertIn("未取得两个模型的一致确认", source)
        self.assertIn("检测到墨迹但未能读出内容", source)


if __name__ == "__main__":
    unittest.main()
