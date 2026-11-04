# -*- coding: utf-8 -*-
"""表格结构引擎失败必须对用户可见（2026-10-22 修）。

背景：``_record_table_engine_failure()`` 有 3 个写入点，但
``_table_structure_engine_failure`` 这个全局**只有 1 个读取点**
（``structure_confirmation`` 分支内）。另外两个写入点
（``borderless_table_structure`` / ``aligned_structure_probe``）的行号
**比那个读取点大**，所以这两处失败永远传不到读取点。

后果：交付到平板后（缺 onnxruntime）表格结构检测静默失效，
客户端拿到的 ``structure_warnings`` 是空的，用户看到的是一份
"看起来成功"的结果；而交付自检里 "table structure engine usable"
那一项只扫 warnings 里的"结构模型"，于是它恒 PASS。

修法：失败现场直接落 ``structural_warnings``。这里用 AST 做护栏，
不依赖源码排版。
"""
from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

import ocr_backend


_FATAL_CONTEXTS = ("borderless_table_structure", "aligned_structure_probe")


def _recognize_function() -> ast.FunctionDef:
    source = inspect.getsource(ocr_backend)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_recognize":
            return node
    raise AssertionError("_recognize not found")


def _failure_handlers() -> dict[str, ast.ExceptHandler]:
    """Map failure context -> the except handler that records it."""
    found: dict[str, ast.ExceptHandler] = {}
    function = _recognize_function()
    for node in ast.walk(function):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            function_name = getattr(call.func, "id", None)
            if function_name != "_record_table_engine_failure":
                continue
            if not call.args:
                continue
            context = call.args[0]
            if isinstance(context, ast.Constant) and isinstance(context.value, str):
                found[context.value] = node
    return found


class StructureEngineFailureVisibilityTests(unittest.TestCase):
    def test_both_silent_failure_paths_are_recorded(self):
        handlers = _failure_handlers()
        for context in _FATAL_CONTEXTS:
            self.assertIn(
                context,
                handlers,
                "no except handler records the %s failure any more" % context,
            )

    def test_each_silent_failure_path_surfaces_a_structure_warning(self):
        handlers = _failure_handlers()
        for context in _FATAL_CONTEXTS:
            handler = handlers[context]
            warnings = [
                node
                for node in ast.walk(handler)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "structural_warnings"
            ]
            self.assertTrue(
                warnings,
                "%s failure is still swallowed: no structural_warnings.append "
                "inside the except handler" % context,
            )
            # The warning has to name the structure model, otherwise the
            # delivery gate (which greps for 结构模型) cannot see it.
            text = ast.dump(handler)
            self.assertIn("结构模型", text, "%s warning must mention 结构模型" % context)

    def test_warning_text_is_user_readable(self):
        source = inspect.getsource(ocr_backend)
        self.assertIn("表格结构模型本次不可用", source)
        self.assertIn("请重点核对行列", source)


if __name__ == "__main__":
    unittest.main()
