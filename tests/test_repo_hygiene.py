# -*- coding: utf-8 -*-
"""仓库卫生门禁（纯标准库、秒级、不跑 OCR / 不需要 Qt）。

锁住三类**流程型**缺陷，每一类都真实发生过：

1. **BOM**：``src\\core\\csvexporter.cpp`` 被改掉 UTF-8 BOM 后，MSVC2015 按 GBK
   解释源码，整树 ``C2001`` / ``C1057``，直到跑 UI 契约测试才暴露。
2. **测试树身份**：``backend\\tests`` 里写死产品树镜像的本机绝对路径（本机在
   D 盘的 AP 目录下），而该模块是 unittest discover 的第一个，加载即把镜像塞进
   ``sys.modules`` ⇒ 整套后端单测在测镜像，还额外产生 4 个只在全量 discover
   时出现的假 ERROR。
3. **镜像漂移**：产品树镜像与仓库工作树靠人工同步，会静默落后/超前。

每个检查都有两层用例：
  * 行为层 —— 在临时目录里造出"坏样本"，证明检查器真的会红（防止门禁退化成空跑）；
  * 现场层 —— 对**仓库本身**跑一遍（产品树镜像缺失时跳过，不误报）。

跑法（``<venv>`` = 产品树下的 ``.venv\\Scripts\\python.exe``）::

    <venv> -B -m unittest tests/test_repo_hygiene.py

也由 ``scripts\\verify_all.ps1`` 作为一项门禁调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gate_checks  # noqa: E402  （必须在 sys.path 调整之后）
import delivery_notes  # noqa: E402


def product_mirror() -> Path:
    override = os.environ.get("OCRTABLE_PRODUCT_MIRROR")
    return Path(override) if override else gate_checks.DEFAULT_PRODUCT_MIRROR


def find_repository_root(start: Path) -> Path | None:
    """结构性找出**真正的仓库根**（带 ``.git`` 且带 ``scripts/gate_checks.py``）。

    2026-11-02：这条查找是必需的 —— ``test_product_mirror_matches_repo_worktree``
    原本直接 ``check_mirror(ROOT, mirror)``。从**产品树**跑这份测试时
    ``ROOT`` 就是产品树本身 ⇒ 拿产品树跟自己比 ⇒ **恒真**（假绿牌）。
    实测当时该用例 16/16 全绿，而同一时刻正确调用报 30+ 文件内容不同。

    查找刻意**不写死任何本机路径**（identity 门禁会拦），改成按结构找：
    先看环境变量 ``OCRTABLE_REPO_ROOT``，再从 ``start`` 的父目录往下找两层。
    """
    override = os.environ.get("OCRTABLE_REPO_ROOT", "").strip()
    if override:
        candidate = Path(override)
        if (candidate / ".git").exists():
            return candidate
        return None

    def _is_repo(path: Path) -> bool:
        return (
            path.is_dir()
            and (path / ".git").exists()
            and (path / "scripts" / "gate_checks.py").is_file()
        )

    base = start.parent
    if not base.is_dir():
        return None
    try:
        children = sorted(base.iterdir())
    except OSError:
        return None
    for child in children:
        if _is_repo(child):
            return child
        if not child.is_dir() or child.name == ".git":
            continue
        try:
            grandchildren = sorted(child.iterdir())
        except OSError:
            continue
        for grandchild in grandchildren:
            if _is_repo(grandchild):
                return grandchild
    return None


# identity 门禁扫的是**连写的盘符路径**，而本文件就在 tests\ 下、也在扫描范围内。
# 所以测试夹具里的路径故意拆成几段拼出来：既保留"坏样本"的语义，又不让这个文件
# 自己被自己判红（真写死了就必须加行内豁免注释，见 ALLOW_MARKER 用例）。
MACHINE_ROOT = "D:" + "\\" + "AP" + "\\" + "AP7000" + "\\" + "OcrTableTool"
MACHINE_ROOT_FORWARD = MACHINE_ROOT.replace("\\", "/")


def write_text(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


class BomGateBehaviourTest(unittest.TestCase):
    """BOM 检查器必须真的能红，不能因为空目录就"全绿"。"""

    def test_detects_chinese_source_without_bom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(root / "src" / "core" / "csvexporter.cpp", "// 导出表格\nint a = 1;\n")
            result = gate_checks.check_bom(root)
            self.assertFalse(result["ok"])
            self.assertEqual(result["missing_bom"], ["src/core/csvexporter.cpp"])

    def test_accepts_bom_and_ascii_only_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "src" / "core" / "csvexporter.cpp"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xef\xbb\xbf" + "// 导出表格\n".encode("utf-8"))
            write_text(root / "src" / "core" / "plain.cpp", "// ascii only\n")
            result = gate_checks.check_bom(root)
            self.assertTrue(result["ok"], result["missing_bom"])
            self.assertEqual(result["scanned"], 2)

    def test_scans_tests_and_interface_sdk_as_well(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(root / "tests" / "tst_core.cpp", "// 中文\n")
            write_text(root / "interface-sdk" / "qt" / "cameraocrclient.h", "// 中文\n")
            result = gate_checks.check_bom(root)
            self.assertEqual(
                sorted(result["missing_bom"]),
                ["interface-sdk/qt/cameraocrclient.h", "tests/tst_core.cpp"],
            )


class TestTreeIdentityGateBehaviourTest(unittest.TestCase):
    """写死的本机路径必须被抓到；测试夹具里的假路径不许误报。"""

    def test_detects_hardcoded_product_tree_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            write_text(
                root / "backend" / "tests" / "test_x.py",
                'from pathlib import Path\nPROJECT = Path(r"%s")\n' % MACHINE_ROOT,
            )
            # ⚠ 2026-11-02（F22）：同样不能拿真实产品树当夹具 ——
            #   产品树一旦出现违规，这条"只有 1 处违规"的断言就会跟着漂。
            product = Path(directory) / "product"
            write_text(product / "tests" / "test_v.py", "VALUE = 1\n")
            result = gate_checks.check_test_tree_identity(root, product=product)
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["violations"]), 1)
            self.assertEqual(result["violations"][0]["path"], "backend/tests/test_x.py")

    def test_detects_path_into_same_project_other_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            write_text(
                root / "tests" / "test_y.py",
                'MIRROR = "%s/package"\n' % MACHINE_ROOT_FORWARD,
            )
            result = gate_checks.check_test_tree_identity(root)
            # 含项目名 ⇒ 仍属于本项目的本机路径，必须报。
            self.assertFalse(result["ok"])

    def test_ignores_system_and_fixture_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            write_text(
                root / "backend" / "tests" / "test_z.py",
                'FONTS = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"\n'
                'payload = {"image_path": "C:/input.png"}\n'
                'sample = r"E:\\设备日志\\2026\\08\\SN-1.txt"\n',
            )
            result = gate_checks.check_test_tree_identity(root)
            self.assertTrue(result["ok"], result["violations"])
            self.assertEqual(len(result["other_absolute_paths"]), 3)

    def test_comment_mentions_do_not_fail_and_allow_marker_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            write_text(
                root / "backend" / "tests" / "test_w.py",
                "# 2026-10-28：原来这里写死 %s，已修。\n"
                'REAL = Path(r"%s")  # gate:allow-machine-path 交付位自检\n'
                % (MACHINE_ROOT, MACHINE_ROOT),
            )
            # ⚠ 2026-11-02（F22）：原来这里把**真实产品树**当第二棵树传进来 ——
            #   于是"注释提及"的条数取决于这台机器的产品树里恰好有几处注释提及，
            #   机器状态一变（新增带本机路径注释的测试文件）这条断言就假红。
            #   2026-11-02 实测：产品树新增 2 处注释提及 ⇒ 期望 1 条、实测 3 条 ⇒ FAIL。
            #   改成用临时目录里的第二棵树（内容干净）：条数回到夹具自身那 1 条，
            #   既保住"注释提及不算失败"的本意，也不再依赖机器现状。
            product = Path(directory) / "product"
            write_text(product / "tests" / "test_v.py", "VALUE = 1\n")
            result = gate_checks.check_test_tree_identity(root, product=product)
            self.assertTrue(result["ok"], result["violations"])
            self.assertEqual(len(result["comment_mentions"]), 1)


class MirrorGateBehaviourTest(unittest.TestCase):
    """镜像比对必须 LF 归一化，并把"缺 / 多 / 内容不同"分开报。"""

    def test_crlf_against_lf_is_not_a_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            mirror = base / "mirror"
            (repo / "backend").mkdir(parents=True)
            (mirror / "backend").mkdir(parents=True)
            (repo / "backend" / "a.py").write_bytes(b"x = 1\n\r\ny = 2\n".replace(b"\n\r\n", b"\r\n"))
            (mirror / "backend" / "a.py").write_bytes(b"x = 1\ny = 2\n")
            result = gate_checks.check_mirror(repo, mirror)
            self.assertTrue(result["ok"], result["content_diff"])

    def test_detects_missing_and_stale_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            mirror = base / "mirror"
            (repo / "scripts").mkdir(parents=True)
            (mirror / "scripts").mkdir(parents=True)
            (repo / "scripts" / "only_repo.py").write_text("a = 1\n", encoding="utf-8")
            (repo / "scripts" / "changed.py").write_text("new\n", encoding="utf-8")
            (mirror / "scripts" / "changed.py").write_text("old\n", encoding="utf-8")
            (mirror / "scripts" / "only_mirror.py").write_text("b = 2\n", encoding="utf-8")
            result = gate_checks.check_mirror(repo, mirror)
            self.assertFalse(result["ok"])
            self.assertEqual(result["missing_in_product"], ["scripts/only_repo.py"])
            self.assertEqual(result["unexpected_in_product"], ["scripts/only_mirror.py"])
            self.assertEqual(result["content_diff"], ["scripts/changed.py"])

    def test_ignored_generated_directories_are_not_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            mirror = base / "mirror"
            (repo / "runtime").mkdir(parents=True)
            (mirror / "runtime" / "models").mkdir(parents=True)
            (repo / "backend" / "__pycache__").mkdir(parents=True)
            (mirror / "runtime" / "models" / "PP-OCRv6_det_medium.xml").write_text("m", encoding="utf-8")
            (repo / "backend" / "__pycache__" / "x.pyc").write_bytes(b"\0")
            result = gate_checks.check_mirror(repo, mirror)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["product_only_allowed"], ["runtime/models/PP-OCRv6_det_medium.xml"])

    def test_self_comparison_is_rejected_instead_of_reporting_ok(self):
        """仓库根 == 产品树根 ⇒ 自我比较恒真 ⇒ 必须报 FAIL，不能发绿牌。

        2026-11-02 实测：`--root` 默认是「本脚本上一级」。直接跑**产品树**那份
        ``gate_checks.py``、再把 ``--product`` 也指到产品树，就变成自己跟自己比 ——
        实测报「128 个文件逐一对齐」PASS，而同一时刻正确调用（``--root`` 指仓库）
        报 30+ 文件内容不同 ⇒ 一张**假绿牌**。
        """
        with tempfile.TemporaryDirectory() as directory:
            same = Path(directory) / "same"
            (same / "scripts").mkdir(parents=True)
            (same / "scripts" / "a.py").write_text("a = 1\n", encoding="utf-8")
            result = gate_checks.check_mirror(same, same)
            self.assertFalse(result["ok"], "自我比较必须判 FAIL：%r" % result)
            self.assertIn("自我比较", result["summary"])

    def test_self_comparison_is_rejected_even_when_files_differ(self):
        """自我比较的拦截不能被「目录里确实有内容差异」绕过 —— 拦截的是**比较本身**。"""
        with tempfile.TemporaryDirectory() as directory:
            same = Path(directory) / "same"
            (same / "scripts").mkdir(parents=True)
            (same / "scripts" / "a.py").write_text("a = 1\n", encoding="utf-8")
            result = gate_checks.check_mirror(same, same)
            self.assertFalse(result["ok"])
            self.assertEqual(result["content_diff"], [])


class DeliveryNotesTemplateTest(unittest.TestCase):
    """交付说明.md 的数字必须能由模板 + 真实目录重算，过期必须能被发现。"""

    def _fake_package(self, root: Path) -> Path:
        package = root / "backend-delivery"
        runtime = package / delivery_notes.RUNTIME_SUBTREE
        (runtime / "models").mkdir(parents=True)
        (runtime / "OcrBackend.exe").write_bytes(b"x" * 1000)
        (runtime / "models" / "PP-OCRv6_det_small.onnx").write_bytes(b"m" * 500)
        (package / "licenses").mkdir()
        (package / "licenses" / "apache-2.0.txt").write_text("license\n", encoding="utf-8")
        (package / "selfcheck.bat").write_text("@echo off\n", encoding="utf-8")
        # 真实交付位里 交付说明.md 本来就在根目录（它自己也计入整包文件数）。
        (package / delivery_notes.OUTPUT_NAME).write_text("", encoding="utf-8")
        return package

    def test_render_then_verify_then_detect_stale_numbers(self):
        template = ROOT / "packaging" / "delivery" / delivery_notes.TEMPLATE.name
        if not template.is_file():
            self.skipTest("交付说明模板不在版本库：%s" % template)
        with tempfile.TemporaryDirectory() as directory:
            package = self._fake_package(Path(directory))
            delivery_notes.render_file(
                package, template=template, measured_at="2026-10-24 08:42"
            )
            fresh = delivery_notes.verify(package, template=template)
            self.assertTrue(fresh["ok"], fresh["summary"])
            # 包内容变了（新增一个文件）而说明没重算 ⇒ 必须红。
            (package / "ocr-runtime" / "OcrBackend" / "extra.dll").write_bytes(b"e" * 999)
            stale = delivery_notes.verify(package, template=template)
            self.assertFalse(stale["ok"])
            self.assertIn("已过期", stale["summary"])

    def test_template_has_no_untouched_placeholder_values(self):
        template = ROOT / "packaging" / "delivery" / delivery_notes.TEMPLATE.name
        if not template.is_file():
            self.skipTest("交付说明模板不在版本库：%s" % template)
        text = template.read_text(encoding="utf-8")
        placeholders = set(delivery_notes.PLACEHOLDER_PATTERN.findall(text))
        # 模板里出现的每个占位符都必须由 collect_facts / MEASURED_AT 提供；
        # 渲染时不认识的占位符会直接报错，这里提前把名字列出来防手改漏。
        known = {
            "EXE_BYTES",
            "EXE_MIB",
            "PACKAGE_FILES",
            "PACKAGE_MIB",
            "RUNTIME_FILES",
            "RUNTIME_BYTES",
            "RUNTIME_MIB",
            "DELTA_KB",
            "EXTRA_FILES",
            "ROOT_DELIVERY_FILES",
            "LICENSE_FILES",
            "RUNTIME_MAX_MTIME",
            "MEASURED_AT",
        }
        self.assertTrue(placeholders, "模板里没有任何占位符，说明它没被模板化")
        self.assertEqual(placeholders - known, set())


class RepositoryStateTest(unittest.TestCase):
    """现场层：对仓库本身跑一遍。这些用例就是"当前树是否可提交"的答案。"""

    def test_repository_cpp_and_headers_with_non_ascii_have_utf8_bom(self):
        result = gate_checks.check_bom(ROOT)
        self.assertTrue(
            result["ok"],
            "缺 UTF-8 BOM（MSVC2015 会 C2001/C1057）：\n  " + "\n  ".join(result["missing_bom"]),
        )

    def test_test_modules_do_not_hardcode_this_machine_path(self):
        result = gate_checks.check_test_tree_identity(ROOT, product=product_mirror())
        details = [
            "%s:%d: %s" % (entry["path"], entry["line"], entry["text"])
            for entry in result["violations"]
        ]
        self.assertTrue(
            result["ok"],
            "测试里写死了本机项目路径（会把产品树镜像塞进 sys.modules）：\n  "
            + "\n  ".join(details),
        )

    def test_product_mirror_matches_repo_worktree(self):
        mirror = product_mirror()
        if not mirror.is_dir():
            self.skipTest("产品树镜像不存在，跳过：%s（用 OCRTABLE_PRODUCT_MIRROR 指定）" % mirror)
        # 从产品树跑这份测试时 ROOT == 镜像 ⇒ 自我比较恒真 ⇒ 必须换成**真仓库**来比。
        # 找不到真仓库时**跳过并说明**（不能拿自我比较冒充"通过"）。
        repo_root = ROOT
        if repo_root.resolve() == mirror.resolve():
            discovered = find_repository_root(ROOT)
            if discovered is None:
                self.skipTest(
                    "从产品树运行，未找到真仓库根（用 OCRTABLE_REPO_ROOT 指定）："
                    "镜像与工作树的一致性在产品树里无法自证"
                )
            repo_root = discovered
        result = gate_checks.check_mirror(repo_root, mirror)
        details = (
            ["镜像缺文件: " + name for name in result["missing_in_product"]]
            + ["镜像多文件: " + name for name in result["unexpected_in_product"]]
            + ["内容不同: " + name for name in result["content_diff"]]
        )
        self.assertTrue(
            result["ok"],
            "镜像与工作树不一致（仓库 %s ↔ 产品树 %s）：\n  %s"
            % (repo_root, mirror, "\n  ".join(details)),
        )

    def test_verify_all_script_exists_and_parses(self):
        script = ROOT / "scripts" / "verify_all.ps1"
        self.assertTrue(script.is_file(), "一键门禁脚本缺失：%s" % script)
        text = script.read_text(encoding="utf-8-sig")
        for expected in ("gate_checks.py", "backend\\tests", "mirror", "-Json"):
            self.assertIn(expected, text)
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if not pwsh:
            self.skipTest("没有 pwsh/powershell，跳过语法解析")
        completed = subprocess.run(
            [
                pwsh,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$errors = $null; "
                "[void][System.Management.Automation.Language.Parser]::ParseFile("
                "'%s', [ref]$null, [ref]$errors); "
                "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.Message }; exit 1 }"
                % str(script).replace("'", "''"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "verify_all.ps1 语法解析失败：\n" + completed.stdout.decode("utf-8", "replace"),
        )


if __name__ == "__main__":
    unittest.main()
