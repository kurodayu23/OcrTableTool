#!/usr/bin/env python
"""**真实翻拍照片**的端到端准确率门禁。

为什么要有这个脚本：合成语料（field-corpus）全部是渲染出来的清晰表格，
**测不到**翻拍照片上的框线残留、背面透印、倾斜拍摄、低分辨率这些问题。
"识别结果瞎编"（真值 55 被读成 552、真值 2298 被读成 12298）就是只靠合成
语料测了几个月才漏出去的 —— 合成语料全绿，真实照片全错。

这个门禁同时守住**两件**事：

  ① **准确率**：逐格精确比对真实照片的人工真值，低于阈值就失败；
  ② **不许静默**：每个错值格都必须是 ``needs_review=true``（界面标黄 +
     禁止当作已验证结果导出）。有错值却没被标黄 ⇒ 门禁失败 ——
     这正是"瞎编且没人发现"的制度性防线。

用法：
    python scripts/gate_photo_acceptance.py \
        --corpus photo-corpus --output _work/photo-gate \
        [--min-cell-exact-rate 0.98] [--max-silent-wrong-cells 0] [--ids PF-00014]

退出码：0 = 通过；1 = 未达标；2 = 语料/环境不可用。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="真实照片端到端准确率门禁")
    parser.add_argument("--corpus", type=Path, required=True,
                        help="语料目录（images/ + _truth/）")
    parser.add_argument("--output", type=Path, required=True,
                        help="报告输出目录")
    parser.add_argument("--model-dir", type=Path, default=None,
                        help="模型目录（默认 <项目>/runtime/models）")
    parser.add_argument("--ids", default="",
                        help="只跑指定用例，逗号分隔")
    parser.add_argument("--min-cell-exact-rate", type=float, default=0.97,
                        help="逐格准确率下限（默认 0.97）")
    parser.add_argument("--max-silent-wrong-cells", type=int, default=0,
                        help="允许的'错值却没标黄'格数上限（默认 0，"
                             "这是产品的硬承诺：错值必须被标出来）")
    parser.add_argument("--max-wrong-cells", type=int, default=4,
                        help="允许的错格总数上限（默认 4）")
    parser.add_argument("--skip-models", action="store_true",
                        help="跳过模型指纹检查")
    return parser.parse_args()


def load_truth(path: Path) -> tuple[list[list[str]], set[tuple[int, int]]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    cells = payload.get("cells") or []
    grid = [
        [("" if value is None else str(value)) for value in row]
        for row in cells
    ]
    ambiguous = {
        (int(item["row"]), int(item["column"]))
        for item in payload.get("ambiguous_cells", [])
    }
    return grid, ambiguous


def main() -> int:
    args = parse_args()
    corpus = args.corpus.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    model_dir = (
        args.model_dir.resolve() if args.model_dir
        else (PROJECT_ROOT / "runtime" / "models").resolve()
    )
    if args.model_dir:
        os.environ["OCR_TABLE_MODEL_DIR"] = str(model_dir)
    elif not os.environ.get("OCR_TABLE_MODEL_DIR"):
        os.environ["OCR_TABLE_MODEL_DIR"] = str(model_dir)

    import ocr_backend

    if not args.skip_models:
        import batch_acceptance_runner

        model_report = batch_acceptance_runner.verify_models(model_dir)
        print("model gate: %d/%d verified, %d problem(s)"
              % (model_report["checked"], model_report["expected"],
                 len(model_report["problems"])), flush=True)
        if model_report["problems"]:
            for problem in model_report["problems"]:
                print("  MODEL PROBLEM  " + problem)
            (output / "photo-gate-report.json").write_text(
                json.dumps({"status": "model_gate_failed",
                            "problems": model_report["problems"]},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            return 2

    truth_dir = corpus / "_truth"
    image_dir = corpus / "images"
    if not truth_dir.is_dir() or not image_dir.is_dir():
        print("语料目录不完整（需要 images/ 与 _truth/）：%s" % corpus)
        return 2

    wanted = {value.strip() for value in args.ids.split(",") if value.strip()}
    cases = []
    for truth_path in sorted(truth_dir.glob("*.json")):
        case_id = truth_path.stem
        if wanted and case_id not in wanted:
            continue
        image_path = image_dir / ("%s.png" % case_id)
        if not image_path.is_file():
            print("跳过（缺图）：%s" % case_id)
            continue
        cases.append((case_id, image_path, truth_path))
    if not cases:
        print("没有可跑的用例：%s" % corpus)
        return 2

    records: list[dict[str, Any]] = []
    total_cells = 0
    wrong_cells = 0
    silent_wrong = 0
    started_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    for case_id, image_path, truth_path in cases:
        grid, ambiguous = load_truth(truth_path)
        case_started = time.time()
        try:
            result = ocr_backend.handle_request({
                "protocol": 1,
                "action": "recognize",
                "image_path": str(image_path),
                "output_directory": str(output / "tmp"),
                "options": {"crop_mode": "auto"},
            })
        except Exception as exc:  # noqa: BLE001 - 门禁要把异常算成失败
            print("[%s] 识别抛异常：%s: %s" % (case_id, type(exc).__name__, exc))
            records.append({
                "case_id": case_id, "status": "error",
                "error": "%s: %s" % (type(exc).__name__, exc),
                "elapsed_seconds": round(time.time() - case_started, 3),
            })
            wrong_cells += 1
            silent_wrong += 1
            continue

        cells = result.get("cells", [])
        case_total = 0
        case_wrong = 0
        case_silent = 0
        details = []
        for row_index, row in enumerate(grid):
            for column_index, expected in enumerate(row):
                if (row_index, column_index) in ambiguous:
                    continue
                case_total += 1
                actual_cell = (
                    cells[row_index][column_index]
                    if row_index < len(cells) and column_index < len(cells[row_index])
                    else None
                )
                actual = (
                    str(actual_cell.get("text", ""))
                    if isinstance(actual_cell, dict) else str(actual_cell or "")
                )
                if actual == expected:
                    continue
                case_wrong += 1
                needs_review = bool(
                    isinstance(actual_cell, dict) and actual_cell.get("needs_review")
                )
                if not needs_review:
                    case_silent += 1
                details.append({
                    "row": row_index,
                    "column": column_index,
                    "expected": expected,
                    "actual": actual,
                    "needs_review": needs_review,
                })
        total_cells += case_total
        wrong_cells += case_wrong
        silent_wrong += case_silent
        rate = (case_total - case_wrong) / max(1, case_total)
        review_count = int(result.get("review_cell_count") or 0)
        print("[%s] %d 格，错 %d（准确率 %.2f%%），标黄 %d 格，其中错值未标黄 %d 个  %.1fs"
              % (case_id, case_total, case_wrong, 100.0 * rate, review_count,
                 case_silent, time.time() - case_started))
        for item in details:
            print("    r%-2dc%-2d 真值=%-10r 识别=%-10r %s"
                  % (item["row"], item["column"], item["expected"], item["actual"],
                     "【已标黄】" if item["needs_review"] else "【未标黄】"))
        records.append({
            "case_id": case_id,
            "status": "ok",
            "total_cells": case_total,
            "wrong_cells": case_wrong,
            "cell_exact_rate": round(rate, 6),
            "silent_wrong_cells": case_silent,
            "review_cell_count": review_count,
            "publication_blocked": bool(result.get("publication_blocked")),
            "recognition_state": result.get("recognition_state"),
            "details": details,
            "elapsed_seconds": round(time.time() - case_started, 3),
        })

    overall = (total_cells - wrong_cells) / max(1, total_cells)
    failures = []
    if overall < args.min_cell_exact_rate:
        failures.append("逐格准确率 %.4f 低于阈值 %.4f"
                        % (overall, args.min_cell_exact_rate))
    if wrong_cells > args.max_wrong_cells:
        failures.append("错格 %d 个，超过上限 %d" % (wrong_cells, args.max_wrong_cells))
    if silent_wrong > args.max_silent_wrong_cells:
        failures.append("有 %d 个错值格没有被标黄（静默错值），超过上限 %d"
                        % (silent_wrong, args.max_silent_wrong_cells))

    report = {
        "status": "failed" if failures else "passed",
        "started_at": started_at,
        "finished_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus": str(corpus),
        "model_dir": str(model_dir),
        "thresholds": {
            "min_cell_exact_rate": args.min_cell_exact_rate,
            "max_wrong_cells": args.max_wrong_cells,
            "max_silent_wrong_cells": args.max_silent_wrong_cells,
        },
        "total_cells": total_cells,
        "wrong_cells": wrong_cells,
        "cell_exact_rate": round(overall, 6),
        "silent_wrong_cells": silent_wrong,
        "failures": failures,
        "records": records,
    }
    (output / "photo-gate-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n逐格准确率 %.4f（%d 格错 %d）  静默错值 %d"
          % (overall, total_cells, wrong_cells, silent_wrong))
    if failures:
        for item in failures:
            print("门禁未通过：" + item)
        print("报告：%s" % (output / "photo-gate-report.json"))
        return 1
    print("门禁通过。报告：%s" % (output / "photo-gate-report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
