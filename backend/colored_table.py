from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import cv2
import numpy as np

try:
    from fast_stats import median_u8, median_u8_per_channel
except ImportError:  # pragma: no cover - 只在打包漏掉新模块时兜底
    try:
        # 2026-10-30（lead 复查 P2）：包模式（`import backend.colored_table`）下
        # 绝对导入拿不到同级模块，需要相对导入兜底。缺了它这条路径会**静默**退回
        # np.median —— 功能正确但白丢加速（实测单批 median 12.9 秒 → 慢路径），
        # 且没有任何告警，和 `ocr_backend.py` 里同类写法（相对导入兜底）不一致。
        from .fast_stats import median_u8, median_u8_per_channel
    except ImportError:
        def median_u8(values: np.ndarray) -> float:
            return float(np.median(values))

        def median_u8_per_channel(values: np.ndarray) -> np.ndarray:
            return np.median(values, axis=(0, 1))


@dataclass
class ColoredGrid:
    image: np.ndarray
    rows: list[int]
    columns: list[int]
    corners: list[list[float]]
    merged_title: bool = False
    row_columns: list[list[int]] | None = None
    spans: list[dict] | None = None
    complete_ruled: bool = False
    source_projection: bool = False
    geometry_source: str = "photographic_complete_ruled_line_grid"

    def cell_image(self, row: int, column: int, *, full_title: bool = False) -> np.ndarray:
        edges = self.row_columns[row] if self.row_columns is not None else self.columns
        merged = next((span for span in self.spans or []
                       if span["row"] == row and span["column"] == column), None)
        row_span = int(merged.get("row_span", 1)) if merged else 1
        column_span = int(merged.get("column_span", 1)) if merged else 1
        left = edges[0] if full_title else edges[column]
        right = edges[-1] if full_title else edges[column + column_span]
        if self.row_columns is not None and row_span > 1:
            left = min(self.row_columns[r][column] for r in range(row, row + row_span))
            right = max(self.row_columns[r][column + column_span] for r in range(row, row + row_span))
        return self.image[
            self.rows[row] + 2:self.rows[row + row_span] - 2,
            left + 2:right - 2,
        ]


def _bands(indices: np.ndarray) -> list[tuple[int, int]]:
    groups: list[list[int]] = []
    for value in indices:
        index = int(value)
        if groups and index <= groups[-1][-1] + 2:
            groups[-1].append(index)
        else:
            groups.append([index])
    return [(group[0], group[-1]) for group in groups]


def _ink_mask(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 31, 11,
    )
    horizontal = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, image.shape[1] // 12), 1)),
    )
    vertical = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, image.shape[0] // 12))),
    )
    return cv2.subtract(cv2.subtract(binary, horizontal), vertical)


def _local_rule_support(
    binary: np.ndarray, *, orientation: str,
    fixed: int, start: int, end: int, search_radius: int,
) -> float:
    """Measure a long local rule without requiring it to align globally.

    Phone perspective correction can leave a separator several pixels away from
    the median grid boundary.  The global morphology then reports a missing rule
    even though the rule is plainly visible inside that single row or column.
    Only a stroke covering most of the cell edge counts here, so ordinary glyphs
    and check marks near a boundary cannot turn a real merge into split cells.
    """
    if end - start < 8:
        return 0.0
    if orientation == "vertical":
        band = binary[
            max(0, start):min(binary.shape[0], end),
            max(0, fixed - search_radius):min(binary.shape[1], fixed + search_radius + 1),
        ]
        if not band.size:
            return 0.0
        length = max(5, int(round(band.shape[0] * 0.72)))
        opened = cv2.morphologyEx(
            band, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, length)),
        )
        return float(np.mean(np.any(opened > 0, axis=1)))
    band = binary[
        max(0, fixed - search_radius):min(binary.shape[0], fixed + search_radius + 1),
        max(0, start):min(binary.shape[1], end),
    ]
    if not band.size:
        return 0.0
    length = max(5, int(round(band.shape[1] * 0.72)))
    opened = cv2.morphologyEx(
        band, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1)),
    )
    return float(np.mean(np.any(opened > 0, axis=0)))


def _vertical_line_crosses_cell(vertical: np.ndarray, x: int, top: int, bottom: int) -> bool:
    band = vertical[max(0, top + 1):bottom, max(0, x - 3):x + 4]
    if not band.size:
        return False
    lines = cv2.HoughLinesP(
        band, 1, np.pi / 180,
        threshold=max(6, int((bottom - top) * .50)),
        minLineLength=(bottom - top) * .85, maxLineGap=2,
    )
    return bool(lines is not None and any(
        abs(int(y2) - int(y1)) >= (bottom - top) * .85
        for _, y1, _, y2 in lines.reshape(-1, 4)
    ))


