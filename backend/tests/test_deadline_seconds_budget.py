# -*- coding: utf-8 -*-
"""请求级 ``options.deadline_seconds`` 必须真的参与中止判定（2026-10-23 修）。

缺陷（findings 103 / FINAL-REPORT §六·E P1-1）：客户传
``options.deadline_seconds = 5`` 实测仍跑满 ~58 秒。那个 5 秒只进了响应字段
``deadline_seconds``，以及几何穷举的 70% 收尾线；而**真正会把请求打停的两个
检查点**（``_enforce_request_budget`` 的模型入口判定与
``_check_request_wall_clock`` 的解码/几何/结构阶段判定）一律硬比较
``_MAX_REQUEST_SECONDS``（60 秒），请求级 deadline 完全没参与。

修法：判定线统一取 ``_effective_request_limit_seconds()`` =
``min(60 秒墙钟契约, 本次请求预算)``。**只收不放**：``deadline_seconds <= 0``
（GUI 正式集成固定传 0）解析成 120 秒 → min 后仍是 60 秒，60 秒墙钟契约、
错误码枚举、``retryable`` 语义、客户可见文案全部不变。

这里用行为测试锁住语义，不依赖源码排版，也不加载任何识别模型。
"""
from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

# 让 `python -m unittest discover -s backend/tests` 在任意 cwd 下都能 import 后端
_BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import ocr_backend  # noqa: E402


# 预算解析发生在图片存在性检查之前（ocr_backend.py: `raw_deadline = options...`
# 在 `image_path.is_file()` 之前），所以用一个肯定不存在的路径就能走完
# **真实的**解析链路而不付解码/建模/推理的代价。
MISSING_IMAGE = str(Path(tempfile.gettempdir()) / "ocr-table-tool" / "no-such-file.png")

RATE_MESSAGE_WORDING = (
    "这张图的表格结构过于复杂，逐格复核超过 60 秒仍未完成，"
    "已中止以免长时间无响应。请用「框选区域」只框住表格本体"
    "（去掉桌面背景、条纹或其他网格），或裁剪掉图片周围的干扰内容后重新识别。"
)


def request_budget_for(raw_deadline):
    """走真实 ``_recognize`` 链路拿到本次请求的预算秒数。"""
    try:
        ocr_backend._recognize({
            "protocol": 1,
            "action": "recognize",
            "request_id": 1,
            "image_path": MISSING_IMAGE,
            "output_directory": str(Path(tempfile.gettempdir()) / "ocr-table-tool"),
            "options": {"accuracy_mode": "maximum", "deadline_seconds": raw_deadline},
        })
    except ValueError as error:
        assert "image does not exist" in str(error), str(error)
    else:  # pragma: no cover - 路径不存在，必定抛错
        raise AssertionError("_recognize 没有因图片不存在而失败")
    return ocr_backend._request_budget_seconds


class DeadlineSecondsBudgetTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "_request_active": ocr_backend._request_active,
            "_request_started_at": ocr_backend._request_started_at,
            "_request_budget_seconds": ocr_backend._request_budget_seconds,
            "_budget_enabled": ocr_backend._budget_enabled,
            "_fine_grained_seconds": ocr_backend._fine_grained_seconds,
            "_fine_grained_images": ocr_backend._fine_grained_images,
        }

    def tearDown(self):
        ocr_backend._end_request_budget()
        for name, value in self._saved.items():
            setattr(ocr_backend, name, value)

    # --- 工具 ---------------------------------------------------------

    def _enter_request(self, budget_seconds=None):
        """模拟请求已开始；budget_seconds 对应真实链路里 ``_recognize`` 的赋值。"""
        ocr_backend._begin_request_budget()
        if budget_seconds is not None:
            ocr_backend._request_budget_seconds = budget_seconds

    def _age(self, seconds):
        """把请求起始戳往回拨 seconds 秒（等价于墙钟已经走了这么久）。"""
        ocr_backend._request_started_at = ocr_backend.time.perf_counter() - seconds

    def _wall_clock_raises(self, seconds):
        self._enter_request(self._budget)
        self._age(seconds)
        try:
            ocr_backend._check_request_wall_clock("image_decode")
            return None
        except ocr_backend._FineGrainedBudgetExceeded as error:
            return error

    def _model_entry_raises(self, seconds):
        self._enter_request(self._budget)
        self._age(seconds)
        try:
            ocr_backend._enforce_request_budget("v6-small", 32, True)
            return None
        except ocr_backend._FineGrainedBudgetExceeded as error:
            return error

    # --- 小预算必须提前中止 --------------------------------------------

    def test_small_deadline_is_honoured_by_wall_clock_checkpoint(self):
        self._budget = request_budget_for(5)
        self.assertEqual(self._budget, 5.0)
        self.assertEqual(ocr_backend._effective_request_limit_seconds(), 5.0)

        self.assertIsNone(self._wall_clock_raises(4.0), "未到 5 秒不该中止")
        error = self._wall_clock_raises(5.2)
        self.assertIsNotNone(error, "5 秒预算在 5.2 秒时仍然放行 —— 缺陷未修")
        self.assertIn("识别已用 5 秒仍未完成", str(error))
        # 修复前的表现：58 秒也不中止。这里连 20 秒都必须已经中止。
        self.assertIsNotNone(self._wall_clock_raises(20.0))
        self.assertIsNotNone(self._wall_clock_raises(58.0))

    def test_small_deadline_is_honoured_at_model_entry(self):
        self._budget = request_budget_for(5)
        self.assertIsNone(self._model_entry_raises(2.0), "未到 5 秒不该中止")
        error = self._model_entry_raises(5.2)
        self.assertIsNotNone(error, "5 秒预算在模型入口没有生效")
        # 前瞻估算仍按本次预算收口：一批 32 张约 2.6 秒，4 秒起步就该拒
        self.assertIsNotNone(self._model_entry_raises(4.0))

    def test_deadline_below_five_seconds_is_clamped_to_five(self):
        # 既有行为：显式正数预算的下限是 5 秒（`max(5.0, requested_deadline)`），
        # 修 deadline 生效不许把这条下限顺手改掉。
        self._budget = request_budget_for(3)
        self.assertEqual(self._budget, 5.0)
        self.assertEqual(ocr_backend._effective_request_limit_seconds(), 5.0)

    # --- 0 / 负数：不额外收紧 -------------------------------------------

    def test_zero_and_negative_deadline_keep_sixty_second_contract(self):
        for raw in (0, -1, -100, 0.0, -0.5):
            with self.subTest(raw=raw):
                self._budget = request_budget_for(raw)
                # 预算本身仍是 120 秒硬上限（"不额外收紧"的原语义）
                self.assertEqual(
                    self._budget, ocr_backend._HARD_RECOGNITION_CEILING_SECONDS
                )
                # 但判定线仍是 60 秒墙钟契约，一秒都不放宽
                self.assertEqual(
                    ocr_backend._effective_request_limit_seconds(),
                    ocr_backend._MAX_REQUEST_SECONDS,
                )
                self.assertIsNone(self._wall_clock_raises(59.0))
                self.assertIsNotNone(self._wall_clock_raises(61.0))
                self.assertIsNone(self._model_entry_raises(50.0))
                self.assertIsNotNone(self._model_entry_raises(61.0))

    # --- 非法值：行为不变（F-07 已定口径）-------------------------------

    def test_non_numeric_deadline_falls_back_to_sixty_and_behaviour_unchanged(self):
        for raw in ("abc", None, "", [5], {"value": 5}):
            with self.subTest(raw=raw):
                self._budget = request_budget_for(raw)
                self.assertEqual(self._budget, ocr_backend._MAX_REQUEST_SECONDS)
                self.assertEqual(
                    ocr_backend._effective_request_limit_seconds(),
                    ocr_backend._MAX_REQUEST_SECONDS,
                )
                self.assertIsNone(self._wall_clock_raises(59.0))
                self.assertIsNotNone(self._wall_clock_raises(61.0))

    def test_non_finite_deadline_stays_at_five_seconds_budget(self):
        # F-07 定的口径：非法值一律**不许比原逻辑更宽松**。NaN 原本落在 5 秒
        # 预算上，修复后这 5 秒会真的参与中止判定（收紧方向，不是放宽）。
        for raw in (float("nan"),):
            with self.subTest(raw=raw):
                self._budget = request_budget_for(raw)
                self.assertEqual(self._budget, 5.0)
                self.assertEqual(ocr_backend._effective_request_limit_seconds(), 5.0)

    # --- 判据口径一致 ---------------------------------------------------

    def test_geometry_truncation_line_shares_the_same_limit(self):
        self._budget = request_budget_for(5)
        self._enter_request(self._budget)
        self._age(3.4)  # 5 × 0.70 = 3.5
        self.assertFalse(ocr_backend._geometry_search_budget_exhausted())
        self._age(3.6)
        self.assertTrue(ocr_backend._geometry_search_budget_exhausted())

        self._budget = request_budget_for(0)  # 判定线 60 秒 → 42 秒
        self._enter_request(self._budget)
        self._age(41.0)
        self.assertFalse(ocr_backend._geometry_search_budget_exhausted())
        self._age(43.0)
        self.assertTrue(ocr_backend._geometry_search_budget_exhausted())

    # --- 错误码 / 文案 / retryable 不变 ---------------------------------

    def test_abort_keeps_error_code_message_and_retryable(self):
        self._budget = request_budget_for(5)
        error = self._wall_clock_raises(5.2)
        self.assertIsNotNone(error)
        self.assertEqual(
            ocr_backend._classify_error(error),
            ("RECOGNITION_BUDGET_EXCEEDED", None, True),
        )
        response = ocr_backend._error_response(error, {"action": "recognize"})
        self.assertEqual(response["error_code"], "RECOGNITION_BUDGET_EXCEEDED")
        self.assertTrue(response["retryable"])
        # 客户可见文案与 60 秒契约下的中止**逐字一致**（本轮不改文案，见报告）
        self.assertEqual(response["message"], RATE_MESSAGE_WORDING)

    def test_image_volume_branch_message_unchanged(self):
        # 图片量闸门在墙钟之后、且与 deadline 无关：保持原样。
        self._enter_request(ocr_backend._MAX_REQUEST_SECONDS)
        ocr_backend._fine_grained_images = ocr_backend._MAX_FINE_GRAINED_IMAGES + 1
        self._age(1.0)
        with self.assertRaises(ocr_backend._FineGrainedBudgetExceeded) as caught:
            ocr_backend._enforce_request_budget("v6-small", 32, True)
        error = caught.exception
        self.assertIn("累计送识别的图片", str(error))
        response = ocr_backend._error_response(error, {"action": "recognize"})
        self.assertIn("网格切分异常", response["message"])
        self.assertEqual(
            ocr_backend._classify_error(error),
            ("RECOGNITION_BUDGET_EXCEEDED", None, True),
        )

    # --- 请求之外不受影响 ------------------------------------------------

    def test_checkpoints_stay_inert_outside_a_request(self):
        self._enter_request(5.0)
        self._age(30.0)
        ocr_backend._end_request_budget()
        # 请求结束后没有活跃请求：检查点不该拿上一次的起始戳算账
        ocr_backend._check_request_wall_clock("image_decode")
        ocr_backend._enforce_request_budget("v6-small", 32, True)
        self.assertFalse(ocr_backend._geometry_search_budget_exhausted())

    def test_budget_disabled_switch_still_bypasses_the_checkpoints(self):
        # OCRTABLE_BUDGET_DISABLED=1 的批量验收开关语义不变
        self._budget = request_budget_for(5)
        ocr_backend._budget_enabled = False
        self.assertIsNone(self._wall_clock_raises(58.0))
        self.assertFalse(ocr_backend._geometry_search_budget_exhausted())


if __name__ == "__main__":
    unittest.main(verbosity=2)
