"""Re-entrancy and transaction ownership guard for production Build.

Blender operators can indirectly trigger depsgraph handlers, timers or UI callbacks.
A second Build entering while the first transaction is still mutating Result state is
unsafe: it can observe half-swapped datablocks and invalidate Undo ownership.

This module deliberately stores only lightweight runtime tokens.  No Blender ID or
BMesh object is retained between transactions.
"""
from __future__ import annotations

from contextlib import contextmanager
import uuid

_ACTIVE_KEYS = set()


class BuildReentryError(RuntimeError):
    pass


def _safe_pointer(value):
    if value is None:
        return 0
    try:
        pointer = value.as_pointer()
        return int(pointer) if pointer else 0
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return 0


def transaction_key(context):
    """Return a bounded runtime key without persisting Blender RNA wrappers."""
    wm = getattr(context, "window_manager", None)
    scene = getattr(context, "scene", None)
    wm_ptr = _safe_pointer(wm)
    scene_ptr = _safe_pointer(scene)
    if wm_ptr or scene_ptr:
        return (wm_ptr, scene_ptr)
    # Test doubles/headless fallback: identity exists only for this process.
    return (id(wm), id(scene))


def transaction_is_active(context):
    key = transaction_key(context)
    wm = getattr(context, "window_manager", None)
    try:
        rna_active = bool(getattr(wm, "uvwall_build_transaction_active", False))
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        rna_active = False
    return key in _ACTIVE_KEYS or rna_active


def reset_build_guard(context=None):
    """Clear volatile locks after Undo/Redo/load/unregister.

    The set contains only pointer integers/``id`` values; clearing never mutates
    production geometry.  A WM flag is reset only when a live context is supplied.
    """
    _ACTIVE_KEYS.clear()
    if context is None:
        return
    wm = getattr(context, "window_manager", None)
    if wm is not None:
        try:
            wm.uvwall_build_transaction_active = False
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass


@contextmanager
def build_transaction(context):
    """Single-entry transaction lock, always released with ``finally`` semantics."""
    key = transaction_key(context)
    wm = getattr(context, "window_manager", None)
    if transaction_is_active(context):
        raise BuildReentryError("UV Wall: повторный Build во время активной transaction запрещён")
    token = uuid.uuid4().hex
    _ACTIVE_KEYS.add(key)
    if wm is not None:
        try:
            wm.uvwall_build_transaction_active = True
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    try:
        yield token
    finally:
        _ACTIVE_KEYS.discard(key)
        if wm is not None:
            try:
                wm.uvwall_build_transaction_active = False
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