def physical_spans(
    image: np.ndarray, rows: list[int], columns: list[int],
    row_columns: list[list[int]] | None = None,
    *, stacked_header_roles: bool = False, crossing_glyph_merges: bool = False,
) -> list[dict] | None:
    """以强网格中局部缺失的分隔线恢复矩形合并区域。"""
    import table_pipeline as pipeline
    row_count, column_count = len(rows) - 1, len(columns) - 1
    if row_count < 3 or column_count < 2:
        return []
    horizontal, vertical = pipeline._physical_rule_maps(image)
    dark_vertical = pipeline._grid_maps(image)[1]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h = np.zeros((row_count - 1, column_count), dtype=float)
    v = np.zeros((row_count, column_count - 1), dtype=float)
    row_radius = max(3, min(8, int(round(float(np.median(np.diff(rows))) * 0.15))))
    for row in range(row_count - 1):
        y = rows[row + 1]
        edges = row_columns[row] if row_columns is not None else columns
        for column in range(column_count):
            left, right = edges[column:column + 2]
            inset = max(4, int((right - left) * 0.12))
            band = horizontal[max(0, y - row_radius):min(image.shape[0], y + row_radius + 1), left + inset:right - inset]
            h[row, column] = float(np.mean(np.any(band > 0, axis=0))) if band.size else 0.0
    for row in range(row_count):
        top, bottom = rows[row:row + 2]
        inset = max(4, int((bottom - top) * 0.15))
        edges = row_columns[row] if row_columns is not None else columns
        for column in range(column_count - 1):
            x = edges[column + 1]
            band = vertical[top + inset:bottom - inset, max(0, x - 3):min(image.shape[1], x + 4)]
            ends = vertical[top + 3:bottom - 3, max(0, x - 3):min(image.shape[1], x + 4)]
            end_height = max(3, int((bottom - top) * 0.12))
            end_support = min(
                float(np.mean(np.any(ends[:end_height] > 0, axis=1))) if ends.size else 0.0,
                float(np.mean(np.any(ends[-end_height:] > 0, axis=1))) if ends.size else 0.0,
            )
            light_supported = min(
                float(np.mean(np.any(band > 0, axis=1))) if band.size else 0.0,
                end_support,
            )
            dark_band = dark_vertical[top + inset:bottom - inset, max(0, x - 3):min(image.shape[1], x + 4)]
            dark_ends = dark_vertical[top + 3:bottom - 3, max(0, x - 3):min(image.shape[1], x + 4)]
            # 分隔线必须连接两端，单侧碰线的字母笔画不能拆开合并格。
            dark_supported = min(
                float(np.mean(np.any(dark_band > 0, axis=1))) if dark_band.size else 0.0,
                min(float(np.mean(np.any(dark_ends[:end_height] > 0, axis=1))) if dark_ends.size else 0.0,
                    float(np.mean(np.any(dark_ends[-end_height:] > 0, axis=1))) if dark_ends.size else 0.0),
            )
            # 局部增强也可能检出字母的短竖笔画；新增证据必须贯穿整格。
            if light_supported > dark_supported and not _vertical_line_crosses_cell(
                vertical, x, top, bottom,
            ):
                light_supported = dark_supported
            v[row, column] = max(dark_supported, light_supported)
            if v[row, column] < .70:
                width = min(edges[column + 1] - edges[column],
                            edges[column + 2] - edges[column + 1])
                radius = min(40, max(8, int(round(width * .15))))
                start, stop = max(0, x - radius), min(vertical.shape[1], x + radius + 1)
                nearby = vertical[top + inset:bottom - inset, start:stop]
                if nearby.size:
                    support = np.mean(nearby > 0, axis=0)
                    candidates = np.flatnonzero(support >= .85)
                    if any(_vertical_line_crosses_cell(vertical, start + int(offset), top, bottom)
                           for offset in sorted(candidates, key=lambda value: abs(start + value - x))[:3]):
                        v[row, column] = .90
            if (v[row, column] < .70 and x >= 5 and x + 5 < gray.shape[1]
                    and bottom - top >= 16):
                center = np.max(gray[top + inset:bottom - inset, x - 1:x + 2], axis=1)
                neighbours = np.median(np.stack((
                    gray[top + inset:bottom - inset, x - 4],
                    gray[top + inset:bottom - inset, x + 4],
                ), axis=1), axis=1)
                if (center.size and float(np.median(neighbours)) < 230
                        and float(np.mean(center - neighbours >= 8)) >= .85):
                    v[row, column] = .90
    if float(np.median(h)) < 0.65 or float(np.median(v)) < 0.65:
        return []
    local_binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 31, 11,
    )
    median_row_height = float(np.median(np.diff(rows)))
    median_column_width = float(np.median(np.diff(columns)))
    parents = list(range(row_count * column_count))
    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index
    def join(first: int, second: int) -> None:
        parents[find(second)] = find(first)

    def separate_centered_text(row: int, column: int) -> bool:
        for adjacent in (row, row + 1):
            edges = row_columns[adjacent] if row_columns is not None else columns
            left, right = edges[column:column + 2]
            top, bottom = rows[adjacent:adjacent + 2]
            crop = local_binary[top + 6:bottom - 6, left + 8:right - 8]
            if crop.size == 0:
                return False
            bands = _bands(np.flatnonzero(np.count_nonzero(crop, axis=1) >= 2))
            if not any(
                last - first + 1 >= 5
                and abs((first + last) * .5 + top + 6 - (top + bottom) * .5)
                <= (bottom - top) * .22
                for first, last in bands
            ):
                return False
        return True

    def glyph_crosses_missing_row_rule(row: int, column: int) -> bool:
        boundary = rows[row + 1]
        left, right = columns[column:column + 2]
        half_height = min(25, max(12, int(median_row_height * .4)))
        top = max(0, boundary - half_height)
        bottom = min(gray.shape[0], boundary + half_height)
        crop = gray[top:bottom, left + 8:right - 8]
        if crop.size == 0:
            return False
        ink = np.uint8(crop < min(160, float(np.median(crop)) - 30))
        _, _, components, _ = cv2.connectedComponentsWithStats(ink, 8)
        center = boundary - top
        return any(
            y + 4 <= center <= y + height - 4
            and 2 <= width <= min(40, (right - left) * .6)
            and 12 <= height <= median_row_height * .7
            and area >= 15
            for x, y, width, height, area in components[1:]
        )

    for row in range(row_count - 1):
        anchor_columns = int(np.count_nonzero(h[row] >= .85))
        independent = {column for column in range(column_count)
                       if separate_centered_text(row, column)}
        for column in range(column_count):
            crossing_glyph = False
            if crossing_glyph_merges and h[row, column] <= .35:
                first = last = row
                while first > 0 and h[first - 1, column] <= .35:
                    first -= 1
                while last + 1 < row_count - 1 and h[last + 1, column] <= .35:
                    last += 1
                crossing_glyph = bool(
                    last - first < 3
                    and glyph_crosses_missing_row_rule(row, column)
                )
            if ((h[row, column] <= 0.10 or crossing_glyph)
                    and (float(np.median(np.delete(h[row], column))) >= 0.70
                         or (column_count >= 8 and anchor_columns >= 2))):
                if column in independent and not crossing_glyph:
                    continue
                if (not crossing_glyph and column - 1 in independent
                        and column + 1 in independent):
                    first = last = row
                    while first and h[first - 1, column] <= .10:
                        first -= 1
                    while last + 1 < row_count - 1 and h[last + 1, column] <= .10:
                        last += 1
                    long_blank_merge = bool(
                        0 < column < column_count - 1 and last - first >= 3
                        and all(min(v[index, column - 1], v[index, column]) >= .85
                                for index in range(first, last + 2))
                    )
                    if not long_blank_merge:
                        continue
                edges = row_columns[row] if row_columns is not None else columns
                left, right = edges[column:column + 2]
                column_inset = max(4, int((right - left) * 0.12))
                search_radius = max(
                    12,
                    min(22, int(round(median_row_height * 0.28))),
                )
                if _local_rule_support(
                    local_binary, orientation="horizontal",
                    fixed=rows[row + 1], start=left + column_inset,
                    end=right - column_inset, search_radius=search_radius,
                ) >= 0.78:
                    continue
                join(row * column_count + column, (row + 1) * column_count + column)
    for row in range(row_count):
        for column in range(column_count - 1):
            if v[row, column] <= 0.10 and float(np.median(np.delete(v[:, column], row))) >= 0.70:
                top, bottom = rows[row:row + 2]
                edges = row_columns[row] if row_columns is not None else columns
                search_radius = max(18, min(28, int(round(median_column_width * 0.10))))
                if _local_rule_support(
                    local_binary, orientation="vertical",
                    fixed=edges[column + 1], start=top + 2,
                    end=bottom - 2, search_radius=search_radius,
                ) >= 0.78:
                    continue
                join(row * column_count + column, row * column_count + column + 1)
    groups: dict[int, list[tuple[int, int]]] = {}
    for row in range(row_count):
        for column in range(column_count):
            groups.setdefault(find(row * column_count + column), []).append((row, column))
    result = []
    text_ink = None
    for group in groups.values():
        if len(group) == 1:
            continue
        top = min(row for row, _ in group); bottom = max(row for row, _ in group) + 1
        left = min(column for _, column in group); right = max(column for _, column in group) + 1
        if (bottom - top) * (right - left) != len(group):
            return None
        if text_ink is None:
            text_ink = _ink_mask(image)
        region = text_ink[rows[top] + 4:rows[bottom] - 4, columns[left] + 6:columns[right] - 6]
        if bottom - top >= 5 and right - left == 1:
            interiors = [image[rows[row] + 4:rows[row + 1] - 4,
                               columns[left] + 6:columns[right] - 6]
                         for row in range(top, bottom)]
            if interiors and all(cell.size for cell in interiors):
                medians = [float(np.median(cell)) for cell in interiors]
                if (max(medians) < 245 and max(medians) - min(medians) <= 5
                        and all(float(np.mean(np.abs(cell.astype(np.int16) - median) <= 2)) >= .95
                                for cell, median in zip(interiors, medians))):
                    continue
        paragraph_banner = False
        if (top > 0 and bottom == top + 1 and right == column_count
                and left <= 1 and column_count >= 4):
            surface = image[rows[top] + 4:rows[bottom] - 4,
                            columns[left] + 6:columns[right] - 6]
            if surface.size:
                color = np.median(surface, axis=(0, 1))
                paragraph_banner = bool(
                    np.min(color) < 230
                    and float(np.mean(np.max(np.abs(surface.astype(np.int16) - color),
                                              axis=2) <= 10)) >= .70
                )
                if paragraph_banner:
                    local = [image[rows[top] + 4:rows[bottom] - 4,
                                   columns[column] + 3:columns[column + 1] - 3]
                             for column in range(left, right)]
                    backgrounds = np.asarray([
                        np.median(cell, axis=(0, 1)) for cell in local if cell.size
                    ])
                    if (len(backgrounds) != right - left
                            or np.max(np.ptp(backgrounds, axis=0)) > 20):
                        paragraph_banner = False
                if paragraph_banner:
                    aligned = 0
                    for cell in local:
                        gray = cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY)
                        threshold = max(55, min(210, float(np.median(gray)) - 30))
                        ink = gray < threshold
                        near = float(np.mean(ink[:, :min(26, ink.shape[1])]))
                        tail = float(np.mean(ink[:, -min(9, ink.shape[1]):]))
                        aligned += near >= .08 and tail <= .03
                    if aligned >= max(3, int(np.ceil((right - left) * .60))):
                        paragraph_banner = False
        glyphs = []
        if region.size and right - left > 1:
            _, _, components, _ = cv2.connectedComponentsWithStats(region, 8)
            glyphs = sorted((int(x), int(x + width), int(height))
                            for x, _, width, height, area in components[1:]
                            if area >= 8 and height >= 8 and width >= 2
                            and not (
                                height >= region.shape[0] * 0.85
                                and (
                                    x <= region.shape[1] * 0.04
                                    or x + width >= region.shape[1] * 0.96
                                )
                            ))
            if glyphs:
                gap = max(14.0, float(np.median([height for _, _, height in glyphs])) * 1.25)
                words = 1
                end = glyphs[0][1]
                for start, finish, _ in glyphs[1:]:
                    words += start - end > gap
                    end = max(end, finish)
                full_width_first_row = bool(
                    top == 0
                    and bottom == 1
                    and left == 0
                    and right == column_count
                )
                full_width_single_anchor = bool(
                    left == 0
                    and right == column_count
                    and len(columns) >= 2
                    and max(finish for _, finish, _ in glyphs)
                    < columns[1] - columns[0] - 6
                )
                if words > (
                    2 if full_width_first_row or full_width_single_anchor else 1
                ) and not paragraph_banner:
                    continue
        if region.size and bottom - top > 1:
            bands = _bands(np.flatnonzero(np.count_nonzero(region, axis=1) >= 2))
            if bottom - top == 2 and right - left == 1:
                significant = [(first, last) for first, last in bands
                               if last - first + 1 >= 5]
                if len(significant) == 1:
                    first, last = significant[0]
                    text_center = (first + last) * .5 + rows[top] + 4
                    lower_center = (rows[top + 1] + rows[bottom]) * .5
                    if abs(text_center - lower_center) <= (rows[bottom] - rows[top + 1]) * .20:
                        # 下格居中的文字不能证明它与上方空格纵向合并。
                        continue
            if bottom - top >= 5 and right - left == 1:
                significant = [(first, last) for first, last in bands
                               if last - first + 1 >= max(5, median_row_height * 0.12)]
                if not significant:
                    right_edge_supported = bool(
                        column_count == 2 and left == 1 and right == 2
                        and all(v[row, 0] >= .85 for row in range(top, bottom))
                        and float(np.mean(np.any(vertical[
                            rows[top] + 4:rows[bottom] - 4,
                            max(0, columns[-1] - 3):min(image.shape[1], columns[-1] + 4),
                        ] > 0, axis=1))) >= .85
                    )
                    blank_merge_proven = bool(
                        ((0 < left and right < column_count)
                         or right_edge_supported)
                        and float(np.median(image[
                            rows[top] + 4:rows[bottom] - 4,
                            columns[left] + 6:columns[right] - 6,
                        ])) >= 245
                        and (right_edge_supported or all(
                            min(v[row, left - 1], v[row, right - 1]) >= .85
                            for row in range(top, bottom)))
                        and all(h[row, left] <= .10
                                and float(np.median(np.delete(h[row], left))) >= .85
                                for row in range(top, bottom - 1))
                    )
                    if not blank_merge_proven:
                        continue
                row_centers = np.asarray([
                    (rows[row] + rows[row + 1]) * 0.5
                    for row in range(top, bottom)
                ])
                occupied = set()
                for first, last in significant:
                    text_center = (first + last) * 0.5 + rows[top] + 4
                    row_index = int(np.argmin(np.abs(row_centers - text_center)))
                    row_height = rows[top + row_index + 1] - rows[top + row_index]
                    if abs(row_centers[row_index] - text_center) <= row_height * 0.12:
                        occupied.add(row_index)
                if (len(occupied) >= 3 and min(occupied) >= 2
                        and max(occupied) == bottom - top - 1
                        and max(occupied) - min(occupied) + 1 == len(occupied)):
                    continue
            if len(bands) == bottom - top and all(
                abs((first + last) * 0.5 + rows[top] + 4 - (rows[row] + rows[row + 1]) * 0.5)
                < (rows[row + 1] - rows[row]) * 0.10
                for row, (first, last) in zip(range(top, bottom), bands)
            ):
                continue
            if bottom - top >= 5 and len(bands) >= int(np.ceil((bottom - top) * 0.85)):
                row_centers = np.asarray([
                    (rows[row] + rows[row + 1]) * 0.5
                    for row in range(top, bottom)
                ])
                occupied = set()
                for first, last in bands:
                    text_center = (first + last) * 0.5 + rows[top] + 4
                    row_index = int(np.argmin(np.abs(row_centers - text_center)))
                    row_height = rows[top + row_index + 1] - rows[top + row_index]
                    if abs(row_centers[row_index] - text_center) <= row_height * 0.12:
                        occupied.add(row_index)
                if len(occupied) >= max(4, int(np.ceil((bottom - top) * 0.85))):
                    continue
            significant = [(first, last) for first, last in bands
                           if last - first + 1 >= max(5, median_row_height * .12)]
            if (bottom - top >= 3 and len(significant) == bottom - top
                    and all(abs((first + last) * .5 + rows[top] + 4
                                - (rows[row] + rows[row + 1]) * .5)
                            <= (rows[row + 1] - rows[row]) * .12
                            for row, (first, last) in zip(range(top, bottom), significant))):
                continue
            if len(significant) == 1 and bottom - top >= 3:
                first, last = significant[0]
                text_center = (first + last) * .5 + rows[top] + 4
                first_cell_center = (rows[top] + rows[top + 1]) * .5
                last_cell_center = (rows[bottom - 1] + rows[bottom]) * .5
                if (min(abs(text_center - first_cell_center),
                        abs(text_center - last_cell_center)) <= median_row_height * .20):
                    # 长色块只有首格或末格文字，不足以证明整段纵向合并。
                    continue
        if region.size and top == 0 and bottom == 1 and left == 0 and right == column_count:
            ink_groups = _bands(np.flatnonzero(np.count_nonzero(region, axis=0) >= 2))
            last_column_start = columns[-2] - columns[0] - 6
            left_ends = [finish for _, finish in ink_groups if finish < last_column_start]
            right_starts = [start for start, _ in ink_groups if start > last_column_start]
            last_cell = gray[rows[top] + 4:rows[bottom] - 4,
                             columns[-2] + 4:columns[-1] - 4]
            last_cell_has_text = False
            if last_cell.size:
                threshold = min(170, float(np.median(last_cell)) - 30)
                _, _, components, _ = cv2.connectedComponentsWithStats(
                    np.uint8(last_cell < threshold), 8)
                last_cell_has_text = any(
                    area >= 8 and height >= 8 and width >= 2
                    and height < last_cell.shape[0] * .8
                    and width < last_cell.shape[1] * .8
                    for _, _, width, height, area in components[1:]
                )
            if (left_ends and right_starts
                    and last_cell_has_text
                    and min(right_starts) - last_column_start
                    >= (columns[-1] - columns[-2]) * 0.20
                    and min(right_starts) - max(left_ends)
                    >= max(24, (columns[-1] - columns[-2]) * 0.25)):
                right -= 1
        if (top == 0 and bottom == 1 and left == 0
                and right < column_count and right - left > 1 and glyphs):
            first_width = columns[1] - columns[0]
            glyph_left = min(start for start, _, _ in glyphs)
            glyph_right = max(finish for _, finish, _ in glyphs)
            surface = image[rows[top] + 4:rows[bottom] - 4,
                            columns[left] + 6:columns[right] - 6]
            coloured_band = bool(surface.size and
                                  float(np.ptp(np.median(surface, axis=(0, 1)))) >= 24)
            if (coloured_band
                    and glyph_left <= max(12, first_width * .08)
                    and glyph_right <= first_width - 10
                    and (glyph_left + glyph_right) * .5 < region.shape[1] * .32):
                # 标题只占首列且贴左对齐时，缺少竖线不足以证明横向合并。
                continue
        centred_group = bool(
            top <= 2 and left == 0 and right - left > 1 and glyphs
            and abs((min(start for start, _, _ in glyphs)
                     + max(finish for _, finish, _ in glyphs)) * .5
                    - region.shape[1] * .5) <= region.shape[1] * .18
        )
        role = ("merged" if (top == row_count - 1 and right - left > 1)
                or (paragraph_banner and len(glyphs) > 1)
                else "title" if top == 0 and left == 0 and bottom == 1
                and right == column_count
                else "row_header" if (top > 0 and left == 0
                                      and right - left == 1 and bottom - top > 1)
                else "group_header" if top == 0 or (left == 0 and right == column_count)
                or centred_group
                else "merged")
        result.append({"row":top,"column":left,"row_span":bottom-top,"column_span":right-left,"role":role})
    if any(span["row"] == 0 and span["column"] == 0
           and span["column_span"] == column_count and span["role"] == "title"
           for span in result):
        for span in result:
            if (span["row"] == 1 and span["column"] == 0
                    and span["column_span"] == column_count):
                span["role"] = "merged"
    if stacked_header_roles:
        for span in result:
            if (span["role"] == "group_header" and 0 < span["row"] <= 2
                    and any(previous["row"] == span["row"] - 1
                            and previous["column"] == span["column"]
                            and previous["column_span"] == span["column_span"]
                            for previous in result)):
                span["role"] = "merged"
    return sorted(result, key=lambda span: (span["row"], span["column"]))


