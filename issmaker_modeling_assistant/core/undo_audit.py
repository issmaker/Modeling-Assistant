"""Passive ISSMAKER audit of *global* Blender undo/redo.

Never intercept Ctrl+Z, push an undo step, change geometry or mutate Scene RNA.
Other modules own their own modal parameter histories. Collision already has
its own undo_post invalidation; this hook does not replace that logic.
"""
import bpy
from bpy.app.handlers import persistent
from . import action_trace

_REGISTERED = False
_PRE = {}
_HISTORY = []  # bounded, serializable summaries, no live BMesh/ID references
_MAX_HISTORY = 80
_STAGE_ERRORS = []  # bounded, text-only; never own bpy IDs
_EVENT_GUARD = set()


def _state():
    """Capture identities as numbers only; never retain Blender RNA pointers."""
    try:
        context = bpy.context
        obj = getattr(context, 'active_object', None)
        mesh = getattr(obj, 'data', None) if getattr(obj, 'type', None) == 'MESH' else None
        data = dict(mode=str(getattr(context,'mode','UNKNOWN')),
                    object_name=str(getattr(obj,'name','')),
                    object_ptr=obj.as_pointer() if obj is not None else 0,
                    mesh_ptr=mesh.as_pointer() if mesh is not None else 0)
        if mesh is not None:
            # edit mesh counts aren't assumed to be synchronized: identities are the safe signal.
            data.update(vertices=len(mesh.vertices), edges=len(mesh.edges), faces=len(mesh.polygons))
        return data
    except (ReferenceError, RuntimeError, AttributeError, TypeError) as exc:
        return {'error':type(exc).__name__+': '+str(exc)[:120]}


def _record(phase, kind, before=None):
    current = _state()
    changed = bool(before is not None and before != current)
    row = {'phase':phase,'operation':kind,'changed':changed,'before':before,'after':current}
    _HISTORY.append(row)
    del _HISTORY[:-_MAX_HISTORY]
    _trace(kind+'_'+phase, 'UNDO', 'changed='+str(changed)+'; '+str(current)[:340])
    return changed



def _fault(stage, exc):
    """Failure evidence must never interrupt Blender's undo hook chain."""
    try:
        message = f'{stage}: {type(exc).__name__}: {str(exc)[:240]}'
    except Exception:
        message = f'{stage}: unknown error'
    _STAGE_ERRORS.append(message)
    del _STAGE_ERRORS[:-40]
    return message


def _trace(event, subsystem, message):
    try:
        action_trace.record(event, subsystem, message)
    except Exception as exc:
        _fault('action_trace.record', exc)


def _stage(name, callback):
    try:
        return callback()
    except Exception as exc:
        _trace('UNDO_STAGE_FAILED', name, _fault(name, exc))
        return None


def _disarm_dwos(kind):
    from ..tools.dwos_windows.native import composer
    if getattr(composer, '_ACTIVE', None) is not None:
        composer.remove_active_draw_handler()
        _trace('GLOBAL_UNDO_DISARM_DWOS', 'DWOS', kind)


def _invalidate_dwos(kind):
    from ..tools.dwos_windows.native import dwos_history
    dwos_history.clear()
    _trace('GLOBAL_UNDO_INVALIDATE_DWOS_HISTORY', 'DWOS', kind)


def _invalidate_unified_nav(kind):
    from .. import unified_issue_navigator
    unified_issue_navigator.mark_stale()
    _trace('GLOBAL_UNDO_INVALIDATE_UNIFIED_NAV', 'ISSUE_NAV', kind)


def _redraw():
    """One dead Window/Area cannot starve the rest of the open workspaces."""
    wm = getattr(bpy.context, 'window_manager', None)
    if wm is None:
        return
    try:
        windows = tuple(wm.windows)
    except (ReferenceError, RuntimeError, AttributeError, TypeError) as exc:
        _fault('redraw.windows', exc)
        return
    for window in windows:
        try:
            areas = tuple(window.screen.areas)
        except (ReferenceError, RuntimeError, AttributeError, TypeError) as exc:
            _fault('redraw.window', exc)
            continue
        for area in areas:
            try:
                if area.type in {'VIEW_3D','IMAGE_EDITOR','NODE_EDITOR'}:
                    area.tag_redraw()
            except (ReferenceError, RuntimeError, AttributeError, TypeError) as exc:
                _fault('redraw.area', exc)


