"""Pure planning for Blender-native Brick Hole FACE extrusion (Pass 15.49.4).

This is NOT a custom geometry solver. It chooses one of Blender's native
macros, validates world-space manual direction and returns operator parameters.
No bpy/mathutils imports: invariant tests run independently of Blender.
"""
import math
from typing import NamedTuple


class NativeFacePlan(NamedTuple):
    mode: str
    world_displacement: tuple[float, float, float] | None
    normal_alignment_min: float | None


def plan_native_faces(source, vector, depth, face_normals=()):
    """Choose ALONG_NORMALS_EVEN or REGION_TRANSLATE_WORLD.

    A manual vector is a *direction*, never a new face normal. Its world-space
    displacement has physical length abs(depth), with negative depth reversing it.
    Near-tangential directions must fail before any geometry transaction.
    """
    if isinstance(depth, bool):
        raise ValueError('Глубина должна быть числом')
    try:
        depth = float(depth)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Глубина должна быть числом') from exc
    if not math.isfinite(depth) or abs(depth) < 1.0e-6 or abs(depth) > 50.0:
        raise ValueError('Глубина должна быть конечной и от 0.000001 до 50 м')
    if source == 'AUTO':
        return NativeFacePlan('ALONG_NORMALS_EVEN', None, None)
    if source not in ('FACE', 'GLOBAL', 'MANUAL'):
        raise ValueError('Неизвестный режим направления Brick Hole')
    try:
        direction = tuple(float(c) for c in vector)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Вектор должен содержать числовые XYZ') from exc
    if len(direction) != 3 or not all(math.isfinite(x) for x in direction):
        raise ValueError('Вектор должен содержать конечные XYZ')
    scale = max(abs(x) for x in direction)
    if scale <= 1.0e-12:
        raise ValueError('Нулевое ручное направление недопустимо')
    scaled = tuple(x / scale for x in direction)
    norm = math.sqrt(sum(x*x for x in scaled))
    unit = tuple(x / norm for x in scaled)
    min_alignment = None
    for index, face_normal in enumerate(face_normals):
        try:
            n = tuple(float(c) for c in face_normal)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('Некорректная нормаль выбранной грани') from exc
        if len(n) != 3 or not all(math.isfinite(x) for x in n):
            raise ValueError('Некорректная нормаль выбранной грани')
        n_scale = max(abs(x) for x in n)
        if n_scale <= 1.0e-12:
            raise ValueError('Нулевая нормаль выбранной грани')
        q = tuple(x / n_scale for x in n)
        n_len = math.sqrt(sum(x*x for x in q))
        alignment = abs(sum(unit[i] * q[i] / n_len for i in range(3)))
        if not math.isfinite(alignment) or alignment < 1.0e-4:
            raise ValueError('Ручная ось почти лежит в плоскости грани %d: экструзия вырождается' % (index + 1))
        min_alignment = alignment if min_alignment is None else min(min_alignment, alignment)
    displacement = tuple(c * depth for c in unit)
    if not all(math.isfinite(x) for x in displacement):
        raise ValueError('Смещение переполняет допустимые координаты')
    return NativeFacePlan('REGION_TRANSLATE_WORLD', displacement, min_alignment)
