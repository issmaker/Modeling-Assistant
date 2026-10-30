"""BH-03.2F — deterministic viewport-local vertex candidate buckets.

Pure Python; importable outside Blender. The mutable grid is an internal,
read-only snapshot from a frozen mesh/view; callers invalidate on any change.
Index construction is incremental, lookup visits only neighboring cells.
"""
import math

CELL_PX = 32.0
RADIUS_PX = 18.0
MAX_CANDIDATES = 256


class SnapScreenGrid:
    def __init__(self, width, height, *, cell_px=CELL_PX, radius_px=RADIUS_PX):
        self.width = int(width)
        self.height = int(height)
        self.cell = float(cell_px)
        self.radius = float(radius_px)
        if not (self.width > 0 and self.height > 0 and
                8 <= self.cell <= 128 and 1 <= self.radius <= 60):
            raise ValueError('Некорректная сетка экранной привязки')
        self._cells = {}
        self.count = 0

    def add(self, index, world, screen):
        if screen is None:
            return False
        try:
            x, y = float(screen[0]), float(screen[1])
            xyz = tuple(float(v) for v in world)
            ident = int(index)
        except (TypeError, ValueError, OverflowError):
            return False
        if len(xyz) != 3 or not all(math.isfinite(v) for v in (x, y, *xyz)):
            return False
        pad = self.radius
        if x < -pad or x > self.width + pad or y < -pad or y > self.height + pad:
            return False
        key = (math.floor(x / self.cell), math.floor(y / self.cell))
        self._cells.setdefault(key, []).append((ident, xyz, (x, y)))
        self.count += 1
        return True

    def query(self, pointer, *, max_candidates=MAX_CANDIDATES):
        """Returns sorted visible-*screen* candidates, overflow marker.

        Visibility in depth MUST be proven by a separate Blender BVH adapter.
        On overcrowding we refuse Snap rather than selecting an arbitrary
        member of a truncated list.
        """
        try:
            px, py = float(pointer[0]), float(pointer[1])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Некорректные координаты курсора') from exc
        if not (math.isfinite(px) and math.isfinite(py)):
            raise ValueError('Некорректные координаты курсора')
        cell = self.cell
        r = self.radius
        xmin = math.floor((px - r) / cell)
        xmax = math.floor((px + r) / cell)
        ymin = math.floor((py - r) / cell)
        ymax = math.floor((py + r) / cell)
        found = []
        r2 = r * r
        for ix in range(xmin, xmax + 1):
            for iy in range(ymin, ymax + 1):
                for ident, xyz, (sx, sy) in self._cells.get((ix, iy), ()):
                    dist2 = (sx - px) ** 2 + (sy - py) ** 2
                    if dist2 <= r2:
                        found.append((dist2, ident, xyz, (sx, sy)))
                        if len(found) > max_candidates:
                            return (), True
        found.sort(key=lambda x: (x[0], x[1]))
        return tuple((ident, xyz, screen) for _, ident, xyz, screen in found), False
