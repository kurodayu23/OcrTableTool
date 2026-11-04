"""Delivery package self-verification.

Runs the *packaged* backend executable, not the development tree.  A package
can look perfect on disk and still be broken: PyInstaller puts pure-Python
packages into the PYZ archive instead of a directory, so the absence of
``_internal\\openpyxl`` says nothing, while the absence of
``_internal\\onnxruntime`` means the table structure engine cannot start at
all -- and because the three table-engine call sites swallow the exception,
the delivered build simply loses table structure detection with no error in
the log.  The development machine has onnxruntime installed, so the bug is
invisible until the tablet gets the package.

Every check therefore ends in "run the exe and look at the answer":

1. models            - every model in MODEL_HASHES exists and matches SHA-256
2. dictionary        - en_US.dic and its licence ship next to the executable
3. native runtime    - the ONNX Runtime native library is present
4. health action     - the executable answers and reports valid models
5. export_xlsx       - openpyxl really is importable inside the frozen app
6. recognize         - the table structure engine really runs (ONNX Runtime)

Usage:
    python verify_delivery_package.py --package-dir <OcrBackend directory>
                                      [--backend-source <backend directory>]

``--backend-source`` may be omitted: the script then walks up from
``--package-dir`` looking for ``backend/ocr_backend.py``.  When no backend
source can be found at all the two model checks are reported as FAIL -- the
gate never prints "everything passed" for a check it could not perform.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any


def _safe_working_directory(executable: Path) -> str | None:
    """返回可作 `cwd` 的目录；UNC 路径不能当 cwd（2026-10-30 lead 复查 P2）。

    客户/复检脚本从网络共享（`\\\\server\\share\\...`）运行时，把 UNC 目录传给
    `subprocess.run(cwd=...)` 会抛 `NotADirectoryError [WinError 267]`，
    health / 导出 / 识别三条实跑全部 FAIL。UNC 下返回 None（用当前目录），
    EXE 仍以绝对路径启动。
    """
    directory = str(executable.parent)
    if directory.startswith("\\\\") or directory.startswith("//"):
        return None
    return directory


def _run_executable(executable: Path, request: dict[str, Any], timeout: int) -> dict[str, Any]:
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    completed = subprocess.run(
        [str(executable)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=_safe_working_directory(executable),
    )
    text = completed.stdout.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError(
            "no response from %s (stderr: %s)"
            % (executable.name, completed.stderr.decode("utf-8", "replace")[-800:])
        )
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise RuntimeError("response is not JSON: %s" % text[-800:])


def _model_hashes_from_source(source: Path) -> tuple[dict[str, str], str]:
    """Read ``MODEL_HASHES`` out of the backend source **without importing it**.

    2026-10-23: importing ``ocr_backend`` installs the offline audit hook into
    *this* process, and the hook denies ``subprocess.Popen`` -- so the delivery
    self-check could no longer launch the very executable it is supposed to
    verify (health / export_xlsx / recognize all failed with "blocked starting
    an external program").  ``MODEL_HASHES`` is a module level literal, so an
    AST read gives the same mapping with no side effects at all.

    Returns ``(mapping, error)``; ``error`` is non-empty when the manifest
    could not be read, and the caller then reports FAIL instead of silently
    skipping the model checks.
    """
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except Exception as error:  # noqa: BLE001 - report, never abort
        return {}, "cannot parse backend sources: %s" % error
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "MODEL_HASHES" not in targets:
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception as error:  # noqa: BLE001 - report, never abort
            return {}, "MODEL_HASHES is not a literal mapping: %s" % error
        if not isinstance(value, dict):
            return {}, "MODEL_HASHES is not a mapping"
        return {str(key): str(item) for key, item in value.items()}, ""
    return {}, "MODEL_HASHES not found in %s" % source


def _synthetic_table_image(path: Path) -> None:
    """Write a small ruled table so the table structure engine has real work."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 520), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = 60, 60, 840, 460
    rows, columns = 6, 5
    for index in range(rows + 1):
        y = top + (bottom - top) * index // rows
        draw.line([(left, y), (right, y)], fill=(0, 0, 0), width=3)
    for index in range(columns + 1):
        x = left + (right - left) * index // columns
        draw.line([(x, top), (x, bottom)], fill=(0, 0, 0), width=3)
    for row in range(rows):
        for column in range(columns):
            x = left + (right - left) * column // columns + 12
            y = top + (bottom - top) * row // rows + 10
            draw.text((x, y), "%d-%d" % (row, column), fill=(0, 0, 0))
    image.save(path)


REQUIRED_BACKEND_MODULES = (
    "fast_stats",
    "colored_table",
    "table_pipeline",
    "recognition_scheduler",
)

# The rapid_table ONNX Runtime engine is pure Python, so PyInstaller freezes it
# into the PYZ archive.  The copy under ``_internal`` is only a shadow
# duplicate: the frozen application imports the archive entry (FrozenImporter
# wins over sys.path), so a file-existence check on ``_internal`` proves
# nothing about the module the package really runs.
#
# The ONNX Runtime *native* library is the opposite case: it is a ``.pyd`` and
# never lives in the archive, so it must keep being checked under ``_internal``.
REQUIRED_PYZ_ENGINE_MODULES = (
    "rapid_table.inference_engine.onnxruntime.main",
)


@lru_cache(maxsize=4)
def _read_pyz_names(package_key: str) -> tuple[str, frozenset[str], str]:
    """Read the module names stored in the frozen PYZ archive.

    Returns ``(status, names, detail)``:

    ``"ok"``
        the archive was read; ``names`` holds every module entry.
    ``"skip"``
        the archive could not be inspected at all (PyInstaller is not
        installed here).  The caller must report SKIP -- never PASS.
    ``"fail"``
        the archive is expected to be there but is unreadable, which is a
        broken package.
    """
    package = Path(package_key)
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except Exception as error:  # noqa: BLE001 - PyInstaller is a dev dependency
        return "skip", frozenset(), "skipped: PyInstaller not importable (%s)" % error
    executable = package / "OcrBackend.exe"
    if not executable.is_file():
        return "fail", frozenset(), "executable missing: %s" % executable
    try:
        reader = CArchiveReader(str(executable))
        payload = reader.extract("PYZ.pyz")
    except Exception as error:  # noqa: BLE001
        return "fail", frozenset(), "cannot read PYZ: %s" % error
    # 2026-10-30（lead 复查 P2）：临时文件原来用**固定名**
    # `%TEMP%\ocr-delivery-pyz-check.pyz`，而批量复检脚本会把 TEMP 统一到同一个
    # 临时目录 ⇒ 两路并行复检互相覆盖，报出假的"cannot read PYZ entries"。
    # 改成每个进程/每次调用唯一的名字。
    handle, temporary_name = tempfile.mkstemp(prefix="ocr-delivery-pyz-", suffix=".pyz")
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(payload)
    except OSError as error:  # noqa: BLE001
        try:
            temporary.unlink()
        except OSError:
            pass
        return "fail", frozenset(), "cannot stage PYZ: %s" % error
    try:
        names = frozenset(ZlibArchiveReader(str(temporary)).toc)
    except Exception as error:  # noqa: BLE001
        return "fail", frozenset(), "cannot read PYZ entries: %s" % error
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return "ok", names, "read %d PYZ entries" % len(names)


def _bundled_backend_modules(package: Path) -> tuple[bool | None, str]:
    """Report which backend modules are really inside the frozen archive.

    Returns (True, detail) when the archive could be inspected, (False, detail)
    when the modules are missing, and **(None, detail) when the check could not
    be performed at all** (PyInstaller is not installed here).

    2026-10-22：以前这里在 PyInstaller 缺失时返回 True，调用方把它记成 PASS
    —— 一个**根本没检查过**的项被算成通过，退出码还是 0。交付方在没有
    PyInstaller 的机器上复检时，缺模块的破损包会被判为合格。跳过必须是
    第三种状态，不能混进 PASS。
    """
    status, names, detail = _read_pyz_names(str(package))
    if status == "skip":
        return None, detail
    if status == "fail":
        return False, detail
    missing = [name for name in REQUIRED_BACKEND_MODULES if name not in names]
    if missing:
        return False, "missing from PYZ: %s" % ", ".join(missing)
    return True, "present: %s" % ", ".join(REQUIRED_BACKEND_MODULES)


def _rapid_table_engine_module(package: Path) -> tuple[bool | None, str]:
    """Report whether the frozen rapid_table ONNX Runtime engine is usable.

    2026-10-23（F5）：以前这项查的是
    ``_internal\\rapid_table\\inference_engine\\onnxruntime\\main.py``——
    那是 PyInstaller 落地的**影子副本**，冻结态真正 import 的是 PYZ 归档里
    的同名条目（FrozenImporter 优先于 sys.path）。查影子副本等于没查。
    改成查归档条目；原生 .pyd 不在归档里，仍留在 ``_internal`` 单独查。
    """
    status, names, detail = _read_pyz_names(str(package))
    if status == "skip":
        return None, detail
    if status == "fail":
        return False, detail
    missing = [name for name in REQUIRED_PYZ_ENGINE_MODULES if name not in names]
    if missing:
        return False, "missing from PYZ: %s" % ", ".join(missing)
    return True, "present: %s" % ", ".join(REQUIRED_PYZ_ENGINE_MODULES)


def _resolve_backend_source(package: Path) -> Path | None:
    """Find the backend sources that define the expected model list.

    2026-10-22：默认值是 ``package.parents[2] / "backend"``。对着真实交付位
    ``...\\package\\backend-delivery\\ocr-runtime\\OcrBackend`` 算出来是
    ``...\\package\\backend``（不存在）→ 两项模型校验被**整段跳过**，只报
    10/12，看上去像"少了两项"而不是"没查"。改成向上逐级找
    ``backend/ocr_backend.py``，找不到就明确失败。

    2026-10-24（D-06）：上面这条只沿**包路径**往上找。一旦把交付包单独拷到别处
    （例如给客户做验收、或换机复跑），包路径的祖先里没有源码树 ⇒ 两项模型校验
    恒 FAIL（原因串 ``backend sources not found``），**看起来像包坏了，其实只是
    找不到可比对的源码**。补一条候选：**本脚本自己所在的源码树**的 ``backend``。
    这样"脚本在源码树里 + 包在任意位置"都能自动定位到源码。
    ⚠ **判定语义不变**：仍然找不到源码时依旧 FAIL —— "没查"必须等同于"不通过"
    （2026-10-22 的加固），此处只增强**定位能力**，不放松**判定**。
    """
    for parent in package.parents:
        candidate = parent / "backend" / "ocr_backend.py"
        if candidate.is_file():
            return candidate.parent
    # 本脚本所在源码树（scripts/ 的上一级）里的 backend/
    try:
        source_root = Path(__file__).resolve().parent.parent
        candidate = source_root / "backend" / "ocr_backend.py"
        if candidate.is_file():
            return candidate.parent
    except NameError:  # pragma: no cover - __file__ 不可用时静默降级
        pass
    return None


def _table_structure_engine_check(response: dict[str, Any]) -> tuple[bool | None, str]:
    """Decide the "table structure engine usable" item from a recognize response.

    2026-10-23（F-04 残留）：以前是
    ``warnings = response.get("structure_warnings") or []``，空列表即 PASS ——
    "这一步根本没跑/没产出"和"查过且通过"在报告里长得一模一样。改成三态：

    * 字段缺失                     -> SKIP（查不了，不算通过）
    * 字段在但为空列表             -> SKIP（没有证据说明引擎跑过）
    * 字段在、非空且无引擎失败告警 -> PASS
    * 有引擎失败告警               -> FAIL
    """
    raw_warnings = response.get("structure_warnings")
    if raw_warnings is None:
        return None, (
            "response has no structure_warnings field "
            "(cannot confirm the table engine ran)"
        )
    warnings = [str(item) for item in raw_warnings]
    broken = [item for item in warnings if "结构模型" in item]
    if broken:
        return False, broken[0][:160]
    if warnings:
        return True, "%d warning(s), no table-engine failure" % len(warnings)
    return None, "structure_warnings is empty (no evidence the table engine ran)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a packaged OcrBackend")
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--backend-source", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=180)
    arguments = parser.parse_args()

    package = arguments.package_dir.resolve()
    executable = package / "OcrBackend.exe"
    failures: list[str] = []
    notes: list[str] = []
    skipped: list[str] = []
    messages: list[str] = []
    backend_source = arguments.backend_source
    if backend_source is None:
        backend_source = _resolve_backend_source(package)
        if backend_source is None:
            messages.append(
                "NOTE backend source could not be auto-resolved; pass "
                "--backend-source <...\\backend> or the two model checks FAIL"
            )
        else:
            messages.append("NOTE backend source auto-resolved: %s" % backend_source)
    else:
        messages.append("NOTE backend source given: %s" % backend_source)

    def check(name: str, condition: bool | None, detail: str = "") -> None:
        """Record one check.

        ``condition`` is tri-state: True = pass, False = fail, **None = the
        check could not be performed**.  A skipped check must never be counted
        as a pass (2026-10-22: the "backend modules bundled" item used to be
        reported as PASS whenever PyInstaller was missing, so a broken package
        sailed through the gate on any machine without PyInstaller).
        """
        if condition is None:
            skipped.append("SKIP %s%s" % (name, (" - " + detail) if detail else ""))
        elif condition:
            notes.append("PASS %s%s" % (name, (" - " + detail) if detail else ""))
        else:
            failures.append("FAIL %s%s" % (name, (" - " + detail) if detail else ""))

    check("executable present", executable.is_file(), str(executable))

    # 1. models ----------------------------------------------------------------
    model_hashes: dict[str, str] = {}
    model_baseline_error = ""
    if backend_source is not None and (backend_source / "ocr_backend.py").is_file():
        # 2026-10-23：以前这里是 ``import ocr_backend`` 来取 MODEL_HASHES。
        # 后端模块在 import 时会 ``sys.addaudithook`` 装上**离线钩子**，而钩子会拦
        # ``subprocess.Popen`` —— 于是这个自检脚本**自己**再也启动不了交付 exe：
        # health / export_xlsx / recognize 三项全部报「已拦截「启动外部程序」」，
        # 打包流程的最终闸门恒 FAIL，包被判为"不可交付"。
        #
        # 自检脚本的职责是**观察**交付物，不是在自己进程里跑产品代码。
        # MODEL_HASHES 是模块级字面量，用 AST 读出来既不装钩子、不引入 cv2/openvino，
        # 也更快。读不到就明确 FAIL（"没查"不能算"通过"）。
        model_hashes, model_baseline_error = _model_hashes_from_source(
            backend_source / "ocr_backend.py"
        )
    else:
        model_baseline_error = (
            "backend sources not found (pass --backend-source <...\\backend>)"
        )
    model_directory = package / "models"
    if model_hashes:
        missing = [name for name in model_hashes if not (model_directory / name).is_file()]
        check("all models staged", not missing, "missing: %s" % ", ".join(missing[:6]))
        mismatched = []
        for name, expected in model_hashes.items():
            path = model_directory / name
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                mismatched.append(name)
        check("model hashes match", not mismatched, "mismatched: %s" % ", ".join(mismatched[:6]))
        extra = [
            path.name
            for path in model_directory.glob("*")
            if path.is_file() and path.name not in model_hashes
        ]
        # 2026-10-23（F7）：多余模型以前只记一条 WARN —— 而那条 WARN 被写进
        # `notes`，`notes` 正是"通过数"用的列表，于是它不但不影响退出码，
        # 反而虚增了通过数。交付位多塞一个废弃模型照样全绿。改成 FAIL。
        check(
            "no extra files in models/",
            not extra,
            ("extra: %s" % ", ".join(sorted(extra)[:6])) if extra else "",
        )
    else:
        # 2026-10-22：以前这里是 `if model_hashes:` 守卫，读不到 MODEL_HASHES 时
        # 两项模型校验**整段跳过**、没有任何 FAIL、退出码还是 0 —— 模型缺失和
        # 文件被替换都查不出来。交付闸门"没查"必须等同于"不通过"。
        check("all models staged", False, model_baseline_error)
        check("model hashes match", False, model_baseline_error)

    # 2. dictionary ------------------------------------------------------------
    for name in ("en_US.dic", "SCOWL-LICENSE.txt"):
        check("%s shipped" % name, (package / name).is_file())

    # 3. native runtime --------------------------------------------------------
    # The native library is a .pyd: it never lives in the PYZ archive, so it
    # must be looked for on disk under _internal.
    native = package / "_internal" / "onnxruntime" / "capi" / "onnxruntime_pybind11_state.pyd"
    check("onnxruntime native library present", native.is_file(), str(native))
    # The rapid_table engine shim is pure Python: the frozen app imports it
    # from the PYZ archive, not from the shadow copy in _internal (F5).
    check("rapid_table onnxruntime engine in PYZ", *_rapid_table_engine_module(package))

    # 3b. backend modules inside the frozen archive ----------------------------
    # A backend module that PyInstaller failed to collect makes the packaged
    # app fall back to a slower path (2026-10-22: colored_table falls back to
    # np.median when fast_stats is missing) or crash outright.  The fallback
    # is silent, so "the package runs" is not enough -- look inside the
    # archive and require the module to actually be there.
    check("backend modules bundled", *_bundled_backend_modules(package))

    if not executable.is_file():
        for line in messages:
            print(line)
        for line in notes:
            print(line)
        for line in failures:
            print(line)
        for line in skipped:
            print(line)
        print(
            "summary: %d passed, %d failed, %d skipped"
            % (len(notes), len(failures), len(skipped))
        )
        return 1

    # 4. health ----------------------------------------------------------------
    try:
        response = _run_executable(executable, {"protocol": 1, "action": "health"}, arguments.timeout)
        check(
            "health action",
            response.get("status") == "ok" and bool(response.get("models_valid")),
            "status=%s models_valid=%s" % (response.get("status"), response.get("models_valid")),
        )
    except Exception as error:  # noqa: BLE001
        check("health action", False, str(error))

    # 5. export_xlsx -----------------------------------------------------------
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "self-check.xlsx"
        request = {
            "protocol": 1,
            "action": "export_xlsx",
            "output_path": str(output),
            # cells 必须是二维数组（外层每行、内层每列），不是扁平列表。
            "cells": [
                [{"text": "序号", "confidence": 0.99}, {"text": "名称", "confidence": 0.99}],
                [{"text": "1", "confidence": 0.99}, {"text": "示例", "confidence": 0.99}],
            ],
            "spans": [],
        }
        try:
            response = _run_executable(executable, request, arguments.timeout)
            produced = output.is_file() and zipfile.is_zipfile(output)
            check(
                "export_xlsx action",
                response.get("status") == "ok" and produced,
                "status=%s file=%s" % (response.get("status"), produced),
            )
        except Exception as error:  # noqa: BLE001
            check("export_xlsx action", False, str(error))

    # 6. recognize -------------------------------------------------------------
    with tempfile.TemporaryDirectory() as directory:
        image = Path(directory) / "table.png"
        _synthetic_table_image(image)
        request = {
            "protocol": 1,
            "action": "recognize",
            "image_path": str(image),
            "output_directory": str(Path(directory) / "out"),
            "options": {"crop_mode": "auto"},
        }
        try:
            response = _run_executable(executable, request, arguments.timeout)
            rows = int(response.get("rows", 0) or 0)
            columns = int(response.get("columns", 0) or 0)
            check(
                "recognize action",
                response.get("status") == "ok" and rows > 0 and columns > 0,
                "status=%s %sx%s engine=%s" % (
                    response.get("status"), rows, columns, response.get("engine", "")),
            )
            # 2026-10-23（F-04 残留）：以前是
            # `warnings = response.get("structure_warnings") or []`，空列表即
            # PASS —— "这一步根本没跑/没产出"和"查过且通过"在报告里长得
            # 一模一样。现在按三态判定（见 _table_structure_engine_check）：
            #   字段缺失              -> SKIP（查不了，不算通过）
            #   字段在但为空列表      -> SKIP（没有证据说明引擎跑过）
            #   字段在、非空且无引擎失败告警 -> PASS
            #   有引擎失败告警        -> FAIL
            check("table structure engine usable", *_table_structure_engine_check(response))
        except Exception as error:  # noqa: BLE001
            check("recognize action", False, str(error))

    for line in messages:
        print(line)
    for line in notes:
        print(line)
    for line in failures:
        print(line)
    for line in skipped:
        print(line)
    # 2026-10-22：摘要里加上 skipped。没有 PyInstaller 时 PYZ 那项跑不了，
    # 以前它被算成 PASS。现在它是第三种状态，必须在摘要里看得见。
    print(
        "summary: %d passed, %d failed, %d skipped"
        % (len(notes), len(failures), len(skipped))
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
