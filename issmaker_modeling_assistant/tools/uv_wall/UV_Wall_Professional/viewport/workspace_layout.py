"""Shared collision policy for every UV Wall viewport overlay."""
from dataclasses import dataclass
from math import isfinite


def intersection_area(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1]))


def clamp_rect(rect, region, margin=8.0):
    rw, rh = map(float, region)
    x0, y0, x1, y1 = map(float, rect)
    w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
    w, h = min(w, max(1.0, rw - 2 * margin)), min(h, max(1.0, rh - 2 * margin))
    x0 = max(margin, min(x0, rw - w - margin))
    y0 = max(margin, min(y0, rh - h - margin))
    return (x0, y0, x0 + w, y0 + h)


def normalized_ui_scale(value):
    """Return one safe scale for all custom viewport surfaces.

    Blender preferences and test doubles may expose missing, zero, NaN or very
    large values.  Keeping the policy here prevents individual overlays from
    drifting to incompatible sizes.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 1.0
    if not isfinite(value) or value <= 0.0:
        value = 1.0
    return max(0.75, min(2.0, value))


def scaled(value, ui_scale=1.0):
    return float(value) * normalized_ui_scale(ui_scale)


@dataclass(frozen=True)
class OverlaySurface:
    key: str
    rect: tuple
    priority: int = 0
    interactive: bool = True


class WorkspaceLayoutManager:
    """Collect and place overlays against one authoritative obstacle list."""
    def __init__(self, region, ui_scale=1.0):
        self.region = tuple(map(float, region))
        self.ui_scale = normalized_ui_scale(ui_scale)
        self.margin = scaled(8.0, self.ui_scale)
        self.surfaces = []

    def reserve(self, key, rect, priority=0, interactive=True):
        if rect is None or len(rect) != 4 or not all(isfinite(float(v)) for v in rect):
            return None
        final = clamp_rect(rect, self.region, self.margin)
        self.surfaces = [surface for surface in self.surfaces if surface.key != key]
        self.surfaces.append(OverlaySurface(str(key), final, int(priority), bool(interactive)))
        return final

    def obstacles(self, exclude=()):
        excluded = set(exclude)
        return [surface.rect for surface in self.surfaces
                if surface.interactive and surface.key not in excluded]

    def place(self, key, size, candidates, priority=0, avoid=(), interactive=True):
        w, h = (scaled(size[0], self.ui_scale), scaled(size[1], self.ui_scale))
        obstacles = self.obstacles(exclude=(key,)) + [tuple(map(float, item)) for item in avoid]
        best = None
        best_score = float("inf")
        for index, point in enumerate(candidates):
            rect = clamp_rect((point[0], point[1], point[0] + w, point[1] + h), self.region, self.margin)
            overlap = sum(intersection_area(rect, obstacle) for obstacle in obstacles)
            score = overlap * 1000.0 + index
            if score < best_score:
                best, best_score = rect, score
        if best is None:
            best = clamp_rect((self.margin, self.margin, self.margin + w, self.margin + h), self.region, self.margin)
        return self.reserve(key, best, priority, interactive)
