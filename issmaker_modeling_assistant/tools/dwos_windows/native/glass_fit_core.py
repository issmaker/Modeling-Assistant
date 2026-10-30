"""Blender-independent dimensional preflight for non-destructive depth changes."""
from __future__ import annotations
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class GlassFit:
    gap: float
    glass_offset: float
    interior_offset: float
    warnings: tuple[str, ...]


def fit_to_frame_depth(depth: float, gap: float, glass_offset: float,
                       interior_offset: float, full: bool) -> GlassFit:
    """Keep a changed depth authoritative and fit its dependent dimensions.

    A double pane remains symmetric. No glass can overrun the frame envelope.
    The caller must log the explicit corrections and update settings before rebuild.
    """
    values = (depth, gap, glass_offset, interior_offset)
    if not all(math.isfinite(v) for v in values):
        raise ValueError('Все размеры должны быть конечными числами')
    if depth < 0.005:
        raise ValueError('Глубина рамы должна быть не меньше 0,005')
    if gap <= 0:
        raise ValueError('Расстояние между стеклами должно быть больше нуля')
    warnings = []
    # Leave a genuine margin on each face, including when depth becomes tiny.
    max_gap = min(depth * 0.8, depth - 0.002)
    result_gap = gap
    if full and gap > max_gap:
        result_gap = max_gap
        warnings.append('GLASS_GAP_FITTED')
    half = depth * 0.5
    safe_margin = 0.001
    allowed_offset = max(0.0, half - safe_margin - (result_gap * 0.5 if full else 0.0))
    result_glass_offset = max(-allowed_offset, min(allowed_offset, glass_offset))
    if abs(result_glass_offset - glass_offset) > 1e-9:
        warnings.append('GLASS_OFFSET_FITTED')
    # Interior starts 1/4 from facade: allowable offset [-depth/4, +3*depth/4].
    result_interior_offset = max(-depth*0.25, min(depth*0.75, interior_offset))
    if abs(result_interior_offset - interior_offset) > 1e-9:
        warnings.append('INTERIOR_OFFSET_FITTED')
    return GlassFit(result_gap, result_glass_offset, result_interior_offset, tuple(warnings))
