"""Pure fail-closed geometric checks for Brick Hole preview input.

This is NOT a replacement for Blender's BMesh topology validation.
"""
import math


def finite_vec3(value, label='координата'):
    try:
        vec = tuple(float(x) for x in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{label}: требуется XYZ') from exc
    if len(vec) != 3 or not all(math.isfinite(x) for x in vec):
        raise ValueError(f'{label}: недопустимые XYZ')
    return vec


def checked_edge(a, b, *, minimum_length=1e-9):
    a, b = finite_vec3(a, 'начало ребра'), finite_vec3(b, 'конец ребра')
    if sum((x-y)**2 for x, y in zip(a,b)) <= minimum_length**2:
        raise ValueError('Preview: ребро нулевой длины')
    return a, b


def checked_direction(value):
    vector = finite_vec3(value, 'направление')
    length = math.sqrt(sum(x*x for x in vector))
    if length < 1e-12:
        raise ValueError('Preview: нулевое направление')
    return vector


def checked_depth(value, maximum=50.0):
    try:
        depth = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Preview: глубина должна быть числом') from exc
    if not math.isfinite(depth) or abs(depth) > maximum or abs(depth) < 0.001:
        raise ValueError('Preview: глубина должна быть от 0,001 до 50 м по модулю')
    return depth