def _snap_text_cut_boundaries(ink: np.ndarray, rows: list[int], columns: list[int]) -> list[int]:
    """列中线反复穿过正文时，移到相邻的连续空白列间隙。"""
    adjusted = list(columns)
    for index in range(1, len(columns) - 1):
        boundary = columns[index]
        cut_rows = sum(
            bool(np.any(ink[top + 3:bottom - 3, max(0, boundary - 2):boundary + 3]))
            for top, bottom in zip(rows[1:-1], rows[2:])
        )
        if cut_rows < max(3, int((len(rows) - 2) * 0.40)):
            continue
        radius = int(min(boundary - columns[index - 1], columns[index + 1] - boundary) * 0.25)
        left, right = max(0, boundary - radius), min(ink.shape[1], boundary + radius + 1)
        profile = np.count_nonzero(ink[rows[1]:rows[-1], left:right], axis=0)
        gaps = [(first + left, last + left) for first, last in _bands(np.flatnonzero(profile <= 2))
                if last - first >= max(12, radius * 0.20)]
        if gaps:
            first, last = min(gaps, key=lambda gap: abs((gap[0] + gap[1]) * 0.5 - boundary))
            adjusted[index] = (first + last) // 2
    return adjusted


def _physical_geometry(image: np.ndarray) -> tuple[list[int], list[int]] | None:
    height, width = image.shape[:2]
    ink = _ink_mask(image)
    profile = np.count_nonzero(ink > 0, axis=1)
    # 斜竖线残留会贯穿行间空白，先扣除低分位背景再定位文字行。
    profile = np.maximum(0, profile - np.percentile(profile, 5))
    row_bands = [
        (top, bottom)
        for top, bottom in _bands(np.flatnonzero(profile > max(16, width * 0.014)))
        if bottom - top >= 10
    ]
    if not 6 <= len(row_bands) <= 80:
        return None
    centers = np.asarray([(top + bottom) * 0.5 for top, bottom in row_bands])
    gaps = np.diff(centers[1:])
    pitch = float(np.median(gaps)) if gaps.size else 0.0
    if (
        pitch < 18
        or float(gaps.min()) < pitch * 0.70
        or float(gaps.max()) > pitch * 1.35
    ):
        return None

    header_limit = row_bands[0][1] + max(15, height // 30)
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink[:header_limit], 8)
    glyphs = sorted(
        (int(x), int(x + glyph_width), int(area))
        for x, _, glyph_width, glyph_height, area in stats[1:]
        if area >= 5
        and 8 <= glyph_height <= header_limit * 0.90
        and glyph_width >= max(2, glyph_height * 0.06)
        and glyph_width <= width * 0.20
    )
    if not glyphs:
        return None
    words: list[list[tuple[int, int, int]]] = []
    for glyph in glyphs:
        if words and glyph[0] <= words[-1][-1][1] + 20:
            words[-1].append(glyph)
        else:
            words.append([glyph])
    word_areas = [
        sum(glyph[2] for glyph in word)
        for word in words
    ]
    typical_area = float(np.median(word_areas)) if word_areas else 0.0
    words = [
        word for word, area in zip(words, word_areas)
        if area >= max(20.0, typical_area * 0.20)
    ]
    anchors = [min(glyph[0] for glyph in word) for word in words]
    if len(anchors) > 2:
        gap_values = np.diff(anchors)
        typical = float(np.median(sorted(gap_values)[len(gap_values) // 3:]))
        anchors = [anchors[0]] + [
            anchor for anchor, gap in zip(anchors[1:], gap_values)
            if gap >= typical * 0.52
        ]
    if not 3 <= len(anchors) <= 12:
        return None
    anchor_gaps = np.diff(anchors).astype(float)
    typical_column_gap = float(np.median(anchor_gaps))
    if (
        typical_column_gap < 25
        or float(anchor_gaps.min()) < typical_column_gap * 0.55
        or float(anchor_gaps.max()) > typical_column_gap * 1.70
    ):
        return None

    rows = [0] + [
        int(round((first + second) * 0.5))
        for first, second in zip(centers, centers[1:])
    ] + [min(height, int(round(centers[-1] + pitch * 0.49)))]
    columns = [0] + [
        int(round((first + second) * 0.5))
        for first, second in zip(anchors, anchors[1:])
    ] + [width]
    if min(np.diff(rows)) <= 0 or min(np.diff(columns)) <= 0:
        return None
    return rows, _snap_text_cut_boundaries(ink, rows, columns)


def _complete_ruled_geometry(
    image: np.ndarray, maximum_cells: int,
) -> tuple[list[int], list[int]] | None:
    """Recover a complete unequal-width grid from its physical rules.

    Text anchors are deliberately conservative and fail on legitimate tables
    whose columns have very different widths or whose first row contains
    several group headers.  A grid that reaches every side of the already
    isolated coloured-table crop is independent, stronger structure evidence.
    """
    import table_pipeline as pipeline

    grid = pipeline.extract_ruled_grid(image, prefer_adaptive=True)
    if grid is None:
        return None
    columns, rows, _ = grid
    row_count, column_count = len(rows) - 1, len(columns) - 1
    height, width = image.shape[:2]
    if (
        not 6 <= row_count <= 80
        or not 3 <= column_count <= 10
        or row_count * column_count > maximum_cells
        or columns[0] > width * 0.04
        or width - columns[-1] > width * 0.04
        or rows[0] > height * 0.05
        or height - rows[-1] > height * 0.05
        or min(np.diff(columns)) <= 0
        or min(np.diff(rows)) <= 0
    ):
        return None
    # The outer photographed rule is frequently clipped, faded, or merged into
    # the crop edge.  Once every interior interval is independently present and
    # the first/last detected rule is already within the strict edge tolerance,
    # use the isolated table crop itself as the outer boundary.  Keeping the
    # inset rule would silently cut the leading characters of the first column.
    columns = list(columns)
    rows = list(rows)
    if len(rows) >= 3:
        horizontal, _, _ = pipeline._grid_maps(image)

        def rule_support(value: int) -> float:
            band = horizontal[
                max(0, int(value) - 4):min(horizontal.shape[0], int(value) + 5)
            ]
            return (
                float(np.mean(np.any(band > 0, axis=0)))
                if band.size else 0.0
            )

        if rule_support(rows[1]) < 0.35 and rule_support(rows[2]) >= 0.65:
            # Expanding a crop can leave its former top edge as a weak
            # text-stroke pseudo-rule.  It sits inside the first visible row
            # and must not become an extra exported row.
            rows.pop(1)
    columns[0], columns[-1] = 0, width
    rows[0], rows[-1] = 0, height
    return rows, columns


def _complete_rules_supersede_text_geometry(
    image: np.ndarray,
    text_geometry: tuple[list[int], list[int]],
    ruled_geometry: tuple[list[int], list[int]] | None,
) -> bool:
    """Prefer a complete physical lattice when header text undercounts columns."""
    if ruled_geometry is None:
        return False
    text_rows, text_columns = text_geometry
    ruled_rows, ruled_columns = ruled_geometry
    if (
        len(ruled_rows) != len(text_rows)
        or len(ruled_columns) < len(text_columns) + 2
        or not 3 <= len(ruled_columns) - 1 <= 10
    ):
        return False

    row_pitch = float(np.median(np.diff(ruled_rows)))
    if row_pitch <= 0 or max(
        abs(int(first) - int(second))
        for first, second in zip(text_rows, ruled_rows)
    ) > max(6.0, row_pitch * 0.18):
        return False

    import table_pipeline as pipeline

    _, vertical, _ = pipeline._grid_maps(image)
    top = max(0, min(vertical.shape[0] - 1, ruled_rows[0]))
    bottom = max(top + 1, min(vertical.shape[0], ruled_rows[-1] + 1))
    supports = []
    for boundary in ruled_columns[1:-1]:
        band = vertical[
            top:bottom,
            max(0, boundary - 3):min(vertical.shape[1], boundary + 4),
        ]
        supports.append(
            float(np.mean(np.any(band > 0, axis=1))) if band.size else 0.0
        )
    if not supports or min(supports) < 0.70:
        return False

    widths = np.diff(ruled_columns).astype(float)
    for boundary in text_columns[1:-1]:
        nearest = min(range(1, len(ruled_columns) - 1),
                      key=lambda index: abs(ruled_columns[index] - boundary))
        tolerance = min(widths[nearest - 1], widths[nearest]) * 0.25
        if abs(ruled_columns[nearest] - boundary) > max(8.0, tolerance):
            return False
    return True


def _merged_title_geometry(image: np.ndarray) -> tuple[list[int], list[int]] | None:
    ink = _ink_mask(image)
    profile = np.count_nonzero(ink > 0, axis=1)
    profile = np.maximum(0, profile - np.percentile(profile, 5))
    bands = [
        (top, bottom)
        for top, bottom in _bands(np.flatnonzero(profile > max(16, image.shape[1] * 0.014)))
        if bottom - top >= 10
    ]
    if len(bands) < 6:
        return None
    split = (bands[0][1] + bands[1][0]) // 2
    geometry = _physical_geometry(image[split:])
    if geometry is None:
        return None
    lower_rows, columns = geometry
    if not 4 <= len(columns) - 1 <= 10:
        return None
    return [0] + [split + boundary for boundary in lower_rows], columns


def _ruled_row_columns(
    image: np.ndarray, rows: list[int], column_count: int,
    ink: np.ndarray | None = None,
) -> tuple[list[list[int]] | None, bool]:
    if not 3 <= column_count <= 10 or len(rows) < 7:
        return None, False
    if ink is None:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        ink = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 31, 11,
        )
    width = image.shape[1]
    selected: list[list[int] | None] = []
    ruled_rows = 0
    for top, bottom in zip(rows, rows[1:]):
        margin = min(5, max(3, (bottom - top) // 8))
        band = ink[top + margin:bottom - margin]
        if band.size == 0:
            selected.append(None)
            continue
        profile = np.count_nonzero(band, axis=0) / band.shape[0]
        peaks = [
            (left + right) // 2
            for left, right in _bands(np.flatnonzero(profile > 0.70))
            if right - left < 25
        ]
        candidate_peaks = [peaks]
        possible_starts = [peaks]
        if peaks and peaks[0] <= max(10, width * 0.04):
            possible_starts.append(peaks[1:])
        if (
            len(peaks) >= 5
            and peaks[-1] >= width * 0.985
            and peaks[-1] - peaks[-2] > float(np.median(np.diff(peaks[1:-1]))) * 1.12
        ):
            possible_starts.append(peaks[:-1])
        for base in possible_starts:
            if len(base) != column_count:
                continue
            gaps = np.diff(base).astype(float)
            pitch = float(np.median(gaps)) if gaps.size else 0.0
            if (
                pitch >= 30 and float(gaps.min()) >= pitch * 0.75
                and float(gaps.max()) <= pitch * 1.25
                and width - pitch * 0.30 <= base[-1] + pitch <= width + pitch * 0.30
            ):
                candidate_peaks.append(
                    base + [min(width - 1, int(round(base[-1] + pitch)))]
                )
            if (
                pitch >= 30 and float(gaps.min()) >= pitch * 0.75
                and float(gaps.max()) <= pitch * 1.25
                and -pitch * 0.08 <= base[0] - pitch
                <= max(width * 0.08, pitch * 0.20)
            ):
                candidate_peaks.append(
                    [max(0, int(round(base[0] - pitch)))] + base
                )
        ruled_rows += len(peaks) >= column_count
        best = None
        for row_peaks in candidate_peaks:
            if not column_count + 1 <= len(row_peaks) <= 15:
                continue
            sequences = np.asarray(
                list(combinations(row_peaks, column_count + 1)),
                dtype=np.float64,
            )
            extents = sequences[:, -1] - sequences[:, 0]
            wide = extents >= width * 0.60
            if not np.any(wide):
                continue
            sequences = sequences[wide]
            extents = extents[wide]
            gaps = np.diff(sequences, axis=1)
            pitches = np.median(gaps, axis=1)
            valid = (
                (pitches >= 30)
                & (np.min(gaps, axis=1) >= pitches * 0.75)
                & (np.max(gaps, axis=1) <= pitches * 1.25)
            )
            if not np.any(valid):
                continue
            sequences = sequences[valid]
            gaps = gaps[valid]
            pitches = pitches[valid]
            extents = extents[valid]
            scores = np.std(gaps, axis=1) / pitches - extents / width * 0.02
            position = int(np.argmin(scores))
            score = float(scores[position])
            if best is None or score < best[0]:
                best = (score, [int(value) for value in sequences[position]])
        selected.append(None if best is None else best[1])
    suspected = ruled_rows >= max(3, (len(rows) - 1) * 0.40)
    if not suspected:
        return None, False
    if any(lines is None for lines in selected[1:]):
        known = [index for index, lines in enumerate(selected) if lines is not None]
        if len(known) < max(3, int(np.ceil(len(selected) * 0.80))):
            return None, True
        for index, lines in enumerate(selected):
            if lines is not None:
                continue
            before = [row for row in known if row < index]
            after = [row for row in known if row > index]
            first, second = ((before[-1], after[0]) if before and after
                             else tuple(known[:2]) if not before else tuple(known[-2:]))
            fraction = (index - first) / float(second - first)
            selected[index] = [
                max(0, min(width - 1, int(round(left + fraction * (right - left)))))
                for left, right in zip(selected[first], selected[second])
            ]
    if selected[0] is None:
        first, second = selected[1], selected[2]
        selected[0] = [
            max(0, min(width - 1, 2 * left - right))
            for left, right in zip(first, second)
        ]
        merged_title = True
    else:
        merged_title = False
    row_columns = [lines for lines in selected if lines is not None]
    pitch = float(np.median(np.diff(row_columns[1], axis=0)))
    if any(
        max(abs(left - right) for left, right in zip(first[:-1], second[:-1]))
        > pitch * 0.12
        or abs(first[-1] - second[-1]) > pitch * 0.20
        for first, second in zip(row_columns, row_columns[1:])
    ):
        return None, True
    return row_columns, merged_title


def _slanted_left_margin_row_columns(
    image: np.ndarray,
    rows: list[int],
    columns: list[int],
) -> list[list[int]] | None:
    """Compensate a residual slanted paper edge without rewarping the table."""
    height, width = image.shape[:2]
    if (
        len(rows) < 12
        or not 4 <= len(columns) - 1 <= 10
        or columns[0] > max(3, int(width * 0.005))
    ):
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    search_width = min(int(width * 0.12), max(24, int(columns[1] * 0.60)))
    centers: list[float] = []
    edges: list[float] = []
    dark_rows = 0
    for top, bottom in zip(rows, rows[1:]):
        margin = min(4, max(2, (bottom - top) // 8))
        strip = gray[top + margin:bottom - margin]
        if strip.size == 0:
            return None
        profile = np.percentile(strip, 75, axis=0)
        edge = next((
            x for x in range(max(0, search_width - 11))
            if float(np.mean(profile[x:x + 12] > 175)) >= 0.90
        ), None)
        if edge is None:
            return None
        if edge >= 5 and float(np.median(profile[:edge - 2])) < 180:
            dark_rows += 1
        centers.append((top + bottom) * 0.5)
        edges.append(float(edge))
    if dark_rows < max(4, int(len(edges) * 0.50)):
        return None
    slope, intercept = np.polyfit(np.asarray(centers), np.asarray(edges), 1)
    fitted = slope * np.asarray(centers) + intercept
    drift = float(fitted[-1] - fitted[0])
    residual = np.abs(np.asarray(edges) - fitted)
    if (
        not max(12.0, width * 0.006) <= abs(drift) <= min(columns[1] * 0.45, width * 0.08)
        or float(np.median(residual)) > 1.5
        or float(np.max(residual)) > 4.0
    ):
        return None
    reference = float(fitted[0])
    result: list[list[int]] = []
    for predicted in fitted:
        shift = float(predicted - reference)
        local = [
            int(round(boundary + shift * (1.0 - boundary / max(1, width))))
            for boundary in columns
        ]
        local[0] = max(0, local[0])
        local[-1] = width
        if min(np.diff(local)) <= 0:
            return None
        result.append(local)
    return result


def _unique_ruled_row_columns(
    image: np.ndarray, rows: list[int],
) -> tuple[list[list[int]], bool] | None:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    ink = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 31, 11,
    )
    candidates = [
        result
        for count in range(3, 11)
        if (result := _ruled_row_columns(image, rows, count, ink))[0] is not None
    ]
    return max(candidates, key=lambda result: len(result[0][0])) if candidates else None


def _line_header_mask(source: np.ndarray) -> np.ndarray | None:
    height, width = source.shape[:2]
    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    lines = cv2.HoughLinesP(
        cv2.Canny(gray, 40, 100), 1, np.pi / 720,
        threshold=max(45, width // 25),
        minLineLength=max(120, int(width * 0.30)),
        maxLineGap=max(20, int(width * 0.12)),
    )
    if lines is None:
        return None
    horizontal = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        distance = abs(int(x2) - int(x1))
        if distance < width * 0.55:
            continue
        slope = (float(y2) - float(y1)) / (int(x2) - int(x1))
        if abs(slope) > 0.16:
            continue
        middle = (float(y1) + float(y2)) * 0.5
        horizontal.append((middle, distance, slope, min(x1, x2), max(x1, x2)))
    horizontal.sort(key=lambda line: line[0])
    distinct = []
    for line in horizontal:
        if distinct and line[0] - distinct[-1][0] <= 10:
            if line[1] > distinct[-1][1]:
                distinct[-1] = line
        else:
            distinct.append(line)
    candidates = []
    for first in distinct:
        if not height * 0.10 <= first[0] <= height * 0.48:
            continue
        for second in distinct:
            gap = second[0] - first[0]
            if not height * 0.025 <= gap <= height * 0.09:
                continue
            if abs(first[2] - second[2]) > 0.035:
                continue
            left, right = max(first[3], second[3]), min(first[4], second[4])
            if right - left < width * 0.50:
                continue
            later = [line for line in distinct if line[0] > second[0] + gap * 1.8]
            if not later:
                continue
            score = first[1] + second[1] - abs(gap - height * 0.045)
            candidates.append((score, first, second, left, right))
    if not candidates:
        return None
    _, first, second, left, right = max(candidates, key=lambda item: item[0])
    if right - left < width * 0.55:
        return None
    mask = np.zeros((height, width), dtype=np.uint8)
    center_x = (left + right) * 0.5
    top_intercept = first[0] - first[2] * center_x
    bottom_intercept = second[0] - second[2] * center_x
    points = np.int32([
        [left, first[2] * left + top_intercept],
        [right, first[2] * right + top_intercept],
        [right, second[2] * right + bottom_intercept],
        [left, second[2] * left + bottom_intercept],
    ])
    cv2.fillConvexPoly(mask, points, 255)
    return mask


def _warp_candidate(source: np.ndarray, *, allow_line_header: bool = True) -> tuple[np.ndarray, np.ndarray] | None:
    height, width = source.shape[:2]
    maximum_header_height = height * (0.30 if width >= height * 2 else 0.16)
    blue, green, red = cv2.split(source.astype(np.int16))
    palest = np.minimum(np.minimum(blue, green), red)
    brightest = np.maximum(np.maximum(blue, green), red)
    tinted = (brightest - palest >= 9) & (blue + green + red >= 450) & (palest >= 130)
    blue_header = (blue - red >= 6) & (green - red >= 2) & (blue >= 130)
    mask = np.uint8(tinted | blue_header) * 255
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
    )
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(35, width // 50), 7)),
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bands = []
    for contour in contours:
        x, y, band_width, band_height = cv2.boundingRect(contour)
        if (
            band_width >= width * 0.40
            and 20 <= band_height <= maximum_header_height
            and band_width / max(1, band_height) >= 5
        ):
            bands.append((band_width * band_height, x, y, band_width, band_height))
    if not bands:
        if not allow_line_header:
            return None
        line_mask = _line_header_mask(source)
        if line_mask is None:
            return None
        mask = line_mask
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, band_width, band_height = cv2.boundingRect(contour)
            if (
                band_width >= width * 0.40
                and 20 <= band_height <= maximum_header_height
                and band_width / max(1, band_height) >= 5
            ):
                bands.append((band_width * band_height, x, y, band_width, band_height))
        if not bands:
            return None
    bands.sort(reverse=True)
    if len(bands) > 1 and bands[1][0] >= bands[0][0] * 0.60:
        return None
    _, left, top, table_width, band_height = bands[0]
    right = left + table_width
    center_x = (left + right) * 0.5
    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 100)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 720,
        threshold=max(45, table_width // 25),
        minLineLength=max(120, int(table_width * 0.30)),
        maxLineGap=max(20, int(table_width * 0.12)),
    )
    if lines is None:
        return None
    top_lines = []
    bottom_lines = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        if (
            min(x1, x2) > left + table_width * 0.30
            or max(x1, x2) < right - table_width * 0.30
            or abs(int(x2) - int(x1)) < 100
        ):
            continue
        slope = (float(y2) - float(y1)) / (int(x2) - int(x1))
        if abs(slope) > 0.16:
            continue
        intercept = float(y1) - slope * float(x1)
        middle_y = slope * center_x + intercept
        if top - 25 <= middle_y <= top + band_height + 25:
            top_lines.append((middle_y, slope, intercept, min(x1, x2), max(x1, x2)))
        elif middle_y > top + band_height + height * 0.12:
            bottom_lines.append((middle_y, slope, intercept, min(x1, x2), max(x1, x2)))
    sample_gap = max(8, int(height * 0.006))
    sample_left = max(0, int(center_x - table_width * 0.25))
    sample_right = min(width, int(center_x + table_width * 0.25))

    def inside_paper(line: tuple[float, float, float, int, int]) -> bool:
        middle_y = int(round(line[0]))
        if not sample_gap + 3 <= middle_y < height - sample_gap - 3:
            return False
        above = gray[middle_y - sample_gap - 3:middle_y - sample_gap + 3,
                     sample_left:sample_right]
        below = gray[middle_y + sample_gap - 3:middle_y + sample_gap + 3,
                     sample_left:sample_right]
        if not above.size or not below.size:
            return True
        # 2026-10-22 性能：这一段是整次请求里最热的循环 —— 候选横线有上千条，
        # 每条都要在 6x1250 的灰度带和同样大小的彩色带上取中位数。原实现每次
        # 固定算 4 个中位数（两个灰度、两个彩色），实测单请求 22 552 次
        # np.median、12.9 秒耗在 partition 上。
        # 1) 8 位图只有 256 个取值，改用直方图中位数，结果与 np.median 完全一致；
        # 2) 彩色中位数只在灰度条件都没判死时才用到，改成惰性求值。
        above_level, below_level = median_u8(above), median_u8(below)
        outside_header = (
            line[3] < left - table_width * 0.02
            or line[4] > right + table_width * 0.02
        )
        if below_level < min(140.0, above_level - 65.0):
            return False
        if outside_header and below_level < above_level - 30.0:
            return False
        above_color = median_u8_per_channel(source[
            middle_y - sample_gap - 3:middle_y - sample_gap + 3,
            sample_left:sample_right,
        ])
        below_color = median_u8_per_channel(source[
            middle_y + sample_gap - 3:middle_y + sample_gap + 3,
            sample_left:sample_right,
        ])
        chroma_edge = (
            float(np.ptp(below_color)) >= 25
            and float(np.ptp(above_color)) < float(np.ptp(below_color)) * 0.60
        )
        return not chroma_edge

    bottom_lines = [line for line in bottom_lines if inside_paper(line)]
    if not bottom_lines:
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 31, 11,
        )
        horizontal = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(100, width // 24), 1)),
        )
        support = np.count_nonzero(horizontal[:, left:right], axis=1) / max(1, table_width)
        row_lines = [
            (first + last) * 0.5
            for first, last in _bands(np.flatnonzero(support > 0.15))
            if first > top + band_height + height * 0.12
        ]
        for candidate_y in reversed(row_lines):
            if not inside_paper((candidate_y, 0.0, candidate_y, left, right)):
                continue
            strong_outer_rule = float(np.max(support[
                max(0, int(candidate_y) - 2):min(height, int(candidate_y) + 3)
            ])) >= 0.70
            if not strong_outer_rule and not any(
                band_height * 0.45 <= candidate_y - earlier <= band_height * 1.60
                for earlier in row_lines if earlier < candidate_y
            ):
                continue
            segments = []
            for x1, y1, x2, y2 in lines.reshape(-1, 4):
                delta_x = int(x2) - int(x1)
                if abs(delta_x) < table_width * 0.15:
                    continue
                slope = (float(y2) - float(y1)) / delta_x
                if abs(slope) > 0.16:
                    continue
                intercept = float(y1) - slope * float(x1)
                if abs(slope * center_x + intercept - candidate_y) <= max(8, band_height * 0.12):
                    segments.append((abs(delta_x), slope, intercept))
            if segments:
                _, slope, intercept = max(segments)
            else:
                slope, intercept = 0.0, float(candidate_y)
            bottom_lines.append((candidate_y, slope, intercept, left, right))
            break

    bottom_lines = [line for line in bottom_lines if inside_paper(line)]
    if not bottom_lines:
        return None
    # Controls or status bars below a photographed on-screen table can look
    # like another table row.  Prefer a lower rule only when it still reaches
    # both sides of the coloured header/table band.  This keeps the crop on the
    # actual grid instead of stretching it down to an unrelated UI separator.
    side_tolerance = table_width * 0.06
    complete_bottom_lines = [
        line for line in bottom_lines
        if line[3] <= left + side_tolerance
        and line[4] >= right - side_tolerance
    ]
    if complete_bottom_lines:
        bottom_lines = complete_bottom_lines
    if top_lines:
        _, top_slope, top_intercept, top_line_left, top_line_right = min(top_lines)
    else:
        top_slope, top_intercept = 0.0, float(top + band_height)
        top_line_left, top_line_right = left, right
    _, bottom_slope, bottom_intercept, bottom_line_left, bottom_line_right = max(bottom_lines)

    upper_points = []
    for x in np.linspace(left + table_width * 0.08, right - table_width * 0.08, 25).astype(int):
        x0, x1 = max(0, x - 3), min(width, x + 4)
        counts = np.count_nonzero(mask[top:top + band_height, x0:x1], axis=1)
        valid = np.flatnonzero(counts >= max(2, (x1 - x0) // 2))
        if valid.size >= band_height * 0.20:
            upper_points.append((x, top + int(valid[0])))
    mask_top_accepted = False
    if len(upper_points) >= 12:
        points = np.asarray(upper_points, dtype=float)
        mask_slope, mask_intercept = np.polyfit(points[:, 0], points[:, 1], 1)
        residual = np.abs(points[:, 1] - (mask_slope * points[:, 0] + mask_intercept))
        if (
            float(np.median(residual)) <= max(6, band_height * 0.08)
            and (
                not top_lines
                or mask_slope * center_x + mask_intercept
                < top_slope * center_x + top_intercept - max(8, band_height * 0.25)
            )
        ):
            top_slope, top_intercept = float(mask_slope), float(mask_intercept) - 2.0
            mask_top_accepted = True
    if not top_lines and not mask_top_accepted:
        return None

    def header_text_outside(first: int, last: int) -> bool:
        region = gray[
            max(0, top - 15):min(height, top + band_height + 15),
            max(0, first):min(width, last),
        ]
        if region.size == 0:
            return False
        _, _, components, _ = cv2.connectedComponentsWithStats(
            np.uint8(region < 140), 8
        )
        glyphs = [
            (int(x), int(x + glyph_width))
            for x, _, glyph_width, glyph_height, area in components[1:]
            if area >= 8 and 8 <= glyph_height <= band_height * 0.80
            and 2 <= glyph_width <= 100
        ]
        return len(glyphs) >= 2 and max(x for _, x in glyphs) - min(x for x, _ in glyphs) >= 10

    # 反光会把表头底色和长横线分段；同一物理边线的共线段仍属于表格。
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        if abs(int(x2) - int(x1)) < table_width * 0.20:
            continue
        for slope, intercept, side in (
            (top_slope, top_intercept, "top"),
            (bottom_slope, bottom_intercept, "bottom"),
        ):
            if max(abs(y1 - slope * x1 - intercept), abs(y2 - slope * x2 - intercept)) > max(6, band_height * 0.10):
                continue
            xs = np.arange(min(x1, x2), max(x1, x2) + 1)
            ys = np.rint(slope * xs + intercept).astype(int)
            support = np.any(edges[np.clip(ys[:, None] + np.arange(-3, 4), 0, height - 1), xs[:, None]] > 0, axis=1)
            supported = [(first, last) for first, last in _bands(np.flatnonzero(support))
                         if last - first >= max(20, table_width * 0.04)]
            if not supported:
                continue
            segment_left, segment_right = xs[supported[0][0]], xs[supported[-1][1]]
            if side == "top":
                top_line_left, top_line_right = min(top_line_left, segment_left), max(top_line_right, segment_right)
            else:
                bottom_line_left, bottom_line_right = min(bottom_line_left, segment_left), max(bottom_line_right, segment_right)
    candidate_right = min(width - 1, max(right, top_line_right, bottom_line_right))
    if candidate_right - right >= max(35, table_width * 0.07):
        if header_text_outside(right + 5, candidate_right):
            if candidate_right - right > table_width * 0.70:
                return None
            right = candidate_right
    candidate_left = max(0, min(left, top_line_left, bottom_line_left))
    if left - candidate_left >= max(35, table_width * 0.07):
        if left - candidate_left > table_width * 0.70:
            return None
        if header_text_outside(candidate_left, left - 5):
            left = candidate_left
        elif (
            top_line_left < left - table_width * 0.07
            and bottom_line_left < left - table_width * 0.07
        ):
            return None

    warp_left = max(0, left - max(10, int(width * 0.025)))
    warp_right = min(width - 1, right + max(8, int(width * 0.008)))
    # 表头宽度不能代表倾斜表格底部宽度；用贯穿表身的侧线保留外沿。
    side_lines = []
    table_height = bottom_slope * center_x + bottom_intercept - top
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        delta_y = int(y2) - int(y1)
        if abs(delta_y) < table_height * 0.50:
            continue
        slope = (float(x2) - float(x1)) / delta_y
        if abs(slope) > 0.20:
            continue
        intercept = float(x1) - slope * float(y1)
        upper_x = (slope * top_intercept + intercept) / (1 - slope * top_slope)
        lower_x = (slope * bottom_intercept + intercept) / (1 - slope * bottom_slope)
        side_lines.append((upper_x, lower_x))
    for edge, is_left in ((left, True), (right, False)):
        nearby = [line for line in side_lines if abs(line[0] - edge) <= table_width * 0.025]
        if nearby:
            upper_x, lower_x = min(nearby, key=lambda line: abs(line[0] - edge))
            if is_left:
                warp_left = max(0, min(warp_left, int(min(upper_x, lower_x)) - 8))
            else:
                warp_right = min(width - 1, max(warp_right, int(max(upper_x, lower_x)) + 8))
    corners = np.float32([
        [warp_left, top_slope * warp_left + top_intercept],
        [warp_right, top_slope * warp_right + top_intercept],
        [warp_right, bottom_slope * warp_right + bottom_intercept],
        [warp_left, bottom_slope * warp_left + bottom_intercept],
    ])
    if min(corners[2, 1] - corners[1, 1], corners[3, 1] - corners[0, 1]) < height * 0.18:
        return None
    output_width = int(warp_right - warp_left)
    output_height = int(round(((corners[3, 1] - corners[0, 1]) + (corners[2, 1] - corners[1, 1])) * 0.5))
    destination = np.float32([
        [0, 0], [output_width - 1, 0],
        [output_width - 1, output_height - 1], [0, output_height - 1],
    ])
    warped = cv2.warpPerspective(
        source, cv2.getPerspectiveTransform(corners, destination),
        (output_width, output_height), flags=cv2.INTER_CUBIC,
        borderValue=(255, 255, 255),
    )
    return warped, corners


def _last_row_has_text(image: np.ndarray, rows: list[int], columns: list[int]) -> bool:
    ink = _ink_mask(image)
    top, bottom = rows[-2:]
    _, _, stats, _ = cv2.connectedComponentsWithStats(ink[top:bottom], 8)
    supported = {
        next((column for column in range(len(columns) - 1)
              if columns[column] <= x < columns[column + 1]), -1)
        for x, _, width, glyph_height, area in stats[1:]
        if area >= 10
        and 8 <= glyph_height <= max(50, (bottom - top) * 0.65)
        and 2 <= width < image.shape[1] * 0.10
    }
    supported.discard(-1)
    if len(supported) >= 2:
        return True
    if supported != {0}:
        return False

    # A legitimate final section row may contain one label in a cell whose
    # missing internal vertical rules visibly merge the entire row.  Requiring
    # two populated columns made the crop-expansion loop stop immediately and
    # silently omitted this row together with every row below it.
    spans = physical_spans(image, rows, columns)
    last_row = len(rows) - 2
    column_count = len(columns) - 1
    return bool(
        spans is not None
        and any(
            int(span.get("row", -1)) == last_row
            and int(span.get("column", -1)) == 0
            and int(span.get("row_span", 0)) == 1
            and int(span.get("column_span", 0)) == column_count
            for span in spans
        )
    )


def _outside_text_rows(
    source: np.ndarray,
    image: np.ndarray,
    corners: np.ndarray,
    rows: list[int],
    *,
    side: str,
) -> int:
    height, width = image.shape[:2]
    destination = np.float32([
        [0, 0], [width - 1, 0],
        [width - 1, height - 1], [0, height - 1],
    ])
    inverse = cv2.getPerspectiveTransform(destination, corners)
    supported = 0
    for row in range(1, len(rows) - 1):
        center_y = (rows[row] + rows[row + 1]) * 0.5
        edge_x = width - 1 if side == "right" else 0
        point = cv2.perspectiveTransform(
            np.float32([[[edge_x, center_y]]]), inverse
        )[0, 0]
        span = max(120, width // 3)
        if side == "right":
            x0 = max(0, int(point[0]) + 12)
            x1 = min(source.shape[1], x0 + span)
        else:
            x1 = max(0, min(source.shape[1], int(point[0]) - 12))
            x0 = max(0, x1 - span)
        y0 = max(0, int(point[1]) - 25)
        y1 = min(source.shape[0], int(point[1]) + 25)
        patch = source[y0:y1, x0:x1]
        if patch.size == 0:
            continue
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        cutoff = min(140, int(np.median(gray)) - 25)
        _, _, components, _ = cv2.connectedComponentsWithStats(
            np.uint8(gray < cutoff), 8
        )
        glyphs = sum(
            area >= 8 and 8 <= glyph_height <= 40 and 2 <= glyph_width <= 100
            for _, _, glyph_width, glyph_height, area in components[1:]
        )
        supported += glyphs >= 2
    return supported


def _expand_right_column(
    source: np.ndarray,
    image: np.ndarray,
    corners: np.ndarray,
    rows: list[int],
    columns: list[int],
) -> tuple[np.ndarray, np.ndarray, list[int], list[int]] | None:
    pitch = float(np.median(np.diff(columns)))
    source_scale = float(corners[1, 0] - corners[0, 0]) / image.shape[1]
    top_slope = float(corners[1, 1] - corners[0, 1]) / max(1.0, float(corners[1, 0] - corners[0, 0]))
    bottom_slope = float(corners[2, 1] - corners[3, 1]) / max(1.0, float(corners[2, 0] - corners[3, 0]))
    candidates = []
    for factor in (0.8, 1.0, 1.2, 1.5):
        shift = pitch * source_scale * factor
        if float(corners[1, 0]) + shift >= source.shape[1] - 3:
            return None
        expanded = corners.copy()
        expanded[1, 0] += shift
        expanded[2, 0] += shift
        expanded[1, 1] += top_slope * shift
        expanded[2, 1] += bottom_slope * shift
        target_width = int(round(image.shape[1] + pitch * factor))
        destination = np.float32([
            [0, 0], [target_width - 1, 0],
            [target_width - 1, image.shape[0] - 1], [0, image.shape[0] - 1],
        ])
        warped = cv2.warpPerspective(
            source, cv2.getPerspectiveTransform(expanded, destination),
            (target_width, image.shape[0]), flags=cv2.INTER_CUBIC,
            borderValue=(255, 255, 255),
        )
        geometry = _physical_geometry(warped)
        if geometry is None:
            return None
        candidate_rows, candidate_columns = geometry
        if len(candidate_rows) != len(rows) or len(candidate_columns) != len(columns) + 1:
            return None
        candidates.append((warped, expanded, candidate_rows, candidate_columns))
    image, corners, rows, columns = candidates[-1]
    locations, _ = cell_crops(ColoredGrid(image, rows, columns, []))
    last_column = len(columns) - 2
    body_support = sum(row > 0 and column == last_column for row, column in locations)
    if body_support < max(3, int((len(rows) - 2) * 0.25)):
        return None
    return image, corners, rows, columns


def _partial_right_text_rows(
    source: np.ndarray,
    image: np.ndarray,
    corners: np.ndarray,
    rows: list[int],
    width_to_check: float,
) -> int:
    height, width = image.shape[:2]
    destination = np.float32([
        [0, 0], [width - 1, 0],
        [width - 1, height - 1], [0, height - 1],
    ])
    inverse = cv2.getPerspectiveTransform(destination, corners)
    supported = 0
    for row in range(1, len(rows) - 1):
        center_y = (rows[row] + rows[row + 1]) * 0.5
        point = cv2.perspectiveTransform(
            np.float32([[[width - 1, center_y]]]), inverse
        )[0, 0]
        x0 = max(0, int(point[0]) - 25)
        x1 = min(source.shape[1], x0 + max(80, int(width_to_check) + 55))
        y0 = max(0, int(point[1]) - 25)
        y1 = min(source.shape[0], int(point[1]) + 25)
        patch = source[y0:y1, x0:x1]
        if patch.size == 0:
            continue
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        cutoff = min(140, int(np.median(gray)) - 25)
        _, _, components, _ = cv2.connectedComponentsWithStats(
            np.uint8(gray < cutoff), 8
        )
        supported += any(
            area >= 8 and 8 <= glyph_height <= 40 and 2 <= glyph_width <= 100
            for _, _, glyph_width, glyph_height, area in components[1:]
        )
    return supported


def _expand_partial_right(
    source: np.ndarray,
    image: np.ndarray,
    corners: np.ndarray,
    rows: list[int],
    columns: list[int],
    deficit: float,
) -> tuple[np.ndarray, np.ndarray, list[int], list[int]] | None:
    source_scale = float(corners[1, 0] - corners[0, 0]) / image.shape[1]
    top_slope = float(corners[1, 1] - corners[0, 1]) / max(1.0, float(corners[1, 0] - corners[0, 0]))
    bottom_slope = float(corners[2, 1] - corners[3, 1]) / max(1.0, float(corners[2, 0] - corners[3, 0]))
    candidates = []
    for factor in (0.8, 1.0):
        shift = deficit * source_scale * factor
        if float(corners[1, 0]) + shift >= source.shape[1] - 3:
            return None
        expanded = corners.copy()
        expanded[1, 0] += shift
        expanded[2, 0] += shift
        expanded[1, 1] += top_slope * shift
        expanded[2, 1] += bottom_slope * shift
        target_width = int(round(image.shape[1] + deficit * factor))
        destination = np.float32([
            [0, 0], [target_width - 1, 0],
            [target_width - 1, image.shape[0] - 1], [0, image.shape[0] - 1],
        ])
        warped = cv2.warpPerspective(
            source, cv2.getPerspectiveTransform(expanded, destination),
            (target_width, image.shape[0]), flags=cv2.INTER_CUBIC,
            borderValue=(255, 255, 255),
        )
        geometry = _physical_geometry(warped)
        if geometry is None:
            return None
        candidate_rows, candidate_columns = geometry
        if len(candidate_rows) != len(rows) or len(candidate_columns) != len(columns):
            return None
        candidates.append((warped, expanded, candidate_rows, candidate_columns))
    return candidates[-1]


def _recover_missing_top_ruled_rows(
    source: np.ndarray,
    image: np.ndarray,
    corners: np.ndarray,
    rows: list[int],
    columns: list[int],
    *,
    minimum_top_fraction: float = 0.25,
    maximum_cells: int = 320,
) -> tuple[bool, tuple[np.ndarray, np.ndarray, tuple[list[int], list[int]]] | None]:
    """用源图中连续的表格横线证实并恢复被裁掉的顶部行。"""
    source_height, _ = source.shape[:2]
    if (
        len(rows) < 9
        or float(np.mean(corners[:2, 1])) < source_height * minimum_top_fraction
    ):
        return False, None
    warp_height, warp_width = image.shape[:2]
    source_span = float(np.mean(corners[2:, 1] - corners[:2, 1]))
    if source_span <= 0:
        return False, None
    warp_scale = warp_height / source_span
    source_pitch = float(np.median(np.diff(rows))) / warp_scale
    if source_pitch < 12:
        return False, None
    table_left = float(min(corners[:, 0]))
    table_right = float(max(corners[:, 0]))
    table_width = table_right - table_left
    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    lines = cv2.HoughLinesP(
        cv2.Canny(gray, 40, 100), 1, np.pi / 720,
        threshold=max(45, int(table_width / 35)),
        minLineLength=max(120, int(table_width * 0.40)),
        maxLineGap=max(20, int(table_width * 0.08)),
    )
    if lines is None:
        return False, None
    center_x = (table_left + table_right) * 0.5
    top_center = float(np.mean(corners[:2, 1]))
    candidates = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        distance = abs(int(x2) - int(x1))
        if distance < table_width * 0.40:
            continue
        slope = (float(y2) - float(y1)) / (int(x2) - int(x1))
        if abs(slope) > 0.16:
            continue
        if min(x1, x2) > table_left + table_width * 0.40:
            continue
        if max(x1, x2) < table_right - table_width * 0.40:
            continue
        intercept = float(y1) - slope * float(x1)
        middle_y = slope * center_x + intercept
        if top_center - source_pitch * 20 <= middle_y < top_center - source_pitch * 0.50:
            candidates.append((middle_y, slope, intercept, distance))
    chain = []
    cursor = top_center
    tolerance = max(8.0, source_pitch * 0.28)
    for _ in range(20):
        nearby = [
            line for line in candidates
            if abs(line[0] - (cursor - source_pitch)) <= tolerance
        ]
        if not nearby:
            break
        selected = min(
            nearby,
            key=lambda line: (abs(line[0] - (cursor - source_pitch)), -line[3]),
        )
        chain.append(selected)
        cursor = selected[0]
    endpoints: list[tuple[int, tuple[float, float, float, int]]] = []
    if len(chain) >= 2:
        endpoints.append((len(chain), chain[-1]))
    else:
        for line in candidates:
            missing_rows = int(round((top_center - line[0]) / source_pitch))
            if (
                1 <= missing_rows <= 20
                and abs(line[0] - (top_center - source_pitch * missing_rows))
                <= tolerance
                and (
                    missing_rows >= 2
                    or line[3] >= table_width * 0.55
                )
            ):
                endpoints.append((missing_rows, line))
        endpoints.sort(key=lambda value: value[0])
    if not endpoints:
        return False, None
    for missing_rows, (_, slope, intercept, _) in endpoints:
        expanded_corners = corners.copy()
        for index in (0, 1):
            expanded_corners[index, 1] = slope * expanded_corners[index, 0] + intercept
        expanded_height = warp_height + int(round(
            (top_center - (slope * center_x + intercept)) * warp_scale
        ))
        if not warp_height < expanded_height <= source_height * 1.30:
            continue
        destination = np.float32([
            [0, 0], [warp_width - 1, 0],
            [warp_width - 1, expanded_height - 1], [0, expanded_height - 1],
        ])
        expanded = cv2.warpPerspective(
            source, cv2.getPerspectiveTransform(expanded_corners, destination),
            (warp_width, expanded_height), flags=cv2.INTER_CUBIC,
            borderValue=(255, 255, 255),
        )
        geometry = _physical_geometry(expanded)
        if geometry is None:
            geometry = _complete_ruled_geometry(expanded, maximum_cells)
        partial_top_replacement = False
        if (
            geometry is not None
            and missing_rows == 1
            and len(geometry[0]) == len(rows)
        ):
            partial_top_replacement = True
        if geometry is not None and missing_rows == 1 and len(geometry[0]) >= 3:
            candidate_rows, candidate_columns = geometry
            import table_pipeline as pipeline

            horizontal, _, _ = pipeline._grid_maps(expanded)

            def horizontal_support(value: int) -> float:
                band = horizontal[
                    max(0, int(value) - 4):min(horizontal.shape[0], int(value) + 5)
                ]
                return (
                    float(np.mean(np.any(band > 0, axis=0)))
                    if band.size else 0.0
                )

            if (
                horizontal_support(candidate_rows[1]) < 0.35
                and horizontal_support(candidate_rows[2]) >= 0.65
            ):
                # The old crop edge can survive as a weak pseudo-boundary after
                # expansion.  Replace it with the newly recovered outer rule;
                # retaining both would split one visible header into two rows.
                candidate_rows = [candidate_rows[0], *candidate_rows[2:]]
                geometry = candidate_rows, candidate_columns
                partial_top_replacement = True
        if (
            geometry is not None
            and (
                len(geometry[0]) >= len(rows) + missing_rows
                or partial_top_replacement and len(geometry[0]) >= len(rows)
            )
            and abs(len(geometry[1]) - len(columns)) <= 1
        ):
            return True, (expanded, expanded_corners, geometry)

    return True, None


def _detect_colored_grid_from_photo(
    source: np.ndarray, *, maximum_cells: int = 320, allow_line_header: bool = True,
) -> ColoredGrid | None:
    if source is None or source.size == 0:
        return None
    initial = _warp_candidate(source, allow_line_header=allow_line_header)
    if initial is None:
        return None
    image, corners = initial
    geometry = _physical_geometry(image)
    merged_title = False
    complete_ruled_geometry = False
    ruled_geometry = None
    if geometry is not None:
        ruled_rows = _unique_ruled_row_columns(image, geometry[0])
        if (
            ruled_rows is not None
            and len(ruled_rows[0][0]) >= len(geometry[1]) + 2
        ):
            ruled_geometry = _complete_ruled_geometry(image, maximum_cells)
    if (
        geometry is not None
        and _complete_rules_supersede_text_geometry(
            image, geometry, ruled_geometry,
        )
    ):
        geometry = ruled_geometry
        complete_ruled_geometry = True
    if geometry is None:
        geometry = _merged_title_geometry(image)
        if geometry is None:
            ruled_geometry = _complete_ruled_geometry(image, maximum_cells)
            geometry = ruled_geometry
            if geometry is None:
                return None
            complete_ruled_geometry = True
        else:
            physical = _unique_ruled_row_columns(image, geometry[0])
            merged_title = physical is None or physical[1]
    else:
        title_geometry = _merged_title_geometry(image)
        if title_geometry is not None and len(title_geometry[0]) == len(geometry[0]):
            physical = _unique_ruled_row_columns(image, title_geometry[0])
            if physical is not None and physical[1]:
                geometry = title_geometry
                merged_title = True
            elif len(title_geometry[1]) == len(geometry[1]):
                first_two, _ = cell_crops(ColoredGrid(image, *geometry, []))
                if (
                    sum(row == 0 for row, _ in first_two) <= 2
                    and sum(row == 1 for row, _ in first_two) >= len(geometry[1]) - 2
                ):
                    geometry = title_geometry
                    merged_title = True
    rows, columns = geometry
    if not merged_title:
        recovered_top = False
        for _ in range(4):
            missing_top_rows, restored = _recover_missing_top_ruled_rows(
                source, image, corners, rows, columns,
                minimum_top_fraction=(
                    0.08 if recovered_top or complete_ruled_geometry else 0.25
                ),
                maximum_cells=maximum_cells,
            )
            if not missing_top_rows:
                break
            if restored is None:
                if recovered_top:
                    break
                return None
            previous_top = float(np.mean(corners[:2, 1]))
            image, corners, geometry = restored
            rows, columns = geometry
            if float(np.mean(corners[:2, 1])) >= previous_top - 4.0:
                break
            recovered_top = True
    widths = np.diff(columns).astype(float)
    typical_width = float(np.median(widths))
    if (
        not merged_title and min(source.shape[:2]) >= 1000
        and typical_width > 0
        and widths[0] < typical_width * 0.80
        and widths[-1] < typical_width * 0.80
    ):
        import table_pipeline as pipeline

        refined = pipeline._refine_vertical_table_sides(source, corners)
        if float(np.max(np.abs(refined[:, 0] - corners[:, 0]))) >= 8.0:
            height, width = image.shape[:2]
            destination = np.float32([
                [0, 0], [width - 1, 0],
                [width - 1, height - 1], [0, height - 1],
            ])
            candidate_image = cv2.warpPerspective(
                source, cv2.getPerspectiveTransform(refined, destination),
                (width, height), flags=cv2.INTER_CUBIC,
                borderValue=(255, 255, 255),
            )
            candidate_geometry = _physical_geometry(candidate_image)
            if (
                candidate_geometry is not None
                and len(candidate_geometry[0]) == len(rows)
                and len(candidate_geometry[1]) + 1 == len(columns)
            ):
                image, corners = candidate_image, refined
                rows, columns = candidate_geometry
                widths = np.diff(columns).astype(float)
                typical_width = float(np.median(widths))
    if (
        not merged_title and typical_width > 0
        and widths[0] < typical_width * 0.80
        and widths[-1] < typical_width * 0.80
    ):
        row_lines = _unique_ruled_row_columns(image, rows)
        if (
            row_lines is not None
            and not row_lines[1]
            and len(row_lines[0][0]) >= 7
            and len(row_lines[0][0]) + 1 == len(columns)
        ):
            columns = [
                int(round(float(np.median([line[index] for line in row_lines[0]]))))
                for index in range(len(row_lines[0][0]))
            ]
    if (len(rows) - 1) * (len(columns) - 1) > maximum_cells:
        return None

    centers = [(first + second) * 0.5 for first, second in zip(rows[1:-2], rows[2:-1])]
    pitch = float(np.median(np.diff(centers))) if len(centers) > 1 else 0.0
    if pitch >= 18 and float(corners[2:, 1].max()) + pitch * 2 < source.shape[0]:
        expanded = []
        base_physical = None
        # A first crop can stop several rows early when the coloured header is
        # much stronger than the pale lower grid.  Keep following the measured
        # row pitch until physical geometry stops growing or the source ends.
        for step in range(1, 13):
            if float(corners[2:, 1].max()) + pitch * step >= source.shape[0]:
                break
            candidate_corners = corners.copy()
            candidate_corners[2:, 1] += pitch * step
            candidate_height = int(round(image.shape[0] + pitch * step))
            destination = np.float32([
                [0, 0], [image.shape[1] - 1, 0],
                [image.shape[1] - 1, candidate_height - 1], [0, candidate_height - 1],
            ])
            candidate = cv2.warpPerspective(
                source, cv2.getPerspectiveTransform(candidate_corners, destination),
                (image.shape[1], candidate_height), flags=cv2.INTER_CUBIC,
                borderValue=(255, 255, 255),
            )
            candidate_geometry = (
                _merged_title_geometry(candidate) if merged_title
                else _complete_ruled_geometry(candidate, maximum_cells)
                if complete_ruled_geometry
                else _physical_geometry(candidate)
            )
            if candidate_geometry is None:
                break
            candidate_rows, candidate_columns = candidate_geometry
            if len(candidate_columns) != len(columns):
                if base_physical is None:
                    base_physical = _unique_ruled_row_columns(image, rows)
                candidate_physical = _unique_ruled_row_columns(candidate, candidate_rows)
                if (
                    base_physical is None or candidate_physical is None
                    or len(base_physical[0][0]) != len(candidate_physical[0][0])
                ):
                    break
                candidate_columns = [
                    int(round(float(np.median([
                        line[index] for line in candidate_physical[0]
                    ]))))
                    for index in range(len(candidate_physical[0][0]))
                ]
                candidate_geometry = candidate_rows, candidate_columns
            if (
                not len(rows) + 1 <= len(candidate_rows) <= len(rows) + step
                or expanded and len(candidate_rows) < len(expanded[-1][2][0])
                or not _last_row_has_text(candidate, candidate_rows, candidate_columns)
            ):
                break
            expanded.append((candidate, candidate_corners, candidate_geometry))
        if len(expanded) >= 2:
            image, corners, geometry = max(expanded, key=lambda item: len(item[2][0]))
            rows, columns = geometry

    physical_row_columns = None
    estimated_column_count = len(columns) - 1
    physical = _unique_ruled_row_columns(image, rows)
    if physical is not None and len(physical[0][0]) >= 7:
        physical_row_columns = physical[0]
        columns = [
            int(round(float(np.median([line[index] for line in physical_row_columns]))))
            for index in range(len(physical_row_columns[0]))
        ]
        typical_width = float(np.median(np.diff(columns)))
        if image.shape[1] - columns[-1] > typical_width * 0.45:
            outside_rows = 0
            for top, bottom in zip(rows[1:-1], rows[2:]):
                strip = image[top + 3:bottom - 3, columns[-1] + 8:image.shape[1] - 8]
                if strip.size == 0:
                    continue
                gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
                cutoff = min(140, int(np.median(gray)) - 25)
                _, _, stats, _ = cv2.connectedComponentsWithStats(
                    np.uint8(gray < cutoff), 8
                )
                outside_rows += any(
                    area >= 8 and 8 <= height < strip.shape[0] * 0.75
                    and 2 <= width < strip.shape[1] * 0.70
                    for _, _, width, height, area in stats[1:]
                )
            if outside_rows >= max(3, int((len(rows) - 2) * 0.25)):
                deficit = columns[-1] + typical_width - image.shape[1]
                recovered = None
                if estimated_column_count == len(columns) and 0 < deficit < typical_width * 0.75:
                    recovered = _expand_partial_right(
                        source, image, corners, rows,
                        columns + [image.shape[1]], deficit,
                    )
                if recovered is None:
                    return None
                image, corners, rows, columns = recovered
                physical_row_columns = None

    missing_threshold = max(3, int((len(rows) - 2) * 0.25))
    if _outside_text_rows(source, image, corners, rows, side="right") >= missing_threshold:
        recovered = _expand_right_column(source, image, corners, rows, columns)
        if recovered is None:
            return None
        image, corners, rows, columns = recovered
        physical_row_columns = None
        if _outside_text_rows(source, image, corners, rows, side="right") >= missing_threshold:
            return None
    if _outside_text_rows(source, image, corners, rows, side="left") >= missing_threshold:
        return None

    widths = np.diff(columns).astype(float)
    if len(widths) >= 5:
        interior_width = float(np.median(widths[1:-1]))
        narrow_symmetric_gutters = bool(
            interior_width > 0
            and max(widths[0], widths[-1]) < interior_width * 0.25
            and max(widths[0], widths[-1])
            < max(1.0, min(widths[0], widths[-1])) * 2.0
        )
        if (
            interior_width > 0
            and widths[0] < interior_width * 0.35
            and widths[-1] < interior_width * 0.35
        ):
            ink = _ink_mask(image)

            def gutter_has_text(left: int, right: int) -> bool:
                region = ink[
                    int(rows[0]) + 4:int(rows[-1]) - 4,
                    int(left) + 4:int(right) - 4,
                ]
                if region.size == 0:
                    return False
                _, _, stats, _ = cv2.connectedComponentsWithStats(region, 8)
                return any(
                    area >= 8 and height >= 8 and width >= 2
                    and height < region.shape[0] * 0.50
                    for _, _, width, height, area in stats[1:]
                )

            if (
                narrow_symmetric_gutters
                or (
                    not gutter_has_text(columns[0], columns[1])
                    and not gutter_has_text(columns[-2], columns[-1])
                )
            ):
                columns = columns[1:-1]
                if physical_row_columns is not None:
                    physical_row_columns = [line[1:-1] for line in physical_row_columns]

    locations, _ = cell_crops(ColoredGrid(
        image, rows, columns, [], merged_title, physical_row_columns,
    ))
    if not complete_ruled_geometry:
        for column in range(len(columns) - 1):
            if any(location_column == column for _, location_column in locations):
                continue
            widths = np.diff(columns).astype(float)
            if (
                column == len(columns) - 2
                and len(widths) >= 4
                and float(widths[-1]) < float(np.median(widths[:-1])) * 0.75
            ):
                columns.pop(-2)
            else:
                return None

    widths = np.diff(columns).astype(float)
    typical_width = float(np.median(widths[:-1])) if len(widths) > 2 else 0.0
    last_width = float(widths[-1])
    last_column = len(columns) - 2
    body_support = sum(row > 0 and column == last_column for row, column in locations)
    if (
        typical_width > 0
        and 0.45 * typical_width <= last_width < 0.75 * typical_width
        and body_support >= (len(rows) - 2) * 0.50
    ):
        deficit = typical_width - last_width
        scale = float(corners[1, 0] - corners[0, 0]) / image.shape[1]
        if _partial_right_text_rows(source, image, corners, rows, deficit * scale) >= missing_threshold:
            expanded = _expand_partial_right(source, image, corners, rows, columns, deficit)
            if expanded is None:
                return None
            image, corners, rows, columns = expanded

    if len(rows) >= 8 and len(columns) >= 4:
        pitch = float(np.median(np.diff(rows)))
        last_gap = rows[-1] - rows[-2]
        if 8 <= pitch and pitch * 1.7 <= last_gap <= pitch * 2.4:
            import table_pipeline as pipeline

            horizontal, _ = pipeline._physical_rule_maps(image)
            candidates = []
            for y in range(rows[-2] + int(pitch * .55),
                           rows[-1] - int(pitch * .55)):
                band = horizontal[max(0, y - 2):min(image.shape[0], y + 3),
                                  columns[0]:columns[-1]]
                if band.size and float(np.mean(np.any(band > 0, axis=0))) >= .85:
                    candidates.append(y)
            groups = _bands(np.asarray(candidates, dtype=int)) if candidates else []
            if len(groups) == 1:
                boundary = int(round(sum(groups[0]) * .5))
                if (pitch * .7 <= boundary - rows[-2] <= pitch * 1.3
                        and pitch * .7 <= rows[-1] - boundary <= pitch * 1.3):
                    rows.insert(-1, boundary)

    row_columns, title_without_rules = _ruled_row_columns(
        image, rows, len(columns) - 1
    )
    if complete_ruled_geometry:
        # The fallback was admitted only because a complete rule lattice
        # reaches every crop edge.  Missing rules in its first row represent
        # ordinary group-header spans, not missing rows or one full-width title.
        title_without_rules = False
    if row_columns is None and not title_without_rules:
        row_columns = _slanted_left_margin_row_columns(image, rows, columns)
    if row_columns is None and title_without_rules:
        import table_pipeline as pipeline

        physical_grid = pipeline.extract_ruled_grid(image, prefer_adaptive=False)
        if physical_grid is None:
            return None
        physical_columns, physical_rows, _ = physical_grid
        if len(physical_columns) > len(columns):
            _, gutter_vertical, _ = pipeline._grid_maps(image)
            gutter_ink = _ink_mask(image)
            ordinary_width = float(np.median(np.diff(physical_columns)))
            for side in ("left", "right"):
                first, last = physical_columns[:2] if side == "left" else physical_columns[-2:]
                edge = first if side == "left" else last
                edge_band = gutter_vertical[
                    physical_rows[0]:physical_rows[-1],
                    max(0, edge - 3):min(image.shape[1], edge + 4),
                ]
                support = float(np.mean(np.any(edge_band > 0, axis=1))) if edge_band.size else 0.0
                strip = gutter_ink[physical_rows[0]:physical_rows[-1], first + 3:last - 3]
                if last - first >= ordinary_width * 0.35 or support >= 0.10 or not strip.size:
                    continue
                _, _, glyphs, _ = cv2.connectedComponentsWithStats(strip, 8)
                if any(area >= 8 and height >= 8 and width >= 2 for _, _, width, height, area in glyphs[1:]):
                    continue
                if side == "left":
                    physical_columns = physical_columns[1:]
                else:
                    physical_columns = physical_columns[:-1]
        if (
            len(physical_rows) != len(rows)
            or not 3 <= len(physical_columns) - 1 <= 10
            or abs(len(physical_columns) - len(columns)) > 2
            or (len(physical_rows) - 1) * (len(physical_columns) - 1) > maximum_cells
        ):
            return None
        row_gaps = np.diff(physical_rows).astype(float)
        pitch = float(np.median(row_gaps))
        if (
            pitch < 8 or float(row_gaps.min()) < pitch * 0.70
            or float(row_gaps.max()) > pitch * 1.30
        ):
            return None
        horizontal, vertical, _ = pipeline._grid_maps(image)
        vertical_support = sum(
            float(np.mean(np.any(vertical[
                physical_rows[1]:physical_rows[-1],
                max(0, boundary - 3):min(image.shape[1], boundary + 4),
            ] > 0, axis=1))) >= 0.35
            for boundary in physical_columns[1:-1]
        )
        horizontal_support = sum(
            float(np.mean(np.any(horizontal[
                max(0, boundary - 3):min(image.shape[0], boundary + 4),
                physical_columns[0]:physical_columns[-1],
            ] > 0, axis=0))) >= 0.45
            for boundary in physical_rows[1:-1]
        )
        detected_spans = physical_spans(image, physical_rows, physical_columns)
        if detected_spans is None:
            return None
        flat_locations, _ = cell_crops(ColoredGrid(
            image, physical_rows, physical_columns, [], False, None, detected_spans,
        ))
        last_effective_columns = len(physical_columns) - 1 - sum(
            span["column_span"] - 1 for span in detected_spans
            if span["row"] <= len(physical_rows) - 2 < span["row"] + span["row_span"]
        )
        if (
            vertical_support < max(2, int(np.ceil((len(physical_columns) - 2) * 0.50)))
            or horizontal_support < (len(physical_rows) - 2) * 0.80
            or sum(row == 0 for row, _ in flat_locations) < len(physical_columns) - 2
            or sum(row == 1 for row, _ in flat_locations) < len(physical_columns) - 2
            or sum(row == len(physical_rows) - 2 for row, _ in flat_locations)
            < max(1, last_effective_columns - 2)
        ):
            return None
        rows, columns = physical_rows, physical_columns
        merged_title = False
    if row_columns is not None:
        if title_without_rules and not merged_title:
            first_two, _ = cell_crops(ColoredGrid(image, rows, columns, []))
            first_count = sum(row == 0 for row, _ in first_two)
            second_count = sum(row == 1 for row, _ in first_two)
            if second_count < len(columns) - 2:
                return None
            if first_count <= 2:
                merged_title = True
            elif first_count < len(columns) - 2:
                return None
        columns = [
            int(round(float(np.median([line[column] for line in row_columns]))))
            for column in range(len(columns))
        ]
        if min(np.diff(columns)) <= 0:
            return None
        final_widths = np.diff(columns).astype(float)
        if len(final_widths) >= 5:
            interior_width = float(np.median(final_widths[1:-1]))
            if (
                interior_width > 0
                and max(final_widths[0], final_widths[-1])
                < interior_width * 0.25
                and max(final_widths[0], final_widths[-1])
                < max(1.0, min(final_widths[0], final_widths[-1])) * 2.0
            ):
                columns = columns[1:-1]
                row_columns = [line[1:-1] for line in row_columns]
        if merged_title:
            flat_locations, _ = cell_crops(ColoredGrid(
                image, rows, columns, [], False, row_columns,
            ))
            if (
                sum(row == 0 for row, _ in flat_locations) >= len(columns) - 2
                and sum(row == 1 for row, _ in flat_locations) >= len(columns) - 2
            ):
                merged_title = False
    pitch = float(np.median(np.diff(rows)))
    if rows[-1] - rows[-2] < pitch * 0.75:
        ink = _ink_mask(image)
        if np.count_nonzero(ink[-4:]) >= max(8, image.shape[1] * 0.002):
            extra = max(8, int(round(pitch - (rows[-1] - rows[-2]) + 4)))
            height, width = image.shape[:2]
            original_frame = np.float32([[0, 0], [width - 1, 0],
                                         [width - 1, height - 1], [0, height - 1]])
            extended_frame = original_frame.copy()
            extended_frame[2:, 1] += extra
            inverse = cv2.getPerspectiveTransform(original_frame, corners.astype(np.float32))
            extended_corners = cv2.perspectiveTransform(extended_frame[None], inverse)[0]
            if (
                float(extended_corners[:, 0].min()) >= 0
                and float(extended_corners[:, 0].max()) < source.shape[1]
                and float(extended_corners[:, 1].min()) >= 0
                and float(extended_corners[:, 1].max()) < source.shape[0]
            ):
                image = cv2.warpPerspective(
                    source, cv2.getPerspectiveTransform(extended_corners, extended_frame),
                    (width, height + extra), flags=cv2.INTER_CUBIC,
                    borderValue=(255, 255, 255),
                )
                rows[-1] = min(image.shape[0], rows[-2] + int(round(pitch)))
                corners = extended_corners
    spans = physical_spans(image, rows, columns, row_columns)
    if spans is None:
        return None
    widths = np.diff(columns).astype(float)
    if spans and len(widths) >= 3:
        typical_width = float(np.median(widths[1:]))
        leading_gutter = any(
            int(span.get("row", -1)) == 0
            and int(span.get("row_span", 0)) == len(rows) - 1
            and int(span.get("column", -1)) == 0
            and int(span.get("column_span", 0)) == 1
            for span in spans
        )
        if leading_gutter and widths[0] < typical_width * 0.25:
            columns = columns[1:]
            if row_columns is not None:
                row_columns = [line[1:] for line in row_columns]
            spans = physical_spans(image, rows, columns, row_columns)
            if spans is None:
                return None
        widths = np.diff(columns).astype(float)
        typical_width = float(np.median(widths[:-1])) if len(widths) >= 2 else 0.0
        trailing_column = len(columns) - 2
        trailing_gutter = any(
            int(span.get("row", -1)) == 0
            and int(span.get("row_span", 0)) == len(rows) - 1
            and int(span.get("column", -1)) == trailing_column
            and int(span.get("column_span", 0)) == 1
            for span in spans
        )
        if trailing_gutter and typical_width > 0 and widths[-1] < typical_width * 0.25:
            columns = columns[:-1]
            if row_columns is not None:
                row_columns = [line[:-1] for line in row_columns]
            spans = physical_spans(image, rows, columns, row_columns)
            if spans is None:
                return None
    if complete_ruled_geometry and row_columns is not None:
        # The complete lattice was needed only to recover columns that sparse
        # header text could not describe.  Per-row rules now independently
        # prove the final geometry, so use the faster cell-consensus route.
        complete_ruled_geometry = False
    if merged_title and not any(span["row"] == 0 for span in spans):
        spans.insert(0, {"row":0,"column":0,"row_span":1,"column_span":len(columns)-1,"role":"title"})
    merged_title = any(span["role"] == "title" for span in spans)
    return ColoredGrid(
        image, rows, columns, corners.astype(float).round(2).tolist(),
        merged_title, row_columns, spans, complete_ruled_geometry,
    )


def _neutral_color_grid(source: np.ndarray, maximum_cells: int) -> ColoredGrid | None:
    if (source is None or source.ndim != 3 or source.size == 0
            or source.shape[0] * source.shape[1] > 1_000_000):
        return None
    spread = np.max(source, axis=2) - np.min(source, axis=2)
    saturated = (spread >= 70) & (np.max(source, axis=2) >= 100)
    ys, xs = np.nonzero(saturated)
    if len(xs) < source.shape[0] * source.shape[1] * 0.15:
        return None
    left, right, top, bottom = map(int, (xs.min(), xs.max(), ys.min(), ys.max()))
    neutral = ((spread <= 18)
               & (np.median(source, axis=2) >= 20)
               & (np.median(source, axis=2) <= 230))
    rows = [int((first + last) // 2) for first, last in _bands(np.flatnonzero(
        np.mean(neutral[:, left:right + 1], axis=1) >= 0.85
    )) if top - 3 <= first <= bottom + 3]
    columns = [int((first + last) // 2) for first, last in _bands(np.flatnonzero(
        np.mean(neutral[top:bottom + 1, :], axis=0) >= 0.70
    )) if left - 3 <= first <= right + 3]
    if (not 4 <= len(rows) <= 41 or not 3 <= len(columns) <= 11
            or (len(rows) - 1) * (len(columns) - 1) > maximum_cells
            or min(np.diff(rows)) < 8 or min(np.diff(columns)) < 12
            or abs(rows[0] - top) > 3 or abs(rows[-1] - bottom) > 3
            or abs(columns[0] - left) > 3 or abs(columns[-1] - right) > 3):
        return None
    import table_pipeline as pipeline
    adaptive = pipeline.extract_ruled_grid(source, prefer_adaptive=True)
    if (adaptive is not None and len(adaptive[0]) > len(columns)
            and adaptive[1][0] <= rows[0] + 5
            and adaptive[1][-1] >= rows[-1] - 5
            and all(any(abs(x - y) <= 5 for x in adaptive[0]) for y in columns)):
        extras = [x for x in adaptive[0]
                  if all(abs(x - y) > 5 for y in columns)]
        _, vertical, _ = pipeline._grid_maps(source)
        pixels = source.astype(np.int16, copy=False)
        top, end = adaptive[1][0], adaptive[1][-1]
        if extras and all(
            float(np.mean(np.any(vertical[top:end, max(0, x - 3):x + 4] > 0,
                                 axis=1))) >= .70
            and float(np.mean(np.max(np.maximum(
                np.abs(pixels[top:end, x] - pixels[top:end, max(0, x - 3)]),
                np.abs(pixels[top:end, x] - pixels[top:end, min(source.shape[1] - 1, x + 3)]),
            ), axis=1) >= 24)) >= .55
            for x in extras
        ):
            return None
    corners = [[float(columns[0]), float(rows[0])],
               [float(columns[-1]), float(rows[0])],
               [float(columns[-1]), float(rows[-1])],
               [float(columns[0]), float(rows[-1])]]
    return ColoredGrid(source, rows, columns, corners, spans=[], complete_ruled=True)


def detect_colored_grid(
    source: np.ndarray, *, maximum_cells: int = 320, allow_line_header: bool = True,
) -> ColoredGrid | None:
    candidate = _detect_colored_grid_from_photo(
        source, maximum_cells=maximum_cells, allow_line_header=allow_line_header,
    )
    return candidate if candidate is not None else _neutral_color_grid(source, maximum_cells)


def _tight_crop(crop: np.ndarray, *, minimum_glyph_height: int | None = None) -> np.ndarray:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    cutoff = min(140, int(np.median(gray)) - 25)
    _, _, components, _ = cv2.connectedComponentsWithStats(np.uint8(gray < cutoff), 8)
    glyphs = [
        (int(x), int(y), int(x + width), int(y + height))
        for x, y, width, height, area in components[1:]
        if area >= 8
        and (minimum_glyph_height or max(8, gray.shape[0] * 0.18)) <= height < gray.shape[0] * 0.75
        and width < gray.shape[1] * 0.75
    ]
    if not glyphs:
        return crop
    left = min(x for x, _, _, _ in glyphs)
    right = max(x for _, _, x, _ in glyphs)
    top = min(y for _, y, _, _ in glyphs)
    bottom = max(y for _, _, _, y in glyphs)
    short_right = [
        int(x + width) for x, y, width, height, area in components[1:]
        if area >= 4 and height < max(8, gray.shape[0] * 0.18)
        and width < gray.shape[1] * 0.40
        and right - 3 <= x <= right + 30
        and top - 5 <= y + height * 0.5 <= bottom + 5
    ]
    short_left = [
        int(x) for x, y, width, height, area in components[1:]
        if area >= 4 and height < max(8, gray.shape[0] * 0.18)
        and width < gray.shape[1] * 0.40
        and left - 30 <= x + width <= left + 3
        and top - 5 <= y + height * 0.5 <= bottom + 5
    ]
    return crop[
        max(0, top - 5):min(crop.shape[0], bottom + 5),
        max(0, min([left, *short_left]) - 5):min(crop.shape[1], max([right, *short_right]) + 5),
    ]


def has_visible_double_space(crop: np.ndarray) -> bool:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    cutoff = min(140, int(np.median(gray)) - 25)
    binary = gray < cutoff
    profile = np.count_nonzero(
        _ink_mask(crop) > 0 if np.max(np.mean(binary, axis=1)) >= 0.75 else binary,
        axis=0,
    )
    bands = _bands(np.flatnonzero(profile >= 2))
    gaps = sorted(
        (right[0] - left[1] - 1 for left, right in zip(bands, bands[1:])),
        reverse=True,
    )
    return bool(
        gaps and gaps[0] >= max(14, crop.shape[0] * 0.18)
        and (len(gaps) == 1 or gaps[0] >= gaps[1] * 2.5)
    )


def masked_numeric_shape(crop: np.ndarray) -> tuple[int, int, int] | None:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    cutoff = min(170, int(np.median(gray)) - 25)
    _, _, stats, _ = cv2.connectedComponentsWithStats(np.uint8(gray < cutoff), 8)
    glyphs = sorted(
        (int(x), int(width), int(height))
        for x, _, width, height, area in stats[1:]
        if area >= 12 and height >= 4 and width >= 2
    )
    if len(glyphs) < 8:
        return None
    tall_height = max(height for _, _, height in glyphs)
    stars = [
        (x, width) for x, width, height in glyphs
        if tall_height * 0.28 <= height <= tall_height * 0.58
    ]
    if not 2 <= len(stars) <= 8:
        return None
    first_star = stars[0][0]
    last_star = stars[-1][0] + stars[-1][1]
    tall = [
        (x, width) for x, width, height in glyphs
        if height >= tall_height * 0.72
    ]
    prefix = [(x, width) for x, width in tall if x + width <= first_star + 1]
    suffix = [(x, width) for x, width in tall if x >= last_star - 1]
    if not 2 <= len(prefix) <= 4 or not suffix:
        return None
    pitch = float(np.median(np.diff([x for x, _ in prefix])))
    if not 7 <= pitch <= 30:
        return None
    suffix_width = max(x + width for x, width in suffix) - min(x for x, _ in suffix)
    suffix_count = round(suffix_width / pitch)
    if (
        not 3 <= suffix_count <= 6
        or abs(suffix_width / pitch - suffix_count) > 0.35
        or first_star - (prefix[-1][0] + prefix[-1][1]) > pitch * 0.8
        or suffix[0][0] - last_star > pitch * 0.8
    ):
        return None
    return len(prefix), len(stars), suffix_count


def visible_masked_star_count(crop: np.ndarray) -> int | None:
    """Count visible masking glyphs without relying on joined digit components."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    cutoff = min(170, int(np.median(gray)) - 25)
    _, _, stats, _ = cv2.connectedComponentsWithStats(np.uint8(gray < cutoff), 8)
    glyphs = sorted(
        (int(x), int(y), int(width), int(height))
        for x, y, width, height, area in stats[1:]
        if area >= 12 and height >= 4 and width >= 2
        and not (height >= crop.shape[0] * 0.75 and width <= 3)
    )
    if len(glyphs) < 7:
        return None
    tall = float(np.median(sorted((height for _, _, _, height in glyphs), reverse=True)[:3]))
    stars = [
        (x, y, width, height) for x, y, width, height in glyphs
        if tall * 0.28 <= height <= tall * 0.60
    ]
    if not 2 <= len(stars) <= 8:
        return None
    starts = [x for x, _, _, _ in stars]
    gaps = np.diff(starts)
    pitch = float(np.median(gaps))
    if (
        pitch < 5 or float(gaps.min()) < pitch * 0.75
        or float(gaps.max()) > pitch * 1.25
        or max(y for _, y, _, _ in stars) - min(y for _, y, _, _ in stars) > 4
        or not any(x + width < starts[0] for x, _, width, height in glyphs
                   if height >= tall * 0.75)
        or not any(x > starts[-1] for x, _, _, height in glyphs
                   if height >= tall * 0.75)
    ):
        return None
    count = sum(max(1, int(round(width / pitch))) for _, _, width, _ in stars)
    return count if 2 <= count <= 8 else None


def cell_crops(candidate: ColoredGrid) -> tuple[list[tuple[int, int]], list[np.ndarray]]:
    locations = []
    crops = []
    full_width_title = bool(
        candidate.merged_title
        and not any(
            span["row"] == 0 and span["column"] == 0
            and span.get("role") == "title"
            and span["column_span"] < len(candidate.columns) - 1
            for span in candidate.spans or []
        )
    )
    covered = {(row, column) for span in candidate.spans or []
               for row in range(span["row"], span["row"] + span["row_span"])
               for column in range(span["column"], span["column"] + span["column_span"])
               if (row, column) != (span["row"], span["column"])}
    vertical_anchors = {(span["row"], span["column"]) for span in candidate.spans or [] if span["row_span"] > 1}
    for row in range(len(candidate.rows) - 1):
        for column in range(len(candidate.columns) - 1):
            if (row, column) in covered:
                continue
            if full_width_title and row == 0 and column > 0:
                continue
            crop = candidate.cell_image(
                row, column, full_title=full_width_title and row == 0
            )
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            cutoff = min(140, int(np.median(gray)) - 25)
            _, _, components, _ = cv2.connectedComponentsWithStats(np.uint8(gray < cutoff), 8)
            visible_glyph = any(
                area >= 7
                and 8 <= glyph_height < crop.shape[0] * 0.75
                and glyph_width >= max(2, glyph_height * 0.06)
                and glyph_width < crop.shape[1] * 0.70
                for _, _, glyph_width, glyph_height, area in components[1:]
            )
            if not visible_glyph and not candidate.complete_ruled:
                continue
            locations.append((row, column))
            crops.append(_tight_crop(crop, minimum_glyph_height=8) if (row, column) in vertical_anchors else _tight_crop(crop))
    return locations, crops
