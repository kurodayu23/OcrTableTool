# -*- coding: utf-8 -*-
"""识别守卫的**可执行行为**测试（2026-11-02 新增，B14 收口）。

补的是哪块缺口
--------------
``backend/tests/test_recognition_guard_behavior.py``（运行时接线）与
``backend/tests/test_recognition_guard_ast.py``（结构护栏）都验不到一层东西：
**守卫自己的判定逻辑在给定输入下到底怎么判**。

那 6 处守卫是**内联在 `_recognize` 里**的（不在独立函数里），所以：

* 要用假断言硬凑（"写了这段代码"）⇒ 换种写法就红、逻辑写错反而绿；
* 要真跑 `_recognize` 触发它们 ⇒ 需要构造能走到那条分支的真实图像，
  廉价场景不可达 ⇒ 硬造只会写出永远走不到的假断言。

本模块的做法：**把守卫的真实源码从产品源文件里原样抽出来执行**。
守卫的判定表达式、阈值、分支顺序、告警文案 —— 全部来自产品源码本身，
测试不复制任何一份逻辑。输入是构造出来的最小状态，断言的是**可观察结果**
（告警文案 / 阻断原因列表的内容）。

这样：

* 把阈值 ``>= 8`` 改成 ``>= 80`` ⇒ 用例真的红（因为执行的是被改过的源码）；
* 把守卫整段删掉 ⇒ 找不到守卫 ⇒ 用例红；
* 把告警文案改掉 ⇒ 用例红。

覆盖的 6 处（与 ``test_recognition_guard_behavior.py`` 文档里列的"只有静态守卫"一一对应）：

==========================================  =========================================
守卫                                        断言的判定语义
==========================================  =========================================
模糊告警（``图片较模糊``）                   背景是照片且清晰度 < 8.0 才告警
贴边告警（``表格或纸张边缘贴近画面边界``）    纸张贴边且未被透视/非对称间隙修正过才告警
结构模型不可用 / 线框冲突                     未确认结构时，按引擎失败与否给两种不同告警
跨模型冲突过多                               负置信度格 >= 8 且占比 > 25% 才告警
黄色单元格阻断（``pending_review_locations``） 有待确认格就阻断，并在文案里列出格位
跨模型冲突阻断（``unresolved_nonblank_conflicts``） 排除"墨迹空白格"后的冲突才阻断
==========================================  =========================================

跑法::

    <venv> -B -m unittest discover -s backend\\tests -t backend\\tests
"""

from __future__ import annotations

import ast
import textwrap
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

# ⚠ 刻意**不** import ocr_backend：本模块只需要它的**源码**。
#   import ocr_backend 会触发 OCR 模型准备（复制模型到临时目录，实测 1~2 分钟），
#   而这里要的只是 AST —— 解析 2.6 MB 源码是秒级的。
#   路径取本文件所在树自己的 backend\ocr_backend.py，与该树里被测的模块是同一个文件。
_SOURCE_PATH = BACKEND / "ocr_backend.py"
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")
# ⚠ 行表**只切一次**：ast.get_source_segment() 每次调用都会把整份 2.5 MB 源码
#   重新 splitlines() 一遍；而 _recognize 里有上千个 If 节点 ⇒ 那样做要重复切上千次，
#   实测把这条用例拖到几分钟。改成"行表切一次 + 自己按节点区间取片段"。
_SOURCE_LINES = _SOURCE.splitlines(True)
_TREE = ast.parse(_SOURCE)
_FUNCTIONS: dict[str, ast.FunctionDef] = {
    node.name: node
    for node in ast.walk(_TREE)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}


# ------------------------------------------------------------------ 抽取基础设施

def _node_source(node: ast.AST) -> str:
    """等价 ``ast.get_source_segment``，但复用已切好的行表。

    ⚠ **每一行**都要去掉同样的 base 缩进（不只是首行）：片段是从函数体里
    取出来的，首行去掉缩进、其余行保留 ⇒ 相对缩进就错位了 ⇒ 片段根本编译不过。
    """
    end_lineno = getattr(node, "end_lineno", None)
    end_col = getattr(node, "end_col_offset", None)
    if end_lineno is None or end_col is None:
        return ""
    lines = _SOURCE_LINES[node.lineno - 1: end_lineno]
    if not lines:
        return ""
    base = node.col_offset
    last = len(lines) - 1
    out = []
    for index, line in enumerate(lines):
        if index == last:
            out.append(line[base:end_col] if base <= end_col else line[end_col:])
        elif len(line) >= base:
            out.append(line[base:])
        else:
            out.append(line.lstrip())
    return "".join(out)


def _lines_containing(fragment: str) -> list[int]:
    """含指定文案的行号（1 起）。"""
    return [index + 1 for index, line in enumerate(_SOURCE_LINES) if fragment in line]


