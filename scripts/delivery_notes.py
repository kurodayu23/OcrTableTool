#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付说明.md 的"模板 + 生成 / 校验"钩子（纯标准库）。

## 为什么要有这个脚本

``交付说明.md`` 随交付包一起发给客户，里面有**随包而变**的数字：整包文件数、
整包体积、运行时本体文件数 / 字节数 / 最新修改时间、交付 exe 体积、许可文件份数。
2026-10-27 之前这份文件**不在版本库里**，是手工维护在交付位上的：换台机器重打包
就会丢，而且包重建后这些数字会静默过期（客户拿到的是"上一版包的体积"）。

现在：**版本库里的模板是唯一来源**，打包时用真实目录重算数字渲染出来；
CI 里用 ``verify`` 把渲染结果和交付位里的文件逐字节比，过期立刻红。

## 哪些数字必须重算，哪些必须**手工**改

必须重算（本脚本自动算，全部来自实际目录遍历）::

    {{EXE_BYTES}}          ocr-runtime\\OcrBackend\\OcrBackend.exe 的字节数
    {{EXE_MIB}}            同上，四舍五入到 0.1 MiB
    {{PACKAGE_FILES}}      交付包内全部文件数
    {{PACKAGE_MIB}}        交付包总体积，0.1 MiB
    {{RUNTIME_FILES}}      ocr-runtime\\OcrBackend 子树文件数（"运行时本体"口径）
    {{RUNTIME_BYTES}}      同上子树总字节数
    {{RUNTIME_MIB}}        同上，0.1 MiB
    {{DELTA_KB}}           整包与运行时本体的字节差，KB
    {{EXTRA_FILES}}        PACKAGE_FILES - RUNTIME_FILES（包内非运行时文件数）
    {{ROOT_DELIVERY_FILES}}交付根目录下的文件数（selfcheck.bat / 交付说明.md / …）
    {{LICENSE_FILES}}      licenses\\ 下的文件数
    {{RUNTIME_MAX_MTIME}}  运行时子树里最新的文件修改时间

不得自动重算（脚本**故意**不碰，改它们必须人工判断）::

    * 准确率 / 耗时 / 内存等**历史测量记录**（它们描述某一次特定语料运行，
      连同"口径"一起才有意义，跟着包重建而自动变化就等于伪造记录）；
    * 构建指纹 ``DCF04B22A473…`` 之类"本交付件是哪个后端构建"的标记；
    * ``licenses\\`` 内部分类明细（57 份里 45 份实体 / 2 份声明 / 10 份冗余）；
    * ``{{MEASURED_AT}}``：清点时间，渲染时写入，校验时**从文件里回读**，
      因此不参与差异判定。

## 用法::

    # 打包时渲染
    python -B scripts/delivery_notes.py render \\
        --package-dir D:\\...\\package\\backend-delivery \\
        --out D:\\...\\package\\backend-delivery\\交付说明.md

    # CI / 交付位自检：逐字节校验（渲染到临时文件后比对）
    python -B scripts/delivery_notes.py verify --package-dir D:\\...\\package\\backend-delivery

退出码：0 = 一致 / 通过，1 = 数字过期或不一致，2 = 目录结构不对（缺 exe 等）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "packaging" / "delivery" / "交付说明.md.tmpl"
SELFCHECK = REPO_ROOT / "packaging" / "delivery" / "selfcheck.bat"
OUTPUT_NAME = "交付说明.md"
RUNTIME_SUBTREE = Path("ocr-runtime") / "OcrBackend"

PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Z_]+)\}\}")
MEASURED_AT_PATTERN = re.compile(r"\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) 于交付位实际清点\*\*")

MIB = 1024 * 1024


def _stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _mib(total_bytes: int) -> str:
    return "%.1f" % (total_bytes / MIB)


