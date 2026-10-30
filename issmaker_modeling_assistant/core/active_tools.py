"""Concrete session ownership adapters. No geometry modifications."""
import bpy
from . import session_switch, window_visibility
from .. import modeling_tools
from ..tools import brick_hole as brick
from ..tools import collision_inspector as collision
from ..tools.brick_hole import integration as brick_i
from ..tools.collision_inspector import viewport_ui as collision_ui
from ..tools.uv_wall import integration as wall_i
from ..tools.dwos_windows import integration as dwos_i
from ..tools.dwos_windows import native as dwos


def _brick_active():
    from ..tools.brick_hole import issue_inspector, seam_inspector
    # Fixture navigator owns a separate GPU handler; stopping Preview alone
    # previously left it running after handoff to a different tool.
    return bool(brick_i.is_preview_active() or issue_inspector.is_active() or seam_inspector.is_active() or
                getattr(brick, '_FIXTURE_NAV_HUD', None) is not None)


def _stop_brick(context):
    brick.stop_interactive_preview()
    brick.stop_fixture_nav_hud()
    from ..tools.brick_hole import issue_inspector, seam_inspector
    issue_inspector.stop()
    seam_inspector.stop()
    stopped = not _brick_active()
    if stopped:
        window_visibility.invalidate_matching('BRICK_')
    return stopped


def _collision_active(context):
    # Modal pickers can still own input when an overlay visibility flag is OFF.
    sessions = getattr(collision, '_PICKER_SESSIONS', {})
    if sessions:
        return True
    settings = getattr(getattr(context, 'scene', None), 'collision_inspector', None)
    if settings and (settings.overlay_enabled or settings.overlay_pick_mode):
        return True
    # Multiple Blender scenes can retain a live inspector or overlay.
    try:
        return any(bool(getattr(scene, 'collision_inspector', None) and
                        (scene.collision_inspector.overlay_enabled or
                         scene.collision_inspector.overlay_pick_mode))
                   for scene in bpy.data.scenes)
    except (AttributeError, ReferenceError, RuntimeError):
        return False



def _stop_collision(context):
    failures = []
    try:
        scenes = tuple(bpy.data.scenes)
    except (AttributeError, ReferenceError, RuntimeError):
        scenes = (getattr(context, 'scene', None),)
    for scene in scenes:
        if scene is None:
            continue
        try:
            settings = getattr(scene, 'collision_inspector', None)
            if settings is not None:
                settings.overlay_enabled = False
                settings.overlay_pick_mode = False
        except Exception as exc:
            failures.append('scene overlay: ' + str(exc)[:150])
    # A picker receives an invalid session token and exits on its next event.
    # Do not mistake hidden overlay buttons for a stopped modal.
    try:
        collision._PICKER_SESSIONS.clear()
    except Exception as exc:
        failures.append('picker sessions: ' + str(exc)[:150])
    for callback in (collision_ui.close_marker_menu,
                     collision_ui.close_footer_group_menu,
                     collision_ui.redraw):
        try:
            callback()
        except Exception as exc:
            failures.append('viewport cleanup: ' + str(exc)[:150])
    stopped = not _collision_active(context)
    if stopped:
        window_visibility.invalidate_matching('COLLISION_')
    if failures:
        raise RuntimeError(' | '.join(failures)[:480])
    return stopped


def _wall_active(context):
    native=wall_i._WALL
    if native is None:
        return False
    controller=native._viewport_controller
    return bool(tuple(controller._ACTIVE_SESSIONS) or
                getattr(context.window_manager, 'uvwall_viewport_active', False))


def _stop_wall(context):
    native=wall_i._WALL
    if native is None:
        return True
    controller=native._viewport_controller
    # ISSUE-017: a failed SURFACE/CORNICE drag restore used to interrupt this
    # loop before _finish(), leaving GPU handlers, timers and modal ownership
    # alive. Always *attempt* session cleanup even when rollback raises.
    failures=[]
    for session in tuple(controller._ACTIVE_SESSIONS):
        try:
            if getattr(session, '_drag_kind', None) is not None:
                session._restore_drag()
        except Exception as exc:
            failures.append('drag rollback: '+type(exc).__name__+': '+str(exc))
        finally:
            try:
                session._finish(context)
            except Exception as exc:
                failures.append('session cleanup: '+type(exc).__name__+': '+str(exc))
    try:
        context.window_manager.uvwall_viewport_active=False
    except Exception as exc:
        failures.append('viewport flag: '+type(exc).__name__+': '+str(exc))
    stopped = not _wall_active(context)
    if stopped:
        window_visibility.invalidate_matching('UV_WALL_')
    if failures:
        # Do not claim that partial rollback was safe; stop_others records the
        # cause and rejects this target handoff. Previous HUD was still closed.
        raise RuntimeError('UV Wall аварийно завершён: '+ ' | '.join(failures)[:480])
    return stopped


def _dwos_active():
    return bool(getattr(dwos.composer, '_ACTIVE', None))


def _stop_dwos(context):
    # Native API disarms draw and marks the existing modal for FINISHED on its
    # next event. It does not accept or commit a partially edited numeric field.
    dwos.composer.remove_active_draw_handler()
    stopped = not _dwos_active()
    if stopped:
        window_visibility.invalidate_matching('DWOS_')
    return stopped


def _quick_select_active():
    return bool(modeling_tools.linked_active())


def _stop_quick_select(context):
    modeling_tools.stop_linked_runtime()
    stopped = not _quick_select_active()
    if stopped:
        window_visibility.invalidate_matching('QUICK_SELECT_')
    return stopped


def ownership_snapshot(context):
    """Tool-specific active flags only. No BMesh reads or object mutations."""
    getters = (
        ('BRICK_HOLE', _brick_active),
        ('COLLISION', lambda: _collision_active(context)),
        ('UV_WALL', lambda: _wall_active(context)),
        ('DWOS', _dwos_active),
        ('QUICK_SELECT', _quick_select_active),
    )
    result = {}
    for name, getter in getters:
        try:
            result[name] = {'active': bool(getter())}
        except Exception as exc:
            result[name] = {'active': None,
                            'error': f'{type(exc).__name__}: {exc}'[:160]}
    return {'owners': result, 'coordinator': session_switch.snapshot()}


def switch_to(tool_id, context):
    """Close all other tools BEFORE target can_start; even rejected targets close previous."""
    adapters=(
        ('BRICK_HOLE', _brick_active, _stop_brick),
        ('COLLISION', lambda: _collision_active(context), _stop_collision),
        ('UV_WALL', lambda: _wall_active(context), _stop_wall),
        ('DWOS', _dwos_active, _stop_dwos),
        ('QUICK_SELECT', _quick_select_active, _stop_quick_select),
    )
    return session_switch.stop_others(tool_id, context, adapters)
