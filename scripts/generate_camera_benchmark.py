from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def _desk_background(height: int, width: int, rng: np.random.Generator) -> np.ndarray:
    y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    base = 145.0 + 24.0 * x + 10.0 * y
    noise = rng.normal(0.0, 2.5, (height, width)).astype(np.float32)
    gray = np.clip(base + noise, 0, 255).astype(np.uint8)
    return np.stack((gray * 0.72, gray * 0.86, gray), axis=-1).astype(np.uint8)


def _paper_with_table(image: np.ndarray, width: int = 1580, height: int = 850) -> np.ndarray:
    paper = np.full((height, width, 3), (244, 246, 247), dtype=np.uint8)
    margin_x, margin_y = 42, 46
    available_width = width - margin_x * 2
    available_height = height - margin_y * 2
    scale = min(available_width / image.shape[1], available_height / image.shape[0])
    resized = cv2.resize(
        image,
        (max(1, int(round(image.shape[1] * scale))), max(1, int(round(image.shape[0] * scale)))),
        interpolation=cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA,
    )
    left = (width - resized.shape[1]) // 2
    top = (height - resized.shape[0]) // 2
    paper[top : top + resized.shape[0], left : left + resized.shape[1]] = resized
    return paper


def _apply_lighting(
    image: np.ndarray,
    mode: str,
    rng: np.random.Generator,
) -> np.ndarray:
    result = image.astype(np.float32)
    height, width = image.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    if mode in {"shadow", "combined"}:
        center = rng.uniform(0.15, 0.85) * width
        transition = max(70.0, width * 0.09)
        shadow = 0.63 + 0.37 / (1.0 + np.exp(-(xx - center) / transition))
        result *= shadow[:, :, None]
    if mode in {"low_light", "combined"}:
        result = np.power(np.clip(result / 255.0, 0.0, 1.0), 1.32) * 255.0
    if mode in {"glare", "combined"}:
        center_x = rng.uniform(0.25, 0.75) * width
        center_y = rng.uniform(0.25, 0.65) * height
        radius_x = width * rng.uniform(0.12, 0.20)
        radius_y = height * rng.uniform(0.18, 0.28)
        glare = np.exp(
            -(((xx - center_x) / radius_x) ** 2 + ((yy - center_y) / radius_y) ** 2) * 2.4
        )
        result = result * (1.0 - 0.16 * glare[:, :, None]) + 255.0 * 0.16 * glare[:, :, None]
    return np.clip(result, 0, 255).astype(np.uint8)


def _camera_variant(
    source: np.ndarray,
    mode: str,
    rng: np.random.Generator,
) -> np.ndarray:
    canvas_height, canvas_width = 1080, 1920
    paper = _apply_lighting(_paper_with_table(source), mode, rng)
    paper_height, paper_width = paper.shape[:2]
    source_corners = np.float32(
        [[0, 0], [paper_width - 1, 0], [paper_width - 1, paper_height - 1], [0, paper_height - 1]]
    )
    perspective = 18 if mode in {"camera", "shadow", "low_light"} else 34
    left = 150 + int(rng.integers(-25, 26))
    top = 105 + int(rng.integers(-22, 23))
    right = canvas_width - 150 + int(rng.integers(-25, 26))
    bottom = canvas_height - 105 + int(rng.integers(-22, 23))
    destination = np.float32(
        [
            [left + rng.integers(-perspective, perspective + 1), top + rng.integers(-perspective, perspective + 1)],
            [right + rng.integers(-perspective, perspective + 1), top + rng.integers(-perspective, perspective + 1)],
            [right + rng.integers(-perspective, perspective + 1), bottom + rng.integers(-perspective, perspective + 1)],
            [left + rng.integers(-perspective, perspective + 1), bottom + rng.integers(-perspective, perspective + 1)],
        ]
    )
    transform = cv2.getPerspectiveTransform(source_corners, destination)
    background = _desk_background(canvas_height, canvas_width, rng)
    warped = cv2.warpPerspective(paper, transform, (canvas_width, canvas_height))
    mask = cv2.warpPerspective(
        np.full((paper_height, paper_width), 255, dtype=np.uint8),
        transform,
        (canvas_width, canvas_height),
    )
    alpha = cv2.GaussianBlur(mask, (0, 0), 1.2).astype(np.float32)[:, :, None] / 255.0
    frame = np.clip(warped * alpha + background * (1.0 - alpha), 0, 255).astype(np.uint8)
    if mode in {"camera", "shadow", "low_light", "glare"}:
        frame = cv2.GaussianBlur(frame, (3, 3), 0.45)
    elif mode == "combined":
        frame = cv2.GaussianBlur(frame, (3, 3), 0.8)
        noise = rng.normal(0.0, 1.8, frame.shape).astype(np.float32)
        frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    return frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成确定性的Excel截图与模拟相机恶劣环境OCR基准图")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=25, choices=range(20, 31))
    parser.add_argument("--seed", type=int, default=20260805)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise RuntimeError(f"输出目录非空，为避免覆盖历史结果已停止：{args.output}")
    args.output.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.manifest.read_text(encoding="utf-8"))
    if len(cases) < 20:
        raise RuntimeError("至少需要20个带真值的Excel测试用例")
    rng = np.random.default_rng(args.seed)
    modes = ["clean"] * 5 + ["camera"] * 5 + ["shadow"] * 4 + ["low_light"] * 3 + ["glare"] * 3
    while len(modes) < args.count:
        modes.append("combined")

    generated = []
    for index, mode in enumerate(modes[: args.count]):
        source_case = cases[index % len(cases)]
        source = cv2.imread(str(source_case["imagePath"]), cv2.IMREAD_COLOR)
        if source is None:
            raise RuntimeError(f"无法读取源图片：{source_case['imagePath']}")
        image = source.copy() if mode == "clean" else _camera_variant(source, mode, rng)
        case_id = f"{index + 1:02d}_{source_case['id']}_{mode}"
        image_path = args.output / f"{case_id}.png"
        if not cv2.imwrite(str(image_path), image):
            raise RuntimeError(f"无法写入测试图片：{image_path}")
        item = dict(source_case)
        item.update(
            {
                "id": case_id,
                "sourceId": source_case["id"],
                "imagePath": str(image_path),
                "cameraMode": mode,
                "benchmarkSeed": args.seed,
            }
        )
        generated.append(item)

    (args.output / "manifest.json").write_text(
        json.dumps(generated, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "cases": len(generated), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