# 2026-10-30（lead 复查 P1）：统计**必须排除构建残留与字节码缓存**。
# 原来 `rglob("*")` 无任何过滤，于是 `pyinstaller-work/` 的中间产物、`__pycache__/`、
# `*.pyc` 都会被算进客户可见的"整包文件数/体积"（实测一次构建把 430 文件 / 537.7 MiB
# 抬到 446 文件 / 571.0 MiB）。更糟的是渲染与校验走同一个函数 ⇒ 永远"自洽"，
# 校验抓不到口径错误。
_EXCLUDED_DIR_NAMES = {
    "pyinstaller-work",
    "__pycache__",
    ".git",
    ".vs",
    ".idea",
    ".vscode",
}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".bak", ".tmp", ".orig", ".rej"}


def _is_counted(path: Path, package_dir: Path) -> bool:
    """这个文件是否应计入交付统计（排除构建残留/缓存/备份）。"""
    try:
        relative = path.relative_to(package_dir)
    except ValueError:  # pragma: no cover - 理论上不会发生
        return False
    if any(part in _EXCLUDED_DIR_NAMES for part in relative.parts[:-1]):
        return False
    if path.suffix.lower() in _EXCLUDED_SUFFIXES:
        return False
    return True


def collect_facts(package_dir: Path) -> dict[str, str]:
    """从真实交付目录算出所有随包而变的数字。"""
    package_dir = Path(package_dir)
    runtime = package_dir / RUNTIME_SUBTREE
    executable = runtime / "OcrBackend.exe"
    if not executable.is_file():
        raise SystemExit("找不到交付 exe：%s" % executable)
    package_files = [
        path for path in package_dir.rglob("*")
        if path.is_file() and _is_counted(path, package_dir)
    ]
    runtime_files = [
        path for path in runtime.rglob("*")
        if path.is_file() and _is_counted(path, package_dir)
    ]
    if not runtime_files:
        raise SystemExit("运行时本体为空：%s" % runtime)
    package_bytes = sum(path.stat().st_size for path in package_files)
    runtime_bytes = sum(path.stat().st_size for path in runtime_files)
    licenses_dir = package_dir / "licenses"
    license_files = (
        [path for path in licenses_dir.rglob("*") if path.is_file()]
        if licenses_dir.is_dir()
        else []
    )
    root_files = [path for path in package_dir.iterdir() if path.is_file()]
    newest = max(runtime_files, key=lambda path: path.stat().st_mtime)
    return {
        "EXE_BYTES": "{:,}".format(executable.stat().st_size),
        "EXE_MIB": _mib(executable.stat().st_size),
        "PACKAGE_FILES": str(len(package_files)),
        "PACKAGE_MIB": _mib(package_bytes),
        "RUNTIME_FILES": str(len(runtime_files)),
        "RUNTIME_BYTES": "{:,}".format(runtime_bytes),
        "RUNTIME_MIB": _mib(runtime_bytes),
        "DELTA_KB": "{:,}".format(round((package_bytes - runtime_bytes) / 1024)),
        "EXTRA_FILES": str(len(package_files) - len(runtime_files)),
        "ROOT_DELIVERY_FILES": str(len(root_files)),
        "LICENSE_FILES": str(len(license_files)),
        "RUNTIME_MAX_MTIME": dt.datetime.fromtimestamp(
            newest.stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def render_text(template_text: str, facts: dict[str, str], measured_at: str) -> str:
    values = dict(facts)
    values["MEASURED_AT"] = measured_at

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise SystemExit("模板里的占位符没有对应的计算值：{{%s}}" % key)
        return values[key]

    rendered = PLACEHOLDER_PATTERN.sub(replace, template_text)
    leftover = PLACEHOLDER_PATTERN.findall(rendered)
    if leftover:
        raise SystemExit("渲染后仍有未替换的占位符：%s" % ", ".join(sorted(set(leftover))))
    return rendered


def render_file(
    package_dir: Path,
    output: Path | None = None,
    template: Path = TEMPLATE,
    measured_at: str | None = None,
) -> Path:
    template_text = Path(template).read_text(encoding="utf-8")
    if not measured_at:
        measured_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    target = Path(output) if output else Path(package_dir) / OUTPUT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    # 交付说明.md 自己也算在"整包文件数 / 体积"里：写出后文件大小变了，
    # 数字可能跟着跨过 0.1 MiB 的取整边界。所以反复算到稳定为止（通常 2 轮到顶），
    # 保证 render(verify) 幂等，不会在边界上抖动成"永远过期"。
    for _ in range(4):
        facts = collect_facts(package_dir)
        rendered = render_text(template_text, facts, measured_at)
        previous = target.read_bytes() if target.is_file() else None
        target.write_text(rendered, encoding="utf-8", newline="\n")
        if previous == rendered.encode("utf-8"):
            break
    return target


def _first_difference(expected: str, actual: str) -> str:
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    for index in range(max(len(expected_lines), len(actual_lines))):
        left = expected_lines[index] if index < len(expected_lines) else "<文件结束>"
        right = actual_lines[index] if index < len(actual_lines) else "<文件结束>"
        if left != right:
            return "第 %d 行\n  模板算出: %s\n  交付位是: %s" % (index + 1, left, right)
    return "行内容一致但字节不一致（可能是行尾 / BOM 差异）"


def verify(package_dir: Path, template: Path = TEMPLATE) -> dict:
    package_dir = Path(package_dir)
    delivered = package_dir / OUTPUT_NAME
    if not delivered.is_file():
        return {
            "ok": False,
            "summary": "交付位没有 %s：%s" % (OUTPUT_NAME, delivered),
            "facts": {},
        }
    actual = delivered.read_text(encoding="utf-8")
    match = MEASURED_AT_PATTERN.search(actual)
    measured_at = match.group(1) if match else ""
    if not measured_at:
        return {
            "ok": False,
            "summary": "交付位的 %s 里找不到“清点时间”标记，无法校验是否由模板生成" % OUTPUT_NAME,
            "facts": {},
        }
    facts = collect_facts(package_dir)
    expected = render_text(Path(template).read_text(encoding="utf-8"), facts, measured_at)
    if expected == actual:
        return {
            "ok": True,
            "summary": "交付说明.md 与模板 + 实际交付目录一致（%d 个数字重算后逐字节相同）"
            % len(facts),
            "facts": facts,
        }
    difference = _first_difference(expected, actual)
    return {
        "ok": False,
        "summary": "交付说明.md 已过期：" + difference,
        "facts": facts,
        "first_difference": difference,
    }


def main(argv=None) -> int:
    _stdout_utf8()
    parser = argparse.ArgumentParser(description="交付说明.md 模板渲染 / 校验")
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render", help="按实际交付目录渲染交付说明.md")
    render_parser.add_argument("--package-dir", type=Path, required=True)
    render_parser.add_argument("--out", type=Path, default=None, help="输出路径（默认 <package-dir>\\交付说明.md）")
    render_parser.add_argument("--template", type=Path, default=TEMPLATE)
    render_parser.add_argument("--measured-at", default=None, help='清点时间，形如 "2026-10-24 08:42"')

    verify_parser = subparsers.add_parser("verify", help="校验交付位里的交付说明.md 是否过期")
    verify_parser.add_argument("--package-dir", type=Path, required=True)
    verify_parser.add_argument("--template", type=Path, default=TEMPLATE)
    verify_parser.add_argument("--json", action="store_true")

    facts_parser = subparsers.add_parser("facts", help="只打印重算出来的数字（排查用）")
    facts_parser.add_argument("--package-dir", type=Path, required=True)
    facts_parser.add_argument("--json", action="store_true")

    arguments = parser.parse_args(argv)
    if arguments.command == "render":
        target = render_file(
            arguments.package_dir, arguments.out, arguments.template, arguments.measured_at
        )
        print("已渲染: %s" % target)
        for key, value in collect_facts(arguments.package_dir).items():
            print("  %-20s %s" % (key, value))
        return 0
    if arguments.command == "facts":
        facts = collect_facts(arguments.package_dir)
        if arguments.json:
            import json

            print(json.dumps(facts, ensure_ascii=False, indent=2))
        else:
            for key, value in facts.items():
                print("%-20s %s" % (key, value))
        return 0

    result = verify(arguments.package_dir, arguments.template)
    if arguments.json:
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(("[PASS] " if result["ok"] else "[FAIL] ") + result["summary"])
        for key, value in result.get("facts", {}).items():
            print("  %-20s %s" % (key, value))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
