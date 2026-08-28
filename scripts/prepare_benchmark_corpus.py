from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


EXPECTED_DOWNLOADS = {
    "funsd.zip": "f9bbf6a94357e6db900ce023f8cbfa7b1e0462b440a59d372a905fde33d1e46a",
    "xfund-zh-val.zip": "a30e6e5c274ea8236c9b00c46ab96d59829a5bcd08baee966fa433028b644456",
    "cord-v2-test.parquet": "51c65f1788faff392abe2a0b55b023eb23e9be551c509138eaa3a832514224e7",
    "doclaynet-dev.zip": "3abe260031692756341095283c9fb3326b713f2bb9d587f1f6f7d066aff1e52f",
    "ctdar-tracka-test.zip": "15481723d4def43ba54d0ef165134295c5ca91628b104170ec740b2f88bd5aeb",
    "xfund-zh-val.json": None,
}


@dataclass(frozen=True)
class SourceSpec:
    name: str
    count: int
    url: str
    license_name: str
    language: str
    capture_type: str
    expected_mode: str
    metric_scope: str


SOURCES = {
    "funsd": SourceSpec(
        "FUNSD",
        30,
        "https://www.crc.nd.edu/~pmoreira/funsd.zip",
        "research-noncommercial",
        "en",
        "noisy_scan",
        "key_value_form",
        "ocr_text+layout",
    ),
    "xfund": SourceSpec(
        "XFUND-ZH",
        20,
        "https://github.com/doc-analysis/XFUND/releases/tag/v1.0",
        "CC-BY-NC-SA-4.0",
        "zh+mixed",
        "scan_or_photo",
        "key_value_form",
        "ocr_text+layout",
    ),
    "cord": SourceSpec(
        "CORD-v2",
        25,
        "https://huggingface.co/datasets/naver-clova-ix/cord-v2",
        "CC-BY-4.0",
        "id+en",
        "receipt_photo",
        "receipt",
        "ocr_text+semantic_fields",
    ),
    "doclaynet": SourceSpec(
        "DocLayNet-dev",
        20,
        "https://ds4sd.github.io/icdar23-doclaynet/task/",
        "CDLA-Permissive-1.0",
        "en+mixed",
        "digital_or_scan",
        "mixed_regions",
        "layout_presence+stability",
    ),
    "ctdar": SourceSpec(
        "cTDaR-2019-TrackA-test",
        25,
        "https://zenodo.org/records/2649217",
        "research-evaluation",
        "mixed",
        "scan_or_photo",
        "grid_table",
        "table_presence+stability",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evenly_spaced(items: list[Any], count: int) -> list[Any]:
    if len(items) < count:
        raise ValueError(f"样本不足：需要 {count}，实际只有 {len(items)}")
    if count == 1:
        return [items[len(items) // 2]]
    indices = [round(index * (len(items) - 1) / (count - 1)) for index in range(count)]
    return [items[index] for index in indices]


def image_metadata(path: Path) -> tuple[int, int, str]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
        image_format = image.format or path.suffix.lstrip(".").upper()
    return width, height, image_format


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def add_case(
    records: list[dict[str, Any]],
    output: Path,
    source_key: str,
    source_item_id: str,
    image_name: str,
    image_bytes: bytes,
    ground_truth: Any,
    source_split: str,
    source_index: int,
) -> None:
    spec = SOURCES[source_key]
    case_number = len(records) + 1
    case_id = f"B{case_number:03d}"
    suffix = Path(image_name).suffix.lower() or ".png"
    image_path = output / "images" / source_key / f"{case_id}_{Path(image_name).stem}{suffix}"
    write_bytes(image_path, image_bytes)
    width, height, image_format = image_metadata(image_path)
    truth_path = output / "ground_truth" / source_key / f"{case_id}.json"
    write_json(
        truth_path,
        {
            "benchmark_id": case_id,
            "source": spec.name,
            "source_item_id": source_item_id,
            "metric_scope": spec.metric_scope,
            "ground_truth": ground_truth,
        },
    )
    # 每个来源内部固定约三分之二用于调试，其余作为不参与调参的留出集。
    benchmark_split = "holdout" if source_index % 3 == 2 else "development"
    records.append(
        {
            "id": case_id,
            "source": spec.name,
            "source_url": spec.url,
            "license": spec.license_name,
            "source_split": source_split,
            "benchmark_split": benchmark_split,
            "source_item_id": source_item_id,
            "sha256": sha256(image_path),
            "image_path": image_path.relative_to(output).as_posix(),
            "ground_truth_path": truth_path.relative_to(output).as_posix(),
            "width": width,
            "height": height,
            "image_format": image_format,
            "language": spec.language,
            "capture_type": spec.capture_type,
            "expected_mode": spec.expected_mode,
            "metric_scope": spec.metric_scope,
            "allow_training": "false",
            "allow_redistribution": "false",
            "review_status": "needs_human_review",
        }
    )


def prepare_funsd(downloads: Path, output: Path, records: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(downloads / "funsd.zip") as archive:
        images = sorted(
            name
            for name in archive.namelist()
            if name.startswith("FUNSD/testing_data/images/") and Path(name).suffix.lower() == ".png"
        )
        selected = evenly_spaced(images, SOURCES["funsd"].count)
        for index, image_member in enumerate(selected):
            stem = Path(image_member).stem
            annotation_member = f"FUNSD/testing_data/annotations/{stem}.json"
            add_case(
                records,
                output,
                "funsd",
                stem,
                Path(image_member).name,
                archive.read(image_member),
                json.loads(archive.read(annotation_member)),
                "test",
                index,
            )


def prepare_xfund(downloads: Path, output: Path, records: list[dict[str, Any]]) -> None:
    annotation = json.loads((downloads / "xfund-zh-val.json").read_text(encoding="utf-8"))
    documents = {document["id"]: document for document in annotation["documents"]}
    with zipfile.ZipFile(downloads / "xfund-zh-val.zip") as archive:
        images = sorted(
            (name for name in archive.namelist() if Path(name).suffix.lower() == ".jpg"),
            key=lambda name: int(Path(name).stem.rsplit("_", 1)[-1]),
        )
        selected = evenly_spaced(images, SOURCES["xfund"].count)
        for index, image_member in enumerate(selected):
            stem = Path(image_member).stem
            add_case(
                records,
                output,
                "xfund",
                stem,
                Path(image_member).name,
                archive.read(image_member),
                documents[stem],
                "validation",
                index,
            )


def prepare_cord(downloads: Path, output: Path, records: list[dict[str, Any]]) -> None:
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError("准备 CORD 样本需要 duckdb；请通过配套 PowerShell 脚本运行。") from error

    parquet = downloads / "cord-v2-test.parquet"
    connection = duckdb.connect()
    try:
        row_count = connection.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(parquet)]
        ).fetchone()[0]
        indices = evenly_spaced(list(range(row_count)), SOURCES["cord"].count)
        for source_index, row_index in enumerate(indices):
            image, ground_truth = connection.execute(
                "SELECT image, ground_truth FROM read_parquet(?) LIMIT 1 OFFSET ?",
                [str(parquet), row_index],
            ).fetchone()
            image_name = image.get("path") or f"cord_test_{row_index:03d}.png"
            add_case(
                records,
                output,
                "cord",
                f"test-{row_index:03d}",
                Path(image_name).name,
                bytes(image["bytes"]),
                json.loads(ground_truth),
                "test",
                source_index,
            )
    finally:
        connection.close()


def prepare_doclaynet(downloads: Path, output: Path, records: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(downloads / "doclaynet-dev.zip") as archive:
        images = sorted(
            name
            for name in archive.namelist()
            if name.startswith("PNG/") and Path(name).suffix.lower() == ".png"
        )
        selected = evenly_spaced(images, SOURCES["doclaynet"].count)
        for index, image_member in enumerate(selected):
            stem = Path(image_member).stem
            annotation_member = f"JSON/{stem}.json"
            add_case(
                records,
                output,
                "doclaynet",
                stem,
                Path(image_member).name,
                archive.read(image_member),
                json.loads(archive.read(annotation_member)),
                "development",
                index,
            )
        write_bytes(output / "licenses" / "DocLayNet-LICENSE.txt", archive.read("LICENSE"))


def prepare_ctdar(downloads: Path, output: Path, records: list[dict[str, Any]]) -> None:
    with zipfile.ZipFile(downloads / "ctdar-tracka-test.zip") as archive:
        images = sorted(
            name
            for name in archive.namelist()
            if Path(name).suffix.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
        )
        selected = evenly_spaced(images, SOURCES["ctdar"].count)
        for index, image_member in enumerate(selected):
            stem = Path(image_member).stem
            add_case(
                records,
                output,
                "ctdar",
                stem,
                Path(image_member).name,
                archive.read(image_member),
                {
                    "available": False,
                    "reason": "Track A test images do not include public text transcription; score only table presence and stability.",
                },
                "test",
                index,
            )


def copy_provenance(downloads: Path, output: Path) -> None:
    licenses = output / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for name in (
        "cord-LICENSE-CC-BY.txt",
        "funsd-LICENSE.txt",
        "funsd-README.md",
        "xfund-README.md",
        "ctdar-metadata.json",
    ):
        source = downloads / name
        if source.is_file():
            shutil.copy2(source, licenses / name)


def validate_downloads(downloads: Path) -> None:
    for name, expected in EXPECTED_DOWNLOADS.items():
        path = downloads / name
        if not path.is_file():
            raise FileNotFoundError(f"缺少官方归档或标注：{path}")
        if expected and sha256(path).lower() != expected:
            raise ValueError(f"下载文件校验失败：{name}")


def write_manifest(output: Path, records: list[dict[str, Any]]) -> None:
    if len(records) != 120:
        raise ValueError(f"基准集数量错误：期望 120，实际 {len(records)}")
    manifest = output / "manifest.csv"
    with manifest.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    with (output / "manifest.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_readme(output: Path, records: list[dict[str, Any]]) -> None:
    counts = {source: sum(record["source"] == spec.name for record in records) for source, spec in SOURCES.items()}
    lines = [
        "# OCR 120 张本地回归基准集",
        "",
        "本目录仅供公司内部研发与回归测试，不是训练集，不得复制到平板运行包、产品安装包或公开仓库。",
        "所有样本均固定为 `allow_training=false`、`allow_redistribution=false`，许可原文和来源记录见 `licenses/`。",
        "",
        "## 组成",
        "",
    ]
    for key, spec in SOURCES.items():
        lines.append(f"- {spec.name}: {counts[key]} 张，目标模式 `{spec.expected_mode}`，评分范围 `{spec.metric_scope}`。")
    lines.extend(
        [
            "",
            "## 使用规则",
            "",
            "- `development` 可用于调整预处理和结构恢复；`holdout` 只用于最终验收，禁止针对单图写规则。",
            "- cTDaR Track A 测试集没有公开文字转写，只评估表格存在性、结构稳定性和不崩溃，不计文字准确率。",
            "- 任何数字、小数点、负号、百分号、日期和单位必须 exact-match；无法看清时应留空并标待确认，禁止猜测补全。",
            "- 图片、真值、运行输出和报告始终留在本机，不随产品部署。",
        ]
    )
    (output / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从已下载的官方归档构建固定 120 张 OCR 回归基准集")
    parser.add_argument("--downloads", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    downloads = args.downloads.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"输出目录非空，为避免覆盖现有基准集已停止：{output}")
    output.mkdir(parents=True, exist_ok=True)
    validate_downloads(downloads)
    records: list[dict[str, Any]] = []
    prepare_funsd(downloads, output, records)
    prepare_xfund(downloads, output, records)
    prepare_cord(downloads, output, records)
    prepare_doclaynet(downloads, output, records)
    prepare_ctdar(downloads, output, records)
    copy_provenance(downloads, output)
    write_manifest(output, records)
    write_readme(output, records)
    print(json.dumps({"status": "ok", "count": len(records), "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
