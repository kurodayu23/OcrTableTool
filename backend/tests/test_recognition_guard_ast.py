# -*- coding: utf-8 -*-
"""识别守卫的 **AST 级护栏**（2026-11-02 新增）。

为什么还要再来一层
------------------
``backend/tests/test_backend.py`` 里那批 ``inspect.getsource(...)`` + ``assertIn("某段文字", source)``
只证明"源码里有这行字"，有两类**真实漏洞**：

1. **子串洞**：``assertIn("len(output_texts) <= 200", source)`` 在源码被改成
   ``len(output_texts) <= 2000`` 时**照样通过**（"200" 是 "2000" 的前缀）；
   ``"0 < ruled_cell_count <= 320"`` 对 ``<= 3200`` 同理。边界被悄悄放宽 10 倍，
   断言却是绿的。
2. **位置洞**：``assertIn("禁止直接导出", source)`` 不关心这句话在哪个分支里。
   把它挪到别的函数、甚至挪到注释里，断言依然是绿的。

本模块改用 **AST** 断言：定位到具体的 ``If`` / ``BoolOp`` / ``Compare`` / ``Call`` /
``Assign`` 节点，校验它的**操作数与位置**。这样上面两类洞都被堵住，而且不依赖源码排版
（改缩进、加注释、挪空行都不会误报）。

与 ``backend/tests/test_recognition_guard_behavior.py`` 的分工：

* 那个模块验的是**运行时**（守卫有没有真的被调用、有没有被 `_recognize` 直接调用）；
* 本模块验的是**结构**（守卫的阈值与分支位置有没有被改松、改丢）。

两者叠在一起，才既不是"只看字面"、也不是"只看行为"。

跑法::

    <venv> -B -m unittest discover -s backend\\tests -t backend\\tests
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import ocr_backend  # noqa: E402


# ---------------------------------------------------------------- AST 基础设施

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


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """所有调用 ``name`` 的 Call 节点（``name`` 或 ``模块.name`` 都算）。"""
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == name:
            found.append(child)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            found.append(child)
    return found


def _assignments(node: ast.AST, name: str) -> list[ast.Assign]:
    """所有把 ``name`` 作为赋值目标的 Assign 节点。"""
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for target in child.targets:
            if isinstance(target, ast.Name) and target.id == name:
                found.append(child)
    return found


def _string_literals(node: ast.AST) -> list[str]:
    """节点内所有字符串字面量（f-string 的常量片段也算）。"""
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


def _if_nodes(node: ast.AST, test_name: str) -> list[ast.If]:
    """``if <test_name>:`` 形式的 If 节点。"""
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.If)
        and isinstance(child.test, ast.Name)
        and child.test.id == test_name
    ]


def _is_ruled_cell_bound(node: ast.AST) -> bool:
    """``0 < ruled_cell_count <= 320``（链式比较）。"""
    if not isinstance(node, ast.Compare):
        return False
    if not (isinstance(node.left, ast.Constant) and node.left.value == 0):
        return False
    if len(node.ops) != 2 or len(node.comparators) != 2:
        return False
    if not isinstance(node.ops[0], ast.Lt) or not isinstance(node.ops[1], ast.LtE):
        return False
    first = node.comparators[0]
    if not (isinstance(first, ast.Name) and first.id == "ruled_cell_count"):
        return False
    last = node.comparators[1]
    return isinstance(last, ast.Constant) and last.value == 320


def _is_output_count_bound(node: ast.AST) -> bool:
    """``len(output_texts) <= 200``。"""
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return False
    if not isinstance(node.ops[0], ast.LtE):
        return False
    left = node.left
    if not (isinstance(left, ast.Call) and isinstance(left.func, ast.Name)):
        return False
    if left.func.id != "len" or len(left.args) != 1:
        return False
    argument = left.args[0]
    if not (isinstance(argument, ast.Name) and argument.id == "output_texts"):
        return False
    bound = node.comparators[0]
    return isinstance(bound, ast.Constant) and bound.value == 200


def _is_numeric_compare(node: ast.AST, name: str, op: type, value) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return False
    if not isinstance(node.ops[0], op):
        return False
    left = node.left
    if not (isinstance(left, ast.Name) and left.id == name):
        return False
    bound = node.comparators[0]
    return isinstance(bound, ast.Constant) and bound.value == value


class RecognitionGuardAstTest(unittest.TestCase):
    """守卫的**结构**断言（阈值、分支位置、接线），不依赖源码排版。"""

    # --- 页面级复核的双边界 ---------------------------------------------

    def test_page_verification_requires_both_bounds_in_one_condition(self):
        """``0 < ruled_cell_count <= 320`` 与 ``len(output_texts) <= 200`` 必须同属一个条件。

        旧断言 ``assertIn("0 < ruled_cell_count <= 320", source)`` 在源码被改成
        ``<= 3200`` 时照样通过（子串）；本断言会红。
        """
        function = _function("_recognize")
        matched = []
        for node in ast.walk(function):
            if not isinstance(node, ast.BoolOp):
                continue
            has_cells = any(_is_ruled_cell_bound(value) for value in node.values)
            has_outputs = any(_is_output_count_bound(value) for value in node.values)
            if has_cells and has_outputs:
                matched.append(node)
        self.assertTrue(
            matched,
            "页面级复核的两个边界不再同属一个条件（单元格上限与文本数上限被拆开了）",
        )

    def test_page_verification_bounds_are_not_widened(self):
        """单独再钉一次两个上限的数值（防止有人把 320 改成 3200）。"""
        function = _function("_recognize")
        cell_bounds = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Compare)
            and len(node.comparators) == 2
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == "ruled_cell_count"
            and isinstance(node.comparators[1], ast.Constant)
        ]
        self.assertTrue(cell_bounds, "找不到 ruled_cell_count 的链式比较")
        self.assertTrue(
            any(bound.comparators[1].value == 320 for bound in cell_bounds),
            "ruled_cell_count 的上限不再是 320：%r"
            % [bound.comparators[1].value for bound in cell_bounds],
        )
        output_bounds = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "len"
            and node.left.args
            and isinstance(node.left.args[0], ast.Name)
            and node.left.args[0].id == "output_texts"
        ]
        self.assertTrue(output_bounds, "找不到 len(output_texts) 的比较")
        self.assertTrue(
            any(
                bound.comparators[0].value == 200
                for bound in output_bounds
                if len(bound.comparators) == 1
            ),
            "len(output_texts) 的上限不再是 200：%r"
            % [
                bound.comparators[0].value
                for bound in output_bounds
                if len(bound.comparators) == 1
            ],
        )

    # --- 安全拒绝原因 ---------------------------------------------------

    def test_dense_grid_safe_rejection_reason_is_bound_to_the_cost_message(self):
        """``dense_grid_safe_rejection_reason`` 必须被赋成成本超限那个值。"""
        function = _function("_recognize")
        assignments = [
            node
            for node in _assignments(function, "dense_grid_safe_rejection_reason")
            if isinstance(node.value, ast.Constant)
        ]
        self.assertTrue(assignments, "dense_grid_safe_rejection_reason 没有被赋值")
        self.assertTrue(
            any(node.value.value == "large_review_cost_exceeds_bound" for node in assignments),
            "dense_grid_safe_rejection_reason 不再被赋成 large_review_cost_exceeds_bound：%r"
            % [node.value.value for node in assignments],
        )
        self.assertTrue(
            _has_string(function, "大型照片表格的逐格复核成本超过安全上限"),
            "成本超限的提示文案不见了",
        )

    # --- 出版阻断的两个分支 ---------------------------------------------

    def test_unresolved_conflicts_block_publication_inside_that_branch(self):
        """``if unresolved_nonblank_conflicts:`` 的分支体内必须有"禁止直接导出"。"""
        function = _function("_recognize")
        branches = _if_nodes(function, "unresolved_nonblank_conflicts")
        self.assertTrue(branches, "找不到 if unresolved_nonblank_conflicts 分支")
        self.assertTrue(
            any(_has_string(branch, "禁止直接导出") for branch in branches),
            "冲突分支里不再有'禁止直接导出'的措辞（阻断理由被挪走或删掉了）",
        )

    def test_pending_review_blocks_publication_inside_that_branch(self):
        """``if pending_review_locations:`` 的分支体内必须有"存在尚未人工确认的黄色单元格"。"""
        function = _function("_recognize")
        branches = _if_nodes(function, "pending_review_locations")
        self.assertTrue(branches, "找不到 if pending_review_locations 分支")
        self.assertTrue(
            any(_has_string(branch, "存在尚未人工确认的黄色单元格") for branch in branches),
            "待复核分支里不再有'存在尚未人工确认的黄色单元格'的措辞",
        )

    # --- 跨模型冲突阈值 -------------------------------------------------

    def test_excessive_cross_model_conflict_threshold_is_exact(self):
        """``blocking_review_cells >= 8 and blocking_review_ratio > 0.25`` 必须精确成立。"""
        function = _function("_recognize")
        matched = []
        for node in ast.walk(function):
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)):
                continue
            if not isinstance(node.test.op, ast.And):
                continue
            has_cells = any(
                _is_numeric_compare(value, "blocking_review_cells", ast.GtE, 8)
                for value in node.test.values
            )
            has_ratio = any(
                _is_numeric_compare(value, "blocking_review_ratio", ast.Gt, 0.25)
                for value in node.test.values
            )
            if has_cells and has_ratio:
                matched.append(node)
        self.assertTrue(
            matched,
            "跨模型冲突的告警条件不再是 '>= 8 格且 > 25%'",
        )

    # --- 两条结果级告警 -------------------------------------------------

    def test_blur_and_edge_touching_warnings_still_exist(self):
        function = _function("_recognize")
        self.assertTrue(
            _has_string(function, "图片较模糊，部分文字可能无法确认"),
            "模糊告警文案不见了",
        )
        self.assertTrue(
            _has_string(function, "表格或纸张边缘贴近画面边界"),
            "贴边告警文案不见了",
        )

    # --- 证书重对齐接线 -------------------------------------------------

    def test_certificate_realignment_is_wired_to_the_ui_strip(self):
        function = _function("_recognize")
        self.assertTrue(
            _calls(function, "_align_structure_certificate_after_ui_strip"),
            "_recognize 不再调用 _align_structure_certificate_after_ui_strip",
        )
        retrimmed = False
        for node in ast.walk(function):
            if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
                continue
            call = node.value
            if not (isinstance(call.func, ast.Name) and call.func.id == "_strip_spreadsheet_ui_headers"):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Tuple):
                    continue
                if any(
                    isinstance(element, ast.Name) and element.id == "retrimmed_values"
                    for element in target.elts
                ):
                    retrimmed = True
        self.assertTrue(
            retrimmed,
            "retrimmed_values 不再来自 _strip_spreadsheet_ui_headers 的返回值",
        )

    # --- 结构复核策略接线 ------------------------------------------------

    def test_structure_review_policy_is_called_by_recognition(self):
        function = _function("_recognize")
        self.assertTrue(
            _calls(function, "_apply_structure_review_policy"),
            "_recognize 不再调用 _apply_structure_review_policy",
        )

    # --- 表格引擎加载顺序 ------------------------------------------------

    def test_table_engine_is_requested_after_the_plain_engine(self):
        """无参 ``_engines()`` 必须在 ``_engines(load_table=True)`` 之前出现。"""
        function = _function("_recognize")
        calls = sorted(_calls(function, "_engines"), key=lambda node: node.lineno)
        plain = [
            node
            for node in calls
            if not any(keyword.arg == "load_table" for keyword in node.keywords)
        ]
        with_table = [
            node
            for node in calls
            if any(
                keyword.arg == "load_table"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
        ]
        self.assertTrue(plain, "找不到无参 _engines() 调用")
        self.assertTrue(with_table, "找不到 _engines(load_table=True) 调用")
        self.assertLess(
            plain[0].lineno,
            with_table[0].lineno,
            "表格引擎的请求出现在页面 OCR 引擎之前（顺序反了）",
        )

    # --- 线框与空间结构 -------------------------------------------------

    def test_ruled_and_spatial_structures_are_both_consulted(self):
        function = _function("_recognize")
        self.assertTrue(
            _calls(function, "extract_ruled_grid"),
            "_recognize 不再提取线框网格",
        )
        self.assertTrue(
            _calls(function, "assign_ocr_to_grid"),
            "_recognize 不再把 OCR 结果落到网格",
        )
        self.assertTrue(
            _has_string(function, "线框、文字布局与结构模型结果不一致"),
            "线框与结构模型冲突的告警文案不见了",
        )
        self.assertTrue(
            _assignments(function, "structure_confirmed"),
            "structure_confirmed 不再被赋值",
        )

    # --- 数值序列风险路由 ------------------------------------------------

    def test_numeric_sequence_risks_feed_the_rejection_decision(self):
        function = _function("_recognize")
        routed = [
            node
            for node in _assignments(function, "numeric_sequence_cells")
            if isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_dense_numeric_sequence_risks"
        ]
        self.assertTrue(
            routed,
            "numeric_sequence_cells 不再由 _dense_numeric_sequence_risks 赋值",
        )
        used = False
        for node in ast.walk(function):
            if not (isinstance(node, ast.Tuple) and len(node.elts) == 3):
                continue
            first, second, third = node.elts
            if (
                isinstance(first, ast.Name)
                and first.id == "numeric_sequence_cells"
                and isinstance(second, ast.Constant)
                and second.value is False
                and isinstance(third, ast.Constant)
                and third.value is False
            ):
                used = True
        self.assertTrue(
            used,
            "数值序列风险没有被带进安全拒绝决策（(numeric_sequence_cells, False, False) 这一路不见了）",
        )

    # --- 列补全的 8% 判定 ------------------------------------------------

    def test_column_completion_uses_the_stable_eight_percent_pair(self):
        function = _function("_recover_vertically_truncated_photo_grid")
        pairs = set()
        for node in ast.walk(function):
            if not (isinstance(node, ast.Tuple) and len(node.elts) == 2):
                continue
            values = []
            for element in node.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, float):
                    values.append(element.value)
                else:
                    break
            if len(values) == 2:
                pairs.add((values[0], values[1]))
        self.assertIn(
            (0.08, 0.085),
            pairs,
            "稳定的 8%% 判定对 (0.08, 0.085) 不见了：%r" % (sorted(pairs),),
        )
        self.assertIn(
            (0.095, 0.1),
            pairs,
            "稳定的 (0.095, 0.10) 判定对不见了：%r" % (sorted(pairs),),
        )
        matched = False
        for node in ast.walk(function):
            if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And)):
                continue
            has_flag = any(
                isinstance(value, ast.Name) and value.id == "morphology_columns_recovered"
                for value in node.values
            )
            has_rows = any(
                _is_numeric_compare(value, "recovered_rows", ast.GtE, 2)
                for value in node.values
            )
            if has_flag and has_rows:
                matched = True
        self.assertTrue(
            matched,
            "列补全的成立条件不再是 'morphology_columns_recovered 且 recovered_rows >= 2'",
        )

    # --- 结构引擎失败告警 ------------------------------------------------

    def test_table_engine_failure_warning_text_still_exists(self):
        function = _function("_recognize")
        self.assertTrue(
            _has_string(function, "表格结构模型本次不可用"),
            "结构模型不可用的告警文案不见了",
        )
        self.assertTrue(
            _has_string(function, "请重点核对行列"),
            "告警里的引导措辞不见了",
        )

    # --- 未确认提示不得带阻断措辞 ----------------------------------------

    def test_unconfirmed_notice_never_carries_blocking_wording(self):
        """「未取得两个模型的一致确认」只能是提示，不能再带"整表不能…直接导出"。"""
        function = _function("_recognize")
        marker = "未取得两个模型的一致确认"
        self.assertTrue(
            _has_string(function, marker),
            "未确认提示文案不见了（这条提示应当仍然存在，只是不得阻断）",
        )
        offenders = [
            text
            for text in _string_literals(function)
            if "整表不能作为已验证结果直接导出" in text
        ]
        self.assertFalse(
            offenders,
            "识别流程里又出现了'整表不能作为已验证结果直接导出'的措辞：%r" % (offenders,),
        )


if __name__ == "__main__":
    unittest.main()
