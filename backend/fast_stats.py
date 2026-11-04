"""Exact median for 8-bit image data.

Why this exists
---------------
``np.median`` partitions a copy of the array.  On the 8-bit bands the table
geometry code measures constantly (a 6 x 1250 pixel band per candidate
rule line, thousands of candidate lines per request) the partition copy
dominates the cost: 2026-10-22 profiling of one FC-00016 request showed
22 552 median calls over ~100 million elements, 12.9 s of the 88 s request
spent inside ``ndarray.partition`` alone.

An 8-bit image has only 256 possible values, so the same number can be
produced with a 256-bin histogram: one pass to count, one cumulative sum
to locate the middle.  The result is **bit-identical** to ``np.median``,
including the "mean of the two middle values" rule for even element counts,
so no geometric decision changes.
"""

from __future__ import annotations

import numpy as np

_BINS = 256


def _value_at(counts: np.ndarray, total: int, position: int) -> float:
    """Return the value at ``position`` (0-based) of the sorted data."""
    cumulative = np.cumsum(counts)
    index = int(np.searchsorted(cumulative, position + 1, side="left"))
    if index >= _BINS:
        index = _BINS - 1
    return float(index)


def median_u8(values: np.ndarray) -> float:
    """Median of an 8-bit array; identical to ``float(np.median(values))``."""
    array = np.asarray(values)
    if array.size == 0:
        return float(np.median(array))
    if array.dtype != np.uint8:
        return float(np.median(array))
    counts = np.bincount(array.ravel(), minlength=_BINS)
    total = int(array.size)
    if total % 2 == 1:
        return _value_at(counts, total, total // 2)
    lower = _value_at(counts, total, total // 2 - 1)
    upper = _value_at(counts, total, total // 2)
    return (lower + upper) / 2.0


def median_u8_per_channel(values: np.ndarray) -> np.ndarray:
    """Per-channel median of an ``(H, W, C)`` 8-bit array.

    Equivalent to ``np.median(values, axis=(0, 1))`` for a 3-channel image.
    """
    array = np.asarray(values)
    if array.ndim != 3:
        return np.median(array, axis=(0, 1))
    if array.dtype != np.uint8 or array.size == 0:
        return np.median(array, axis=(0, 1))
    flat = array.reshape(-1, array.shape[2])
    channels = array.shape[2]
    result = np.empty(channels, dtype=np.float64)
    total = int(flat.shape[0])
    for channel in range(channels):
        counts = np.bincount(flat[:, channel], minlength=_BINS)
        if total % 2 == 1:
            result[channel] = _value_at(counts, total, total // 2)
        else:
            lower = _value_at(counts, total, total // 2 - 1)
            upper = _value_at(counts, total, total // 2)
            result[channel] = (lower + upper) / 2.0
    return result
