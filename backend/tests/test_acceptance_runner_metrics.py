# -*- coding: utf-8 -*-
"""验收统计工具：空网格口径（F-01）与时间戳/档位记录（F-03）的回归测试。

2026-10-23 修的两处验收工具链缺陷（详见
``ai-workspace/overnight/findings/08-acceptance-metrics.md``）：

1. **F-01**：一次识别**没有识别出任何单元格**（空网格）时，runner 把它记成
   ``status="ok"``；又因为 ``cell_exact_rate=None`` 把它从平均准确率的分母里
   剔掉 → 成功率、均准确率**同时偏高**，正好藏住"识别结果为空"这类最严重的
   缺陷。现在空结果 = 失败（``status="empty_result"``），并按 **0 分**计入
   分母；汇总里空结果数**单列可见**。
2. **F-03**：验收记录**没有时间戳**，事后无法判断这份验收是什么时候跑的、
   跑的是哪一档。现在每条记录带 ISO-8601 时间戳 + 当次请求**实际生效**的
   内存档位（后端在请求开始时选定的 ``runtime_profile.memory_mode``），并
   落一份"本次运行的环境指纹"。

这里用行为测试锁住语义（不依赖源码排版），不跑真实识别：runner 依赖的
``ocr_backend`` 两个入口（``_load_runtime`` / ``handle_request``）用假模块
替掉，识别结果由用例直接给定。
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

# 2026-10-28：原来这里写死 `D:\AP\AP7000\OcrTableTool`（产品树镜像）。后果不只是
# "测错文件"：本模块是 unittest discover 里**第一个**被加载的测试模块，它 import 的
# batch_acceptance_runner 会 `sys.path.insert(backend)` 再 `import ocr_backend`，
# 于是**产品树镜像**被塞进 sys.modules，后续所有测试（含 test_backend）都在测镜像
# 而不是被测工作树；还连带让 test_backend 里 4 个"起子进程跑后端"的用例因路径身份
# 不匹配被离线钩子拦下（表现为只在全量 discover 时出现的 4 个 ERROR）。
# 改成从本文件位置推导仓库根 ⇒ 一律指向被测树。
PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts" / "batch_acceptance_runner.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("batch_acceptance_runner_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runner = _load_module()


def _response(rows, memory_mode="normal", threads=4):
    """造一个后端识别结果（cells 是逐格 dict，和真实后端一致）。"""
    cells = [[{"text": text} for text in row] for row in rows]
    return {
        "cells": cells,
        "engine": "stub-engine",
        "rows": len(rows),
        "columns": max((len(row) for row in rows), default=0),
        "runtime_profile": {
            "memory_mode": memory_mode,
            "inference_threads": threads,
        },
    }


class _StubBackend:
    """只实现 runner 用到的两个入口的假后端。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def _load_runtime(self):
        return None

    def handle_request(self, payload):
        self.calls.append(payload)
        case_id = Path(payload["image_path"]).stem
        response = self.responses[case_id]
        if isinstance(response, Exception):
            raise response
        return response


