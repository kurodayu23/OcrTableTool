import unittest
from unittest.mock import patch

import cv2
import numpy as np

import colored_table


class ColoredTableTests(unittest.TestCase):
    def test_body_vertical_merge_is_row_header_not_group_title(self):
        image = np.full((161, 401, 3), 255, np.uint8)
        rows = [0, 40, 80, 120, 160]
        columns = [0, 100, 200, 300, 400]
        for y in rows:
            start = 100 if y in (80, 120) else 0
            cv2.line(image, (start, y), (400, y), (25, 25, 25), 2)
        for x in columns:
            cv2.line(image, (x, 0), (x, 160), (25, 25, 25), 2)
        cv2.putText(image, "1", (20, 93), cv2.FONT_HERSHEY_SIMPLEX,
                    .8, (25, 25, 25), 2)
        self.assertIn({"row": 1, "column": 0, "row_span": 3,
                       "column_span": 1, "role": "row_header"},
                      colored_table.physical_spans(image, rows, columns))

    def test_first_row_text_in_gray_band_does_not_merge_blank_rows(self):
        image = np.full((161, 401, 3), 255, np.uint8)
        rows = [0, 40, 80, 120, 160]
        columns = [0, 100, 200, 300, 400]
        image[40:160, 100:200] = 205
        for y in rows:
            cv2.line(image, (0, y), (400, y), (35, 35, 35), 2)
        for x in columns:
            cv2.line(image, (x, 0), (x, 160), (35, 35, 35), 2)
        for y in (80, 120):
            cv2.line(image, (100, y), (200, y), (205, 205, 205), 3)
        cv2.putText(image, "350", (116, 69), cv2.FONT_HERSHEY_SIMPLEX,
                    .55, (15, 15, 15), 1)
        self.assertEqual(colored_table.physical_spans(image, rows, columns), [])
        centered = image.copy()
        cv2.rectangle(centered, (111, 42), (183, 76), (205, 205, 205), -1)
        cv2.putText(centered, "350", (116, 107), cv2.FONT_HERSHEY_SIMPLEX,
                    .55, (15, 15, 15), 1)
        self.assertIn({"row": 1, "column": 1, "row_span": 3,
                       "column_span": 1, "role": "merged"},
                      colored_table.physical_spans(centered, rows, columns))

    def test_blank_vertical_merge_needs_both_side_rules(self):
        image = np.full((445, 391, 3), 255, np.uint8)
        rows = [24, 66, 108, 150, 192, 234, 276, 318, 360, 402, 444]
        columns = [24, 155, 286, 390]
        for y in rows:
            if y in (234, 276, 318, 360, 402):
                cv2.line(image, (24, y), (155, y), (35, 35, 35), 2)
                cv2.line(image, (286, y), (390, y), (35, 35, 35), 2)
            else:
                cv2.line(image, (24, y), (390, y), (35, 35, 35), 2)
        for x in columns:
            cv2.line(image, (x, 24), (x, 444), (35, 35, 35), 2)
        expected = {"row": 4, "column": 1, "row_span": 6,
                    "column_span": 1, "role": "merged"}
        self.assertIn(expected, colored_table.physical_spans(image, rows, columns))
        for fill in (175, 0):
            shaded = image.copy()
            shaded[196:440, 159:282] = fill
            self.assertNotIn(expected, colored_table.physical_spans(shaded, rows, columns))

    def test_repeated_row_text_is_not_a_vertical_merge(self):
        image = np.full((241, 361, 3), 255, np.uint8)
        rows = [0, 40, 80, 120, 160, 200, 240]
        columns = [0, 120, 240, 360]
        for y in rows:
            cv2.line(image, (0, y), (360, y), (35, 35, 35))
        for x in columns:
            cv2.line(image, (x, 0), (x, 240), (35, 35, 35))
        for y in (80, 120, 160, 200):
            cv2.line(image, (240, y), (360, y), (255, 255, 255), 3)
        for row in range(1, 6):
            cv2.putText(image, "no", (265, rows[row] + 27),
                        cv2.FONT_HERSHEY_SIMPLEX, .7, (20, 20, 20), 2)
        for y in (84, 236):
            cv2.line(image, (265, y), (345, y), (30, 30, 30))
        self.assertFalse(any(span["column"] == 2 and span["row_span"] > 1
                             for span in colored_table.physical_spans(image, rows, columns)))

    def test_aligned_colored_cell_labels_are_not_a_paragraph_banner(self):
        image = np.full((161, 501, 3), 255, np.uint8)
        rows = [0, 40, 80, 120, 160]
        columns = [0, 100, 200, 300, 400, 500]
        for y in rows:
            cv2.line(image, (0, y), (500, y), (35, 35, 35), 2)
        for x in columns:
            cv2.line(image, (x, 0), (x, 160), (35, 35, 35), 2)
        image[42:79, 2:499] = (190, 190, 190)
        for x in columns[1:-1]:
            cv2.line(image, (x, 41), (x, 79), (190, 190, 190), 3)
        for index in range(5):
            cv2.putText(image, str(index + 1), (columns[index] + 8, 68),
                        cv2.FONT_HERSHEY_SIMPLEX, .6, (20, 20, 20), 2)
        self.assertFalse(any(span["row"] == 1 and span["column_span"] > 1
                             for span in colored_table.physical_spans(image, rows, columns)))

    def test_local_letter_stem_does_not_prove_a_full_cell_separator(self):
        vertical = np.zeros((60, 40), np.uint8)
        cv2.line(vertical, (20, 9), (20, 36), 255, 1)
        self.assertFalse(colored_table._vertical_line_crosses_cell(vertical, 20, 3, 42))
        cv2.line(vertical, (20, 3), (21, 42), 255, 1)
        self.assertTrue(colored_table._vertical_line_crosses_cell(vertical, 20, 3, 42))

    def test_color_only_probe_does_not_guess_a_gray_header(self):
        image = np.full((400, 800, 3), 255, np.uint8)
        with patch.object(colored_table, "_line_header_mask", side_effect=AssertionError("不得猜测灰色表头")):
            self.assertIsNone(colored_table._warp_candidate(image, allow_line_header=False))
        self.assertIsNotNone(colored_table._warp_candidate(self.table_photo(), allow_line_header=False))

    def test_letter_stem_near_missing_separator_does_not_split_a_merge(self):
        rows, columns = list(range(20, 221, 40)), list(range(20, 521, 100))
        image = np.full((241, 541, 3), 255, np.uint8)
        for y in rows:
            cv2.line(image, (20, y), (520, y), (0, 0, 0), 1)
        for x in columns:
            cv2.line(image, (x, 20), (x, 220), (0, 0, 0), 1)
        image[101:140, 219:222] = 255
        cv2.putText(image, "Jhon", (206, 133), cv2.FONT_HERSHEY_SIMPLEX, .9, (0, 0, 0), 2)
        spans = colored_table.physical_spans(image, rows, columns)
        self.assertTrue(any(s['row'] == 2 and s['column'] == 1 and s['column_span'] == 2 for s in spans))
        cv2.line(image, (229, 100), (229, 140), (0, 0, 0), 1)
        spans = colored_table.physical_spans(image, rows, columns)
        self.assertFalse(any(s['row'] == 2 and s['column'] == 1 for s in spans))

    def test_light_separator_on_dark_fill_does_not_merge_two_rows(self):
        image = np.full((250, 1150, 3), 255, np.uint8)
        rows = list(range(24, 225, 40))
        columns = list(range(24, 1125, 100))
        cv2.rectangle(image, (columns[6], rows[0]), (columns[7], rows[2]), (0, 0, 255), -1)
        for y in rows:
            cv2.line(image, (columns[0], y), (columns[-1], y), (196, 188, 184), 1)
        for x in columns:
            cv2.line(image, (x, rows[0]), (x, rows[-1]), (196, 188, 184), 1)
        self.assertEqual(colored_table.physical_spans(image, rows, columns), [])

    def table_photo(self, *, bottom_rule: bool = True, faded_last_header: bool = False):
        image = np.full((1100, 1200, 3), (95, 105, 110), dtype=np.uint8)
        cv2.rectangle(image, (120, 100), (1080, 1010), (245, 245, 245), -1)
        cv2.rectangle(image, (170, 220), (1030, 300), (235, 231, 222), -1)
        if faded_last_header:
            cv2.rectangle(image, (815, 221), (1030, 299), (245, 245, 245), -1)
        for row in range(2, 10, 2):
            top = 220 + row * 76
            cv2.rectangle(image, (170, top), (1030, top + 76), (240, 241, 240), -1)
        for y in (220, 300, 980) if bottom_rule else (220, 300):
            cv2.line(image, (170, y), (1030, y), (65, 65, 65), 2)
        for column, header in enumerate(("ID", "ITEM", "QTY", "COST")):
            cv2.putText(
                image, header, (180 + column * 215, 267),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2,
            )
        for row in range(1, 10):
            y = 220 + row * 76 + 50
            for column, value in enumerate((str(row), "PART", "12", "30.00")):
                cv2.putText(
                    image, value, (180 + column * 215, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2,
                )
        return image

    def test_physical_header_and_rows_define_grid(self):
        candidate = colored_table.detect_colored_grid(self.table_photo())

        self.assertIsNotNone(candidate)
        self.assertEqual((len(candidate.rows) - 1, len(candidate.columns) - 1), (10, 4))
        locations, crops = colored_table.cell_crops(candidate)
        self.assertEqual(len(locations), len(crops))
        self.assertIn((9, 3), locations)

    def test_missing_outer_rule_does_not_claim_grid(self):
        self.assertIsNone(colored_table.detect_colored_grid(self.table_photo(bottom_rule=False)))

    def test_saturated_short_grid_uses_continuous_neutral_rules(self):
        image = np.full((258, 620, 3), 255, dtype=np.uint8)
        rows = [24, 66, 108, 150, 192, 234]
        columns = [24, 123, 217, 596]
        for row in range(5):
            cv2.rectangle(
                image, (columns[0], rows[row]), (columns[-1], rows[row + 1]),
                (0, 0, 255) if row == 3 else (80, 176, 0), -1,
            )
        for y in rows:
            cv2.line(image, (columns[0], y), (columns[-1], y), (197, 190, 185))
        for x in columns:
            cv2.line(image, (x, rows[0]), (x, rows[-1]), (197, 190, 185))
        candidate = colored_table.detect_colored_grid(image)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.rows, rows)
        self.assertEqual(candidate.columns, columns)

    def test_saturated_header_and_dark_rules_preserve_one_header_row(self):
        image = np.full((250, 620, 3), 255, dtype=np.uint8)
        rows = [24, 104, 144, 184, 224]
        columns = [24, 154, 304, 464, 596]
        cv2.rectangle(image, (24, 24), (596, 104), (130, 0, 230), -1)
        cv2.rectangle(image, (154, 104), (304, 224), (0, 240, 240), -1)
        for y in rows:
            cv2.line(image, (24, y), (596, y), (48, 48, 48), 2)
        for x in columns:
            cv2.line(image, (x, 24), (x, 224), (48, 48, 48), 2)
        candidate = colored_table.detect_colored_grid(image)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.rows, rows)
        self.assertEqual(candidate.columns, columns)

    def test_colored_subset_cannot_hide_an_uncolored_physical_column(self):
        image = np.full((384, 438, 3), 255, dtype=np.uint8)
        rows = [24, 66, 108, 150, 192, 234, 276, 318, 360]
        columns = [24, 155, 286, 414]
        for row in range(1, 8):
            cv2.rectangle(image, (24, rows[row]), (286, rows[row + 1]),
                          (0, 0, 255) if row % 2 else (80, 176, 0), -1)
        for y in rows:
            cv2.line(image, (24, y), (414, y), (185, 190, 197))
        for x in columns:
            cv2.line(image, (x, 24), (x, 360), (185, 190, 197))
        with patch("table_pipeline.extract_ruled_grid", return_value=(columns, rows, image)):
            self.assertIsNone(colored_table._neutral_color_grid(image, 320))

    def test_full_width_merged_terminal_row_counts_as_visible_content(self):
        image = np.full((280, 420, 3), 245, dtype=np.uint8)
        rows = [0, 70, 140, 210, 280]
        columns = [0, 140, 280, 420]
        cv2.putText(
            image, "Definition", (12, 255),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2,
        )
        span = {
            "row": 3, "column": 0, "row_span": 1,
            "column_span": 3, "role": "group_header",
        }

        with patch.object(colored_table, "physical_spans", return_value=[span]):
            self.assertTrue(
                colored_table._last_row_has_text(image, rows, columns)
            )

        with patch.object(colored_table, "physical_spans", return_value=[]):
            self.assertFalse(
                colored_table._last_row_has_text(image, rows, columns)
            )

    def test_faded_header_color_does_not_drop_visible_last_column(self):
        candidate = colored_table.detect_colored_grid(
            self.table_photo(faded_last_header=True)
        )

        self.assertIsNotNone(candidate)
        self.assertEqual((len(candidate.rows) - 1, len(candidate.columns) - 1), (10, 4))

    def test_missing_top_rows_recover_from_one_distant_outer_rule(self):
        source = np.full((1000, 1000, 3), 245, dtype=np.uint8)
        cropped = np.full((500, 800, 3), 245, dtype=np.uint8)
        corners = np.float32([
            [100, 300], [900, 300], [900, 800], [100, 800],
        ])
        rows = list(range(0, 501, 50))
        columns = list(range(0, 801, 100))
        recovered_rows = list(range(0, 651, 50))
        line = np.asarray([[[100, 150, 900, 150]]], dtype=np.int32)

        with patch.object(cv2, "HoughLinesP", return_value=line), patch.object(
            colored_table, "_physical_geometry",
            return_value=(recovered_rows, columns),
        ):
            missing, recovered = colored_table._recover_missing_top_ruled_rows(
                source, cropped, corners, rows, columns
            )

        self.assertTrue(missing)
        self.assertIsNotNone(recovered)
        self.assertEqual(len(recovered[2][0]), len(rows) + 3)
        self.assertEqual(float(recovered[1][0, 1]), 150.0)

    def test_one_clipped_top_row_recovers_from_long_outer_rule(self):
        source = np.full((720, 900, 3), 245, dtype=np.uint8)
        cropped = np.full((500, 800, 3), 245, dtype=np.uint8)
        corners = np.float32([
            [50, 120], [850, 120], [850, 620], [50, 620],
        ])
        rows = list(range(0, 501, 50))
        columns = list(range(0, 801, 100))
        recovered_rows = list(range(0, 551, 50))
        line = np.asarray([[[60, 70, 840, 70]]], dtype=np.int32)

        with patch.object(cv2, "HoughLinesP", return_value=line), patch.object(
            colored_table, "_physical_geometry", return_value=None,
        ), patch.object(
            colored_table, "_complete_ruled_geometry",
            return_value=(recovered_rows, columns),
        ):
            missing, recovered = colored_table._recover_missing_top_ruled_rows(
                source, cropped, corners, rows, columns,
                minimum_top_fraction=0.08,
            )

        self.assertTrue(missing)
        self.assertIsNotNone(recovered)
        self.assertEqual(len(recovered[2][0]), len(rows) + 1)
        self.assertEqual(float(recovered[1][0, 1]), 70.0)

    def test_clipped_top_replaces_weak_old_crop_edge_instead_of_splitting_header(self):
        source = np.full((720, 900, 3), 245, dtype=np.uint8)
        cropped = np.full((500, 800, 3), 245, dtype=np.uint8)
        corners = np.float32([
            [50, 120], [850, 120], [850, 620], [50, 620],
        ])
        rows = list(range(0, 501, 50))
        columns = list(range(0, 801, 100))
        expanded_rows = [0, 48, *list(range(90, 541, 50))]
        line = np.asarray([[[60, 70, 840, 70]]], dtype=np.int32)
        horizontal = np.zeros((600, 800), dtype=np.uint8)
        horizontal[88:93] = 255

        with patch.object(cv2, "HoughLinesP", return_value=line), patch.object(
            colored_table, "_physical_geometry", return_value=None,
        ), patch.object(
            colored_table, "_complete_ruled_geometry",
            return_value=(expanded_rows, columns),
        ), patch("table_pipeline._grid_maps", return_value=(
            horizontal, np.zeros_like(horizontal), np.zeros_like(horizontal)
        )):
            missing, recovered = colored_table._recover_missing_top_ruled_rows(
                source, cropped, corners, rows, columns,
                minimum_top_fraction=0.08,
            )

        self.assertTrue(missing)
        self.assertIsNotNone(recovered)
        self.assertNotIn(48, recovered[2][0])
        self.assertEqual(len(recovered[2][0]), len(rows))

    def test_neutral_gray_header_uses_physical_rules(self):
        image = self.table_photo()
        cv2.rectangle(image, (171, 221), (1029, 299), (226, 226, 226), -1)
        for column, header in enumerate(("ID", "ITEM", "QTY", "COST")):
            cv2.putText(
                image, header, (180 + column * 215, 267),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2,
            )

        candidate = colored_table.detect_colored_grid(image)
        self.assertIsNotNone(candidate)
        self.assertEqual((len(candidate.rows) - 1, len(candidate.columns) - 1), (10, 4))

    def test_complete_rules_replace_header_text_that_undercounts_columns(self):
        image = np.full((320, 700, 3), 255, dtype=np.uint8)
        rows = list(range(0, 321, 40))
        ruled_columns = [0, 100, 200, 300, 400, 500, 600, 700]
        for column in ruled_columns:
            x = min(column, image.shape[1] - 1)
            cv2.line(image, (x, 0), (x, image.shape[0] - 1), (0, 0, 0), 3)
        for row in rows:
            y = min(row, image.shape[0] - 1)
            cv2.line(image, (0, y), (image.shape[1] - 1, y), (0, 0, 0), 3)

        text_geometry = rows, [0, 110, 405, 700]
        ruled_geometry = rows, ruled_columns
        self.assertTrue(colored_table._complete_rules_supersede_text_geometry(
            image, text_geometry, ruled_geometry,
        ))

        image[:, 298:303] = 255
        self.assertFalse(colored_table._complete_rules_supersede_text_geometry(
            image, text_geometry, ruled_geometry,
        ))

    def test_complete_rules_remove_weak_pseudo_boundary_inside_first_row(self):
        image = np.full((360, 560, 3), 245, dtype=np.uint8)
        columns = [0, 140, 280, 420, 559]
        rows = [0, 42, 80, 150, 220, 290, 359]
        horizontal = np.zeros(image.shape[:2], dtype=np.uint8)
        for row in rows:
            if row != 42:
                horizontal[max(0, row - 1):min(image.shape[0], row + 2)] = 255

        with patch("table_pipeline.extract_ruled_grid", return_value=(
            columns, rows, image.copy()
        )), patch("table_pipeline._grid_maps", return_value=(
            horizontal, np.zeros_like(horizontal), np.zeros_like(horizontal)
        )):
            geometry = colored_table._complete_ruled_geometry(image, 320)

        self.assertIsNotNone(geometry)
        self.assertNotIn(42, geometry[0])

    def test_terminal_full_width_label_is_a_visible_merged_span(self):
        image = np.full((240, 420, 3), 245, dtype=np.uint8)
        rows = [0, 60, 120, 180, 239]
        columns = [0, 105, 210, 315, 419]
        for row in rows:
            cv2.line(image, (0, row), (419, row), (25, 25, 25), 3)
        for column in columns:
            cv2.line(image, (column, 0), (column, 180), (25, 25, 25), 3)
        cv2.putText(
            image, "Final definition", (8, 222),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (25, 25, 25), 1,
        )

        spans = colored_table.physical_spans(image, rows, columns)

        self.assertIn({
            "row": 3, "column": 0, "row_span": 1,
            "column_span": 4, "role": "merged",
        }, spans)

    def test_visible_word_gap_distinguishes_one_and_two_spaces(self):
        def sample(gap):
            crop = np.full((64, 120, 3), 255, dtype=np.uint8)
            cv2.rectangle(crop, (10, 8), (29, 55), (25, 25, 25), -1)
            cv2.rectangle(crop, (30 + gap, 8), (49 + gap, 55), (25, 25, 25), -1)
            return crop

        self.assertFalse(colored_table.has_visible_double_space(sample(10)))
        self.assertTrue(colored_table.has_visible_double_space(sample(20)))

        crop = np.full((64, 140, 3), 255, dtype=np.uint8)
        for left, right in ((10, 29), (38, 57), (79, 98), (107, 126)):
            cv2.rectangle(crop, (left, 8), (right, 55), (25, 25, 25), -1)
        self.assertTrue(colored_table.has_visible_double_space(crop))

    def test_masked_digits_are_counted_from_visible_glyphs(self):
        def sample(text):
            crop = np.full((70, 260, 3), 255, dtype=np.uint8)
            cv2.putText(crop, text, (5, 50), cv2.FONT_HERSHEY_SIMPLEX,
                        1, (20, 20, 20), 2, cv2.LINE_AA)
            return crop

        self.assertEqual(
            colored_table.masked_numeric_shape(sample("138****9229")), (3, 4, 4)
        )
        self.assertEqual(
            colored_table.masked_numeric_shape(sample("138***9229")), (3, 3, 4)
        )
        self.assertIsNone(colored_table.masked_numeric_shape(sample("1389229")))

    def test_slanted_vertical_rules_define_each_row_independently(self):
        image = np.full((600, 800, 3), 245, dtype=np.uint8)
        rows = list(range(0, 601, 60))
        for column in range(6):
            cv2.line(image, (40 + column * 140, 60),
                     (50 + column * 140, 599), (35, 35, 35), 2)

        boundaries, merged_title = colored_table._ruled_row_columns(image, rows, 5)
        self.assertTrue(merged_title)
        self.assertEqual(len(boundaries), 10)
        self.assertLess(boundaries[1][0], boundaries[-1][0])
        self.assertEqual(len(boundaries[1]), 6)

        incomplete = image.copy()
        cv2.line(incomplete, (740, 60), (750, 599), (245, 245, 245), 4)
        boundaries, suspected = colored_table._ruled_row_columns(incomplete, rows, 5)
        self.assertIsNone(boundaries)
        self.assertTrue(suspected)

    def test_short_mask_glyph_is_not_dropped(self):
        for count in (2, 3, 4):
            crop = np.full((29, 130, 3), 255, dtype=np.uint8)
            for index in range(3 + count + 4):
                height = (7 if index == 3 + count - 1 else 8) if 3 <= index < 3 + count else 19
                cv2.rectangle(crop, (5 + index * 10, 5),
                              (11 + index * 10, 4 + height), (20, 20, 20), -1)
            self.assertEqual(colored_table.masked_numeric_shape(crop), (3, count, 4))
            self.assertEqual(colored_table.visible_masked_star_count(crop), count)

    def test_vertical_rule_residue_does_not_create_text_rows(self):
        image = np.full((540, 960, 3), 255, dtype=np.uint8)
        ink = np.zeros(image.shape[:2], dtype=np.uint8)
        for row in range(12):
            for column in range(7):
                cv2.putText(ink, f"A{column}", (35 + column * 130, row * 45 + 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, 255, 2)
        for y in range(ink.shape[0]):
            for column in range(8):
                x = 5 + column * 130 + y // 60
                ink[y, x:x + 3] = 255
        with patch.object(colored_table, "_ink_mask", return_value=ink):
            geometry = colored_table._physical_geometry(image)
        self.assertIsNotNone(geometry)
        self.assertEqual((len(geometry[0]) - 1, len(geometry[1]) - 1), (12, 7))

    def test_slanted_outer_side_stays_inside_warp(self):
        image = self.table_photo()
        cv2.line(image, (170, 220), (170, 980), (35, 35, 35), 3)
        cv2.line(image, (1030, 220), (1030, 980), (35, 35, 35), 3)
        transform = np.float32([[1, -0.09, 20], [0, 1, 0]])
        slanted = cv2.warpAffine(image, transform, (1200, 1100), borderValue=(95, 105, 110))
        warped = colored_table._warp_candidate(slanted)
        self.assertIsNotNone(warped)
        _, corners = warped
        bottom_left = 170 - 0.09 * 980 + 20
        self.assertLess(float(corners[3, 0]), bottom_left)

    def test_slanted_dark_margin_shifts_each_borderless_row(self):
        image = np.full((660, 800, 3), 242, dtype=np.uint8)
        for y in range(image.shape[0]):
            image[y, :int(round(y * 0.04))] = 105
        rows = list(range(0, 661, 60))
        columns = [0, 110, 250, 390, 530, 670, 800]

        boundaries = colored_table._slanted_left_margin_row_columns(
            image, rows, columns
        )

        self.assertIsNotNone(boundaries)
        self.assertGreater(boundaries[-1][0], boundaries[0][0] + 18)
        self.assertGreater(boundaries[-1][1], boundaries[0][1] + 15)
        self.assertEqual(boundaries[-1][-1], image.shape[1])
        self.assertIsNone(colored_table._slanted_left_margin_row_columns(
            np.full_like(image, 242), rows, columns
        ))


if __name__ == "__main__":
    unittest.main()