def _before(kind):
    """Global Blender PRE: disarm volatile draw state before RNA replacement."""
    key = (kind, 'PRE')
    if key in _EVENT_GUARD:
        return
    _EVENT_GUARD.add(key)
    try:
        _stage('disarm_dwos', lambda: _disarm_dwos(kind))
        _PRE[kind] = _stage('capture_pre', _state)
        _stage('audit_pre', lambda: _record('PRE',kind))
    finally:
        _EVENT_GUARD.discard(key)


def _after(kind):
    """POST cleanup failures are isolated; no Blender IDs survive in cache."""
    key = (kind, 'POST')
    if key in _EVENT_GUARD:
        return
    _EVENT_GUARD.add(key)
    try:
        before = _PRE.pop(kind, None)
        _stage('audit_post', lambda: _record('POST', kind, before))
        _stage('invalidate_dwos', lambda: _invalidate_dwos(kind))
        _stage('invalidate_unified_nav', lambda: _invalidate_unified_nav(kind))
        _stage('redraw', _redraw)
    finally:
        _EVENT_GUARD.discard(key)


@persistent
def _undo_pre(_): _before('UNDO')


@persistent
def _undo_post(_): _after('UNDO')


@persistent
def _redo_pre(_): _before('REDO')


@persistent
def _redo_post(_): _after('REDO')


HOOKS = (('undo_pre',_undo_pre),('undo_post',_undo_post),
         ('redo_pre',_redo_pre),('redo_post',_redo_post))


def _remove_stale(hooks, callback):
    """Remove callbacks from previous *module incarnations* after hot reload."""
    for old in tuple(hooks):
        if (getattr(old, '__module__', '') == __name__ and
                getattr(old, '__name__', '') == callback.__name__):
            try:
                hooks.remove(old)
            except (ValueError, RuntimeError, ReferenceError):
                pass


def register():
    global _REGISTERED
    # A module-global boolean is insufficient after an in-place ZIP update;
    # Blender's handler list may still retain callbacks from the old module.
    # Never rely on _REGISTERED to skip verifying actual handler membership.
    installed = []
    try:
        for collection, callback in HOOKS:
            hooks = getattr(bpy.app.handlers, collection)
            _remove_stale(hooks, callback)
            hooks.append(callback)
            installed.append((collection, callback))
    except Exception:
        for collection, callback in reversed(installed):
            try:
                hooks = getattr(bpy.app.handlers, collection)
                while callback in hooks:
                    hooks.remove(callback)
            except Exception:
                pass
        _REGISTERED = False
        raise
    _REGISTERED = True


def unregister():
    global _REGISTERED
    for collection, callback in HOOKS:
        try:
            _remove_stale(getattr(bpy.app.handlers, collection), callback)
        except (AttributeError, ValueError, RuntimeError, ReferenceError):
            pass
    _REGISTERED = False
    _PRE.clear()
    _EVENT_GUARD.clear()


def snapshot():
    return list(_HISTORY)


def lifecycle_status():
    """Pure-data Support ZIP diagnostics, no live Blender ID references."""
    return {
        'registered': bool(_REGISTERED),
        'history_rows': len(_HISTORY),
        'pending_events': sorted(_PRE),
        'guarded_events': [str(x) for x in sorted(_EVENT_GUARD)],
        'stage_errors': list(_STAGE_ERRORS),
        'handler_counts': {
            name: sum(1 for fn in tuple(getattr(bpy.app.handlers, name, ()))
                      if getattr(fn, '__module__', None) == __name__
                      and getattr(fn, '__name__', None) == callback.__name__)
            for name, callback in HOOKS
        },
    }