class _RunnerHarness(unittest.TestCase):
    """跑一遍真实 main()，返回 (退出码, stdout, records, report)。"""

    def _run(self, responses, truths):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            output = root / "out"
            (corpus / "images").mkdir(parents=True)
            (corpus / "_truth").mkdir(parents=True)
            for case_id, grid in truths.items():
                (corpus / "images" / (case_id + ".png")).write_bytes(b"\x89PNG\r\n\x1a\n")
                (corpus / "_truth" / (case_id + ".json")).write_text(
                    json.dumps({"cells": grid}, ensure_ascii=False), encoding="utf-8"
                )

            stub = _StubBackend(responses)
            saved_backend = runner.ocr_backend
            saved_argv = sys.argv
            stdout = io.StringIO()
            runner.ocr_backend = stub
            sys.argv = [
                "batch_acceptance_runner.py",
                "--corpus",
                str(corpus),
                "--output",
                str(output),
                "--skip-models",
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    code = runner.main()
            finally:
                sys.argv = saved_argv
                runner.ocr_backend = saved_backend

            records = json.loads((output / "records.json").read_text(encoding="utf-8"))
            report = json.loads(
                (output / "acceptance-report.json").read_text(encoding="utf-8")
            )
            environment_file = output / "run-environment.json"
            environment = (
                json.loads(environment_file.read_text(encoding="utf-8"))
                if environment_file.is_file()
                else None
            )
            return code, stdout.getvalue(), records, report, environment

    @staticmethod
    def _by_id(records):
        return {record["id"]: record for record in records}


class EmptyGridAccountingTests(_RunnerHarness):
    """F-01：空网格必须是失败，且必须进准确率分母、还要看得见。"""

    def test_empty_grid_is_a_failure_not_a_success(self):
        code, _stdout, records, report, _env = self._run(
            responses={
                "FC-OK": _response([["1", "2"], ["3", "4"]]),
                "FC-EMPTY": _response([]),
            },
            truths={
                "FC-OK": [["1", "2"], ["3", "4"]],
                "FC-EMPTY": [["1", "2"], ["3", "4"]],
            },
        )
        records_by_id = self._by_id(records)
        empty = records_by_id["FC-EMPTY"]

        self.assertEqual(empty["status"], "empty_result", "空网格绝不能记成 ok")
        self.assertNotEqual(empty["status"], "ok")
        self.assertTrue(empty["empty_result"])
        self.assertEqual(empty["recognised_cells"], 0)
        self.assertTrue(empty["failure_reason"])

        self.assertEqual(report["succeeded"], 1, "空结果不能算成功")
        self.assertEqual(report["failed"], 1, "空结果必须算失败")
        self.assertEqual(report["empty_results"], 1, "空结果数必须单列可见")
        self.assertEqual(report["status"], "failed")
        self.assertNotEqual(code, 0, "有空结果时退出码必须非 0")

    def test_empty_grid_stays_in_the_accuracy_denominator(self):
        """空结果按 0 分计入分母 —— 不能既不算失败、又不进任何分母。"""
        _code, _stdout, _records, report, _env = self._run(
            responses={
                "FC-OK": _response([["1", "2"], ["3", "4"]]),
                "FC-EMPTY": _response([]),
            },
            truths={
                "FC-OK": [["1", "2"], ["3", "4"]],
                "FC-EMPTY": [["1", "2"], ["3", "4"]],
            },
        )
        self.assertEqual(report["scored_cases"], 2, "分母必须包含空结果")
        self.assertEqual(
            report["mean_cell_exact_rate"], 0.5, "空结果必须按 0 分拉低平均准确率"
        )

    def test_empty_result_is_visible_in_stdout_summary(self):
        _code, stdout, _records, _report, _env = self._run(
            responses={
                "FC-OK": _response([["1"]]),
                "FC-EMPTY": _response([]),
            },
            truths={"FC-OK": [["1"]], "FC-EMPTY": [["1"]]},
        )
        self.assertIn("empty_results", stdout, "stdout 汇总里必须有 empty_results")
        summary_lines = [
            line
            for line in stdout.splitlines()
            if line.strip().startswith("{") and "empty_results" in line
        ]
        self.assertTrue(summary_lines, "stdout 必须有一行带 empty_results 的 JSON 汇总")
        summary = json.loads(summary_lines[-1])
        self.assertEqual(summary["empty_results"], 1)
        self.assertEqual(summary["failed"], 1)

    def test_empty_grid_summary_count_only(self):
        """全是空结果时：成功 0、失败 N、平均分 0、分母 N。"""
        _code, _stdout, _records, report, _env = self._run(
            responses={"FC-E1": _response([]), "FC-E2": _response([])},
            truths={"FC-E1": [["1"]], "FC-E2": [["1"]]},
        )
        self.assertEqual(report["succeeded"], 0)
        self.assertEqual(report["failed"], 2)
        self.assertEqual(report["empty_results"], 2)
        self.assertEqual(report["scored_cases"], 2)
        self.assertEqual(report["mean_cell_exact_rate"], 0.0)


class ExistingStatisticsTests(_RunnerHarness):
    """正常用例的既有统计口径不能被改坏。"""

    def test_normal_cases_keep_their_old_statistics(self):
        code, _stdout, records, report, _env = self._run(
            responses={
                "FC-A": _response([["1", "2"], ["3", "4"]]),
                "FC-B": _response([["1", "2"], ["3", "x"]]),
            },
            truths={
                "FC-A": [["1", "2"], ["3", "4"]],
                "FC-B": [["1", "2"], ["3", "4"]],
            },
        )
        records_by_id = self._by_id(records)
        self.assertEqual(records_by_id["FC-A"]["status"], "ok")
        self.assertEqual(records_by_id["FC-B"]["status"], "ok")
        self.assertEqual(records_by_id["FC-A"]["cell_exact_rate"], 1.0)
        self.assertEqual(records_by_id["FC-B"]["cell_exact_rate"], 0.75)
        self.assertTrue(records_by_id["FC-A"]["shape_match"])
        # shape_match 只看行列数：FC-B 形状对但内容错一格 → 形状仍然匹配。
        self.assertTrue(records_by_id["FC-B"]["shape_match"])

        self.assertEqual(report["cases"], 2)
        self.assertEqual(report["succeeded"], 2)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["empty_results"], 0)
        self.assertEqual(report["shape_matches"], 2)
        self.assertEqual(report["scored_cases"], 2)
        self.assertEqual(report["mean_cell_exact_rate"], 0.875)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(code, 0, "全部成功时退出码必须是 0")

    def test_crash_case_still_counts_as_failure_without_a_score(self):
        """真正报错（崩溃/超时）没有分数，不进分母，但数量可见。"""
        _code, _stdout, records, report, _env = self._run(
            responses={
                "FC-OK": _response([["1"]]),
                "FC-CRASH": RuntimeError("boom"),
            },
            truths={"FC-OK": [["1"]], "FC-CRASH": [["1"]]},
        )
        records_by_id = self._by_id(records)
        self.assertEqual(records_by_id["FC-CRASH"]["status"], "error")
        self.assertIn("boom", records_by_id["FC-CRASH"]["error"])
        self.assertEqual(report["error_cases"], 1)
        self.assertEqual(report["succeeded"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["scored_cases"], 1)
        self.assertEqual(report["mean_cell_exact_rate"], 1.0)

    def test_compare_scoring_is_untouched(self):
        """compare() 的逐格计分口径没被改（1 格错 = 3/4）。"""
        result = runner.compare([["1", "2"], ["3", "4"]], [["1", "2"], ["3", "x"]])
        self.assertEqual(result["total_cells"], 4)
        self.assertEqual(result["exact_cells"], 3)
        self.assertEqual(result["cell_exact_rate"], 0.75)


class TimestampAndTierTests(_RunnerHarness):
    """F-03：记录要有时间戳和当次实际生效的档位。"""

    def test_every_record_carries_an_iso_timestamp(self):
        _code, _stdout, records, _report, _env = self._run(
            responses={"FC-A": _response([["1"]], memory_mode="low_memory")},
            truths={"FC-A": [["1"]]},
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        for field in ("timestamp", "started_at"):
            self.assertIn(field, record, "记录必须带 %s" % field)
            parsed = datetime.datetime.fromisoformat(record[field])
            self.assertIsNotNone(
                parsed.tzinfo, "%s 必须带时区偏移" % field
            )

    def test_record_carries_the_memory_mode_of_the_request(self):
        """档位取后端在**请求开始时**选定的值（runtime_profile 回传）。"""
        _code, _stdout, records, report, _env = self._run(
            responses={
                "FC-A": _response([["1"]], memory_mode="normal"),
                "FC-B": _response([["1"]], memory_mode="low_memory"),
            },
            truths={"FC-A": [["1"]], "FC-B": [["1"]]},
        )
        records_by_id = self._by_id(records)
        self.assertEqual(records_by_id["FC-A"]["memory_mode"], "normal")
        self.assertEqual(records_by_id["FC-B"]["memory_mode"], "low_memory")
        self.assertEqual(report["environment"]["memory_mode"], "mixed")
        self.assertEqual(
            sorted(report["environment"]["memory_modes_observed"]),
            ["low_memory", "normal"],
        )

    def test_environment_fingerprint_block(self):
        _code, _stdout, _records, report, environment = self._run(
            responses={"FC-A": _response([["1"]], memory_mode="low_memory")},
            truths={"FC-A": [["1"]]},
        )
        env = report["environment"]
        for field in (
            "started_at",
            "finished_at",
            "memory_mode",
            "low_memory_threshold_env",
            "python_version",
            "platform",
        ):
            self.assertIn(field, env, "环境指纹必须有 %s" % field)
        self.assertEqual(env["memory_mode"], "low_memory")
        self.assertTrue(env["started_at"])
        self.assertTrue(env["python_version"])
        self.assertTrue(env["platform"])
        self.assertIsNotNone(
            environment, "必须单独落一份 run-environment.json"
        )
        self.assertEqual(environment["memory_mode"], "low_memory")

    def test_environment_records_the_backend_source_fingerprint(self):
        """F-07：续跑时每轮可能来自不同代码状态，必须能追溯。

        环境指纹里要带后端入口源码的 SHA-256 与修改时间 —— 否则跨轮续跑后，
        事后只看 records.json 分不清某条记录是哪份代码产出的。
        """
        _code, _stdout, _records, report, environment = self._run(
            responses={"FC-A": _response([["1"]], memory_mode="normal")},
            truths={"FC-A": [["1"]]},
        )
        env = report["environment"]
        for field in (
            "backend_source",
            "backend_source_sha256",
            "backend_source_mtime",
        ):
            self.assertIn(field, env, "环境指纹必须有 %s" % field)
        self.assertEqual(len(env["backend_source_sha256"]), 64)
        self.assertTrue(env["backend_source_mtime"])
        self.assertEqual(
            env["backend_source_sha256"],
            environment["backend_source_sha256"],
            "run-environment.json 必须和报告里的指纹一致",
        )


if __name__ == "__main__":
    unittest.main()
