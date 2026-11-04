#!/usr/bin/env python
"""Batch acceptance runner for the OcrTableTool backend.

Two jobs, in this order:

1.  Model fingerprint gate.  Every portable model the backend declares in
    ocr_backend.MODEL_HASHES must exist in the model directory and match its
    recorded SHA-256.  The backend calls validate_model_files() before it
    builds an engine, so a missing or damaged model is a hard start-up
    failure on the tablet, not a quality regression.  Catching it here turns
    a field failure into a build-time failure.

2.  Corpus acceptance.  Every case is pushed through handle_request() and the
    recognised grid is compared cell by cell against the recorded truth.

Usage:
    python batch_acceptance_runner.py --corpus <dir> --output <dir>
        [--ids FC-00001,FC-00002] [--limit N] [--skip-models]

The corpus directory is expected to hold ``images/<case>.png`` and
``_truth/<case>.json`` (the field corpus layout).  A directory with
``manifest.csv`` is also accepted, in which case the manifest drives the
case list.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import gc
import hashlib
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import ocr_backend  # noqa: E402


# The portable model set the production backend depends on.  Keep this list
# byte-for-byte in sync with ocr_backend.MODEL_HASHES: the packaging script
# stages exactly these files next to the executable, and the backend refuses
# to start when any of them is missing.
MODEL_NAMES = (
    "PP-OCRv6_det_medium.bin",
    "PP-OCRv6_det_medium.xml",
    "PP-OCRv6_det_small.onnx",
    "PP-OCRv6_rec_medium.bin",
    "PP-OCRv6_rec_medium.xml",
    "PP-OCRv6_rec_small.bin",
    "PP-OCRv6_rec_small.xml",
    "ch_PP-OCRv5_rec_mobile.onnx",
    "ch_PP-OCRv5_rec_server.onnx",
    "ch_ppocr_mobile_v2.0_cls_mobile.bin",
    "ch_ppocr_mobile_v2.0_cls_mobile.xml",
    "en_PP-OCRv5_rec_mobile.onnx",
    "latin_PP-OCRv5_dict.txt",
    "latin_PP-OCRv5_rec_mobile.onnx",
    "slanet-plus.onnx",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_models(model_dir: Path) -> dict[str, Any]:
    """Check every declared model against its recorded fingerprint."""
    problems: list[str] = []
    checked = 0
    for name in sorted(MODEL_NAMES):
        path = model_dir / name
        if not path.is_file():
            problems.append("missing: %s" % name)
            continue
        expected = ocr_backend.MODEL_HASHES.get(name)
        if expected is None:
            problems.append("not declared in MODEL_HASHES: %s" % name)
            continue
        actual = sha256_of(path)
        if actual != expected:
            problems.append("fingerprint mismatch: %s" % name)
            continue
        checked += 1
    declared = set(ocr_backend.MODEL_HASHES)
    listed = set(MODEL_NAMES)
    for name in sorted(declared - listed):
        problems.append("declared by backend but not staged: %s" % name)
    return {"checked": checked, "expected": len(MODEL_NAMES), "problems": problems}


def load_cases(corpus: Path, ids: set[str], limit: int) -> list[dict[str, str]]:
    manifest = corpus / "manifest.csv"
    if manifest.is_file():
        with manifest.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        cases = [
            {
                "id": row["id"],
                "image": str(corpus / row["image_path"]),
                "truth": str(corpus / row["ground_truth_path"]),
            }
            for row in rows
        ]
    else:
        cases = []
        truth_dir = corpus / "_truth"
        image_dir = corpus / "images"
        for truth_path in sorted(truth_dir.glob("*.json")):
            case_id = truth_path.stem
            image_path = image_dir / (case_id + ".png")
            if not image_path.is_file():
                continue
            cases.append(
                {"id": case_id, "image": str(image_path), "truth": str(truth_path)}
            )
    if ids:
        cases = [case for case in cases if case["id"] in ids]
    if limit:
        cases = cases[:limit]
    return cases


def cell_text_grid(result: dict[str, Any]) -> list[list[str]]:
    grid: list[list[str]] = []
    for row in result.get("cells", []):
        line: list[str] = []
        for cell in row:
            if isinstance(cell, dict):
                line.append(str(cell.get("text", "")))
            else:
                line.append(str(cell))
        grid.append(line)
    return grid


def truth_grid(truth: dict[str, Any]) -> list[list[str]]:
    cells = truth.get("cells")
    if not isinstance(cells, list):
        return []
    grid: list[list[str]] = []
    for row in cells:
        if isinstance(row, list):
            grid.append([("" if value is None else str(value)) for value in row])
        else:
            grid.append([str(row)])
    return grid


def _aligned_rates(
    expected: list[list[str]],
    actual: list[list[str]],
    row_offset: int,
    column_offset: int,
) -> tuple[int, int, int, int]:
    """按给定行列偏移对齐后统计（完全一致格数 / 总格数 / 非空真值格 / 非空命中）。"""
    truth_rows = len(expected)
    truth_columns = max((len(row) for row in expected), default=0)
    actual_rows = len(actual)
    actual_columns = max((len(row) for row in actual), default=0)
    first_row = max(0, -row_offset)
    last_row = min(truth_rows, actual_rows - row_offset)
    first_column = max(0, -column_offset)
    last_column = min(truth_columns, actual_columns - column_offset)
    total = 0
    exact = 0
    nonempty_total = 0
    nonempty_exact = 0
    for truth_row_index in range(first_row, last_row):
        actual_row_index = truth_row_index + row_offset
        expected_row = expected[truth_row_index]
        actual_row = actual[actual_row_index]
        for truth_column_index in range(first_column, last_column):
            actual_column_index = truth_column_index + column_offset
            want = (
                expected_row[truth_column_index]
                if truth_column_index < len(expected_row)
                else ""
            ).strip()
            got = (
                actual_row[actual_column_index]
                if actual_column_index < len(actual_row)
                else ""
            ).strip()
            total += 1
            if want == got:
                exact += 1
            if want:
                nonempty_total += 1
                if want == got:
                    nonempty_exact += 1
    return total, exact, nonempty_total, nonempty_exact


def best_alignment(
    expected: list[list[str]],
    actual: list[list[str]],
    max_row_offset: int = 3,
    max_column_offset: int = 2,
) -> dict[str, Any]:
    """搜索最佳行列对齐。

    2026-10-22 修正：原来计分器按索引硬对齐，**任何 ±1 行偏移一律记 0.0000**。
    FC-00003 输出相对真值整体上移一行，正确偏移下逐格一致率是 0.6806
    （98/144），却被记成 0.0 —— "内容 68% 正确但整体错行"和"完全没识别出来"
    在报告里长得一模一样，缺陷分级被彻底扭曲，排查会被误导到识别质量
    而不是几何。

    这里**不改变** cell_exact_rate 的口径（仍然是索引硬对齐，代表实际交付
    质量），只额外给出"最佳对齐下的内容正确率"和偏移量，让错行能被分级。
    """
    best: dict[str, Any] = {
        "aligned_cell_exact_rate": None,
        "aligned_nonempty_exact_rate": None,
        "aligned_row_offset": 0,
        "aligned_column_offset": 0,
        "aligned_total_cells": 0,
        "aligned_exact_cells": 0,
    }
    if not expected or not actual:
        return best
    best_rate = -1.0
    for row_offset in range(-max_row_offset, max_row_offset + 1):
        for column_offset in range(-max_column_offset, max_column_offset + 1):
            total, exact, nonempty_total, nonempty_exact = _aligned_rates(
                expected, actual, row_offset, column_offset
            )
            if total <= 0:
                continue
            rate = exact / total
            if rate > best_rate:
                best_rate = rate
                best.update({
                    "aligned_cell_exact_rate": round(rate, 4),
                    "aligned_nonempty_exact_rate": (
                        round(nonempty_exact / nonempty_total, 4)
                        if nonempty_total
                        else None
                    ),
                    "aligned_row_offset": row_offset,
                    "aligned_column_offset": column_offset,
                    "aligned_total_cells": total,
                    "aligned_exact_cells": exact,
                })
    return best


def compare(expected: list[list[str]], actual: list[list[str]]) -> dict[str, Any]:
    """Cell-by-cell agreement on the overlapping rectangle."""
    if not expected:
        return {
            "truth_rows": 0,
            "truth_columns": 0,
            "actual_rows": len(actual),
            "actual_columns": max((len(row) for row in actual), default=0),
            "shape_match": False,
            "total_cells": 0,
            "exact_cells": 0,
            "cell_exact_rate": None,
            "nonempty_truth_cells": 0,
            "nonempty_exact_cells": 0,
            "nonempty_exact_rate": None,
            **best_alignment(expected, actual),
        }
    truth_rows = len(expected)
    truth_columns = max(len(row) for row in expected)
    actual_rows = len(actual)
    actual_columns = max((len(row) for row in actual), default=0)
    overlap_rows = min(truth_rows, actual_rows)
    overlap_columns = min(truth_columns, actual_columns)
    total = 0
    exact = 0
    nonempty_total = 0
    nonempty_exact = 0
    for row_index in range(overlap_rows):
        expected_row = expected[row_index]
        actual_row = actual[row_index] if row_index < actual_rows else []
        for column_index in range(overlap_columns):
            want = expected_row[column_index] if column_index < len(expected_row) else ""
            got = actual_row[column_index] if column_index < len(actual_row) else ""
            want = want.strip()
            got = got.strip()
            total += 1
            if want == got:
                exact += 1
            if want:
                nonempty_total += 1
                if want == got:
                    nonempty_exact += 1
    return {
        "truth_rows": truth_rows,
        "truth_columns": truth_columns,
        "actual_rows": actual_rows,
        "actual_columns": actual_columns,
        "shape_match": truth_rows == actual_rows and truth_columns == actual_columns,
        "total_cells": total,
        "exact_cells": exact,
        "cell_exact_rate": round(exact / total, 4) if total else None,
        "nonempty_truth_cells": nonempty_total,
        "nonempty_exact_cells": nonempty_exact,
        "nonempty_exact_rate": round(nonempty_exact / nonempty_total, 4)
        if nonempty_total
        else None,
        **best_alignment(expected, actual),
    }


def iso_timestamp() -> str:
    """带本地时区偏移的 ISO-8601 时间戳（如 2026-10-23T00:35:12+08:00）。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def classify_case(record: dict[str, Any], actual: list[list[str]]) -> None:
    """给一条验收记录定状态。

    2026-10-23（08 号文件 F-01）：原来这里**无条件**写 ``status="ok"``，
    于是"一个格子都没识别出来"的空网格用例会被算成成功；而它的
    ``cell_exact_rate`` 又是 ``None``（``compare()`` 在 total==0 时不评分），
    于是又被**静默剔出平均准确率的分母** —— 成功率、准确率**同时偏高**，
    正好藏住"识别结果为空"这类最严重的缺陷。

    现在：
    * 空识别结果 = **失败**（status="empty_result"），并按 **0 分**计入平均
      准确率的分母；
    * 真值退化导致算不出分的用例单列 status="unscored"，**也算失败**，
      不再被静默吞掉。
    """
    recognised_cells = sum(len(row) for row in actual)
    record["recognised_cells"] = recognised_cells
    record["empty_result"] = False
    if recognised_cells == 0:
        record["status"] = "empty_result"
        record["empty_result"] = True
        # 按 0 分计入分母：空结果必须拉低平均准确率，而不是从平均里消失。
        record["cell_exact_rate"] = 0.0
        record["failure_reason"] = "空识别结果：没有识别出任何单元格"
        return
    if record.get("cell_exact_rate") is None:
        record["status"] = "unscored"
        record["failure_reason"] = "无法计分：真值或识别网格为空，逐格一致率算不出来"
        return
    record["status"] = "ok"


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总验收统计（口径见 08 号文件 F-01）。

    * ``succeeded``：status 为 "ok" 的用例数。
    * ``empty_results``：空识别结果（0 单元格）的用例数 —— **单列可见**。
    * ``failed``：总数 - succeeded（含空结果、无法计分、真正报错）。
    * ``mean_cell_exact_rate`` 的分母 = ``scored_cases``（status 为 ok 或
      empty_result 的用例数）：空结果按 0 分计入；真正报错（崩溃/超时）没有
      分数、不进分母，但数量在 ``error_cases`` 里可见。
    """
    scored = [record for record in records if record.get("status") == "ok"]
    empty_results = [
        record for record in records if record.get("status") == "empty_result"
    ]
    unscored = [record for record in records if record.get("status") == "unscored"]
    errors = [record for record in records if record.get("status") == "error"]
    rates = [
        float(record["cell_exact_rate"])
        for record in records
        if record.get("status") in ("ok", "empty_result")
        and isinstance(record.get("cell_exact_rate"), (int, float))
        and not isinstance(record.get("cell_exact_rate"), bool)
    ]
    return {
        "cases": len(records),
        "succeeded": len(scored),
        "failed": len(records) - len(scored),
        "empty_results": len(empty_results),
        "unscored": len(unscored),
        "error_cases": len(errors),
        "shape_matches": sum(1 for record in scored if record.get("shape_match")),
        "scored_cases": len(rates),
        "mean_cell_exact_rate": round(sum(rates) / len(rates), 4) if rates else None,
    }


def collect_machine_identity() -> dict[str, Any]:
    """**不会自己翻转**的机器身份字段（2026-11-02 F47）。

    ⚠ 为什么要单独加这些：``platform.platform()`` 取的是 **cmd.exe 看到的系统
      版本** —— CPython 的 ``platform.win32_ver()`` 先调 ``_syscmd_ver()`` 去跑
      ``cmd /c ver``，而 cmd 看到的是**应用兼容 shim 之后**的版本号。
      实测（2026-11-02）：**同一台机器**上它在 06:06 记 ``Windows-10-10.0.26100-SP0``、
      06:23 又记 ``Windows-10-10.0.26200-SP0``，中间没有重启、没有系统更新
      （``ntdll.dll`` 文件版本自 2026-10-14 起一直是 ``10.0.26100.9949``）。
      ⇒ **它不能当机器身份键**，只能在对照报告里当"环境状态"提示。

    下面这些是真正稳定的身份：CPU 型号、逻辑核数、物理内存、每次开机都变的
    boot_id、**真实 OS 构建号**。取不到就留空，**绝不因为取不到而让整轮失败**。

    ⚠⚠ ``os_build`` 为什么必须用 ``sys.getwindowsversion()[2]`` 而不是
      ``platform.platform()``：**本产品自己的离线审计钩子会禁掉 ``cmd /c ver``**
      （实测 ``ocr_backend`` 装上钩子后 ``platform._syscmd_ver()`` 返回空元组）
      ⇒ CPython 回退到 ``getwindowsversion().platform_version``，
      那是**兼容性 shim 后的版本号（26100）**，而真实构建号是 **26200**。
      ⇒ ``platform.platform()`` 记下来的是**被自家钩子污染过的错值**，
      不能当 OS 身份；而 ``getwindowsversion()[2]`` 是直接调系统 API，
      **不受钩子影响** ⇒ 这才是该记的。
    """
    info: dict[str, Any] = {}
    try:
        info["os_build"] = int(sys.getwindowsversion()[2])
    except Exception:  # pragma: no cover - 非 Windows / 取不到
        info["os_build"] = 0
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as handle:
            info["cpu_model"] = str(
                winreg.QueryValueEx(handle, "ProcessorNameString")[0]
            ).strip()
    except Exception:  # pragma: no cover - 取不到就留空
        info["cpu_model"] = ""
    info["cpu_logical_cores"] = os.cpu_count() or 0
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            info["total_physical_memory_bytes"] = int(status.ullTotalPhys)
    except Exception:  # pragma: no cover
        pass
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
            r"\PrefetchParameters",
        ) as handle:
            info["boot_id"] = str(winreg.QueryValueEx(handle, "BootId")[0])
    except Exception:  # pragma: no cover
        info["boot_id"] = ""
    return info


def build_environment(
    records: list[dict[str, Any]],
    started_at: str,
    resumed: bool,
    resumed_records: int,
) -> dict[str, Any]:
    """本次运行的环境指纹（08 号文件 F-03）。

    事后回看一份验收记录，必须能判断"当时跑的是哪一档、什么时候跑的、
    什么机器、Python 版本、标定环境变量的原值是多少"。档位取**各用例实际
    生效的档位**（后端在请求开始时选定的值，``runtime_profile.memory_mode``），
    一轮里出现多个档位就记 "mixed"。
    """
    modes: list[str] = []
    for record in records:
        mode = record.get("memory_mode")
        if mode and mode not in modes:
            modes.append(mode)
    raw_threshold = os.environ.get("OCRTABLE_LOW_MEMORY_THRESHOLD_MB")
    # 2026-10-23（F-07）：把**产生这轮结果的代码**也记进环境指纹。
    # --resume 续跑时，每一轮可能来自不同代码状态（改了代码又接着跑），
    # 事后只看 records.json 无法判断某条记录是哪份代码产出的。
    # 这里记后端入口源码的 SHA-256 + 修改时间，跨轮状态不一致时一眼能看出来。
    backend_source = Path(__file__).resolve().parent.parent / "backend" / "ocr_backend.py"
    backend_sha256 = ""
    backend_mtime = ""
    if backend_source.is_file():
        backend_sha256 = hashlib.sha256(backend_source.read_bytes()).hexdigest()
        backend_mtime = datetime.datetime.fromtimestamp(
            backend_source.stat().st_mtime
        ).astimezone().isoformat(timespec="seconds")
    identity = collect_machine_identity()
    return {
        "started_at": started_at,
        "finished_at": iso_timestamp(),
        "backend_source": str(backend_source),
        "backend_source_sha256": backend_sha256,
        "backend_source_mtime": backend_mtime,
        "memory_mode": modes[0] if len(modes) == 1 else ("mixed" if modes else "unknown"),
        "memory_modes_observed": modes,
        "memory_mode_mixed": len(modes) > 1,
        "low_memory_threshold_env": raw_threshold,
        "low_memory_threshold_env_set": bool((raw_threshold or "").strip()),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        # ⚠ platform 是 **cmd 可见版本**（会因应用兼容 shim 翻转），**不是身份键**。
        "platform": platform.platform(),
        "machine": platform.machine(),
        "node": platform.node(),
        # ---- 稳定身份（F47）：与上面那组分开，别混着用 ----
        "os_build": identity.get("os_build", 0),
        "cpu_model": identity.get("cpu_model", ""),
        "cpu_logical_cores": identity.get("cpu_logical_cores", 0),
        "total_physical_memory_bytes": identity.get("total_physical_memory_bytes", 0),
        "boot_id": identity.get("boot_id", ""),
        "resumed": resumed,
        "resumed_records": resumed_records,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量验收 OcrTableTool 后端")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--ids", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip-models", action="store_true")
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="只跑第 N 个分片（0 起），配合 --shard-count 切分语料",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="把语料切成几份。1 = 不切分（默认）。",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="复用输出目录里已有的 records.json，跳过已跑完的用例",
    )
    return parser.parse_args()


def main() -> int:
    run_started_at = iso_timestamp()
    args = parse_args()
    corpus = args.corpus.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    model_dir = (
        args.model_dir.resolve()
        if args.model_dir
        else (PROJECT_ROOT / "runtime" / "models").resolve()
    )
    if args.model_dir:
        os.environ["OCR_TABLE_MODEL_DIR"] = str(model_dir)

    report: dict[str, Any] = {"corpus": str(corpus), "model_dir": str(model_dir)}

    if not args.skip_models:
        model_report = verify_models(model_dir)
        report["models"] = model_report
        print(
            "model gate: %d/%d verified, %d problem(s)"
            % (
                model_report["checked"],
                model_report["expected"],
                len(model_report["problems"]),
            ),
            flush=True,
        )
        for problem in model_report["problems"]:
            print("  MODEL PROBLEM  " + problem, flush=True)
        if model_report["problems"]:
            report["status"] = "model_gate_failed"
            (output / "acceptance-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return 2

    ids = {value.strip() for value in args.ids.split(",") if value.strip()}
    cases = load_cases(corpus, ids, args.limit)
    if not cases:
        raise RuntimeError("没有符合筛选条件的验收样本")

    # 2026-10-22：分片。1200 例全量要跑数小时，切片后可以并行压测
    # （每个分片一个独立进程 + 独立输出目录）。
    # **注意**：并行跑会互相抢内存，后端会切到 low_memory 档，
    # 逐格一致率会跟着变 —— 所以**分片只适合压测/健壮性**，
    # 要和基线做**准确率对比**仍然必须串行、安静机器。
    if args.shard_count > 1:
        if not 0 <= args.shard_index < args.shard_count:
            raise RuntimeError(
                "--shard-index 必须在 0..%d 之间" % (args.shard_count - 1)
            )
        cases = [
            case
            for position, case in enumerate(cases)
            if position % args.shard_count == args.shard_index
        ]
        print(
            "shard %d/%d -> %d case(s)"
            % (args.shard_index, args.shard_count, len(cases)),
            flush=True,
        )
        if not cases:
            raise RuntimeError("该分片没有样本")

    # A full corpus run takes hours, so the records are checkpointed after
    # every case and an interrupted run can be continued instead of restarted.
    checkpoint = output / "records.json"
    records: list[dict[str, Any]] = []
    done: set[str] = set()
    if args.resume and checkpoint.is_file():
        try:
            records = json.loads(checkpoint.read_text(encoding="utf-8"))
            # 2026-10-23（08 号文件 F-01）："empty_result" / "unscored" 也算
            # **已跑完**（它们是失败，不是没跑）。不登记的话 --resume 会把
            # 同一个 id 再跑一遍，records.json 里出现重复条目。
            done = {
                record["id"]
                for record in records
                if record.get("status")
                in ("ok", "error", "empty_result", "unscored")
            }
            print("resumed %d completed case(s)" % len(done), flush=True)
        except Exception as error:  # noqa: BLE001 - a bad checkpoint is not fatal
            print("checkpoint unusable (%s), starting fresh" % error, flush=True)
            records = []
            done = set()
    if done:
        cases = [case for case in cases if case["id"] not in done]

    print("cases: %d" % len(cases), flush=True)

    ocr_backend._load_runtime()
    for position, case in enumerate(cases, start=1):
        started = time.perf_counter()
        record: dict[str, Any] = {
            "id": case["id"],
            "status": "error",
            # 2026-10-23（08 号文件 F-03）：每条记录都带 ISO-8601 时间戳，
            # 事后才能判断这份验收是什么时候跑的。
            "started_at": iso_timestamp(),
        }
        case_output = output / case["id"]
        case_output.mkdir(parents=True, exist_ok=True)
        try:
            result = ocr_backend.handle_request(
                {
                    "protocol": 1,
                    "action": "recognize",
                    "image_path": case["image"],
                    "output_directory": str(case_output),
                    "options": {"crop_mode": "auto"},
                }
            )
            actual = cell_text_grid(result)
            truth = truth_grid(json.loads(Path(case["truth"]).read_text(encoding="utf-8")))
            record.update(compare(truth, actual))
            # 2026-10-23（08 号文件 F-01）：状态由 classify_case 定，
            # 空识别结果不再被无条件记成 "ok"。
            classify_case(record, actual)
            record["engine"] = result.get("engine", "")
            runtime_profile = result.get("runtime_profile") or {}
            record["runtime_profile"] = {
                "memory_mode": runtime_profile.get("memory_mode"),
                "inference_threads": runtime_profile.get("inference_threads"),
            }
            record["review_cells"] = sum(
                bool(cell.get("needs_review", False))
                for row in result.get("cells", [])
                for cell in row
                if isinstance(cell, dict)
            )
            # Keep the recognised grid next to the case so a shape or
            # alignment problem can be diagnosed without re-running the model.
            #
            # 2026-10-22 追加：原来只落 grid / truth / 行列数，事后无法区分
            # "抬头被删掉"和"表头没读出来"——两条路径产生完全一样的 +1 行
            # 偏移，每次都要重跑一次识别才能定位。把结构告警、span 和结构
            # 证书一并落盘（体积很小）。
            (case_output / "grid.json").write_text(
                json.dumps(
                    {
                        "grid": actual,
                        "truth": truth,
                        "result_rows": result.get("rows"),
                        "result_columns": result.get("columns"),
                        "structure_warnings": result.get("structure_warnings", []),
                        "spans": result.get("spans", []),
                        "structure_certificate": result.get("structure_certificate"),
                        "engine": result.get("engine", ""),
                        "review_cell_count": result.get("review_cell_count"),
                        "publication_blocked": result.get("publication_blocked"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as error:  # noqa: BLE001 - report, never abort the batch
            record["error"] = "%s: %s" % (type(error).__name__, error)
            if record.get("status") == "ok":
                # 出了异常就绝不能还是 "ok"（原来在写 grid.json 失败时就会这样）。
                record["status"] = "error"
        record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        record["timestamp"] = iso_timestamp()
        # The backend drops to a 4-thread low-memory profile when the machine
        # has less than 4 GB available, which makes it roughly 1.5x slower and
        # pushes borderline tables past the 60 second wall clock.  Recording the
        # profile per case keeps "the machine was loaded" separate from "the
        # product is wrong".
        #
        # 2026-10-22 更正：**必须用后端报回来的档位**（请求开始时选定的那一
        # 档），不能用模块级的当前值 —— 请求中途档位一变，就会把"低档跑出来
        # 的结果"标成正常档。同一份代码因此被追了半个多小时：FC-00006 两次
        # 跑出 0.9268 与 0.939，差别其实只是请求开始时是不是低内存档。
        profile = record.get("runtime_profile") or {}
        # 错误路径没有结果体，回传字段拿不到；这时读后端记录的"本次请求选定
        # 的档位"（_REQUEST_MEMORY_MODE），而不是模块当前值。
        record["memory_mode"] = (
            profile.get("memory_mode")
            or getattr(ocr_backend, "_REQUEST_MEMORY_MODE", None)
            or getattr(ocr_backend, "_ACTIVE_RECOGNITION_MEMORY_MODE", "unknown")
        )
        record["inference_threads"] = (
            profile.get("inference_threads")
            or getattr(ocr_backend, "_RECOGNITION_INFERENCE_THREADS", None)
        )
        records.append(record)
        checkpoint.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            "[%d/%d] %s %s %s %.3fs"
            % (
                position,
                len(cases),
                case["id"],
                record["status"],
                "%sx%s" % (record.get("actual_rows", 0), record.get("actual_columns", 0)),
                record["elapsed_seconds"],
            ),
            flush=True,
        )
        gc.collect()

    # 2026-10-23（08 号文件 F-01）：空识别结果不再被算成成功、也不再被
    # 静默剔出平均准确率的分母 —— 分母口径见 summarize_records()。
    summary = summarize_records(records)
    report.update(summary)
    # 2026-11-02（F15）：把"**选中了**几例"也记下来。
    # 以前只有 summary["cases"]（= 实际写进 records 的条数），
    # 于是"选了 20 例、只跑出 18 条记录"这件事在产物里**看不出来**，
    # 逐例对照又只比交集 ⇒ 少跑的 2 例被静默吞掉，"无回退"照旧成立。
    # 现在 selected_cases 与 cases 一比就知道有没有掉例。
    report["selected_cases"] = len(cases)
    report["records"] = records
    # 2026-10-23（08 号文件 F-03）：本次运行的环境指纹（开始时间、实际生效
    # 的档位、标定环境变量原值、Python 版本、平台）。
    report["environment"] = build_environment(
        records, run_started_at, bool(args.resume), len(done)
    )
    # 2026-10-23（F-06）：成功路径下 acceptance-report.json 里原来**根本没有**
    # `status` 字段（只有模型门禁失败时才会写 "model_gate_failed"），读报告的
    # 人无从判断这次验收是"全部跑完"还是"跑到一半炸了"。现在显式写出来。
    report["status"] = "ok" if not report["failed"] else "failed"
    (output / "acceptance-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 2026-10-23（08 号文件 F-03）：环境指纹单独落一份，贴着 records.json，
    # 回看验收时一眼能看到"这次是哪一档、什么时候跑的"。
    (output / "run-environment.json").write_text(
        json.dumps(report["environment"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output / "acceptance-summary.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "id",
                "status",
                "empty_result",
                "failure_reason",
                "timestamp",
                "started_at",
                "memory_mode",
                "inference_threads",
                "truth_rows",
                "truth_columns",
                "actual_rows",
                "actual_columns",
                "shape_match",
                "total_cells",
                "exact_cells",
                "cell_exact_rate",
                "recognised_cells",
                "elapsed_seconds",
                "error",
            ],
            extrasaction="ignore",
            # 老 checkpoint 里的记录可能没有新字段，补空串而不是报错。
            restval="",
        )
        writer.writeheader()
        writer.writerows(records)
    # 2026-10-23（08 号文件 F-01）：人类可读汇总里**必须**看得到空结果数。
    print(
        "summary: %d case(s) | ok=%d empty_result=%d unscored=%d error=%d "
        "| mean_cell_exact_rate=%s (denominator=%d) | tier=%s"
        % (
            report["cases"],
            report["succeeded"],
            report["empty_results"],
            report["unscored"],
            report["error_cases"],
            report["mean_cell_exact_rate"],
            report["scored_cases"],
            report["environment"]["memory_mode"],
        ),
        flush=True,
    )
    print(
        json.dumps(
            {
                # 2026-10-23（F-06）：以前这里是硬编码字面量 "complete"，与真实
                # 结果无关 —— 1200 例全部 error 时它照样打印 "complete"。
                "status": report["status"],
                "cases": report["cases"],
                "succeeded": report["succeeded"],
                "failed": report["failed"],
                # 2026-10-23（08 号文件 F-01）：空结果用例数必须可见。
                "empty_results": report["empty_results"],
                "unscored": report["unscored"],
                "error_cases": report["error_cases"],
                "scored_cases": report["scored_cases"],
                "shape_matches": report["shape_matches"],
                "mean_cell_exact_rate": report["mean_cell_exact_rate"],
                # 2026-10-23（08 号文件 F-03）：档位 + 开始时间。
                "memory_mode": report["environment"]["memory_mode"],
                "started_at": report["environment"]["started_at"],
            },
            ensure_ascii=False,
        )
    )
    # 2026-10-23（F-06）：以前这里**无条件** `return 0` —— 逐例失败只写进
    # JSON 的 report["failed"]，完全不参与退出码。任何用 $LASTEXITCODE 判断
    # 验收结果的流水线/CI，**即使 1200 个用例全部 error 也判定为通过**。
    # 现在失败数 > 0 就返回非 0：
    #   0 = 全部用例成功
    #   1 = 至少一个用例失败（report["failed"] > 0）
    #   2 = 模型指纹门禁失败（见上方 model_gate_failed 分支）
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