def _statement_lists(func: ast.FunctionDef) -> dict[int, list]:
    """id(语句) → 它所在的语句列表。"""
    mapping: dict[int, list] = {}
    for node in ast.walk(func):
        for field in ("body", "orelse", "finalbody"):
            statements = getattr(node, field, None)
            if isinstance(statements, list):
                for statement in statements:
                    mapping[id(statement)] = statements
    return mapping


def _find_guard(func_name: str, warning_fragment: str) -> ast.If:
    """找到"告警文案里含这一句"的守卫 if 语句。

    用**告警文案**而不是条件表达式来定位：条件可能重名（好几个分支都提到
    同一个变量），文案才是这段守卫独有的指纹。

    取**最内层**命中文案的那个 if（行跨度最小）：外层的 if/elif/else 大链子
    会把无关分支一起带进来，既跑不起来（缺变量），也测不准到底哪条分支生效。
    """
    func = _FUNCTIONS.get(func_name)
    if func is None:
        raise AssertionError("产品源码里找不到函数 %s" % func_name)
    fragment_lines = _lines_containing(warning_fragment)
    if not fragment_lines:
        raise AssertionError(
            "守卫已消失或文案已改：%s 里找不到告警含 %r 的 if 语句" % (func_name, warning_fragment)
        )
    best: ast.If | None = None
    best_span: int | None = None
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", None) or start
        if not any(start <= line <= end for line in fragment_lines):
            continue
        span = end - start
        if best_span is None or span < best_span:
            best = node
            best_span = span
    if best is None:
        raise AssertionError(
            "守卫已消失或文案已改：%s 里找不到告警含 %r 的 if 语句" % (func_name, warning_fragment)
        )
    return best


def _strip_base_indent(segment: str) -> str:
    """去掉片段自带的公共缩进（片段是从函数体里原样取出来的）。"""
    lines = segment.split("\n")
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    if not indents:
        return segment
    indent = min(indents)
    return "\n".join(line[indent:] if line.strip() else "" for line in lines)


def _guard_source_under(func_name: str, warning_fragment: str, condition_fragment: str) -> str:
    """取"条件命中 + 文案命中"的**外层**守卫：外层 if 的条件 + 内层分支原文。

    用于那种**两层**守卫：外层决定"要不要进这一段"，内层决定"报哪一种警"。
    只抽内层会丢掉外层条件（"结构已确认 ⇒ 根本不报" 这条就测不到）。
    """
    func = _FUNCTIONS[func_name]
    fragment_lines = _lines_containing(warning_fragment)
    outer: ast.If | None = None
    outer_span: int | None = None
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        if condition_fragment not in (_node_source(node.test) or ""):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if not any(node.lineno <= line <= end for line in fragment_lines):
            continue
        body_source = "".join(_node_source(statement) for statement in node.body)
        if warning_fragment not in body_source:
            continue
        span = end - node.lineno
        if outer_span is None or span < outer_span:
            outer = node
            outer_span = span
    if outer is None:
        raise AssertionError(
            "找不到条件含 %r 的外层守卫（文案 %r）" % (condition_fragment, warning_fragment)
        )
    inner = _find_guard(func_name, warning_fragment)
    block = _strip_base_indent(_node_source(inner))
    indented = "\n".join(("    " + line) if line.strip() else "" for line in block.split("\n"))
    # ⚠ 用 .format() 而不是百分号格式化：后者的占位符紧跟冒号，会被门禁的
    #   机器路径正则误判成一个假的绝对路径 ⇒ 身份门禁的噪声清单里会多一条。
    return "if {0}:{1}{2}".format(
        (_node_source(outer.test) or "").strip(), "\n", indented
    )


def _guard_source(
    func_name: str,
    warning_fragment: str,
    extra_assignments: tuple[str, ...] = (),
) -> str:
    """取出守卫的可执行源码片段（守卫本身 + 它依赖的前置赋值）。"""
    func = _FUNCTIONS[func_name]
    node = _find_guard(func_name, warning_fragment)
    lists = _statement_lists(func)
    statements = lists.get(id(node))
    if statements is None:
        raise AssertionError("定位不到守卫所在的语句列表")

    parts: list[str] = []
    if extra_assignments:
        index = statements.index(node)
        collected: dict[str, str] = {}
        for statement in statements[:index]:
            if not isinstance(statement, ast.Assign):
                continue
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in extra_assignments:
                    collected[target.id] = _node_source(statement)
        for name in extra_assignments:
            if name not in collected:
                raise AssertionError("守卫依赖的赋值没找到：%s" % name)
            parts.append(collected[name])

    parts.append(_node_source(node))
    return "\n".join(parts)


