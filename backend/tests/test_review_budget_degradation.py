# -*- coding: utf-8 -*-
"""超时降级：复核预算耗尽时**降级发布**，不再整单丢弃。

背景（2026-11-05）
------------------
真实照片全扫 607 张里有 200 张以 ``_FineGrainedBudgetExceeded`` 收场 —— 用户
拿不到任何表格，只有一句"表格结构过于复杂或存在干扰网格"。空闲条件复测
11 张里 8 张是同一个原因，其中 8 张的失败点在**逐格复核**阶段（另 2 张在结构
提取阶段，那 2 张本来就降级不了：结构不可信时不存在"已识别部分"）。

降级语义
--------
* 没复核完的格子 ⇒ ``confidence = -1.0`` ⇒ ``needs_review`` ⇒ 界面标黄 +
  禁止直接导出；
* 已经复核过的部分照常交付；
* 空格不标（没有内容就没有"读错"的风险）；
* 数字复核没跑完 ⇒ **留空**（不给未验证的数字）。

实现落点
--------
``_timed_recognition_call`` 是**所有**模型调用的唯一收口（page-mobile /
v6-small / medium / v6-medium-alternate 全部经它），预算守卫的 3 个抛出点
（墙钟用尽 / 前瞻估算超限 / 图片量上限）也全在它里面。所以只改这一个函数
就能把"抛异常 = 整单丢弃"换成"登记降级 + 返回空结果"。

守住两条不能退让的红线
----------------------
* 降级标记必须活到最终网格上 —— 中途换网格导致标记丢失时**必须继续失败**，
  否则等于把没复核过的格子当成已复核交付；
* 预算判定本身不许放宽（60 秒墙钟契约照旧）。
"""
from __future__ import annotations

import ast
import os
import sys
import time
import unittest
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ocr_backend  # noqa: E402


# ------------------------------------------------------------------ 测试夹具

def _fake_engine():
    """一个只让复核模型调用抛预算异常的假引擎。

    走真实的 ``_timed_recognition_call`` 收口 —— 降级发生在那一层，
    绕开它就测不到真正的路径。
    """

    class _Engine:
        def __init__(self):
            self.calls = 0

        def _recognize(self, *args, **kwargs):
            self.calls += 1
            raise ocr_backend._FineGrainedBudgetExceeded("测试用预算耗尽")

        @property
        def text_rec(self):
            def _call(*args, **kwargs):
                return ocr_backend._timed_recognition_call(
                    "medium", self._recognize, args, kwargs
                )

            return _call

    return _Engine()


def _grid_with_ink(rows: int = 3, columns: int = 3, cell_w: int = 40, cell_h: int = 20):
    """造一张有墨迹的网格图（每个格子中间画一块黑），并给出对应的文本网格。"""
    height = rows * cell_h
    width = columns * cell_w
    image = np.full((height, width, 3), 255, dtype=np.uint8)
    grid = [[""] * columns for _ in range(rows)]
    for row in range(rows):
        for column in range(columns):
            top = row * cell_h + cell_h // 2 - 3
            left = column * cell_w + cell_w // 2 - 6
            image[top : top + 6, left : left + 12] = 0
            grid[row][column] = "值%d%d" % (row, column)
    # 第二行第一列留空：空格不能被标黄
    grid[1][0] = ""
    columns_boundaries = [column * cell_w for column in range(columns + 1)]
    rows_boundaries = [row * cell_h for row in range(rows + 1)]
    return image, grid, columns_boundaries, rows_boundaries


class _DegradationStateMixin:
    """降级状态是模块级全局，测试之间必须互相隔离。"""

    def setUp(self):
        # cv2 / np / pipeline 在模块里是**懒加载占位符**（None），真正用之前
        # 必须先 _load_runtime()，否则任何图像运算都会在 None 上炸。
        ocr_backend._load_runtime()
        self._reset_degradation_state()

    def tearDown(self):
        self._reset_degradation_state()

    @staticmethod
    def _reset_degradation_state():
        ocr_backend._budget_exhausted = False
        ocr_backend._budget_exhausted_model = ""
        ocr_backend._budget_exhausted_reason = ""
        ocr_backend._budget_degraded_cells.clear()
        ocr_backend._budget_degraded_components = 0


# ------------------------------------------------------------------ 收口行为

