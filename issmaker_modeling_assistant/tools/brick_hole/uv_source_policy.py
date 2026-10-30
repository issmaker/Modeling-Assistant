"""Source-wall UV density policy for Brick Hole (pure Python, no bpy).

The source wall owns its authored UV, including non-standard texel density.
Newly created reveal faces must use the prescribed 1000 px/m instead.
"""
import math


def corrected_reveal_uv_edge(source_a, source_b, world_length_m, resolution,
                             target_pixels_per_meter=1000.0,
                             density_tolerance=5.0):
    """Return a 1000 px/m oriented UV edge without modifying the source UV.

    Preserve source orientation (incl. reflected winding, not correctable by
    density alone), and anchor the *midpoint* to minimise maximum seam shift.
    A source mismatch is advisory; zero/invalid source UV direction is not.
    """
    a = tuple(float(v) for v in source_a)
    b = tuple(float(v) for v in source_b)
    length = float(world_length_m)
    size = float(resolution)
    ppm = float(target_pixels_per_meter)
    if (len(a) != 2 or len(b) != 2 or
            not all(math.isfinite(v) for v in (*a, *b, length, size, ppm)) or
            length <= 1e-9 or size <= 0 or ppm <= 0):
        raise ValueError('Недопустимая геометрия или UV для расчёта откоса')
    source_length = math.dist(a, b)
    if source_length <= 1e-12:
        raise ValueError('UV исходного ребра вырождена (направление не определено)')
    real_density = source_length * size / length
    corrected_length = length * ppm / size
    direction = ((b[0] - a[0]) / source_length,
                 (b[1] - a[1]) / source_length)
    midpoint = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
    half_span = corrected_length * 0.5
    updated_a = (midpoint[0] - half_span * direction[0],
                 midpoint[1] - half_span * direction[1])
    updated_b = (midpoint[0] + half_span * direction[0],
                 midpoint[1] + half_span * direction[1])
    source_mismatch = abs(real_density - ppm) > density_tolerance
    max_seam_shift_pixels = math.dist(a, updated_a) * size
    return {
        'uv_a': updated_a,
        'uv_b': updated_b,
        'source_density': real_density,
        'target_density': ppm,
        'needs_warning': source_mismatch,
        'max_seam_shift_pixels': max_seam_shift_pixels,
    }


def source_uv_warning_message(summary):
    """Human-readable advisory shared by Preview and final report."""
    count = int(summary.get('uv_warning_count', 0))
    if not count:
        return ''
    example = float(summary['uv_warning_sample'][0])
    return (f'UV стены: {example:.2f} px/м (несовпадений: {count}); '
            'проём создадим, новые откосы — 1000 px/м. '
            'На стыке текстуры возможен шов; UV стены не меняем.')
