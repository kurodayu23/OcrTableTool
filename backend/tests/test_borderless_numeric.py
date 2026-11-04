import copy
import unittest

import cv2
import numpy as np

import ocr_backend


class BorderlessNumericTests(unittest.TestCase):
    def test_unit_symbols_use_one_unicode_form_in_ocr_results(self):
        grid = [["µmol/L", "μg", "℃", "°C", "°°C", "频率（MHz）"]]
        ocr_backend._normalize_ocr_unit_symbols(grid)
        self.assertEqual(grid, [["μmol/L", "μg", "°C", "°C", "°C", "频率（MHz）"]])
        self.assertEqual(ocr_backend._comparable_text("°°C"), "°C")

    def test_masked_numeric_uses_the_visible_star_count_only_with_matching_edges(self):
        shape = (3, 4, 4)
        self.assertEqual(
            ocr_backend._canonical_masked_numeric("138***4550", shape),
            "138****4550",
        )
        self.assertEqual(
            ocr_backend._canonical_masked_numeric("138***45450", shape),
            "138***45450",
        )

    def test_celsius_peer_multiview_support_requires_three_model_families(self):
        readings = (
            ("℃", 0.70), ("°C", 0.76), ("℃", 0.85),
            ("℃", 0.73), ("℃", 0.55),
        )
        self.assertTrue(ocr_backend._celsius_peer_multiview_support(
            ("℃", 0.87), readings, has_confirmed_peer=True
        ))
        self.assertFalse(ocr_backend._celsius_peer_multiview_support(
            ("℃", 0.87), readings, has_confirmed_peer=False
        ))
        weak_v5 = readings[:-1] + (("℃", 0.49),)
        self.assertFalse(ocr_backend._celsius_peer_multiview_support(
            ("℃", 0.87), weak_v5, has_confirmed_peer=True
        ))

    def test_note_and_signature_after_regular_rows_are_outside_table(self):
        ocr_backend._load_runtime()
        boxes, texts, scores = [], [], []
        for row in range(7):
            for column in range(5):
                x, y = 40 + column * 100, 40 + row * 30
                boxes.append([[x - 18, y - 8], [x + 18, y - 8],
                              [x + 18, y + 8], [x - 18, y + 8]])
                texts.append("年级" if row == 0 and column == 0 else f"{row + column}.50")
                scores.append(0.99)
        boxes.extend([
            [[20, 266], [340, 266], [340, 284], [20, 284]],
            [[410, 266], [495, 266], [495, 284], [410, 284]],
            [[435, 306], [490, 306], [490, 324], [435, 324]],
        ])
        texts.extend(["注：金额按规定计算。", "某单位", "2026.09"])
        scores.extend([0.99] * 3)

        grid, _, spans, strong, geometry = ocr_backend._borderless_spatial_grid(
            boxes, texts, scores, return_geometry=True
        )

        self.assertTrue(strong)
        self.assertEqual((len(grid), len(grid[0])), (7, 5))
        self.assertEqual(spans, [])
        self.assertEqual(len(geometry["grouped_rows"]), 7)

    def test_regular_numeric_certificate_requires_visible_aligned_cells(self):
        ocr_backend._load_runtime()
        anchors = [40.0 + 100.0 * column for column in range(5)]
        centers = [40.0 + 30.0 * row for row in range(7)]
        grid = [["年级", "书款", "作业款", "应收", "实收"]] + [
            [f"{row}年级", "31.15", "7.5", "38.65", "38.70"]
            for row in range(1, 7)
        ]
        geometry = {
            "anchors": anchors,
            "row_centers": centers,
            "grouped_rows": [
                [{"center_x": x, "score": 0.99} for x in anchors]
                for _ in centers
            ],
        }
        image = np.full((270, 520, 3), 255, dtype=np.uint8)
        for y in centers:
            for x in anchors:
                cv2.rectangle(image, (int(x) - 5, int(y) - 7),
                              (int(x) + 5, int(y) + 7), (25, 25, 25), -1)

        certificate = ocr_backend._regular_numeric_borderless_certificate(
            image, grid, [], geometry
        )
        self.assertIsNotNone(certificate)
        self.assertTrue(ocr_backend._structure_certificate_matches(certificate, grid, []))

        misaligned = copy.deepcopy(geometry)
        misaligned["grouped_rows"][3][2]["center_x"] += 30
        self.assertIsNone(ocr_backend._regular_numeric_borderless_certificate(
            image, grid, [], misaligned
        ))
        occluded = image.copy()
        cv2.rectangle(occluded, (230, 183), (250, 197), (255, 255, 255), -1)
        self.assertIsNone(ocr_backend._regular_numeric_borderless_certificate(
            occluded, grid, [], geometry
        ))

    def test_sparse_certificate_accepts_only_explained_text_overhang(self):
        ocr_backend._load_runtime()
        anchors = [60.0, 180.0, 300.0, 420.0]
        centers = [40.0 + 30.0 * row for row in range(9)]
        grid = [["", "费用", "天", "小计"]]
        grid += [
            ["税费", "20", "", "20"] for _ in range(4)
        ]
        grid += [["服务费", "10", "5", "50"] for _ in range(2)]
        grid += [["购物及租用用具", "", "", "30"], ["合计", "", "", "400"]]
        image = np.full((300, 520, 3), 255, dtype=np.uint8)
        grouped = []
        for row, values in enumerate(grid):
            entries = []
            y = int(centers[row])
            for column, value in enumerate(values):
                if not value:
                    continue
                x = int(anchors[column])
                cv2.rectangle(image, (x - 5, y - 7), (x + 5, y + 7),
                              (25, 25, 25), -1)
                left, right = x - 8, x + 8
                center_x = float(x)
                if row == 7 and column == 0:
                    cv2.rectangle(image, (125, y - 7), (130, y + 7),
                                  (25, 25, 25), -1)
                    left, right, center_x = 50, 132, 90.0
                entries.append({
                    "center_x": center_x, "left": left, "right": right,
                    "top": y - 8, "bottom": y + 8, "score": 0.99,
                })
            grouped.append(entries)
        geometry = {
            "anchors": anchors, "row_centers": centers,
            "grouped_rows": grouped, "header_cells_verified": True,
        }
        self.assertIsNotNone(ocr_backend._regular_numeric_borderless_certificate(
            image, grid, [], geometry
        ))

        hidden_text = image.copy()
        cv2.rectangle(hidden_text, (175, 243), (185, 257), (25, 25, 25), -1)
        self.assertIsNone(ocr_backend._regular_numeric_borderless_certificate(
            hidden_text, grid, [], geometry
        ))


if __name__ == "__main__":
    unittest.main()