class TimedRecognitionCallDegradesInsteadOfRaising(_DegradationStateMixin, unittest.TestCase):
    """模型调用收口：预算耗尽 ⇒ 空结果，不抛异常。"""

    def test_budget_exhaustion_returns_empty_output_instead_of_raising(self):
        engine = _fake_engine()

        result = engine.text_rec()
        self.assertEqual(list(result.txts), [])
        self.assertEqual(list(result.scores), [])
        self.assertTrue(ocr_backend._budget_exhausted)
        self.assertEqual(engine.calls, 1)

    def test_degraded_output_has_usable_boxes_shape(self):
        """调用点有三百多处直接用 boxes —— 空结果也必须是标准形状。"""
        engine = _fake_engine()
        result = engine.text_rec()
        boxes = np.asarray(result.boxes)
        self.assertEqual(boxes.shape[0], 0)
        self.assertEqual(len(boxes.shape), 2)

    def test_calls_after_exhaustion_do_not_touch_the_model(self):
        """预算爆掉后再调模型只是白烧时间，必须直接短路。"""
        engine = _fake_engine()
        engine.text_rec()
        self.assertEqual(engine.calls, 1)
        engine.text_rec()
        self.assertEqual(engine.calls, 1)

    def test_exhaustion_reason_is_recorded(self):
        engine = _fake_engine()
        engine.text_rec()
        self.assertIn("预算", ocr_backend._budget_exhausted_reason)
        self.assertEqual(ocr_backend._budget_exhausted_model, "medium")


# --------------------------------------------------------------- 逐格复核降级

class ReviewBudgetDegradation(_DegradationStateMixin, unittest.TestCase):
    """逐格复核超时时降级而不是整单失败。"""

    def test_budget_error_does_not_escape_the_review_call(self):
        image, grid, columns, rows = _grid_with_ink()
        confidence = [[0.9] * len(grid[0]) for _ in grid]
        engine = _fake_engine()

        result = ocr_backend._verify_motion_blurred_ruled_cells(
            image,
            grid,
            confidence,
            columns,
            rows,
            engine,
            view_mode="standard",
            selected_cells={(0, 0), (0, 1), (1, 0)},
        )

        self.assertIsInstance(result, list)
        self.assertEqual(engine.calls, 1)

    def test_unreviewed_cells_with_text_are_marked_for_review(self):
        image, grid, columns, rows = _grid_with_ink()
        confidence = [[0.9] * len(grid[0]) for _ in grid]
        engine = _fake_engine()

        ocr_backend._verify_motion_blurred_ruled_cells(
            image,
            grid,
            confidence,
            columns,
            rows,
            engine,
            view_mode="standard",
            selected_cells={(0, 0), (0, 1)},
        )

        self.assertIn((0, 0), ocr_backend._budget_degraded_cells)
        self.assertIn((0, 1), ocr_backend._budget_degraded_cells)
        self.assertLess(confidence[0][0], 0.0)
        self.assertLess(confidence[0][1], 0.0)

    def test_blank_cells_are_not_marked(self):
        image, grid, columns, rows = _grid_with_ink()
        confidence = [[0.9] * len(grid[0]) for _ in grid]
        engine = _fake_engine()

        ocr_backend._verify_motion_blurred_ruled_cells(
            image,
            grid,
            confidence,
            columns,
            rows,
            engine,
            view_mode="standard",
            selected_cells={(0, 0), (1, 0)},
        )

        # (1,0) 是空格：没有内容就没有"读错"的风险，不该被标黄
        self.assertNotIn((1, 0), ocr_backend._budget_degraded_cells)
        self.assertEqual(confidence[1][0], 0.9)

    def test_cells_outside_the_selection_are_not_marked(self):
        image, grid, columns, rows = _grid_with_ink()
        confidence = [[0.9] * len(grid[0]) for _ in grid]
        engine = _fake_engine()

        ocr_backend._verify_motion_blurred_ruled_cells(
            image,
            grid,
            confidence,
            columns,
            rows,
            engine,
            view_mode="standard",
            selected_cells={(0, 0)},
        )

        self.assertEqual(ocr_backend._budget_degraded_cells, {(0, 0)})
        self.assertEqual(confidence[2][2], 0.9)

    def test_marked_cells_satisfy_the_human_confirmation_rule(self):
        """-1.0 必须真的被下游认成"要人工确认"，否则降级等于没降。"""
        image, grid, columns, rows = _grid_with_ink()
        confidence = [[0.9] * len(grid[0]) for _ in grid]
        engine = _fake_engine()

        ocr_backend._verify_motion_blurred_ruled_cells(
            image,
            grid,
            confidence,
            columns,
            rows,
            engine,
            view_mode="standard",
            selected_cells={(0, 0)},
        )

        self.assertTrue(
            ocr_backend._cell_requires_human_confirmation(
                grid[0][0], confidence[0][0]
            ),
            msg="降级标记必须让该格进入人工确认集合",
        )


# --------------------------------------------------------------- 数字复核降级

