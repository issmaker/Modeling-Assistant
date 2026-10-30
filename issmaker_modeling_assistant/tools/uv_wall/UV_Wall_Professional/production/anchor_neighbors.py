"""Pure-Python 3D anchor lookup for production welds.

Exact distance policy matches the original exhaustive scan; spatial bins
only reduce the number of candidates and do not alter coordinates.
"""
from math import floor, isfinite, sqrt


class AnchorNeighbors:
    def __init__(self, positions, radius):
        self.radius = float(radius)
        if not isfinite(self.radius) or self.radius <= 0:
            raise ValueError("Anchor radius must be positive and finite")
        self.radius_sq = self.radius * self.radius
        self.cells = {}
        self.positions = []
        for index, point in enumerate(positions):
            coords = tuple(float(v) for v in point)
            if len(coords) != 3 or not all(isfinite(v) for v in coords):
                raise ValueError("Invalid authored anchor coordinate")
            self.positions.append(coords)
            self.cells.setdefault(self._cell(coords), []).append(index)

    def _cell(self, point):
        return tuple(floor(value / self.radius) for value in point)

    def matches(self, point):
        coords = tuple(float(v) for v in point)
        if len(coords) != 3 or not all(isfinite(v) for v in coords):
            return set()
        x, y, z = self._cell(coords)
        found = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for index in self.cells.get((x + dx, y + dy, z + dz), ()):
                        other = self.positions[index]
                        squared = sum((a-b)**2 for a,b in zip(coords,other))
                        if sqrt(squared) <= self.radius:
                            found.add(index)
        return found
