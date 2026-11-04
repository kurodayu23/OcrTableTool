#!/usr/bin/env python3
"""仓库门禁检查（纯标准库，秒级，不依赖 OpenCV / Qt / 网络）。

这里放的是**不跑产品代码就能查出来的流程型缺陷**，三项各自对应一次真实事故：

``bom``
    2026-10-28：有人改了 ``src\\core\\csvexporter.cpp`` 却把 UTF-8 BOM 丢了，
    MSVC2015 按 GBK 解释源码 ⇒ 整树 ``C2001`` / ``C1057`` 编译失败，
    直到跑 UI 契约测试才发现。带非 ASCII 字符的 ``.cpp`` / ``.h`` 必须带 BOM。

``identity``
    2026-10-28：``backend\\tests\\test_acceptance_runner_metrics.py`` 写死了
    ``D:\\AP\\AP7000\\OcrTableTool``（产品树镜像），而它是 unittest discover 的
    第一个模块，加载即把镜像塞进 ``sys.modules`` ⇒ 整套后端单测**测的是镜像**
    而不是被测工作树。测试里出现本机绝对路径就是这条事故的复现条件。

    判定只管**指向本项目本机路径**的绝对路径（盘符路径里含仓库根 / 产品树根 /
    ``AP7000`` / ``OcrTableTool`` / ``AI-MR-Upload-Workspace`` 字样），
    并且只看行内**代码**部分（``#`` 之前）—— 注释里"记载历史事故"的路径不算失败。
    确需写死时在**同一行**加豁免注释 ``gate:allow-machine-path`` 并写明理由。
    ``C:/Windows``、``C:/input.png``、``E:\\设备日志\\…`` 这类系统兜底值和测试夹具
    里的假路径只作提示，否则门禁会因为假路径常年变红、失去信号价值。

``mirror``
    产品树镜像 ``D:\\AP\\AP7000\\OcrTableTool`` 靠人工同步。协议约定镜像文件是
    **LF 行尾**、仓库工作树是 CRLF，所以逐字节比会全红；这里先做换行归一化
    （CRLF / CR -> LF）再比 SHA-256，比出来的是**真实内容差异**。

用法::

    python -B scripts/gate_checks.py bom
    python -B scripts/gate_checks.py identity --json
    python -B scripts/gate_checks.py mirror --product D:\\AP\\AP7000\\OcrTableTool
    python -B scripts/gate_checks.py all --json

退出码：0 = 全部通过，1 = 有失败项（``--json`` 时失败明细在 JSON 里）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# 本机约定的产品树镜像位置。测试 / CI 里一律通过环境变量
# OCRTABLE_PRODUCT_MIRROR 或 --product 覆盖，不要在 backend\tests、tests\ 里
# 再写一份（那正是 identity 门禁要抓的缺陷）。
DEFAULT_PRODUCT_MIRROR = Path("D:/AP/AP7000/OcrTableTool")

BOM = b"\xef\xbb\xbf"
BOM_SCAN_DIRS = ("src", "tests", "interface-sdk")
BOM_SUFFIXES = (".cpp", ".h")

IDENTITY_SCAN_DIRS = ("backend/tests", "tests")
# 本机绝对路径：盘符 + 分隔符。``D:\\AP\\``、``D:/AP/``、``C:/Windows`` 都命中。
MACHINE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]{1,2}(?![\\/])"
    r"""(?:[^\s"'`<>|,;:*?)\]}，。；、]+)"""
)
# 命中但确实必须写死的行，在同一行加这个注释即可豁免（并要求写明理由）。
ALLOW_MARKER = "gate:allow-machine-path"
# 只有指向**本项目本机路径**的写死路径才是那条事故的复现条件。
# C:/Windows、C:/input.png、E:\\设备日志\\ 这类是测试数据 / 系统兜底值，不算。
IDENTITY_PROJECT_HINTS = ("ap7000", "ai-mr-upload-workspace", "ocrtabletool")

MIRROR_DIRS = (
    "backend",
    "src",
    "tests",
    "scripts",
    "interface-sdk",
    "assets",
    "docs",
    "runtime",
    # packaging\ 是交付包内容的版本库源头（selfcheck.bat / 交付说明.md 模板），
    # scripts\package_backend.ps1 直接从这里取；镜像里少了它就没法在产品树重新打包。
    "packaging",
)
MIRROR_EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".git",
}
MIRROR_EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".log",
    ".tmp",
    ".bak",
    ".orig",
    ".rej",
    ".user",
)
# 只在产品树存在、且已被 .gitignore 排除的产物：模型权重、历史计划文档。
# 这些不是"漂移"，比对时跳过并把跳过条数报出来。
MIRROR_PRODUCT_ONLY_ALLOWED = ("runtime/models/", "docs/superpowers/")


