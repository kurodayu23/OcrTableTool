# -*- coding: utf-8 -*-
"""识别守卫的**运行时接线**行为测试（2026-11-02 新增）。

为什么单独开这个文件
------------------
``backend/tests/test_backend.py`` 里有 13 处用 ``inspect.getsource(...)`` 取源码再
``assertIn("<某段文字>", source)`` 的断言。它们**看起来**像行为测试，实际只证明
"源码里还有这行字"：把守卫改成等价的另一种写法就红，改成**错的**实现反而可能绿。
这类断言冒充行为测试，会让人以为守卫被验证过了。

本模块给出对应的**真行为断言**：在真实的 ``_recognize`` 调用里用 spy 记录守卫
helper 到底有没有被调用、**由谁调用**（最近一帧产品代码的函数名），并断言可观察
的结果。断言"由 ``_recognize`` 直接调用"是关键 —— 它既能证明守卫在运行时真的
执行了，又能钉住它挂在哪条路径上，且不依赖源码里那行字的写法。

覆盖的守卫（对应 test_backend.py 里那批源码字符串断言）：

=========================================  ===========================================
源码字符串断言                             本模块的行为断言
=========================================  ===========================================
``_apply_structure_review_policy(``       结构复核策略**由 _recognize 直接调用**
``_align_structure_certificate_after_ui_strip``  剥标尺后证书真的被重对齐
``_unresolved_model_conflict_locations``   跨模型冲突扫描**由 _recognize 直接调用**
``_blocking_unresolved_visible_blank_locations``  墨迹空白格阻断扫描**由 _recognize 直接调用**
``_dense_numeric_sequence_risks``          数值序列风险扫描确实在识别流程里跑
``_engines()`` / ``_engines(load_table=True)``  表格引擎只在结构兜底时加载
=========================================  ===========================================

仍然只有静态守卫、**没有**行为断言的 6 处（无法在廉价场景里触达，改成行为断言
只会写出永远走不到的假断言）：
``0 < ruled_cell_count <= 320`` / ``len(output_texts) <= 200``、
``dense_grid_safe_rejection_reason``、``blocking_review_cells >= 8 and
blocking_review_ratio > 0.25``、``pending_review_locations``、
``图片较模糊，部分文字可能无法确认``、``表格或纸张边缘贴近画面边界``、
``线框、文字布局与结构模型结果不一致``。这些已在交付说明里登记为静态守卫。

跑法::

    <venv> -B -m unittest discover -s backend\\tests -t backend\\tests
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import ocr_backend  # noqa: E402
import table_pipeline  # noqa: E402
from table_pipeline import PROTOCOL_VERSION  # noqa: E402

_PRODUCT_FILE = os.path.normcase(os.path.abspath(ocr_backend.__file__))


def _product_caller() -> tuple[str, int]:
    """向上找最近一帧**产品代码**，返回 (函数名, 行号)。

    unittest.mock 的 side_effect 是被 mock 自己的派发器调用的，直接取
    ``sys._getframe(1)`` 会拿到 mock 内部帧（``_mock_call``）而不是产品代码。
    """
    frame = sys._getframe(1)
    while frame is not None:
        filename = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if filename == _PRODUCT_FILE:
            return frame.f_code.co_name, frame.f_lineno
        frame = frame.f_back
    return "<no-product-frame>", -1


def _screen_grid_image() -> tuple[np.ndarray, SimpleNamespace]:
    """一张最小屏幕截图网格：1 个识别框、1 个文本。"""
    image = np.full((60, 80, 3), 255, dtype=np.uint8)
    boxes = np.array([[[5, 5], [25, 5], [25, 20], [5, 20]]], dtype=float)
    return image, SimpleNamespace(boxes=boxes, txts=["A1"], scores=[0.99])


def _run_recognize(image: np.ndarray, patches: list) -> dict:
    """在给定的 mock 组合下跑一次 ``_recognize``，返回结果字典。"""
    with tempfile.TemporaryDirectory() as directory:
        image_path = Path(directory) / "probe.png"
        cv2.imencode(".png", image)[1].tofile(str(image_path))
        request = {
            "protocol": PROTOCOL_VERSION,
            "action": "recognize",
            "image_path": str(image_path),
            "output_directory": directory,
        }
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            return ocr_backend._recognize(request)


def _screen_grid_patches(image: np.ndarray, ocr_result, extra: list | None = None) -> list:
    """屏幕截图成功路径的标准 mock 组合（与 test_backend.py 里的写法一致）。"""
    patches = [
        patch.object(
            table_pipeline,
            "extract_screen_grid",
            return_value=([0, 40, 80], [0, 30, 60], image.copy()),
        ),
        patch.object(
            table_pipeline,
            "prepare_image",
            return_value=(image.copy(), {"detected": False, "mode": "auto"}),
        ),
        patch.object(ocr_backend, "_screen_grid_is_credible", return_value=True),
        patch.object(
            ocr_backend,
            "_engines",
            return_value=(lambda _candidate: ocr_result, None),
        ),
    ]
    patches.extend(extra or [])
    return patches


def _spy(name: str, calls: list):
    """把模块级 helper 换成"记录调用（调用者/行号/参数）后再执行原函数"的 spy。"""
    original = getattr(ocr_backend, name)

    def wrapper(*args, **kwargs):
        caller, lineno = _product_caller()
        calls.append({"caller": caller, "lineno": lineno, "args": args, "kwargs": kwargs})
        return original(*args, **kwargs)

    return patch.object(ocr_backend, name, side_effect=wrapper)


def _callers(calls: list) -> list[str]:
    return ["%s:L%d" % (call["caller"], call["lineno"]) for call in calls]


class RecognitionGuardWiringTest(unittest.TestCase):
    """守卫在真实识别流程里的接线（runtime spy，不是源码字符串匹配）。"""

    def setUp(self):
        ocr_backend._load_runtime()

    # --- 表格引擎：只在结构兜底时加载 ---------------------------------

    def test_table_engine_is_not_loaded_on_the_screen_grid_path(self):
        """屏幕截图直接出结果时，表格模型一次都不该加载。

        表格结构模型（slanet-plus）比页面 OCR 模型重得多；把它无条件加载会让
        轻量截图场景白白多付一份内存与首帧延迟。旧断言只能证明源码里
        ``_engines()`` 写在 ``_engines(load_table=True)`` 前面，证明不了
        "成功路径根本不会去加载它"。
        """
        image, ocr_result = _screen_grid_image()
        load_flags: list[bool] = []

        def engines_spy(load_table: bool = False):
            load_flags.append(load_table)
            return (lambda _candidate: ocr_result), None

        result = _run_recognize(
            image,
            _screen_grid_patches(
                image,
                ocr_result,
                extra=[patch.object(ocr_backend, "_engines", side_effect=engines_spy)],
            ),
        )

        self.assertEqual(result["rows"], 2, "识别结果本身应当正常（保证测的是成功路径）")
        self.assertTrue(load_flags, "没有记录到任何 _engines 调用，说明 mock 没生效")
        self.assertNotIn(
            True,
            load_flags,
            "成功路径也加载了表格结构模型（load_table=True）：%r" % (load_flags,),
        )

    def test_table_engine_is_loaded_only_after_the_fast_path_failed(self):
        """快路径（屏幕网格 / 线框网格）全部落空后，才允许去加载表格模型。"""
        image, ocr_result = _screen_grid_image()
        load_flags: list[bool] = []

        def engines_spy(load_table: bool = False):
            load_flags.append(load_table)
            return (lambda _candidate: ocr_result), None

        blank = np.full((60, 80, 3), 255, dtype=np.uint8)
        # 2026-11-07（lead，客户要求"绝不报错"）：快路径全部落空时不再抛 ValueError，
        # 而是返回一张退化的空表（status 仍 ok、全部单元格标黄）。本用例的正题是
        # **表格模型的加载顺序**，所以这里改成断言"退化结果"，顺序断言保持原样。
        degraded = _run_recognize(
            blank,
            [
                patch.object(table_pipeline, "extract_screen_grid", return_value=None),
                patch.object(
                    table_pipeline,
                    "prepare_image",
                    return_value=(blank.copy(), {"detected": False, "mode": "auto"}),
                ),
                patch.object(table_pipeline, "extract_ruled_grid", return_value=None),
                patch.object(ocr_backend, "_screen_grid_is_credible", return_value=False),
                patch.object(ocr_backend, "_engines", side_effect=engines_spy),
            ],
        )
        self.assertEqual(degraded["status"], "ok", "识别失败也必须交出 status=ok 的结果")
        self.assertTrue(degraded.get("degraded"), "兜底结果必须标记 degraded=True")
        self.assertTrue(degraded["publication_blocked"], "兜底结果必须阻断发布")

        self.assertIn(
            True,
            load_flags,
            "快路径落空后仍然没有加载表格模型，兜底路径没走通：%r" % (load_flags,),
        )
        self.assertLess(
            load_flags.index(False),
            load_flags.index(True),
            "表格模型在页面 OCR 引擎之前就被加载了（顺序反了）：%r" % (load_flags,),
        )

    # --- 结构复核策略 ---------------------------------------------------

    def test_structure_review_policy_is_applied_by_recognition_before_publication(self):
        """发布结果之前，结构复核策略必须**由 _recognize 直接调用**过。

        旧断言只检查源码里有 ``_apply_structure_review_policy(`` 这几个字；
        真问题（策略被挪到别的分支、参数传错、调用被跳过）它一条都抓不到。
        """
        image, ocr_result = _screen_grid_image()
        calls: list[dict] = []

        result = _run_recognize(
            image,
            _screen_grid_patches(
                image, ocr_result, extra=[_spy("_apply_structure_review_policy", calls)]
            ),
        )

        self.assertTrue(calls, "识别过程中完全没有调用 _apply_structure_review_policy")
        direct = [call for call in calls if call["caller"] == "_recognize"]
        self.assertTrue(
            direct,
            "结构复核策略不是由 _recognize 直接调用的（实际调用者：%s）"
            % ", ".join(_callers(calls)),
        )
        keywords = direct[0]["kwargs"]
        self.assertIn(
            "structure_verified",
            keywords,
            "调用没有显式传 structure_verified，无法判定复核策略是否按'已验证/未验证'分流：%r"
            % (sorted(keywords),),
        )
        self.assertIsInstance(
            keywords["structure_verified"],
            bool,
            "structure_verified 必须是布尔值，实际是 %r" % (keywords["structure_verified"],),
        )
        self.assertEqual(result["rows"], 2)

    # --- 证书重对齐 -----------------------------------------------------

    def test_certificate_is_realigned_after_spreadsheet_ui_strip(self):
        """剥掉电子表格标尺后，结构证书必须被重对齐（而不是留着旧尺寸）。"""
        image, ocr_result = _screen_grid_image()
        calls: list[dict] = []

        _run_recognize(
            image,
            _screen_grid_patches(
                image,
                ocr_result,
                extra=[_spy("_align_structure_certificate_after_ui_strip", calls)],
            ),
        )

        self.assertTrue(
            calls,
            "识别过程中没有调用 _align_structure_certificate_after_ui_strip —— "
            "剥标尺后证书尺寸可能仍与网格不符",
        )
        arguments = calls[0]["args"]
        self.assertGreaterEqual(
            len(arguments),
            3,
            "调用参数少于 3 个（certificate, before_shape, after_shape）：%r" % (arguments,),
        )
        certificate = arguments[0]
        self.assertIsInstance(certificate, dict, "第一个参数应当是证书字典")
        for key in ("rows", "columns"):
            self.assertIn(key, certificate, "证书里没有 %s 字段：%r" % (key, certificate))

    # --- 三条扫描器 -----------------------------------------------------

    def test_model_conflict_scanner_is_called_by_recognition(self):
        """跨模型冲突扫描必须由 _recognize 直接调用（冲突格不能带着负置信度发布）。"""
        image, ocr_result = _screen_grid_image()
        calls: list[dict] = []

        _run_recognize(
            image,
            _screen_grid_patches(
                image, ocr_result, extra=[_spy("_unresolved_model_conflict_locations", calls)]
            ),
        )

        self.assertTrue(calls, "识别过程中没有扫描跨模型冲突")
        self.assertTrue(
            [call for call in calls if call["caller"] == "_recognize"],
            "跨模型冲突扫描不是由 _recognize 直接调用的（实际调用者：%s）"
            % ", ".join(_callers(calls)),
        )

    def test_blocking_blank_ink_scanner_is_called_by_recognition(self):
        """'有墨迹但读不出字'的空白格扫描必须由 _recognize 直接调用（这类格要阻断发布）。"""
        image, ocr_result = _screen_grid_image()
        calls: list[dict] = []

        _run_recognize(
            image,
            _screen_grid_patches(
                image,
                ocr_result,
                extra=[_spy("_blocking_unresolved_visible_blank_locations", calls)],
            ),
        )

        self.assertTrue(calls, "识别过程中没有扫描'有墨迹但读不出字'的空白格")
        self.assertTrue(
            [call for call in calls if call["caller"] == "_recognize"],
            "空白格阻断扫描不是由 _recognize 直接调用的（实际调用者：%s）"
            % ", ".join(_callers(calls)),
        )

    def test_dense_numeric_sequence_scanner_runs_in_the_recognition_pipeline(self):
        """密集数值序列的错位风险扫描必须在识别流程里真的跑起来。"""
        image, ocr_result = _screen_grid_image()
        calls: list[dict] = []

        _run_recognize(
            image,
            _screen_grid_patches(
                image, ocr_result, extra=[_spy("_dense_numeric_sequence_risks", calls)]
            ),
        )

        self.assertTrue(
            calls,
            "识别过程中没有扫描密集数值序列的错位风险 —— 小数位漂移会被当成正常数字发布",
        )
        self.assertTrue(
            [call for call in calls if call["caller"] != "<no-product-frame>"],
            "风险扫描的调用者不在产品代码里（调用链异常）：%s" % ", ".join(_callers(calls)),
        )


if __name__ == "__main__":
    unittest.main()