def _run_guard(source: str, namespace: dict) -> dict:
    """执行守卫片段（真实产品源码），返回执行后的命名空间。"""
    scope = dict(namespace)
    code = compile(_strip_base_indent(source), "<guard-source>", "exec")
    exec(code, scope)  # noqa: S102 - 目的就是执行产品源码本身
    return scope


def _grid(rows: int, columns: int, negatives: int) -> list[list[float]]:
    """造一张 confidence_grid：前 negatives 格为负置信度（< 0）。"""
    grid = [[0.9] * columns for _ in range(rows)]
    for index in range(negatives):
        row = index // columns
        column = index % columns
        grid[row][column] = -1.0
    return grid


# ------------------------------------------------------------------ 各守卫的用例

class BlurryWarningTest(unittest.TestCase):
    """模糊告警：背景是照片且清晰度 < 8.0。"""

    SOURCE = _guard_source("_recognize", "图片较模糊，部分文字可能无法确认。")

    def test_guard_source_was_found(self):
        self.assertIn("图片较模糊", self.SOURCE)

    def test_warns_when_photo_is_blurry(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "photographic_background": True,
                "source_quality": {"sharpness": 3.0},
                "structural_warnings": [],
            },
        )
        self.assertEqual(
            scope["structural_warnings"],
            ["图片较模糊，部分文字可能无法确认。"],
        )

    def test_does_not_warn_when_sharpness_reaches_threshold(self):
        """清晰度正好 8.0 不告警（判据是 < 8.0，不是 <= 8.0）。"""
        scope = _run_guard(
            self.SOURCE,
            {
                "photographic_background": True,
                "source_quality": {"sharpness": 8.0},
                "structural_warnings": [],
            },
        )
        self.assertEqual(scope["structural_warnings"], [])

    def test_does_not_warn_for_non_photo_background(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "photographic_background": False,
                "source_quality": {"sharpness": 1.0},
                "structural_warnings": [],
            },
        )
        self.assertEqual(scope["structural_warnings"], [])


class EdgeTouchWarningTest(unittest.TestCase):
    """贴边告警：纸张贴边且未被修正过。"""

    SOURCE = _guard_source("_recognize", "表格或纸张边缘贴近画面边界")

    def test_warns_when_paper_touches_edge(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "paper_touches_image_edge": True,
                "rectification": {},
                "structural_warnings": [],
            },
        )
        self.assertEqual(
            scope["structural_warnings"],
            ["表格或纸张边缘贴近画面边界，结果可能缺少边缘行列。"],
        )

    def test_does_not_warn_when_rectified_from_paper(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "paper_touches_image_edge": True,
                "rectification": {"refined_from_paper": True},
                "structural_warnings": [],
            },
        )
        self.assertEqual(scope["structural_warnings"], [])

    def test_does_not_warn_when_not_touching_edge(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "paper_touches_image_edge": False,
                "rectification": {},
                "structural_warnings": [],
            },
        )
        self.assertEqual(scope["structural_warnings"], [])


class StructureEngineWarningTest(unittest.TestCase):
    """结构模型不可用 / 线框冲突：两种不同的告警，不能混。"""

    SOURCE = _guard_source_under(
        "_recognize",
        "线框、文字布局与结构模型结果不一致",
        "structure_confirmed",
    )

    def test_engine_failure_gives_its_own_warning(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "structure_confirmed": False,
                "_table_structure_engine_failure": "engine timeout",
                "structural_warnings": [],
            },
        )
        self.assertEqual(len(scope["structural_warnings"]), 1)
        self.assertIn("表格结构模型本次不可用", scope["structural_warnings"][0])
        self.assertIn("engine timeout", scope["structural_warnings"][0])

    def test_conflict_without_engine_failure_gives_conflict_warning(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "structure_confirmed": False,
                "_table_structure_engine_failure": None,
                "structural_warnings": [],
            },
        )
        self.assertEqual(
            scope["structural_warnings"],
            ["线框、文字布局与结构模型结果不一致，请重点核对行列。"],
        )

    def test_no_warning_when_structure_confirmed(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "structure_confirmed": True,
                "_table_structure_engine_failure": "engine timeout",
                "structural_warnings": [],
            },
        )
        self.assertEqual(scope["structural_warnings"], [])


