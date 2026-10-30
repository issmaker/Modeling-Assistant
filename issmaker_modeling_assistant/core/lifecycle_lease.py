"""Cross-reload resource leases for ISSMAKER Viewport/GPU owners.

Blender keeps Python callback references across script reloads. Module globals do
not. A lease stored in driver_namespace lets the new incarnation invalidate
old timers and recover draw-handler tokens without retaining any bpy Mesh IDs.
This is a lifecycle guard, never a geometry validator.
"""
from __future__ import annotations

_PREFIX = "ISSMAKER_PPA_LIFECYCLE_v1:"
_FALLBACK = {}


def _namespace():
    try:
        import bpy
        namespace = bpy.app.driver_namespace
        if namespace is not None:
            return namespace
    except (ImportError, RuntimeError, AttributeError, ReferenceError):
        pass
    return _FALLBACK


def claim(owner):
    """Replace owner lease; return (new epoch identity, previous lease)."""
    ns = _namespace()
    key = _PREFIX + str(owner)
    previous = ns.get(key)
    token = object()
    ns[key] = {"token": token, "handles": [], "timer": None}
    return token, previous if isinstance(previous, dict) else None


def alive(owner, token):
    if token is None:
        return False
    row = _namespace().get(_PREFIX + str(owner))
    return isinstance(row, dict) and row.get("token") is token


def owned(owner, token):
    row = _namespace().get(_PREFIX + str(owner))
    return row if isinstance(row, dict) and row.get("token") is token else None


def retire(owner, token):
    ns = _namespace()
    key = _PREFIX + str(owner)
    if alive(owner, token):
        ns.pop(key, None)
        return True
    return False


def snapshot(owner):
    row = _namespace().get(_PREFIX + str(owner))
    return {"leased": isinstance(row, dict),
            "handle_count": len(row.get("handles", ())) if isinstance(row, dict) else 0,
            "timer_recorded": bool(row.get("timer")) if isinstance(row, dict) else False}
