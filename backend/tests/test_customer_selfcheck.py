# -*- coding: utf-8 -*-
"""交付包客户侧自检脚本的防漂移契约。

`scripts/customer_selfcheck.py` 里**内嵌**了一份 15 个模型的 SHA-256 基线，
因为客户机上没有源码树、没有 PyInstaller、也可能没装第三方库 ——
脚本必须是纯标准库且自带基线。

但内嵌表一旦和 `backend/ocr_backend.py` 的 `MODEL_HASHES` 漂移，
后果是**方向相反的两重错误**：

* 基线比源码旧 → 客户在**完全正确**的包上看到 "模型 SHA-256 不一致"，
  拿着一份没问题的交付件去报故障；
* 基线比源码新 → 包里**缺了**或**换了**模型也照样显示通过。

所以这里把它锁死：两份表必须**逐条相等**。
"""

from __future__ import annotations

import ast
import base64
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SOURCE = PROJECT_ROOT / "backend" / "ocr_backend.py"
SELFCHECK_SOURCE = PROJECT_ROOT / "scripts" / "customer_selfcheck.py"


def _module_level_literal(path: Path, name: str):
    """Read a module-level literal assignment without importing the module.

    Importing the backend would install the offline audit hook and pull in
    cv2/openvino; the self-check script is a customer-facing tool and must
    stay dependency-free.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(target, "id", None) == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("%s does not define %s" % (path, name))


class CustomerSelfCheckContractTests(unittest.TestCase):
    def test_model_hashes_match_backend_source(self):
        expected = _module_level_literal(BACKEND_SOURCE, "MODEL_HASHES")
        actual = _module_level_literal(SELFCHECK_SOURCE, "MODEL_HASHES")
        self.assertEqual(
            sorted(expected),
            sorted(actual),
            "the customer self-check must cover exactly the same model set",
        )
        for name in sorted(expected):
            self.assertEqual(
                expected[name],
                actual[name],
                "SHA-256 baseline drifted for %s" % name,
            )

    def test_model_hashes_are_well_formed(self):
        actual = _module_level_literal(SELFCHECK_SOURCE, "MODEL_HASHES")
        self.assertEqual(15, len(actual))
        for name, digest in actual.items():
            self.assertRegex(name, r"^[A-Za-z0-9_.\-]+\.(onnx|xml|bin|txt)$")
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_embedded_sample_table_is_a_valid_png(self):
        payload = _module_level_literal(
            SELFCHECK_SOURCE, "SAMPLE_TABLE_PNG_BASE64"
        )
        raw = base64.b64decode(payload)
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG")
        self.assertGreater(len(raw), 1024)

    def test_selfcheck_script_is_standard_library_only(self):
        """The customer machine may have nothing but a bare Python.

        Any third-party import would turn the self-check into something that
        cannot run where it matters most.
        """
        source = SELFCHECK_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(SELFCHECK_SOURCE))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        allowed = {
            "argparse", "base64", "hashlib", "json", "marshal", "os",
            "shutil", "struct", "subprocess", "sys", "tempfile",
            "zipfile", "zlib", "__future__",
        }
        self.assertTrue(
            imported <= allowed,
            "customer self-check imports non-stdlib modules: %s"
            % sorted(imported - allowed),
        )


if __name__ == "__main__":
    unittest.main()