class CrossModelConflictWarningTest(unittest.TestCase):
    """跨模型冲突过多：负置信度格 >= 8 且占比 > 25%。"""

    SOURCE = _guard_source(
        "_recognize",
        "识别结果存在较多跨模型冲突",
        extra_assignments=(
            "blocking_review_cells",
            "blocking_review_ratio",
            "unresolved_model_conflicts",
        ),
    )

    @staticmethod
    def _run(grid, total_cells):
        return _run_guard(
            CrossModelConflictWarningTest.SOURCE,
            {
                "confidence_grid": grid,
                "total_cells": total_cells,
                "_unresolved_model_conflict_locations": lambda _grid: [],
                "structural_warnings": [],
            },
        )

    def test_warns_at_exactly_eight_cells_above_quarter(self):
        """8 格负置信度 / 共 20 格（占比 40%）⇒ 告警。"""
        scope = self._run(_grid(4, 5, 8), 20)
        self.assertEqual(
            scope["structural_warnings"],
            ["识别结果存在较多跨模型冲突，请对照原图核对黄色单元格。"],
        )

    def test_does_not_warn_below_eight_cells(self):
        """7 格 ⇒ 不告警（阈值是 >= 8）。"""
        scope = self._run(_grid(4, 5, 7), 20)
        self.assertEqual(scope["structural_warnings"], [])

    def test_does_not_warn_when_ratio_too_small(self):
        """8 格但共 100 格（占比 8%）⇒ 不告警（占比判据是 > 25%）。"""
        scope = self._run(_grid(10, 10, 8), 100)
        self.assertEqual(scope["structural_warnings"], [])


class PendingReviewBlockingTest(unittest.TestCase):
    """黄色单元格阻断：有待确认格就阻断，文案里要列出格位。"""

    SOURCE = _guard_source("_recognize", "存在尚未人工确认的黄色单元格")

    def test_blocks_and_lists_locations(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "pending_review_locations": [(0, 0), (1, 2)],
                "publication_block_reasons": [],
            },
        )
        reasons = scope["publication_block_reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("R1C1", reasons[0])
        self.assertIn("R2C3", reasons[0])
        self.assertIn("禁止作为已验证结果直接导出", reasons[0])

    def test_no_block_without_pending_locations(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "pending_review_locations": [],
                "publication_block_reasons": [],
            },
        )
        self.assertEqual(scope["publication_block_reasons"], [])

    def test_mentions_count_when_more_than_six(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "pending_review_locations": [(row, 0) for row in range(8)],
                "publication_block_reasons": [],
            },
        )
        self.assertIn(" 等 8 格", scope["publication_block_reasons"][0])


class UnresolvedNonblankConflictBlockingTest(unittest.TestCase):
    """跨模型冲突阻断：墨迹空白格不算冲突，其余冲突才阻断。"""

    SOURCE = _guard_source(
        "_recognize",
        "存在跨模型冲突或内容证据不足的单元格",
        extra_assignments=("unresolved_nonblank_conflicts",),
    )

    def test_blocks_on_nonblank_conflicts_only(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "unresolved_model_conflicts": [(0, 0), (1, 1)],
                "unresolved_visible_blanks": [(1, 1)],
                "publication_block_reasons": [],
            },
        )
        reasons = scope["publication_block_reasons"]
        self.assertEqual(len(reasons), 1)
        self.assertIn("R1C1", reasons[0])
        self.assertNotIn("R2C2", reasons[0])

    def test_no_block_without_conflicts(self):
        scope = _run_guard(
            self.SOURCE,
            {
                "unresolved_model_conflicts": [],
                "unresolved_visible_blanks": [],
                "publication_block_reasons": [],
            },
        )
        self.assertEqual(scope["publication_block_reasons"], [])


class GuardCoverageTest(unittest.TestCase):
    """6 处守卫必须都找得到 —— 少一处就是有人把守卫删了/改了文案。"""

    def test_all_six_guards_are_present(self):
        expected = {
            "图片较模糊，部分文字可能无法确认。": ("_recognize", ()),
            "表格或纸张边缘贴近画面边界": ("_recognize", ()),
            "线框、文字布局与结构模型结果不一致": ("_recognize", ()),
            "识别结果存在较多跨模型冲突": (
                "_recognize",
                ("blocking_review_cells", "blocking_review_ratio", "unresolved_model_conflicts"),
            ),
            "存在尚未人工确认的黄色单元格": ("_recognize", ()),
            "存在跨模型冲突或内容证据不足的单元格": (
                "_recognize",
                ("unresolved_nonblank_conflicts",),
            ),
        }
        for fragment, (func_name, extras) in expected.items():
            with self.subTest(fragment=fragment):
                source = _guard_source(func_name, fragment, extras)
                self.assertIn(fragment, source)

    def test_guard_sources_are_not_trivially_empty(self):
        """抽出来的片段必须真的含判定逻辑，不能只剩一行 append。"""
        self.assertIn("photographic_background", BlurryWarningTest.SOURCE)
        self.assertIn("paper_touches_image_edge", EdgeTouchWarningTest.SOURCE)
        self.assertIn("blocking_review_cells >= 8", CrossModelConflictWarningTest.SOURCE)
        self.assertIn("blocking_review_ratio > 0.25", CrossModelConflictWarningTest.SOURCE)


if __name__ == "__main__":
    unittest.main()