class NumericComponentDegradation(_DegradationStateMixin, unittest.TestCase):
    """数字复核没跑完 ⇒ 留空，绝不把未验证的数字当成结论。"""

    def _ocr_output(self):
        class _Output:
            boxes = [np.asarray([[2.0, 2.0], [38.0, 2.0], [38.0, 22.0], [2.0, 22.0]])]
            txts = ["12345"]
            scores = [0.95]

        return _Output()

    def test_unverified_numbers_are_blanked(self):
        image = np.full((60, 80, 3), 255, dtype=np.uint8)
        image[6:18, 8:34] = 0
        engine = _fake_engine()

        boxes, texts, scores, count = ocr_backend._verified_numeric_components(
            image,
            self._ocr_output(),
            engine,
            verify_all=True,
        )

        self.assertGreaterEqual(count, 1)
        self.assertEqual(texts[0], "", msg="未验证的数字必须留空")
        self.assertLess(scores[0], 0.0)


# --------------------------------------------------------------- 契约不被放宽

class BudgetContractIsNotLoosened(unittest.TestCase):
    """降级不许变成"预算放宽"。"""

    def test_budget_thresholds_are_unchanged(self):
        self.assertEqual(ocr_backend._MAX_REQUEST_SECONDS, 60.0)
        self.assertEqual(ocr_backend._MAX_FINE_GRAINED_IMAGES, 1600)
        self.assertEqual(ocr_backend._GEOMETRY_SEARCH_BUDGET_FRACTION, 0.70)

    def test_budget_enforcement_still_raises(self):
        """预算判定本身仍然抛异常 —— 降级只发生在"调用收口"这一层。"""
        previous_started = ocr_backend._request_started_at
        previous_budget = ocr_backend._request_budget_seconds
        ocr_backend._request_started_at = time.perf_counter() - 120.0
        ocr_backend._request_budget_seconds = 60.0
        try:
            with self.assertRaises(ocr_backend._FineGrainedBudgetExceeded):
                ocr_backend._enforce_request_budget("test", 10, True)
        finally:
            ocr_backend._request_started_at = previous_started
            ocr_backend._request_budget_seconds = previous_budget


# ------------------------------------------------------------------- AST 护栏

_SOURCE_PATH = Path(os.path.abspath(ocr_backend.__file__))
_TREE = ast.parse(_SOURCE_PATH.read_text(encoding="utf-8"))
_FUNCTIONS: dict[str, ast.FunctionDef] = {
    node.name: node
    for node in ast.walk(_TREE)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def _function(name: str) -> ast.AST:
    node = _FUNCTIONS.get(name)
    if node is None:
        raise AssertionError("源码里找不到函数 %s" % name)
    return node


def _string_literals(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            found.append(child.value)
        elif isinstance(child, ast.JoinedStr):
            for part in child.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.append(part.value)
    return found


def _has_string(node: ast.AST, needle: str) -> bool:
    return any(needle in text for text in _string_literals(node))


def _identifiers(node: ast.AST) -> set[str]:
    """节点里出现过的全部标识符（函数名、变量名、属性名）。"""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
    return found


class DegradationGuardsStayInPlace(unittest.TestCase):
    """降级的关键守卫必须留在**正确的位置**，不许被挪走或删掉。

    这些逻辑在 ``_recognize`` 内部，单测走不到（要跑完整识别），所以用
    AST 静态断言守住：位置对了、被别的分支包住了，才算数。
    """

    def test_recognize_checks_degraded_marks_survived(self):
        node = _function("_recognize")
        self.assertTrue(
            _has_string(node, "未能把未复核单元格标记出来"),
            msg="_recognize 必须保留「降级标记没落下去就失败」的守卫",
        )
        self.assertIn(
            "degraded_survived",
            _identifiers(node),
            msg="_recognize 必须校验降级标记是否活到最终网格",
        )

    def test_recognize_reports_budget_exhaustion_for_missing_structure(self):
        node = _function("_recognize")
        self.assertTrue(
            _has_string(node, "识别预算已耗尽，未能确认表格结构"),
            msg="结构没认出来时必须报预算耗尽，而不是'未检测到表格'",
        )

    def test_recognize_reports_degraded_cells_to_the_user(self):
        node = _function("_recognize")
        self.assertTrue(
            _has_string(node, "没有独立复核"),
            msg="必须告诉用户哪些格是没来得及复核（核对动作与读错不同）",
        )

    def test_recognition_call_converts_budget_failure_into_degradation(self):
        node = _function("_timed_recognition_call")
        names = _identifiers(node)
        self.assertIn(
            "_note_budget_exhausted",
            names,
            msg="模型调用收口必须把预算异常转成降级登记",
        )
        self.assertIn(
            "_degraded_recognition_output",
            names,
            msg="降级时必须返回空结果而不是 None",
        )

    def test_review_function_marks_cells_when_budget_is_gone(self):
        node = _function("_verify_motion_blurred_ruled_cells")
        self.assertTrue(
            _has_string(node, "review_budget_degraded"),
            msg="逐格复核必须登记降级事件",
        )


if __name__ == "__main__":
    unittest.main()
