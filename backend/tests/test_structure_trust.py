"""结构认证的降级护栏。

核心立场：结构明显不成立时，宁可报「未确认、需人工核对」，也不要报
「已认证」——客户看到「已认证」就会直接信任导出结果。
"""

import unittest

import ocr_backend


def certified(columns, rows, source="screen_ruled_grid"):
    return {
        "verified": True,
        "source": source,
        "columns": columns,
        "rows": rows,
        "column_boundaries": [i * 10 for i in range(columns + 1)],
        "row_boundaries": [i * 10 for i in range(rows + 1)],
        "column_offset": 0,
        "row_offset": 0,
        "spans": [],
    }


def result_with(columns, rows, verified=True):
    return {
        "structure_verified": verified,
        "structure_certificate": certified(columns, rows) if verified else None,
        "structure_review_required": not verified,
        "publication_blocked": not verified,
        "recognition_state": "verified" if verified else "blocked",
        "publication_block_reasons": [],
        "processing_notices": [],
        "structure_warnings": [],
    }


class ImplausibleColumnStructureTests(unittest.TestCase):
    def test_two_column_multi_row_grid_is_not_certified(self):
        # 7 列表被线框划成 2 列的实测形态：整表被标成已认证
        result = result_with(2, 7)
        self.assertTrue(ocr_backend._demote_implausible_column_structure(result))
        self.assertFalse(result["structure_verified"])
        self.assertIsNone(result["structure_certificate"])
        self.assertTrue(result["publication_blocked"])
        self.assertEqual(result["recognition_state"], "blocked")
        self.assertTrue(any("列边界" in r for r in result["publication_block_reasons"]))

    def test_single_column_grid_is_not_certified(self):
        result = result_with(1, 22)
        self.assertTrue(ocr_backend._demote_implausible_column_structure(result))

    def test_normal_tables_keep_their_certificate(self):
        for columns, rows in ((7, 6), (11, 12), (4, 30), (2, 2), (1, 1), (3, 9)):
            result = result_with(columns, rows)
            self.assertFalse(
                ocr_backend._demote_implausible_column_structure(result),
                msg="%dx%d 不应被降级" % (rows, columns),
            )
            self.assertTrue(result["structure_verified"])

    def test_unverified_results_are_left_alone(self):
        result = result_with(2, 7, verified=False)
        result["structure_certificate"] = None
        self.assertFalse(ocr_backend._demote_implausible_column_structure(result))

    def test_repeated_reasons_are_deduplicated(self):
        result = result_with(2, 7)
        ocr_backend._demote_implausible_column_structure(result)
        before = list(result["publication_block_reasons"])
        ocr_backend._demote_implausible_column_structure(result)
        self.assertEqual(result["publication_block_reasons"], before)


class PipelineHelperPresenceTests(unittest.TestCase):
    """收尾阶段按顺序调用的这些函数，一个都不能缺。

    改函数头时把 def 行连带下一段 docstring 一起删掉，语法照样合法、AST 照样
    解析通过，只有真正跑一次端到端才会暴露成
    "name '_demote_heuristic_certified_structure' is not defined"。
    """
    def test_every_pipeline_stage_helper_exists(self):
        for name in (
            "_flag_unbalanced_formula_cells",
            "_demote_implausible_column_structure",
            "_drop_leading_page_caption_rows",
            "_demote_heuristic_certified_structure",
            "_collapse_wrapped_label_rows",
            "_align_structure_certificate_to_grid",
            "_structure_certificate_matches",
            "_seal_structure_certificate",
        ):
            self.assertTrue(callable(getattr(ocr_backend, name, None)),
                            msg="%s 缺失" % name)

    def test_certified_shape_of_a_two_column_grid_is_built_end_to_end(self):
        result = result_with(2, 7)
        self.assertEqual(result["structure_certificate"]["columns"], 2)
        self.assertTrue(ocr_backend._demote_implausible_column_structure(result))
        self.assertIsNone(result["structure_certificate"])


if __name__ == "__main__":
    unittest.main()