import argparse
import shutil
from pathlib import Path

import rapid_table
from rapidocr import RapidOCR
from rapid_table import ModelType, RapidTable, RapidTableInput

from ocr_backend import (
    build_fast_model_download_parameters,
    build_model_download_parameters,
    model_directory,
    validate_model_files,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir")
    arguments = parser.parse_args()
    models = Path(arguments.model_dir).resolve() if arguments.model_dir else model_directory()
    models.mkdir(parents=True, exist_ok=True)

    RapidOCR(params=build_model_download_parameters(models))
    RapidOCR(params=build_fast_model_download_parameters(models))
    packaged_table_model = Path(rapid_table.__file__).resolve().parent / "models" / "slanet-plus.onnx"
    target_table_model = models / packaged_table_model.name
    if not target_table_model.is_file():
        if not packaged_table_model.is_file():
            RapidTable(RapidTableInput(model_type=ModelType.SLANETPLUS, use_ocr=False))
        if packaged_table_model.is_file():
            shutil.copy2(packaged_table_model, target_table_model)
    if not target_table_model.is_file():
        raise FileNotFoundError("表格结构模型缺失：slanet-plus.onnx")
    RapidTable(
        RapidTableInput(
            model_type=ModelType.SLANETPLUS,
            model_dir_or_path=target_table_model,
            use_ocr=False,
        )
    )
    validate_model_files(models)
    print(f"OCR models are ready: {models}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
