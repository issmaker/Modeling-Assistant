"""Strict, Blender-independent placement of floating ISSMAKER UI surfaces.

A visible peer window must never be placed over another registered peer.  If
there is no fitting rectangle, fail CLOSED (caller does not draw or register
hitboxes), rather than selecting the least-bad overlap.  Fixed reservations
may overlap each other: they represent already drawn/Blender-owned regions.
"""
from __future__ import annotations
from dataclasses import dataclass
import math


def rect_xyxy(rect):
    try:
        a = tuple(float(v) for v in rect)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid rectangle") from exc
    if len(a) != 4 or not all(map(math.isfinite, a)) or a[2] <= a[0] or a[3] <= a[1]:
        raise ValueError("Invalid rectangle")
    return a


def overlap_area(a, b):
    a, b = rect_xyxy(a), rect_xyxy(b)
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1]))


@dataclass(frozen=True)
class Surface:
    key: str
    bounds: tuple
    fixed: bool = True


class StrictOverlayAllocator:
    """Frame-local registry; prefer requested origin but never accept overlap.

    API coordinates are bottom-left-origin Blender WINDOW region pixels, xyxy.
    The caller owns mutable windows and their lifecycle; this allocator holds
    neither Blender objects nor GPU handles.
    """
    def __init__(self, width, height, *, margin=8.0, gap=6.0):
        self.width = max(0.0, float(width))
        self.height = max(0.0, float(height))
        if not math.isfinite(self.width) or not math.isfinite(self.height):
            raise ValueError("Non-finite viewport")
        self.margin = max(0.0, float(margin))
        self.gap = max(0.0, float(gap))
        self.surfaces = []
        self.last_failure = None

    def reserve(self, key, bounds):
        """Record an already drawn surface (even if legacy fixed peers overlap)."""
        if bounds is None:
            return False
        try:
            rect = rect_xyxy(bounds)
        except ValueError:
            return False
        self.surfaces = [s for s in self.surfaces if s.key != str(key)]
        self.surfaces.append(Surface(str(key), rect))
        return True

    def _clear(self, rect):
        return all(not self._collides(rect, surface.bounds) for surface in self.surfaces)

    def _collides(self, a, b):
        g = self.gap
        return (a[0] < b[2] + g and a[2] > b[0] - g and
                a[1] < b[3] + g and a[3] > b[1] - g)

    def place(self, key, size, preferred, *, reserve=True):
        """Return xyxy or None; never allocate an overlapped/overflowing rect."""
        try:
            w, h = (float(v) for v in size)
            px, py = (float(v) for v in preferred)
        except (TypeError, ValueError, OverflowError):
            self.last_failure = {"key":str(key), "reason":"INVALID_SIZE_OR_POSITION"}
            return None
        if (not all(map(math.isfinite, (w,h,px,py))) or w <= 0 or h <= 0 or
                w > self.width - 2*self.margin or h > self.height - 2*self.margin):
            self.last_failure = {"key":str(key),"reason":"INSUFFICIENT_VIEWPORT"}
            return None
        # Finite exhaustive set of geometrically distinct free placements:
        # a collision-free solution may always slide until it touches the
        # viewport margin or an obstacle edge on each axis.
        xmax = self.width - self.margin - w
        ymax = self.height - self.margin - h
        clampx = lambda x: min(xmax, max(self.margin, x))
        clampy = lambda y: min(ymax, max(self.margin, y))
        xs = [clampx(px), self.margin, xmax, clampx((self.width-w)*.5)]
        ys = [clampy(py), self.margin, ymax, clampy((self.height-h)*.5)]
        for surf in self.surfaces:
            x0,y0,x1,y1 = surf.bounds
            xs.extend((clampx(x0-w-self.gap), clampx(x1+self.gap)))
            ys.extend((clampy(y0-h-self.gap), clampy(y1+self.gap)))
        best = None
        for x in sorted(set(round(v, 5) for v in xs)):
            for y in sorted(set(round(v, 5) for v in ys)):
                r = (x,y,x+w,y+h)
                if not self._clear(r):
                    continue
                score = (x-px)**2+(y-py)**2
                choice = (score,abs(y-py),abs(x-px),-y,x,r)
                if best is None or choice[:5]<best[:5]:
                    best = choice
        if best is None:
            self.last_failure = {"key":str(key),"reason":"NO_FREE_RECT",
                                 "obstacles":[s.key for s in self.surfaces]}
            return None
        rect = best[-1]
        self.last_failure = None
        if reserve:
            self.reserve(key,rect)
        return rect

    def snapshot(self):
        return {"region":[self.width,self.height],"margin":self.margin,
                "gap":self.gap,"surfaces":[{"key":s.key,"bounds":list(s.bounds)}
                for s in self.surfaces],"failure":self.last_failure}
