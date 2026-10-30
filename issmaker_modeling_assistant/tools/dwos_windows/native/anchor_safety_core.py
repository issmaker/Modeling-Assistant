"""ISSMAKER 1.8.8.41: pure, fail-closed guard for a DWOS rebuild anchor.

It only compares positions; it never updates host, Mesh, Blender transforms or
legacy metadata. An existing window cannot silently jump several metres just
because a saved opening anchor unexpectedly decodes to another floor.
"""
from __future__ import annotations

import math


def anchor_displacement(old_xyz, new_xyz, window_height):
    """Return (distance_m, allowed_m) or reject malformed geometry inputs.

    Allowed: 20% of the opening height, bounded between 0.35 and 0.75 m.
    This is an emergency teleport guard, NOT a geometry tolerance or snap.
    """
    try:
        before = tuple(float(v) for v in old_xyz)
        after = tuple(float(v) for v in new_xyz)
        height = float(window_height)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('DWOS: некорректные координаты привязки окна') from exc
    if len(before) != 3 or len(after) != 3 or not all(map(math.isfinite, (*before, *after, height))):
        raise ValueError('DWOS: привязка содержит NaN, бесконечность или неверную размерность')
    if height <= 0.:
        raise ValueError('DWOS: высота окна должна быть положительной')
    distance = math.dist(before, after)
    if not math.isfinite(distance):
        raise ValueError('DWOS: расстояние изменения привязки переполнено')
    limit = max(0.35, min(0.75, height * 0.2))
    return distance, limit


def require_stable_anchor(old_xyz, new_xyz, window_height, *, existing=True):
    """Gate only an already published window; new windows are unaffected.

    Return shift metres. For large movement, reject before any Mesh commit and
    ask for a fresh Host/opening detection rather than silently repositioning.
    """
    if not existing:
        return 0.
    distance, limit = anchor_displacement(old_xyz, new_xyz, window_height)
    if distance > limit + 1e-8:
        raise ValueError(
            f'DWOS_ANCHOR_JUMP: восстановленная привязка переместит окно на '
            f'{distance:.3f} м (допустимо {limit:.3f} м). '
            'Обновление отменено до изменения Mesh. Проверьте положение стены '
            'и выполните «Найти проёмы» повторно; пришлите Support ZIP.'
        )
    return distance