def _stdout_utf8() -> None:
    """让中文在 pwsh / cmd 里输出不炸。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def normalize_newlines(data: bytes) -> bytes:
    """CRLF / CR 一律归一化成 LF（镜像协议约定 LF，仓库工作树是 CRLF）。"""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def content_digest(data: bytes) -> str:
    return hashlib.sha256(normalize_newlines(data)).hexdigest()


# --------------------------------------------------------------------------
# 1. BOM 门禁
# --------------------------------------------------------------------------
def check_bom(root: Path, subdirs=BOM_SCAN_DIRS) -> dict:
    root = Path(root)
    missing: list[str] = []
    undecodable: list[str] = []
    scanned = 0
    with_bom = 0
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in BOM_SUFFIXES:
                continue
            scanned += 1
            raw = path.read_bytes()
            relative = path.relative_to(root).as_posix()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                # 根本就不是 UTF-8 —— BOM 都无从谈起，MSVC2015 必然出错。
                undecodable.append(relative)
                continue
            has_bom = raw.startswith(BOM)
            if has_bom:
                with_bom += 1
            if not has_bom and any(ord(character) > 127 for character in text):
                missing.append(relative)
    ok = not missing and not undecodable
    if scanned == 0:
        # ⚠ 2026-11-02（F20）：一个文件都没扫到（目录改名 / 指错路径 / 源码被挪走）
        #   ⇒ 以前照样 ok=True ⇒ 门禁报「0 个 .cpp/.h，含非 ASCII 的全部带 BOM」——
        #   **一张真空 PASS**。没查 ≠ 通过。
        ok = False
    if ok:
        summary = "%d 个 .cpp/.h，含非 ASCII 的全部带 BOM（%d 个带 BOM）" % (
            scanned,
            with_bom,
        )
    elif scanned == 0:
        summary = "一个 .cpp/.h 都没扫到（src / tests / interface-sdk 目录不对？）"
    else:
        summary = "%d 个文件缺 BOM、%d 个不是 UTF-8" % (len(missing), len(undecodable))
    return {
        "check": "bom",
        "ok": ok,
        "scanned": scanned,
        "with_bom": with_bom,
        "missing_bom": missing,
        "undecodable": undecodable,
        "summary": summary,
    }


# --------------------------------------------------------------------------
# 2. 测试树身份门禁
# --------------------------------------------------------------------------
def _code_part(line: str) -> str:
    """去掉行内注释后再扫，避免把"记录历史事故"的注释当成违规。"""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return ""
    cut = line.find("#")
    return line if cut < 0 else line[:cut]


def _normalize_for_match(text: str) -> str:
    return text.lower().replace("/", "\\")


def _points_at_project(text: str, roots) -> bool:
    """这条绝对路径是否指向本项目的仓库 / 产品树（或同项目的其它检出）。"""
    lowered = _normalize_for_match(text)
    for root in roots:
        if root is None:
            continue
        base = _normalize_for_match(str(root)).rstrip("\\")
        if base and base in lowered:
            return True
    for part in lowered.split("\\"):
        if part in IDENTITY_PROJECT_HINTS:
            return True
    return False


def check_test_tree_identity(root: Path, subdirs=IDENTITY_SCAN_DIRS, product=None) -> dict:
    root = Path(root)
    violations: list[dict] = []
    other_absolute: list[dict] = []
    comments: list[dict] = []
    scanned = 0
    # ⚠ 2026-11-02（F18）：以前**只扫 root（仓库）**。产品树才是新测试的产地
    #   （新写的用例先落在产品树，同步进仓库后才会被盯）⇒ 产品树里写死本机路径
    #   的测试文件，在同步之前**没人管**。现在两棵树都扫，路径前缀区分。
    trees: list[tuple[Path, str]] = [(root, "")]
    if product is not None:
        product = Path(product)
        try:
            same_tree = product.resolve() == root.resolve()
        except OSError:
            same_tree = False
        if not same_tree:
            trees.append((product, "product/"))
    for base_tree, prefix in trees:
        for sub in subdirs:
            base = base_tree / sub
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.py")):
                if not path.is_file():
                    continue
                scanned += 1
                relative = prefix + path.relative_to(base_tree).as_posix()
                try:
                    text = path.read_text(encoding="utf-8-sig")
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if ALLOW_MARKER in line:
                        continue
                    matches = MACHINE_PATH_PATTERN.findall(line)
                    if not matches:
                        continue
                    entry = {
                        "path": relative,
                        "line": number,
                        "text": line.strip()[:200],
                        "paths": matches,
                    }
                    if not MACHINE_PATH_PATTERN.search(_code_part(line)):
                        comments.append(entry)
                    elif any(_points_at_project(name, (root, product)) for name in matches):
                        violations.append(entry)
                    else:
                        other_absolute.append(entry)
    ok = not violations
    if scanned == 0:
        # ⚠ 2026-11-02（F20）：同上 —— 一个测试模块都没扫到时不能算通过。
        ok = False
    if ok:
        summary = "%d 个测试模块里没有指向本机项目树的绝对路径" % scanned
    elif scanned == 0:
        summary = "一个测试模块都没扫到（backend/tests / tests 目录不对？）"
    else:
        summary = "%d 处写死本项目本机路径（会把产品树镜像塞进 sys.modules）" % len(
            violations
        )
    return {
        "check": "identity",
        "ok": ok,
        "scanned": scanned,
        "violations": violations,
        "other_absolute_paths": other_absolute,
        "comment_mentions": comments,
        "allow_marker": ALLOW_MARKER,
        "summary": summary,
    }


# --------------------------------------------------------------------------
# 3. 镜像一致性门禁
# --------------------------------------------------------------------------
def _is_mirror_candidate(relative: Path) -> bool:
    if any(part in MIRROR_EXCLUDED_DIR_NAMES for part in relative.parts):
        return False
    if relative.suffix.lower() in MIRROR_EXCLUDED_SUFFIXES:
        return False
    return True


def collect_mirror_files(root: Path) -> dict[str, Path]:
    root = Path(root)
    files: dict[str, Path] = {}
    for directory in MIRROR_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if not _is_mirror_candidate(relative):
                continue
            files[relative.as_posix()] = path
    return files


def _git_head_digest(root: Path, relative: str) -> tuple[str | None, str]:
    """返回 (HEAD blob 的内容摘要, 说明)。文件不在 HEAD 里时摘要是 None。"""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "show", "HEAD:" + relative],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:  # 没装 git
        return None, "git 不可用：%s" % error
    if completed.returncode != 0:
        return None, "不在 HEAD 中"
    return content_digest(completed.stdout), ""


def check_mirror(
    root: Path,
    product: Path,
    baseline: str = "worktree",
) -> dict:
    root = Path(root)
    product = Path(product)
    if baseline not in {"worktree", "head"}:
        raise ValueError("baseline 只能是 worktree 或 head：%r" % baseline)
    # 2026-11-02：--root 默认是「本脚本上一级」。直接跑**产品树**那份脚本时，
    # 再把 --product 也指到产品树，就变成**自己跟自己比** ⇒ 恒真 ⇒ 一张假绿牌。
    # 实测：`--root <产品树> --product <产品树>` 报「128 个文件逐一对齐」，
    # 而同一时刻 `--root <仓库> --product <产品树>` 报 30+ 文件内容不同。
    # ⇒ 这里显式拒绝自我比较：检查无效必须**报 FAIL**，不能报 PASS。
    if product.is_dir() and root.resolve() == product.resolve():
        return {
            "check": "mirror",
            "ok": False,
            "baseline": baseline,
            "repo": str(root),
            "product": str(product),
            "missing_in_product": [],
            "unexpected_in_product": [],
            "content_diff": [],
            "product_only_allowed": [],
            "uncommitted_in_worktree": [],
            "baseline_untracked": [],
            "summary": "检查无效：仓库根与产品树根是同一个目录（自我比较恒真）",
        }
    repo_files = collect_mirror_files(root)
    product_files = collect_mirror_files(product)

    missing_in_product = sorted(set(repo_files) - set(product_files))
    product_only = sorted(set(product_files) - set(repo_files))
    unexpected_in_product = [
        name
        for name in product_only
        if not any(name.startswith(prefix) for prefix in MIRROR_PRODUCT_ONLY_ALLOWED)
    ]
    allowed_product_only = [name for name in product_only if name not in unexpected_in_product]

    content_diff: list[str] = []
    uncommitted: list[str] = []
    baseline_untracked: list[str] = []
    for relative in sorted(set(repo_files) & set(product_files)):
        product_digest = content_digest(product_files[relative].read_bytes())
        if baseline == "head":
            head_digest, note = _git_head_digest(root, relative)
            if head_digest is None:
                baseline_untracked.append("%s (%s)" % (relative, note))
                continue
            if head_digest != product_digest:
                content_diff.append(relative)
            elif content_digest(repo_files[relative].read_bytes()) != product_digest:
                # 镜像落后/超前于工作树但和 HEAD 一致：镜像本身没漂，
                # 是工作树有未提交改动还没同步过去。
                uncommitted.append(relative)
        else:
            if content_digest(repo_files[relative].read_bytes()) != product_digest:
                content_diff.append(relative)

    ok = not missing_in_product and not unexpected_in_product and not content_diff
    if not product.is_dir():
        return {
            "check": "mirror",
            "ok": False,
            "baseline": baseline,
            "repo": str(root),
            "product": str(product),
            "missing_in_product": [],
            "unexpected_in_product": [],
            "content_diff": [],
            "product_only_allowed": [],
            "uncommitted_in_worktree": [],
            "baseline_untracked": [],
            "summary": "产品树镜像不存在：%s" % product,
        }
    if ok:
        summary = "%d 个文件逐一对齐（LF 归一化 + SHA-256）" % len(
            set(repo_files) & set(product_files)
        )
        if baseline == "head" and uncommitted:
            summary += "；%d 个工作树未提交改动未进镜像" % len(uncommitted)
    else:
        summary = "缺 %d、多 %d、内容不同 %d" % (
            len(missing_in_product),
            len(unexpected_in_product),
            len(content_diff),
        )
    return {
        "check": "mirror",
        "ok": ok,
        "baseline": baseline,
        "repo": str(root),
        "product": str(product),
        "missing_in_product": missing_in_product,
        "unexpected_in_product": unexpected_in_product,
        "content_diff": content_diff,
        "product_only_allowed": allowed_product_only,
        "uncommitted_in_worktree": uncommitted,
        "baseline_untracked": baseline_untracked,
        "summary": summary,
    }


# --------------------------------------------------------------------------
# 汇合 + CLI
# --------------------------------------------------------------------------
CHECK_NAMES = ("bom", "identity", "mirror")


def run_checks(
    root: Path = REPO_ROOT,
    product: Path | None = None,
    checks=CHECK_NAMES,
    baseline: str = "worktree",
) -> dict:
    results = []
    for name in checks:
        if name == "bom":
            results.append(check_bom(root))
        elif name == "identity":
            results.append(check_test_tree_identity(root, product=product))
        elif name == "mirror":
            results.append(check_mirror(root, product, baseline))
        else:
            raise ValueError("未知检查项：%r" % name)
    return {
        "ok": all(result["ok"] for result in results),
        "checks": results,
    }


def _print_human(result: dict) -> None:
    status = "PASS" if result["ok"] else "FAIL"
    print("[%s] %s - %s" % (status, result["check"], result["summary"]))
    if result["check"] == "bom":
        for name in result["missing_bom"]:
            print("    缺 BOM: %s" % name)
        for name in result["undecodable"]:
            print("    非 UTF-8: %s" % name)
    elif result["check"] == "identity":
        for entry in result["violations"]:
            print(
                "    写死本项目路径: %s:%d: %s"
                % (entry["path"], entry["line"], entry["text"])
            )
        for entry in result["other_absolute_paths"]:
            print(
                "    （其它绝对路径，不计失败）%s:%d: %s"
                % (entry["path"], entry["line"], ", ".join(entry["paths"]))
            )
        for entry in result["comment_mentions"]:
            print("    （仅注释提及，不计失败）%s:%d" % (entry["path"], entry["line"]))
    elif result["check"] == "mirror":
        for name in result["missing_in_product"]:
            print("    镜像缺文件: %s" % name)
        for name in result["unexpected_in_product"]:
            print("    镜像多文件: %s" % name)
        for name in result["content_diff"]:
            print("    内容不同: %s" % name)
        if result["product_only_allowed"]:
            print("    （按 .gitignore 约定跳过 %d 个产品树专属文件）" % len(result["product_only_allowed"]))
        for name in result.get("uncommitted_in_worktree", []):
            print("    镜像=HEAD、工作树有未提交改动: %s" % name)
        for name in result.get("baseline_untracked", []):
            print("    不在 HEAD 中，未比对: %s" % name)


def main(argv=None) -> int:
    _stdout_utf8()
    parser = argparse.ArgumentParser(description="OcrTableTool 仓库门禁检查（BOM / 测试树身份 / 镜像一致性）")
    parser.add_argument("check", nargs="?", default="all", choices=("all",) + CHECK_NAMES)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="仓库根（默认本脚本上一级）")
    parser.add_argument("--product", type=Path, default=None, help="产品树镜像根，mirror 检查必填")
    parser.add_argument(
        "--baseline",
        choices=("worktree", "head"),
        default="worktree",
        help="mirror 基线：worktree（默认，镜像应等于工作树）或 head（镜像应等于 HEAD）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读摘要")
    parser.add_argument("--json-out", type=Path, default=None, help="把机器可读摘要另存到文件")
    arguments = parser.parse_args(argv)

    checks = CHECK_NAMES if arguments.check == "all" else (arguments.check,)
    if "mirror" in checks and arguments.product is None:
        parser.error("mirror 检查需要 --product <产品树根>")
    report = run_checks(arguments.root, arguments.product, checks, arguments.baseline)

    if arguments.json_out is not None:
        arguments.json_out.parent.mkdir(parents=True, exist_ok=True)
        arguments.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for result in report["checks"]:
            _print_human(result)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
