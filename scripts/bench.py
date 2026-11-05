# -*- coding: utf-8 -*-
"""OcrTableTool 标准准确率测量器（单进程、预热一次、可切片并发）。

用法:
  python bench.py --backend DIR --cases 1-60 --out result.json [--tag name]

输出每例：status/rows/columns/elapsed/degraded/memory_mode/hard/best/差异样例(top 8)
汇总：mean_hard / mean_best / degraded 次数 / 失败分类 / 全局差异模式统计

差异模式自动归类（用于定位"数据被改坏"的系统性原因）：
  digit_padded       实际比真值多出数字（如 2298→12298、83→832092）
  digit_missing      实际比真值少数字
  case_flip          仅大小写不同
  punct_swap         仅标点/分隔符不同
  space_only         仅空白不同
  replaced_text      文本整体不同
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\AP\AP7000\OcrTableTool")
IMAGES = ROOT / "field-corpus" / "images"
TRUTH = ROOT / "field-corpus" / "_truth"
MODELS = ROOT / "package" / "backend-delivery-20261030" / "ocr-runtime" / "OcrBackend" / "models"


def parse_cases(spec: str) -> list[str]:
    result: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part and part.replace("-", "").isdigit():
            start, end = part.split("-", 1)
            result.extend("FC-%05d" % number for number in range(int(start), int(end) + 1))
        else:
            result.append(part)
    return result


def classify(expected: str, actual: str) -> str:
    if expected == actual:
        return "same"
    if not expected:
        return "extra_text"
    if not actual:
        return "missing_text"
    if expected.lower() == actual.lower():
        return "case_flip"
    if re.sub(r"[\s]", "", expected) == re.sub(r"[\s]", "", actual):
        return "space_only"
    if re.sub(r"[^\d]", "", expected) == re.sub(r"[^\d]", "", actual):
        return "punct_swap"
    exp_digits = re.sub(r"\D", "", expected)
    act_digits = re.sub(r"\D", "", actual)
    if exp_digits and act_digits and exp_digits in act_digits and len(act_digits) > len(exp_digits):
        return "digit_padded"
    if exp_digits and act_digits and act_digits in exp_digits and len(act_digits) < len(exp_digits):
        return "digit_missing"
    return "replaced_text"


def exact_rate(resp_cells, truth, row_shift=0, col_shift=0):
    if not resp_cells or not truth:
        return None
    total = hit = 0
    for r, truth_row in enumerate(truth):
        for c, expected in enumerate(truth_row):
            rr, cc = r + row_shift, c + col_shift
            if not (0 <= rr < len(resp_cells)) or not (0 <= cc < len(resp_cells[rr])):
                continue
            actual = str(resp_cells[rr][cc].get("text", "")).strip()
            total += 1
            if actual == str(expected).strip():
                hit += 1
    return (hit / total) if total else None


def best_alignment(resp_cells, truth):
    best = None
    for row_shift in range(-3, 4):
        for col_shift in range(-2, 3):
            rate = exact_rate(resp_cells, truth, row_shift, col_shift)
            if rate is not None and (best is None or rate > best):
                best = rate
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default=str(ROOT / "backend"))
    parser.add_argument("--cases", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tag", default="run")
    parser.add_argument("--diff-limit", type=int, default=8)
    parser.add_argument("--no-budget", action="store_true",
                        help="关闭 60 秒墙钟（准确率测量专用，耗时不可作为交付口径）")
    args = parser.parse_args()

    os.environ["OCR_TABLE_MODEL_DIR"] = str(MODELS)
    if args.no_budget:
        os.environ["OCRTABLE_BUDGET_DISABLED"] = "1"
    sys.path.insert(0, str(Path(args.backend).resolve()))
    import ocr_backend as ob  # noqa: E402

    ob._load_runtime()
    ob._engines()  # 预热：模型构建不计入各例耗时

    case_ids = [case for case in parse_cases(args.cases) if (IMAGES / (case + ".png")).is_file()]
    print("[%s] %d 例，backend=%s" % (args.tag, len(case_ids), args.backend), flush=True)

    rows = []
    pattern_counter: Counter = Counter()
    phantom_counter: Counter = Counter()
    started_all = time.time()
    for index, case_id in enumerate(case_ids, 1):
        truth_path = TRUTH / (case_id + ".json")
        truth = json.loads(truth_path.read_text(encoding="utf-8")).get("cells") if truth_path.is_file() else None
        work = ROOT / "_scratch" / "bench" / args.tag / case_id
        work.mkdir(parents=True, exist_ok=True)
        request = {
            "protocol": 1, "action": "recognize", "request_id": index,
            "image_path": str(IMAGES / (case_id + ".png")),
            "output_directory": str(work),
            "options": {"crop_mode": "auto", "accuracy_mode": "maximum", "deadline_seconds": 0},
        }
        t0 = time.time()
        try:
            response = ob.handle_request(request)
        except Exception as error:  # noqa: BLE001
            rows.append({"case": case_id, "status": "exception",
                         "error": "%s: %s" % (type(error).__name__, str(error)[:160])})
            print("[%d/%d] %-10s EXC %s" % (index, len(case_ids), case_id, str(error)[:80]), flush=True)
            continue
        elapsed = time.time() - t0
        cells = response.get("cells") or []
        hard = exact_rate(cells, truth) if truth else None
        best = best_alignment(cells, truth) if truth else None
        diffs = []
        if truth and cells:
            for r, truth_row in enumerate(truth):
                if r >= len(cells):
                    break
                for c, expected in enumerate(truth_row):
                    if c >= len(cells[r]):
                        break
                    actual = str(cells[r][c].get("text", "")).strip()
                    expected_text = str(expected).strip()
                    if actual != expected_text:
                        kind = classify(expected_text, actual)
                        pattern_counter[kind] += 1
                        if len(diffs) < args.diff_limit:
                            diffs.append({"r": r, "c": c, "expected": expected_text,
                                          "actual": actual, "kind": kind})
        row = {
            "case": case_id, "status": response.get("status"),
            "error": response.get("error_code"), "rows": response.get("rows"),
            "columns": response.get("columns"), "elapsed": round(elapsed, 2),
            "degraded": response.get("degraded"), "memory_mode": response.get("memory_mode"),
            "hard": None if hard is None else round(hard, 4),
            "best": None if best is None else round(best, 4),
            "diffs": diffs,
            # 门禁字段（2026-11-05）：决定这份错数据**能不能流到客户**。
            "publication_blocked": response.get("publication_blocked"),
            "recognition_state": response.get("recognition_state"),
            "structure_verified": response.get("structure_verified"),
            "review_cell_count": response.get("review_cell_count"),
        }
        # 结构诊断（2026-11-05）：多/少行列、末尾全空列（幻影列）、重复文字。
        # 这些是"数据被放到错误位置"的直接指标，比逐格准确率更容易定位根因。
        if cells:
            actual_width = len(cells[0])
            truth_width = len(truth[0]) if truth else None
            truth_height = len(truth) if truth else None
            row["shape"] = [len(cells), actual_width]
            row["shape_match"] = (truth_height is not None
                                  and len(cells) == truth_height
                                  and actual_width == truth_width)
            # 末尾/开头全空列
            def _column_empty(index):
                return all(not str(cell.get("text", "")).strip()
                           for cell in (r[index] for r in cells if index < len(r)))
            trailing_empty = 0
            while actual_width - trailing_empty > 1 and _column_empty(actual_width - trailing_empty - 1):
                trailing_empty += 1
            leading_empty = 0
            while leading_empty < actual_width - 1 and _column_empty(leading_empty):
                leading_empty += 1
            row["trailing_empty_columns"] = trailing_empty
            row["leading_empty_columns"] = leading_empty
            texts = [str(cell.get("text", "")).strip() for r in cells for cell in r]
            texts = [text for text in texts if text]
            duplicated = sum(count - 1 for count in Counter(texts).values() if count > 1)
            row["duplicate_cells"] = duplicated
            if trailing_empty:
                phantom_counter["trailing_empty_column"] += trailing_empty
            if leading_empty:
                phantom_counter["leading_empty_column"] += leading_empty
            if duplicated:
                phantom_counter["duplicate_cell"] += duplicated
        rows.append(row)
        print("[%d/%d] %-10s %-6s %sx%s hard=%s best=%s %.1fs%s" % (
            index, len(case_ids), case_id, row["status"], row["rows"], row["columns"],
            "-" if hard is None else "%.3f" % hard,
            "-" if best is None else "%.3f" % best, elapsed,
            " DEGRADED" if row["degraded"] else ""), flush=True)

    scored = [row for row in rows if row.get("hard") is not None]
    ok = [row for row in rows if row.get("status") == "ok"]
    summary = {
        "tag": args.tag, "backend": args.backend, "count": len(rows),
        "ok": len(ok), "scored": len(scored),
        "mean_hard": round(sum(row["hard"] for row in scored) / len(scored), 4) if scored else None,
        "mean_best": round(sum(row["best"] for row in scored) / len(scored), 4) if scored else None,
        "degraded": sum(1 for row in rows if row.get("degraded")),
        "errors": Counter(row.get("error") for row in rows if row.get("status") != "ok"),
        "diff_patterns": pattern_counter,
        "structure_issues": phantom_counter,
        "wall_seconds": round(time.time() - started_all, 1),
        "cases": rows,
    }
    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\n=== %s 汇总 ===" % args.tag)
    print(json.dumps({key: summary[key] for key in
                      ("count", "ok", "scored", "mean_hard", "mean_best", "degraded",
                       "diff_patterns", "structure_issues", "wall_seconds")}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())