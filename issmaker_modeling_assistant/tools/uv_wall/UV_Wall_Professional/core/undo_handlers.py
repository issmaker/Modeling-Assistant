"""Undo/Redo reconciliation: invalidate ephemeral caches, never rebuild in handlers."""
import bpy
from bpy.app.handlers import persistent
from .performance import clear_performance_caches
from .result_lifecycle import shutdown_result_lifecycle
from .build_guard import reset_build_guard

# Volatile; increments only after Blender has actually processed history.
history_generation = 0
_UNDO_ERRORS = []  # text-only bounded evidence, no live Blender data references



def _cleanup_stage(stage, func):
    try:
        func()
    except Exception as exc:
        _UNDO_ERRORS.append(f'{stage}: {type(exc).__name__}: {str(exc)[:200]}')
        del _UNDO_ERRORS[:-32]


def lifecycle_status():
    return {'history_generation': history_generation,
            'cleanup_errors': list(_UNDO_ERRORS)}


def _on_history_changed(*_args):
    global history_generation
    history_generation += 1
    # IDs may be resurrected with new Python wrappers; leave Blender DNA alone.
    _cleanup_stage("result_lifecycle", shutdown_result_lifecycle)
    _cleanup_stage("performance_cache", clear_performance_caches)
    _reset_guard = globals().get("reset_build_guard")
    if callable(_reset_guard):
        _cleanup_stage("build_guard", lambda: _reset_guard(bpy.context))
    try:
        wm = getattr(bpy.context, 'window_manager', None)
    except (ReferenceError, RuntimeError, AttributeError):
        wm = None
    if wm is not None:
        try:
            wm.uvwall_viewport_commit_active = False
            wm.uvwall_build_transaction_active = False
            wm.uvwall_pending_definition_json = ''
            wm.uvwall_pending_surface_edits_json = ''
        except (AttributeError, RuntimeError, ReferenceError):
            pass
    # Undo can invalidate Area/Screen wrappers mid-redraw (especially after
    # closing a window). Never break Blender's remaining undo_post callbacks.
    try:
        windows = tuple(getattr(wm, 'windows', ())) if wm is not None else ()
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        windows = ()
    for win in windows:
        try:
            for area in win.screen.areas:
                try:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                except (AttributeError, ReferenceError, RuntimeError, TypeError):
                    continue
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            continue

@persistent
def uvwall_undo_post(*args):
    _on_history_changed(*args)

@persistent
def uvwall_redo_post(*args):
    _on_history_changed(*args)


def _remove_our_handlers(handlers, name):
    """Remove stale callbacks from previous in-session module reloads only."""
    for fn in tuple(handlers):
        if (getattr(fn, '__module__', None) == __name__
                and getattr(fn, '__name__', None) == name):
            handlers.remove(fn)


def register():
    for handlers, fn in ((bpy.app.handlers.undo_post, uvwall_undo_post),
                         (bpy.app.handlers.redo_post, uvwall_redo_post)):
        _remove_our_handlers(handlers, fn.__name__)
        handlers.append(fn)


def unregister():
    for handlers, fn in ((bpy.app.handlers.undo_post, uvwall_undo_post),
                         (bpy.app.handlers.redo_post, uvwall_redo_post)):
        _remove_our_handlers(handlers, fn.__name__)
