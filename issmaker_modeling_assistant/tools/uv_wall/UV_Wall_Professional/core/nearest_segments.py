"""Pure-Python exact nearest XY polyline segment search.

The BVH only filters candidates; the scoring formula is deliberately the same
as the legacy O(faces * segments) wall normal validators. Every leaf retains
its authored order index so equal-distance ties are deterministic.

No Blender data/API, no mutation, no approximate early-exit radius.
"""
from __future__ import annotations

from heapq import heappop, heappush
from math import isfinite


class GeometryTruthMismatch(RuntimeError):
    """Fail closed when an optimized search disagrees with exact legacy scan."""


def reference_nearest_index(segments, x, y):
    """Independent historical full-scan nearest with first-index tie rule."""
    best_i = None
    best_d = float('inf')
    for i, seg in enumerate(segments):
        ax, ay, dx, dy, l2 = seg[:5]
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / l2))
        px, py = ax + t * dx, ay + t * dy
        d = (x - px) ** 2 + (y - py) ** 2
        if d < best_d:
            best_d, best_i = d, i
    return best_i


def _box_distance2(x, y, box):
    minx, miny, maxx, maxy = box
    dx = max(minx - x, 0.0, x - maxx)
    dy = max(miny - y, 0.0, y - maxy)
    return dx * dx + dy * dy


class NearestSegments:
    """Exact nearest-segment index; returns the legacy first minimizing index.

    Input segment rows: (ax, ay, dx, dy, length_squared, ...). The output
    row index refers to the *same* input list. This class never approximates
    the nearest result and never modifies any input coordinates.
    """

    def __init__(self, segments, leaf_size=12):
        # Immutable numeric source snapshot is required: stale spatial indices
        # after path edits would silently produce incorrect face normal votes.
        self.segments = tuple(tuple(seg) for seg in segments)
        self.truth_mode = 'SAMPLED'
        self.truth_calls = 0
        self.truth_checks = 0
        self.truth_failures = 0
        self.truth_stride = 2048
        self.truth_warmup = 12
        self.leaf_size = max(2, int(leaf_size))
        boxes = []
        for i, seg in enumerate(self.segments):
            ax, ay, dx, dy, l2 = seg[:5]
            if not all(isfinite(float(v)) for v in (ax, ay, dx, dy, l2)) or l2 <= 0:
                raise ValueError(f"Invalid nearest-segment data at {i}")
            bx, by = ax + dx, ay + dy
            boxes.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)))
        self._boxes = boxes
        self._root = self._build(list(range(len(self.segments)))) if boxes else None

    def set_truth_mode(self, mode):
        """Set FULL only on explicit diagnostic runs; default is bounded SAMPLE."""
        if mode not in {'SAMPLED', 'FULL'}:
            raise ValueError('Unknown Geometry Truth mode')
        self.truth_mode = mode
        return self

    def truth_report(self):
        return {'mode': self.truth_mode, 'queries': self.truth_calls,
                'reference_checks': self.truth_checks,
                'mismatches': self.truth_failures,
                'sample_warmup': self.truth_warmup,
                'sample_stride': self.truth_stride}

    def _build(self, ids):
        boxes = self._boxes
        bbox = (min(boxes[i][0] for i in ids), min(boxes[i][1] for i in ids),
                max(boxes[i][2] for i in ids), max(boxes[i][3] for i in ids))
        if len(ids) <= self.leaf_size:
            return (bbox, None, tuple(sorted(ids)))
        spanx, spany = bbox[2] - bbox[0], bbox[3] - bbox[1]
        axis = 0 if spanx >= spany else 1
        # Midpoints rather than polygon IDs group spatially close segments.
        ids.sort(key=lambda i: (boxes[i][axis] + boxes[i][axis + 2], i))
        half = len(ids) // 2
        return (bbox, (self._build(ids[:half]), self._build(ids[half:])), None)

    def _nearest_optimized(self, x, y):
        """Spatial index algorithm; never invokes the reference oracle."""
        if self._root is None:
            return None
        x, y = float(x), float(y)
        if not isfinite(x) or not isfinite(y):
            raise ValueError("Nearest-segment query must be finite")
        best_d = float('inf')
        best_i = None
        # Serial makes heap ordering stable when two bounding boxes tie.
        serial = 0
        heap = [(_box_distance2(x, y, self._root[0]), serial, self._root)]
        while heap:
            bound, _, node = heappop(heap)
            if bound > best_d:
                break
            _, children, ids = node
            if ids is not None:
                for i in ids:
                    ax, ay, dx, dy, l2 = self.segments[i][:5]
                    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / l2))
                    px, py = ax + t * dx, ay + t * dy
                    d = (x - px) ** 2 + (y - py) ** 2
                    if d < best_d or (d == best_d and (best_i is None or i < best_i)):
                        best_d, best_i = d, i
            else:
                for child in children:
                    child_d = _box_distance2(x, y, child[0])
                    if child_d <= best_d:
                        serial += 1
                        heappush(heap, (child_d, serial, child))
        return best_i

    def nearest_index(self, x, y):
        """Search and verify against an independent full-scan oracle.

        First warmup queries and every truth_stride-th query are verified on
        normal builds; FULL diagnostic mode verifies every query. A mismatch
        raises BEFORE returning the candidate to the normal validators.
        """
        result = self._nearest_optimized(x, y)
        self.truth_calls += 1
        check = (self.truth_mode == 'FULL' or
                 self.truth_calls <= self.truth_warmup or
                 self.truth_calls % self.truth_stride == 0)
        if check:
            expected = reference_nearest_index(self.segments, float(x), float(y))
            self.truth_checks += 1
            if expected != result:
                self.truth_failures += 1
                raise GeometryTruthMismatch(
                    f'GEOMETRY_TRUTH_MISMATCH query={self.truth_calls}; '
                    f'x={float(x):.9f}; y={float(y):.9f}; '
                    f'reference={expected}; optimized={result}; '
                    f'segments={len(self.segments)}; mode={self.truth_mode}')
        return result
