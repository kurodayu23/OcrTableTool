#!/usr/bin/env python
"""把**真实翻拍照片**的人工真值构建成验收语料（field-corpus 同款布局）。

为什么要单独一套：合成语料（field-corpus）测的是渲染出来的清晰表格，
**测不到**翻拍照片上的框线残留、背面透印、倾斜拍摄、低分辨率这些问题。
真实照片的逐格准确率一直没有人守着，"识别结果瞎编"这类缺陷才能一路走到
用户手上 —— 这个脚本 + `gate_photo_acceptance.py` 就是把它变成门禁。

用法：
    python scripts/build_photo_corpus.py \
        --source "C:\\Users\\jss18\\Pictures\\excel3" \
        --output photo-corpus

产物：
    photo-corpus/images/<case>.png     原图（转 png，便于语料自洽）
    photo-corpus/_truth/<case>.json    逐格真值（对齐 field-corpus 的格式）
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

import cv2  # noqa: E402  (Windows 上 imread 读不了中文路径，后面用 imdecode)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from photo_corpus_truths import CASES, PHOTO_ROOT, SOURCES  # noqa: E402


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_image(path: Path):
    """读图。Windows 上 OpenCV 的 imread **读不了含中文的路径**，
    必须走 np.fromfile + imdecode。"""
    import numpy as np

    try:
        buffer = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建真实照片验收语料")
    parser.add_argument(
        "--source", type=Path, default=Path(PHOTO_ROOT),
        help="真实照片库根目录（默认 %s）" % PHOTO_ROOT,
    )
    parser.add_argument(
        "--output", type=Path, default=SCRIPTS.parent / "photo-corpus",
        help="语料输出目录（默认 <仓库>/photo-corpus）",
    )
    parser.add_argument(
        "--case", action="append", default=[],
        help="只构建指定用例（可重复）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    images = output / "images"
    truths = output / "_truth"
    images.mkdir(parents=True, exist_ok=True)
    truths.mkdir(parents=True, exist_ok=True)

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        print("需要 opencv：%s" % exc)
        return 2

    wanted = set(args.case) if args.case else set(CASES)
    missing = sorted(wanted - set(SOURCES))
    if missing:
        print("这些用例没有登记原图路径：%s" % "、".join(missing))
        return 2

    built = 0
    skipped = []
    for case_id in sorted(wanted):
        grid, ambiguous = CASES[case_id]
        source_path = source / SOURCES[case_id]
        if not source_path.is_file():
            skipped.append((case_id, "原图不存在：%s" % source_path))
            continue
        image = read_image(source_path)
        if image is None:
            skipped.append((case_id, "原图读不出来：%s" % source_path))
            continue
        target = images / ("%s.png" % case_id)
        if not cv2.imwrite(str(target), image):
            skipped.append((case_id, "写图片失败：%s" % target))
            continue
        rows = len(grid)
        columns = max((len(row) for row in grid), default=0)
        truth = {
            "case_id": case_id,
            "source_type": "real_photo_capture",
            "scenario": "真实翻拍纸质表单",
            "carrier": "photo",
            "degradation": "real",
            "rule_style": "ruled",
            "header_rows": 1,
            "rows": rows,
            "columns": columns,
            "cells": [list(row) for row in grid],
            "spans": [],
            "annotator": "human",
            "verifier": "human",
            "verified_at": datetime.date.today().isoformat(),
            "image_sha256": sha256_of(target),
            "ambiguous_cells": [
                {"row": row, "column": column, "reason": reason}
                for (row, column), reason in sorted(ambiguous.items())
            ],
        }
        (truths / ("%s.json" % case_id)).write_text(
            json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        built += 1
        print("已构建 %s（%d 行 × %d 列，%d 个歧义格）"
              % (case_id, rows, columns, len(ambiguous)))

    for case_id, reason in skipped:
        print("跳过 %s：%s" % (case_id, reason))
    print("共 %d 个用例，输出目录：%s" % (built, output))
    return 0 if built and not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
