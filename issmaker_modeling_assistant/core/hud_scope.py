"""ISSMAKER Wave17: exact one-window, one-View3D-area HUD scope.

No bpy import and no persistent references to Blender RNA wrappers.
"""


def context_scope(context):
    """Return the owning (window_pointer, area_pointer), or None (fail closed)."""
    try:
        window = getattr(context, 'window', None)
        area = getattr(context, 'area', None)
        if window is None or getattr(area, 'type', None) != 'VIEW_3D':
            return None
        wptr, aptr = int(window.as_pointer()), int(area.as_pointer())
        if wptr <= 0 or aptr <= 0:
            return None
        return (wptr, aptr)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError, OverflowError):
        return None


def matches(expected, context):
    return expected is not None and context_scope(context) == expected


def modal_owner_valid(owner_scope, context, owner_generation, current_generation):
    """Fail-closed check for a modal's immutable owner and session generation.

    Never retains or dereferences stale Blender Area/Window RNA references.
    Pointers are session-local IDs, not persistent object identity across files;
    the generation guard forbids a prior owner from controlling a rebound HUD.
    """
    if (not isinstance(owner_scope, tuple) or len(owner_scope) != 2
            or any(type(ptr) is not int or ptr <= 0 for ptr in owner_scope)
            or type(owner_generation) is not int or type(current_generation) is not int
            or owner_generation <= 0 or current_generation <= 0
            or owner_generation != current_generation):
        return False
    return matches(owner_scope, context)
