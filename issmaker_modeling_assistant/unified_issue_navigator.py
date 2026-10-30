"""Wave 8 read-only consolidated Navigator: no Mesh edits, no modal ownership.

Existing UV, Collision, Brick and DWOS inspectors remain authoritative.
This adapter publishes bounded numeric snapshots only on explicit refresh.
"""
import bpy
from bpy.types import Operator, Panel, GizmoGroup, Gizmo
from bpy.app.handlers import persistent

from .core import unified_issues as core
from .core import lifecycle_lease
from .core import issue_lifecycle, issue_ui_policy, ui_layout_scaling, bottom_issue_layout, hud_scope

NAVIGATOR_BUILD_ID = 'ISSMAKER-PPA-WAVE19-WAVE20-20261027'

_STATE = {'issues': (), 'total': 0, 'omitted': 0, 'sources': {},
          'source_errors': {}, 'truncated': False}
_INDEX = -1
_VISIBLE = False
_LAST_ERROR = ''
_STALE = False
_SHOW_DETAILS = False
_SOURCE_TRUNCATION = {}
_VIEW_ROWS = ()
_STALE_REASON = ''
_CHANGED_EVENTS = 0
_SCOPE_PTR = None
_HOOK_TOKEN = None
_HOOK_OWNER = 'ISSMAKER_UNIFIED_ISSUE_WITNESS_HOOKS_W9'
_GPU = None
_GPU_TOKEN = None

# The bottom HUD is optional and shares exactly the same issue state as N-panel.
_BOTTOM_ENABLED = False
_BOTTOM_SCOPE = None  # Window/VIEW_3D AREA pointer pair, never a bpy RNA reference.
_BOTTOM_SCOPE_REASON = 'NOT_OPEN'
_BOTTOM_GENERATION = 0
_BOTTOM_GPU = None
_BOTTOM_GPU_TOKEN = None
_BOTTOM_OWNER = 'ISSMAKER_UNIFIED_ISSUE_BOTTOM_HUD_W11'
_BOTTOM_LAST_LAYOUT = {'status': 'DISABLED', 'buttons': []}
_BOTTOM_ERROR = ''
_BOTTOM_DRAG_ACTIVE = False
_BOTTOM_DRAG_OWNER = None
_BOTTOM_DRAG_BEFORE = None
_BOTTOM_DRAG_SERIAL = 0
_BOTTOM_GRIP_ACTIVE = False
_GRIP_HOVER_UNTIL = 0.0
_GRIP_STATS = {'starts': 0, 'commits': 0, 'cancels': 0, 'last': 'IDLE'}
_HUD_LAB_RESULT = {'status': 'NOT_RUN', 'native_gui': 'NOT_TESTED_GUI'}


def _grip_record(event):
    # Fixed size; never allocate unbounded event logs in a modal callback.
    if event == 'START':
        _GRIP_STATS['starts'] += 1
    elif event == 'COMMIT':
        _GRIP_STATS['commits'] += 1
    elif event == 'CANCEL':
        _GRIP_STATS['cancels'] += 1
    _GRIP_STATS['last'] = event


def _local_navigator_busy(context):
    """Modeling tools and local issue navigators have input priority."""
    try:
        from . import view_tools, weld_preview
        if (getattr(context, 'mode', '') == 'EDIT_MESH' and
                (weld_preview.context_matches(context) or
                 (view_tools.issues_active() and view_tools.issues_context_matches(context)))):
            return True
        from .core import active_tools
        owners = active_tools.ownership_snapshot(context).get('owners', {})
        # Failure to read a tool's state is NOT proof that it is closed.
        return any(row.get('active') is not False for row in owners.values())
    except (AttributeError, ReferenceError, RuntimeError, ImportError):
        return True  # Fail closed to avoid stealing input from a modal tool.


def _bottom_layout(context):
    """Shared native hit/render geometry, only from VIEW_3D WINDOW region."""
    if not _BOTTOM_ENABLED or not _VIEW_ROWS or not hud_scope.matches(_BOTTOM_SCOPE, context):
        return None
    region = getattr(context, 'region', None)
    if region is None or getattr(region, 'type', None) != 'WINDOW':
        return None
    if _local_navigator_busy(context):
        return None
    try:
        from . import ui
        base = ui._nav_layout(context)  # preserves existing dock collision logic
        position = getattr(getattr(context, 'window_manager', None),
                           'ima_unified_issue_hud_position', 'CENTER')
        return bottom_issue_layout.shift_layout(
            base, region.width, region.height, position,
            custom_x=getattr(context.window_manager,'ima_unified_issue_custom_x',.5),
            custom_y=getattr(context.window_manager,'ima_unified_issue_custom_y',.18))
    except (AttributeError, ReferenceError, RuntimeError, ValueError, TypeError):
        return None


def _bottom_hide(reason='CLOSED'):
    global _BOTTOM_ENABLED, _BOTTOM_GENERATION, _BOTTOM_SCOPE, _BOTTOM_SCOPE_REASON
    _BOTTOM_SCOPE = None
    _BOTTOM_SCOPE_REASON = str(reason)[:64]
    _BOTTOM_ENABLED = False
    _BOTTOM_GENERATION += 1
    global _BOTTOM_DRAG_ACTIVE, _BOTTOM_DRAG_OWNER, _BOTTOM_DRAG_BEFORE, _BOTTOM_GRIP_ACTIVE
    if _BOTTOM_GRIP_ACTIVE and _BOTTOM_DRAG_ACTIVE:
        _grip_record('CANCEL')
    _BOTTOM_GRIP_ACTIVE = False
    if _BOTTOM_DRAG_ACTIVE and _BOTTOM_DRAG_BEFORE is not None:
        try:
            wm = bpy.context.window_manager
            wm.ima_unified_issue_hud_position, wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y = _BOTTOM_DRAG_BEFORE
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass
    _BOTTOM_DRAG_ACTIVE = False
    _BOTTOM_DRAG_OWNER = None
    _BOTTOM_DRAG_BEFORE = None
    global _BOTTOM_LAST_LAYOUT
    _BOTTOM_LAST_LAYOUT = {'status': 'DISABLED', 'buttons': []}
    try:
        from . import nav_hud_draw
        nav_hud_draw.reset()
    except (ImportError, RuntimeError, AttributeError):
        pass
    _redraw()


def _draw_bottom_hud():
    """Passive GPU-only paint; GizmoGroup dispatches all actions."""
    global _BOTTOM_LAST_LAYOUT, _BOTTOM_ERROR
    context = bpy.context
    if not _BOTTOM_ENABLED or not hud_scope.matches(_BOTTOM_SCOPE, context):
        # POST_PIXEL runs in EVERY 3D area. Never overwrite the owner's
        # layout diagnostics with an unrelated viewport's NO_ROOM snapshot.
        return
    layout = _bottom_layout(context)
    _BOTTOM_LAST_LAYOUT = bottom_issue_layout.snapshot(layout)
    if layout is None:
        return
    try:
        from . import nav_hud_draw
        issue = active_issue()
        base_kind = issue_ui_policy.presentation(issue['kind'])[1] if issue else 'ПРОБЛЕМЫ'
        kind = bottom_issue_layout.issue_status_label(
            base_kind, stale=_STALE, dragging=_BOTTOM_DRAG_ACTIVE,
            locked=getattr(context.window_manager, 'ima_unified_issue_hud_locked', True))
        # Reuse existing native gizmo hover feed and Motion-enabled renderer.
        from . import ui
        import time
        key = (context.area.as_pointer(), context.region.as_pointer())
        hovered = ui._NAV_HOVER.get(key)
        slot = hovered[0] if hovered and time.monotonic() - hovered[1] < .38 else -1
        nav_hud_draw.draw_complete_hud(layout, kind, _INDEX + 1, len(_VIEW_ROWS),
                                       highlighted_slot=slot,
                                       symbols=('BACK', 'NEXT', 'VIEWZOOM', 'X'),
                                       motion_enabled=getattr(context.window_manager,
                                                              'ima_unified_issue_hud_motion', True),
                                       grip_enabled=not getattr(context.window_manager,
                                                                'ima_unified_issue_hud_locked', True),
                                       grip_active=_BOTTOM_GRIP_ACTIVE,
                                       grip_hovered=time.monotonic() < _GRIP_HOVER_UNTIL)
    except Exception as exc:
        _BOTTOM_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:180])


def _install_bottom_gpu():
    global _BOTTOM_GPU, _BOTTOM_GPU_TOKEN, _BOTTOM_ERROR
    if _BOTTOM_GPU is not None or bpy.app.background:
        return
    token, previous = lifecycle_lease.claim(_BOTTOM_OWNER)
    _BOTTOM_GPU_TOKEN = token
    if previous:
        for handle in previous.get('handles', ()):
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except (RuntimeError, ReferenceError, ValueError):
                pass
    try:
        handle = bpy.types.SpaceView3D.draw_handler_add(_draw_bottom_hud, (), 'WINDOW', 'POST_PIXEL')
        _BOTTOM_GPU = handle
        lease = lifecycle_lease.owned(_BOTTOM_OWNER, token)
        if lease is not None:
            lease['handles'].append(handle)
    except Exception as exc:
        _BOTTOM_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:180])
        lifecycle_lease.retire(_BOTTOM_OWNER, token)
        _BOTTOM_GPU_TOKEN = None
        _BOTTOM_GPU = None


def _uninstall_bottom_gpu():
    global _BOTTOM_GPU, _BOTTOM_GPU_TOKEN
    token, handle = _BOTTOM_GPU_TOKEN, _BOTTOM_GPU
    _BOTTOM_GPU, _BOTTOM_GPU_TOKEN = None, None
    if handle is None or not lifecycle_lease.alive(_BOTTOM_OWNER, token):
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
    except (ReferenceError, RuntimeError, ValueError):
        pass
    finally:
        lifecycle_lease.retire(_BOTTOM_OWNER, token)


def _redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except (ReferenceError, RuntimeError, AttributeError):
        pass


def _source_ledger():
    from .core.reliability.ledger import snapshot
    for row in snapshot()[-core.MAX_ISSUES:]:
        if row.get('kind') == 'VALID':
            continue
        witness = row.get('witness', {})
        if not isinstance(witness, dict):
            witness = {}
        space = witness.get('context', {})
        space = space.get('coordinate_space') if isinstance(space, dict) else None
        # A local-space witness cannot be drawn/focused in world coordinates.
        verified_world = str(space).upper() in ('WORLD', 'WORLD_SPACE')
        yield {'kind': row.get('kind'), 'code': row.get('code'),
               'message': row.get('message'), 'entity': row.get('tool'),
               'proof': row.get('proof'),
               'coordinates': witness.get('coordinates', ()) if verified_world else ()}


def _source_brick():
    from .tools.brick_hole import issue_inspector
    for index, row in enumerate(issue_inspector.issues()[:core.MAX_ISSUES]):
        point = row.get('point')
        yield {'kind': 'REVIEW', 'code': 'BRICK_CONTOUR_CONTACT',
               'entity': 'brick_%d' % index,
               'message': row.get('reason', 'Проверьте контур'),
               'coordinates': (point,) if point is not None else ()}


def _source_seams():
    from .tools.brick_hole import seam_inspector
    for row in seam_inspector.issues()[:core.MAX_ISSUES]:
        yield {'kind': 'WARNING' if row.get('severity') == 'ERROR' else 'REVIEW',
               'code': row.get('reason_code', 'UV_SEAM'),
               'entity': '%s/edge:%s' % (row.get('source_object', ''), row.get('edge_index', -1)),
               'message': 'UV-разрыв: проверить (изображение не анализировалось)',
               'coordinates': (row.get('a'), row.get('b'))}


def _source_collision(context):
    settings = getattr(getattr(context, 'scene', None), 'collision_inspector', None)
    if settings is None:
        return
    for i in range(min(len(settings.events), core.MAX_ISSUES)):
        row = settings.events[i]
        witness = (tuple(row.witness_a), tuple(row.witness_b)) if row.has_witness_pair else ()
        yield {'kind': 'REVIEW', 'code': getattr(row, 'kind', 'COLLISION'),
               'entity': getattr(row, 'obj_name', ''),
               'message': getattr(row, 'detail', '') or getattr(row, 'text', ''),
               'coordinates': witness}


def _snapshot_sources(context):
    # Eager materialization inside per-source try: a single stale RNA event
    # must not stop other independent data sources.
    sources, errors, truncated = [], {}, {}
    for name, producer in (
        ('RELIABILITY', _source_ledger),
        ('BRICK_HOLE', _source_brick),
        ('UV_SEAMS', _source_seams),
        ('COLLISION', lambda: _source_collision(context)),
    ):
        try:
            payload = tuple(producer())
            sources.append((name, payload))
            if name == 'COLLISION':
                settings = getattr(getattr(context, 'scene', None), 'collision_inspector', None)
                n = len(settings.events) if settings is not None else 0
                if n > len(payload):
                    truncated[name] = n - len(payload)
            elif name == 'UV_SEAMS':
                from .tools.brick_hole import seam_inspector
                n = len(seam_inspector.issues())
                if n > len(payload):
                    truncated[name] = n - len(payload)
        except Exception as exc:
            errors[name] = '%s: %s' % (type(exc).__name__, str(exc)[:180])
    return sources, errors, truncated


def _scene_pointer(context):
    try:
        scene = getattr(context, 'scene', None)
        return int(scene.as_pointer()) if scene is not None else None
    except (ReferenceError, RuntimeError, AttributeError, TypeError, ValueError):
        return None


def _current_filter(context):
    wm = getattr(context, 'window_manager', None)
    return (getattr(wm, 'ima_unified_issue_kind', 'ALL'),
            getattr(wm, 'ima_unified_issue_source', 'ALL'))


def _apply_filters(kind='ALL', source='ALL', *, preserve_id=''):
    global _VIEW_ROWS, _INDEX, _VISIBLE
    _VIEW_ROWS = issue_ui_policy.visible_issues(_STATE['issues'], kind, source)
    _INDEX = issue_ui_policy.select_index(_VIEW_ROWS, preserve_id)
    _VISIBLE = bool(_VIEW_ROWS) and not _STALE
    if _BOTTOM_ENABLED and not _VIEW_ROWS:
        _bottom_hide('FILTER_NO_ISSUES')
    _redraw()


def _on_filter_changed(self, context):
    issue = active_issue()
    _apply_filters(*_current_filter(context), preserve_id=issue['id'] if issue else '')


def refresh(context):
    global _STATE, _INDEX, _VISIBLE, _LAST_ERROR, _STALE, _SOURCE_TRUNCATION
    global _STALE_REASON, _SCOPE_PTR, _CHANGED_EVENTS
    sources, errors, truncation = _snapshot_sources(context)
    candidate = core.collect(sources)
    candidate['source_errors'].update(errors)
    candidate['omitted'] += sum(truncation.values())
    candidate['truncated'] = candidate['truncated'] or bool(truncation)
    _SOURCE_TRUNCATION = truncation
    # Refresh rereads cached scanner findings; it DOES NOT rerun geometry
    # analysis. If old records survive, we cannot claim their world witnesses
    # are freshly verified after a geometry edit.
    was_stale = _STALE
    old_ids = {row['id'] for row in _STATE['issues']}
    retained = any(row['id'] in old_ids for row in candidate['issues'])
    _STALE = bool(was_stale and retained)
    if _STALE:
        _STALE_REASON = 'Источники не пересчитаны. Повторите анализ соответствующим инструментом'
    else:
        _STALE_REASON = ''
        _CHANGED_EVENTS = 0
    _SCOPE_PTR = _scene_pointer(context)
    _STATE = candidate
    _LAST_ERROR = ''
    _apply_filters(*_current_filter(context))
    return len(candidate['issues'])


def active_issue():
    if not 0 <= _INDEX < len(_VIEW_ROWS):
        return None
    return _VIEW_ROWS[_INDEX]


def _bottom_setting(name, default):
    try:
        return getattr(bpy.context.window_manager, name, default)
    except (AttributeError, ReferenceError, RuntimeError):
        return default


def snapshot():
    issue = active_issue()
    return {'schema': 'issmaker.unified_issues.2', 'build_id': NAVIGATOR_BUILD_ID, 'active': _VISIBLE,
            'count': len(_STATE['issues']), 'total': _STATE['total'],
            'omitted': _STATE['omitted'], 'sources': dict(_STATE['sources']),
            'source_errors': dict(_STATE['source_errors']),
            'index': _INDEX, 'current_id': issue['id'] if issue else '',
            'current_kind': issue['kind'] if issue else '',
            'gpu_handler': _GPU is not None, 'error': _LAST_ERROR,
            'stale_after_undo': _STALE, 'stale': _STALE,
            'stale_reason': _STALE_REASON, 'geometry_change_events': _CHANGED_EVENTS,
            'visible_count': len(_VIEW_ROWS), 'scope_present': _SCOPE_PTR is not None,
            'source_truncation': dict(_SOURCE_TRUNCATION),
            'bottom_hud': {'enabled': _BOTTOM_ENABLED, 'gpu_handler': _BOTTOM_GPU is not None,
                           'layout': dict(_BOTTOM_LAST_LAYOUT), 'error': _BOTTOM_ERROR,
                           'drag_active': _BOTTOM_DRAG_ACTIVE,
                           'scope': list(_BOTTOM_SCOPE) if _BOTTOM_SCOPE else None,
                           'scope_reason': _BOTTOM_SCOPE_REASON,
                           'scope_model': 'WINDOW_AND_VIEW3D_AREA',
                           'acceptance_lab': dict(_HUD_LAB_RESULT),
                           'drag_owner_present': _BOTTOM_DRAG_OWNER is not None,
                           'position_mode': _bottom_setting('ima_unified_issue_hud_position', 'CENTER'),
                           'locked': bool(_bottom_setting('ima_unified_issue_hud_locked', True)),
                           'motion_enabled': bool(_bottom_setting('ima_unified_issue_hud_motion', True)),
                           'grip': dict(_GRIP_STATS),
                           'grip_active': _BOTTOM_GRIP_ACTIVE,
                           'custom_xy': [_bottom_setting('ima_unified_issue_custom_x', .5),
                                         _bottom_setting('ima_unified_issue_custom_y', .18)]}}


def _draw_active_world():
    if _STALE or not _VISIBLE:
        return
    issue = active_issue()
    if not issue:
        return
    points = issue['coordinates']
    if not points:
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        color = (0.97, 0.31, 0.29, 0.92) if issue['kind'] == 'PROVEN_INVALID' else (1.0, 0.70, 0.20, 0.90)
        # Fixed bounded allocations: only selected points, no mesh traversal.
        gpu.state.blend_set('ALPHA')
        try:
            if len(points) >= 2:
                batch = batch_for_shader(shader, 'LINES', {'pos': points[:2]})
                shader.bind(); shader.uniform_float('color', color); batch.draw(shader)
            # A point remains useful when there is no segment.
            # GPU point sizes use physical pixels; multiply Blender's display
            # scale exactly once, not a second window-size multiplier.
            prefs = getattr(bpy.context, 'preferences', None)
            system = getattr(prefs, 'system', None)
            ui_scale = ui_layout_scaling.sanitized(
                getattr(system, 'ui_scale', 1.0), low=1.0, high=2.5)
            gpu.state.point_size_set(9.0 * ui_scale)
            batch = batch_for_shader(shader, 'POINTS', {'pos': points[:core.MAX_POINTS]})
            shader.bind(); shader.uniform_float('color', color); batch.draw(shader)
        finally:
            gpu.state.point_size_set(1.0)
            gpu.state.blend_set('NONE')
    except Exception as exc:
        global _LAST_ERROR
        _LAST_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:180])


_LEASE_OWNER = 'ISSMAKER_UNIFIED_ISSUE_NAV_W8'


def _install_gpu():
    global _GPU, _GPU_TOKEN, _LAST_ERROR
    if _GPU is not None or bpy.app.background:
        return
    token, previous = lifecycle_lease.claim(_LEASE_OWNER)
    _GPU_TOKEN = token
    # A hot-reloaded module may have left the old GPU draw handler alive.
    # Never leave both registered; the old module must not remove the new one.
    if previous:
        for handle in previous.get('handles', ()):
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except (RuntimeError, ReferenceError, ValueError):
                pass
    try:
        handle = bpy.types.SpaceView3D.draw_handler_add(_draw_active_world, (), 'WINDOW', 'POST_VIEW')
        _GPU = handle
        row = lifecycle_lease.owned(_LEASE_OWNER, token)
        if row is not None:
            row['handles'].append(handle)
    except Exception as exc:
        _LAST_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:180])
        lifecycle_lease.retire(_LEASE_OWNER, token)
        _GPU_TOKEN = None
        _GPU = None


def _uninstall_gpu():
    global _GPU, _GPU_TOKEN
    token, handle = _GPU_TOKEN, _GPU
    _GPU_TOKEN, _GPU = None, None
    if handle is None or not lifecycle_lease.alive(_LEASE_OWNER, token):
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
    except (ReferenceError, RuntimeError, ValueError):
        pass
    finally:
        lifecycle_lease.retire(_LEASE_OWNER, token)


def mark_stale(reason='Undo/Redo или изменение сцены'):
    """Invalidate a witness; an old screenshot cannot be treated as new proof."""
    global _STALE, _VISIBLE, _STALE_REASON
    if not _STATE['issues']:
        return
    _STALE, _STALE_REASON = issue_lifecycle.next_stale(
        _STALE, bool(_STATE['issues']), True, _STALE_REASON or reason)
    _VISIBLE = False
    _redraw()


@persistent
def _depsgraph_update(scene, depsgraph):
    """O(updated IDs), no Mesh traversal, no new issue detection in callbacks."""
    global _CHANGED_EVENTS
    if _STALE or not _STATE['issues'] or not lifecycle_lease.alive(_HOOK_OWNER, _HOOK_TOKEN):
        return
    try:
        for update in depsgraph.updates:
            datablock = update.id
            if isinstance(datablock, bpy.types.Mesh):
                label = 'MESH'
            elif isinstance(datablock, bpy.types.Object):
                label = 'OBJECT:' + str(datablock.type)
            else:
                label = str(getattr(getattr(datablock, 'bl_rna', None), 'identifier', '')).upper()
            if issue_lifecycle.invalidates(
                    label, getattr(update, 'is_updated_geometry', False),
                    getattr(update, 'is_updated_transform', False)):
                _CHANGED_EVENTS += 1
                mark_stale('Изменена геометрия или положение объекта')
                return
    except (RuntimeError, ReferenceError, AttributeError, TypeError) as exc:
        mark_stale('Невозможно проверить обновление сцены: ' + type(exc).__name__)


@persistent
def _on_file_load(*_args):
    if lifecycle_lease.alive(_HOOK_OWNER, _HOOK_TOKEN):
        _bottom_hide('FILE_LOAD')
        mark_stale('Загружен новый проект Blender')


def _register_event_hooks():
    global _HOOK_TOKEN
    token, _previous = lifecycle_lease.claim(_HOOK_OWNER)
    _HOOK_TOKEN = token
    for name, fn in (('depsgraph_update_post', _depsgraph_update),
                     ('load_pre', _on_file_load)):
        registry = getattr(bpy.app.handlers, name, None)
        if registry is None:
            continue
        for previous in tuple(registry):
            if (getattr(previous, '__module__', None) == __name__ and
                    getattr(previous, '__name__', None) == fn.__name__):
                registry.remove(previous)
        registry.append(fn)


def _unregister_event_hooks():
    global _HOOK_TOKEN
    token = _HOOK_TOKEN
    _HOOK_TOKEN = None
    if not lifecycle_lease.alive(_HOOK_OWNER, token):
        return  # A newer incarnation owns the registered callbacks.
    for name, fn in (('depsgraph_update_post', _depsgraph_update),
                     ('load_pre', _on_file_load)):
        registry = getattr(bpy.app.handlers, name, None)
        if registry is None:
            continue
        for previous in tuple(registry):
            if (getattr(previous, '__module__', None) == __name__ and
                    getattr(previous, '__name__', None) == fn.__name__):
                registry.remove(previous)
    lifecycle_lease.retire(_HOOK_OWNER, token)


def close():
    global _VISIBLE, _INDEX, _STATE, _STALE, _SOURCE_TRUNCATION, _VIEW_ROWS
    global _STALE_REASON, _SCOPE_PTR, _CHANGED_EVENTS
    _VISIBLE = False
    _INDEX = -1
    _STATE = core.collect(())
    _VIEW_ROWS = ()
    _STALE = False
    _STALE_REASON = ''
    _SCOPE_PTR = None
    _CHANGED_EVENTS = 0
    _SOURCE_TRUNCATION = {}
    _bottom_hide()
    _redraw()


class IMA_OT_UnifiedIssueBottomToggle(Operator):
    bl_idname = 'ima.unified_issue_bottom_toggle'
    bl_label = 'Нижний навигатор ошибок'
    bl_description = 'Показать или закрыть нижнюю read-only панель навигации'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _BOTTOM_ENABLED, _BOTTOM_GENERATION, _BOTTOM_SCOPE, _BOTTOM_SCOPE_REASON
        target_scope = hud_scope.context_scope(context)
        if target_scope is None:
            self.report({'WARNING'}, 'Выберите область 3D Viewport')
            return {'CANCELLED'}
        if _BOTTOM_ENABLED:
            if _BOTTOM_SCOPE == target_scope:
                _bottom_hide('USER_CLOSE')
                return {'FINISHED'}
            _bottom_hide('REBOUND_TO_OTHER_VIEWPORT')
        if not _VIEW_ROWS:
            self.report({'WARNING'}, 'Сначала соберите проблемы и проверьте фильтры')
            return {'CANCELLED'}
        if getattr(getattr(context, 'area', None), 'type', None) != 'VIEW_3D':
            self.report({'WARNING'}, 'Нижняя панель доступна только в 3D Viewport')
            return {'CANCELLED'}
        _BOTTOM_ENABLED = True
        _BOTTOM_SCOPE = target_scope
        _BOTTOM_SCOPE_REASON = 'ACTIVE'
        _BOTTOM_GENERATION += 1
        self._generation = _BOTTOM_GENERATION
        self._owner_scope = target_scope
        try:
            context.window_manager.modal_handler_add(self)
        except (RuntimeError, ReferenceError, AttributeError) as exc:
            _bottom_hide()
            self.report({'WARNING'}, 'Не удалось открыть HUD: %s' % str(exc)[:100])
            return {'CANCELLED'}
        _redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if (not _BOTTOM_ENABLED or getattr(self, '_generation', None) != _BOTTOM_GENERATION
                or not lifecycle_lease.alive(_BOTTOM_OWNER, _BOTTOM_GPU_TOKEN)):
            return {'FINISHED'}
        if not hud_scope.modal_owner_valid(getattr(self, '_owner_scope', None), context,
                                           self._generation, _BOTTOM_GENERATION):
            return {'PASS_THROUGH'}
        # A separate modeling tool owns Esc while active. In that state
        # close the global HUD without stealing its normal Esc semantics.
        if _BOTTOM_DRAG_ACTIVE:
            return {'PASS_THROUGH'}  # Drag owner alone accepts/cancels its modal.
        if event.type == 'ESC' and event.value == 'PRESS':
            if _local_navigator_busy(context):
                _bottom_hide()
                return {'PASS_THROUGH'}
            try:
                from .core import active_tools
                owners = active_tools.ownership_snapshot(context)['owners']
                if any(row.get('active') is not False for row in owners.values()):
                    _bottom_hide()
                    return {'PASS_THROUGH'}  # Modeling tool has Esc priority.
            except (AttributeError, ReferenceError, RuntimeError, KeyError):
                _bottom_hide()
                return {'PASS_THROUGH'}
            _bottom_hide()
            return {'CANCELLED'}
        return {'PASS_THROUGH'}


class IMA_OT_UnifiedIssueBottomClose(Operator):
    bl_idname = 'ima.unified_issue_bottom_close'
    bl_label = 'Закрыть нижний навигатор'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _bottom_hide()
        return {'FINISHED'}


class IMA_OT_UnifiedIssueBottomMove(Operator):
    """Opt-in modal placement; no mouse interception outside this operation."""
    bl_idname = 'ima.unified_issue_bottom_move'
    bl_label = 'Переместить нижнюю панель'
    bl_description = 'Двигайте мышью в 3D View, ЛКМ — подтвердить, Esc — отменить'
    bl_options = {'INTERNAL'}

    @staticmethod
    def _window_region(area):
        try:
            return next((r for r in area.regions if r.type == 'WINDOW'), None)
        except (ReferenceError, AttributeError, TypeError):
            return None

    def _finish(self, context, cancel):
        global _BOTTOM_DRAG_ACTIVE, _BOTTOM_DRAG_OWNER, _BOTTOM_DRAG_BEFORE
        if _BOTTOM_DRAG_OWNER != getattr(self, '_drag_token', None):
            return {'CANCELLED'}  # Stale legacy modal must not release a new owner.
        if cancel:
            try:
                wm = context.window_manager
                wm.ima_unified_issue_hud_position, wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y = self._before
            except (ReferenceError, AttributeError, RuntimeError, TypeError):
                pass
        _BOTTOM_DRAG_ACTIVE = False
        _BOTTOM_DRAG_OWNER = None
        _BOTTOM_DRAG_BEFORE = None
        _redraw()
        return {'CANCELLED'} if cancel else {'FINISHED'}

    def invoke(self, context, event):
        global _BOTTOM_DRAG_ACTIVE, _BOTTOM_DRAG_OWNER, _BOTTOM_DRAG_BEFORE, _BOTTOM_DRAG_SERIAL
        if not _BOTTOM_ENABLED or not _VIEW_ROWS or _BOTTOM_DRAG_ACTIVE:
            self.report({'WARNING'}, 'Откройте нижний навигатор с результатами')
            return {'CANCELLED'}
        if _local_navigator_busy(context):
            self.report({'WARNING'}, 'Сначала завершите активный инструмент')
            return {'CANCELLED'}
        wm = context.window_manager
        if getattr(wm, 'ima_unified_issue_hud_locked', True):
            self.report({'WARNING'}, 'Отключите «Закрепить HUD»')
            return {'CANCELLED'}
        area = getattr(context, 'area', None)
        region = self._window_region(area) if area is not None else None
        if (getattr(area, 'type', None) != 'VIEW_3D' or region is None or
                getattr(context, 'window', None) is None):
            return {'CANCELLED'}
        self._before = (wm.ima_unified_issue_hud_position,
                        wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y)
        self._owner_scope = hud_scope.context_scope(context)
        if self._owner_scope is None or self._owner_scope != _BOTTOM_SCOPE:
            return {'CANCELLED'}
        self._generation = _BOTTOM_GENERATION
        _BOTTOM_DRAG_ACTIVE = True
        _BOTTOM_DRAG_SERIAL += 1
        self._drag_token = (self._generation, _BOTTOM_DRAG_SERIAL)
        _BOTTOM_DRAG_OWNER = self._drag_token
        _BOTTOM_DRAG_BEFORE = self._before
        try:
            wm.modal_handler_add(self)
        except (RuntimeError, ReferenceError, AttributeError):
            return self._finish(context, True)
        _redraw()
        return {'RUNNING_MODAL'}

    def execute(self, context):
        self.report({'INFO'}, 'Нажмите «Переместить» в интерфейсе 3D View')
        return {'CANCELLED'}

    def modal(self, context, event):
        if (not _BOTTOM_DRAG_ACTIVE or _BOTTOM_DRAG_OWNER != self._drag_token or
                not _BOTTOM_ENABLED or self._generation != _BOTTOM_GENERATION or
                not hud_scope.modal_owner_valid(self._owner_scope, context,
                                                self._generation, _BOTTOM_GENERATION) or
                self._owner_scope != _BOTTOM_SCOPE or
                _local_navigator_busy(context)):
            return self._finish(context, True)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish(context, True)
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            return self._finish(context, False)
        if event.type == 'MOUSEMOVE':
            reg = self._window_region(getattr(context, 'area', None))
            if reg is None:
                return self._finish(context, True)
            coords = bottom_issue_layout.normalized_cursor(
                event.mouse_x, event.mouse_y, reg.x, reg.y, reg.width, reg.height)
            if coords is None:
                return self._finish(context, True)
            try:
                wm = context.window_manager
                wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y = coords
                wm.ima_unified_issue_hud_position = 'CUSTOM'
            except (RuntimeError, ReferenceError, AttributeError):
                return self._finish(context, True)
            _redraw()
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}


class IMA_GT_UnifiedIssueDragGrip(Gizmo):
    """A fifth NATIVE gizmo for a real press-hold-release HUD drag.

    No global mouse listener. The four navigation button gizmos are untouched.
    """
    bl_idname = 'IMA_GT_unified_issue_drag_grip'
    bl_target_properties = ()
    pixel_width: bpy.props.FloatProperty(default=106., min=1., max=600.)
    pixel_height: bpy.props.FloatProperty(default=18., min=1., max=100.)

    def setup(self):
        self.use_draw_hover = True
        self.use_event_handle_all = False
        self.use_undo = False
        self.use_tooltip = True
        self.color = (0., 0., 0.)
        self.color_highlight = (0., 0., 0.)
        self.alpha = 0.
        self.alpha_highlight = 0.
        self._shape = self.new_custom_shape('TRIS', (
            (-.5,-.5,0.), (.5,-.5,0.), (.5,.5,0.),
            (-.5,-.5,0.), (.5,.5,0.), (-.5,.5,0.),
        ))

    def _matrix(self):
        from mathutils import Matrix
        return self.matrix_basis @ Matrix.Diagonal((self.pixel_width, self.pixel_height, 1., 1.))

    def draw(self, context):
        if not self.hide:
            self.draw_custom_shape(self._shape, matrix=self._matrix())

    def draw_select(self, context, select_id):
        if not self.hide:
            self.draw_custom_shape(self._shape, matrix=self._matrix(), select_id=select_id)

    def test_select(self, context, location):
        if (self.hide or getattr(getattr(context, 'region', None), 'type', None) != 'WINDOW'
                or getattr(context.window_manager, 'ima_unified_issue_hud_locked', True)
                or _BOTTOM_DRAG_ACTIVE):
            return -1
        layout = _bottom_layout(context)
        matched = bottom_issue_layout.hit_grip(layout, *location)
        if matched:
            global _GRIP_HOVER_UNTIL
            import time
            _GRIP_HOVER_UNTIL = time.monotonic() + .20
            try:
                context.area.tag_redraw()
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        return 0 if matched else -1

    def invoke(self, context, event):
        global _BOTTOM_DRAG_ACTIVE, _BOTTOM_DRAG_OWNER, _BOTTOM_DRAG_BEFORE, _BOTTOM_DRAG_SERIAL, _BOTTOM_GRIP_ACTIVE
        if (self.hide or not _BOTTOM_ENABLED or not _VIEW_ROWS or _BOTTOM_DRAG_ACTIVE
                or getattr(context.window_manager, 'ima_unified_issue_hud_locked', True)
                or _local_navigator_busy(context)):
            return {'CANCELLED'}
        layout = _bottom_layout(context)
        reg = getattr(context, 'region', None)
        if not layout or getattr(reg, 'type', None) != 'WINDOW':
            return {'CANCELLED'}
        px, py = bottom_issue_layout.normalized_cursor(event.mouse_x, event.mouse_y,
                                                       reg.x, reg.y, reg.width, reg.height) or (None,None)
        if px is None or not bottom_issue_layout.hit_grip(layout, px*reg.width, py*reg.height):
            return {'CANCELLED'}
        wm = context.window_manager
        self._before = (wm.ima_unified_issue_hud_position,
                        wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y)
        self._owner_scope = hud_scope.context_scope(context)
        if self._owner_scope is None or self._owner_scope != _BOTTOM_SCOPE:
            return {'CANCELLED'}
        self._generation = _BOTTOM_GENERATION
        _BOTTOM_DRAG_SERIAL += 1
        self._drag_token = (self._generation, _BOTTOM_DRAG_SERIAL)
        x0, y0, x1, y1 = layout['panel']
        # CUSTOM expects PANEL center, not grip center: otherwise the panel
        # jumps vertically by the header height on the first mouse move.
        self._offset = ((x0+x1)*.5-px*reg.width, (y0+y1)*.5-py*reg.height)
        _BOTTOM_DRAG_ACTIVE = True
        _BOTTOM_GRIP_ACTIVE = True
        _BOTTOM_DRAG_OWNER = self._drag_token
        _BOTTOM_DRAG_BEFORE = self._before
        self._active = True
        _grip_record('START')
        _redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event, tweak):
        if not getattr(self, '_active', False):
            return {'CANCELLED'}
        if (not _BOTTOM_ENABLED or not _BOTTOM_DRAG_ACTIVE
                or _BOTTOM_DRAG_OWNER != self._drag_token
                or _BOTTOM_GENERATION != self._generation
                or not hud_scope.modal_owner_valid(self._owner_scope, context,
                                                   self._generation, _BOTTOM_GENERATION)
                or self._owner_scope != _BOTTOM_SCOPE
                or _local_navigator_busy(context)
                or getattr(context.window_manager, 'ima_unified_issue_hud_locked', True)):
            return {'CANCELLED'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return {'FINISHED'}
        if event.type == 'MOUSEMOVE':
            reg = getattr(context, 'region', None)
            if getattr(reg, 'type', None) != 'WINDOW':
                return {'CANCELLED'}
            layout = _bottom_layout(context)
            target = bottom_issue_layout.drag_position(
                layout, event.mouse_x, event.mouse_y, reg.x, reg.y,
                reg.width, reg.height, *self._offset)
            if target is None:
                return {'CANCELLED'}
            try:
                wm = context.window_manager
                wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y = target
                wm.ima_unified_issue_hud_position = 'CUSTOM'
            except (ReferenceError, RuntimeError, AttributeError, TypeError):
                return {'CANCELLED'}
            _redraw()
        return {'RUNNING_MODAL'}

    def exit(self, context, cancel):
        global _BOTTOM_DRAG_ACTIVE, _BOTTOM_DRAG_OWNER, _BOTTOM_DRAG_BEFORE, _BOTTOM_GRIP_ACTIVE
        if not getattr(self, '_active', False):
            return
        self._active = False
        if not _BOTTOM_DRAG_ACTIVE or _BOTTOM_DRAG_OWNER != getattr(self, '_drag_token', None):
            return  # Do not let a late exit delete a newer drag owner.
        if cancel:
            try:
                wm = context.window_manager
                wm.ima_unified_issue_hud_position, wm.ima_unified_issue_custom_x, wm.ima_unified_issue_custom_y = self._before
            except (ReferenceError, RuntimeError, AttributeError, TypeError):
                pass
        _BOTTOM_DRAG_ACTIVE = False
        _BOTTOM_GRIP_ACTIVE = False
        _BOTTOM_DRAG_OWNER = None
        _BOTTOM_DRAG_BEFORE = None
        _grip_record('CANCEL' if cancel else 'COMMIT')
        _redraw()


class IMA_GGT_UnifiedIssueBottom(GizmoGroup):
    """Blender native buttons, same exact layout consumed by POST_PIXEL GPU."""
    bl_idname = 'IMA_GGT_unified_issue_bottom'
    bl_label = 'ISSMAKER · Нижний навигатор'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return bool(_BOTTOM_ENABLED and _VIEW_ROWS and hud_scope.matches(_BOTTOM_SCOPE, context) and
                    not _local_navigator_busy(context))

    def setup(self, context):
        from . import ui
        self.buttons = (
            ui._nav_button(self, 'ima.unified_issue_step', 0, direction=-1),
            ui._nav_button(self, 'ima.unified_issue_step', 1, direction=1),
            ui._nav_button(self, 'ima.unified_issue_focus', 2),
            ui._nav_button(self, 'ima.unified_issue_bottom_close', 3),
        )
        self.grip = self.gizmos.new(IMA_GT_UnifiedIssueDragGrip.bl_idname)

    def draw_prepare(self, context):
        layout = _bottom_layout(context)
        for button in self.buttons:
            button.hide = True
        self.grip.hide = True
        if layout is None:
            return
        from mathutils import Matrix
        if not getattr(context.window_manager, 'ima_unified_issue_hud_locked', True) and not _BOTTOM_DRAG_ACTIVE:
            gx, gy, gw, gh = layout['grip']
            self.grip.pixel_width = gw
            self.grip.pixel_height = gh
            self.grip.matrix_basis = Matrix.Translation((gx, gy, 0))
            self.grip.hide = False
        for i, button in enumerate(self.buttons):
            x, y, size = layout['buttons'][i]
            button.hide = (_STALE and i == 2) or (i == 2 and core.focus_point(active_issue()) is None)
            button.pixel_size = size
            button.matrix_basis = Matrix.Translation((x, y, 0))


class IMA_OT_UnifiedIssueRefresh(Operator):
    bl_idname = 'ima.unified_issue_refresh'
    bl_label = 'Обновить список проблем'
    bl_description = 'Собрать существующие результаты проверок без повторного анализа Mesh'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        count = refresh(context)
        self.report({'INFO'}, 'Навигатор: %d проблем; новый анализ Mesh не запускался' % count)
        return {'FINISHED'}


class IMA_OT_UnifiedIssueStep(Operator):
    bl_idname = 'ima.unified_issue_step'
    bl_label = 'Следующая проблема'
    bl_options = {'INTERNAL'}
    direction: bpy.props.IntProperty(default=1)

    def execute(self, context):
        global _INDEX
        if not _VIEW_ROWS:
            return {'CANCELLED'}
        _INDEX = core.page(len(_VIEW_ROWS), _INDEX, self.direction)
        _redraw()
        return {'FINISHED'}


class IMA_OT_UnifiedIssueFocus(Operator):
    bl_idname = 'ima.unified_issue_focus'
    bl_label = 'Показать проблему'
    bl_description = 'Сфокусировать 3D-камеру; Mesh, UV и выделение не меняются'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if _STALE or (_SCOPE_PTR is not None and _scene_pointer(context) != _SCOPE_PTR):
            self.report({'WARNING'}, 'Снимок устарел — обновите список проблем')
            return {'CANCELLED'}
        issue = active_issue()
        target = core.focus_point(issue)
        if target is None:
            self.report({'WARNING'}, 'У этой проблемы нет достоверной координаты для фокусировки')
            return {'CANCELLED'}
        area = context.area
        if area is None or area.type != 'VIEW_3D':
            self.report({'WARNING'}, 'Откройте 3D Viewport для перехода к проблеме')
            return {'CANCELLED'}
        space = area.spaces.active
        rv3d = getattr(space, 'region_3d', None)
        if rv3d is None:
            return {'CANCELLED'}
        try:
            rv3d.view_location = target
            area.tag_redraw()
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
            self.report({'WARNING'}, 'Фокус недоступен: %s' % str(exc)[:100])
            return {'CANCELLED'}
        return {'FINISHED'}


class IMA_OT_UnifiedIssueHide(Operator):
    bl_idname = 'ima.unified_issue_hide'
    bl_label = 'Закрыть навигатор'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        close()
        return {'FINISHED'}


class IMA_OT_UnifiedIssueDetails(Operator):
    bl_idname = 'ima.unified_issue_details'
    bl_label = 'Технические подробности'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _SHOW_DETAILS
        _SHOW_DETAILS = not _SHOW_DETAILS
        _redraw()
        return {'FINISHED'}


class IMA_OT_UnifiedIssueCopy(Operator):
    bl_idname = 'ima.unified_issue_copy'
    bl_label = 'Копировать описание проблемы'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        issue = active_issue()
        if issue is None:
            return {'CANCELLED'}
        try:
            lines = [issue['source'] + ' / ' + issue['code'],
                     'ID: ' + issue['id'], 'Classification: ' + issue['kind'],
                     'Original: ' + issue['source_kind'], 'Entity: ' + issue['entity'],
                     'Reason: ' + issue['message'], 'Proof: ' + issue['proof'],
                     'Witness: ' + repr(issue['coordinates']),
                     'Stale after Undo: ' + str(_STALE)]
            context.window_manager.clipboard = '\n'.join(lines)
            self.report({'INFO'}, 'Подробности проблемы скопированы')
            return {'FINISHED'}
        except (AttributeError, ReferenceError, RuntimeError) as exc:
            self.report({'WARNING'}, 'Не удалось скопировать: ' + str(exc)[:120])
            return {'CANCELLED'}


class IMA_OT_UnifiedHudAcceptanceLab(Operator):
    """Run bounded, read-only HUD math/context tests; never claim GUI click PASS."""
    bl_idname = 'ima.unified_hud_acceptance_lab'
    bl_label = 'HUD LAB · Проверить'
    bl_description = 'Запустить read-only проверки HUD, затем соберите Support ZIP'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global _HUD_LAB_RESULT
        try:
            from .core.hud_acceptance import run_lab
            _HUD_LAB_RESULT = run_lab(context, expected_scope=_BOTTOM_SCOPE,
                                      enabled=_BOTTOM_ENABLED)
        except Exception as exc:
            _HUD_LAB_RESULT = {'status': 'FAIL', 'native_gui': 'NOT_TESTED_GUI',
                               'reason': ('%s: %s' % (type(exc).__name__, str(exc)))[:250]}
        _redraw()
        state = _HUD_LAB_RESULT['status']
        if state != 'PASS_OFFLINE':
            self.report({'WARNING'}, 'HUD Lab FAIL: проверьте Support ZIP')
            return {'CANCELLED'}
        self.report({'INFO'}, 'HUD Lab PASS_OFFLINE · клики Blender GUI не проверены · соберите Support ZIP')
        return {'FINISHED'}


class IMA_PT_UnifiedIssueNavigator(Panel):
    bl_idname = 'IMA_PT_unified_issue_navigator'
    bl_label = 'ПРОБЛЕМЫ · общий навигатор'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'ISSMAKER'
    bl_order = 1

    def draw(self, context):
        box = self.layout.box()
        row = box.row(align=True)
        row.operator('ima.unified_issue_refresh', text='Обновить', icon='FILE_REFRESH')
        row.operator('ima.unified_issue_hide', text='', icon='X')
        hud = box.row(align=True)
        label = ('Скрыть нижнюю панель' if _BOTTOM_ENABLED and hud_scope.matches(_BOTTOM_SCOPE, context)
                 else 'Перенести HUD сюда' if _BOTTOM_ENABLED else 'Нижняя панель')
        hud.operator('ima.unified_issue_bottom_toggle', text=label, icon='DOWNARROW_HLT')
        hud.prop(context.window_manager, 'ima_unified_issue_hud_position', text='')
        controls = box.row(align=True)
        controls.prop(context.window_manager, 'ima_unified_issue_hud_locked', text='Закрепить HUD')
        move = controls.row(align=True)
        move.enabled = _BOTTOM_ENABLED and not _BOTTOM_DRAG_ACTIVE and not _local_navigator_busy(context) and not context.window_manager.ima_unified_issue_hud_locked
        move.operator('ima.unified_issue_bottom_move', text='Переместить через меню', icon='ORIENTATION_CURSOR')
        box.label(text='Захват за верхнюю ручку HUD · ЛКМ удерживать', icon='MOUSE_LMB')
        box.prop(context.window_manager, 'ima_unified_issue_hud_motion', text='Анимация HUD')
        lab_row = box.row(align=True)
        lab_row.operator('ima.unified_hud_acceptance_lab', text='HUD LAB · Проверить', icon='CHECKMARK')
        lab_row.operator('ima.diagnostics_zip', text='Support ZIP', icon='PACKAGE')
        box.label(text='Lab: ' + str(_HUD_LAB_RESULT.get('status', 'NOT_RUN'))[:28])
        if _BOTTOM_DRAG_ACTIVE:
            box.label(text='Перемещение · отпустите ЛКМ или Esc', icon='INFO')
        column = box.column(align=True)
        column.prop(context.window_manager, 'ima_unified_issue_kind', text='Тип')
        column.prop(context.window_manager, 'ima_unified_issue_source', text='Источник')
        issue = active_issue()
        status = issue_ui_policy.list_state(len(_STATE['issues']), len(_VIEW_ROWS),
                                            _STALE, _STATE['omitted'], len(_STATE['source_errors']))
        if status == 'STALE':
            box.label(text='УСТАРЕЛО · обновите список', icon='FILE_REFRESH')
            box.label(text=_STALE_REASON[:66])
        if issue is None:
            if _STATE['issues'] and not _VIEW_ROWS:
                box.label(text='Фильтр скрыл все результаты', icon='INFO')
            else:
                box.label(text='Нет активного снимка', icon='INFO')
                box.label(text='Сначала выполните проверки инструментов')
            if _STATE['source_errors']:
                box.label(text='Некоторые источники недоступны', icon='ERROR')
            if _STATE['omitted']:
                box.label(text='Пропущено: %d' % _STATE['omitted'], icon='INFO')
            return
        icon, status_label, _hint = issue_ui_policy.presentation(issue['kind'], _STALE)
        box.label(text='%d / %d · %s' % (_INDEX + 1, len(_VIEW_ROWS), status_label), icon=icon)
        if status in {'PARTIAL', 'SOURCE_FAILED'}:
            box.label(text='Частичный результат · проверьте ZIP', icon='INFO')
        box.label(text=issue['source'] + ' · ' + issue['code'][:30])
        if issue['entity']:
            box.label(text=issue['entity'][:43])
        if issue['message']:
            # Blender N panels truncate long labels: preserve accessible source
            # through the one-click Support ZIP, never throw a UI exception.
            for offset in range(0, min(len(issue['message']), 112), 44):
                box.label(text=issue['message'][offset:offset + 44])
        row = box.row(align=True)
        row.operator('ima.unified_issue_step', text='', icon='TRIA_LEFT').direction = -1
        row.operator('ima.unified_issue_step', text='', icon='TRIA_RIGHT').direction = 1
        focus = row.row(align=True)
        focus.enabled = not _STALE and core.focus_point(issue) is not None
        focus.operator('ima.unified_issue_focus', text='Показать', icon='VIEWZOOM')
        if _STALE:
            box.label(text='Координаты устарели · обновите', icon='INFO')
        row = box.row(align=True)
        row.operator('ima.unified_issue_details', text='Подробнее', icon='INFO')
        row.operator('ima.unified_issue_copy', text='Копировать', icon='COPYDOWN')
        if _SHOW_DETAILS:
            box.label(text='Исходный статус: ' + issue['source_kind'][:30])
            box.label(text='ID: ' + issue['id'])
            if issue['proof']:
                for offset in range(0, min(len(issue['proof']), 132), 44):
                    box.label(text=issue['proof'][offset:offset + 44])
            else:
                box.label(text='Доказательство не предоставлено', icon='INFO')
            if issue['coordinates']:
                box.label(text='Witness: %d точек' % len(issue['coordinates']))
            else:
                box.label(text='Witness XYZ отсутствует', icon='INFO')
        if _STATE['truncated']:
            box.label(text='Частичный список: %d пропущено' % _STATE['omitted'], icon='INFO')
        if _STATE['source_errors']:
            box.label(text='Часть источников недоступна · см. ZIP', icon='ERROR')
        box.label(text='READ ONLY · без изменения Mesh/UV', icon='LOCKED')


CLASSES = (IMA_GT_UnifiedIssueDragGrip,
           IMA_OT_UnifiedIssueBottomToggle, IMA_OT_UnifiedIssueBottomClose,
           IMA_OT_UnifiedIssueBottomMove,
           IMA_OT_UnifiedIssueRefresh, IMA_OT_UnifiedIssueStep,
           IMA_OT_UnifiedIssueFocus, IMA_OT_UnifiedIssueHide,
           IMA_OT_UnifiedIssueDetails, IMA_OT_UnifiedIssueCopy,
           IMA_OT_UnifiedHudAcceptanceLab,
           IMA_PT_UnifiedIssueNavigator, IMA_GGT_UnifiedIssueBottom)


def register():
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.WindowManager.ima_unified_issue_kind = bpy.props.EnumProperty(
            name='Тип проблемы', items=(
                ('ALL', 'Все', ''), ('PROVEN_INVALID', 'Доказанные', ''),
                ('WARNING', 'Предупреждения', ''), ('REVIEW', 'Проверить', ''),
                ('UNCERTAIN', 'Неопределённые', ''), ('OTHER', 'Прочие', '')),
            default='ALL', update=_on_filter_changed)
        bpy.types.WindowManager.ima_unified_issue_source = bpy.props.EnumProperty(
            name='Источник', items=(
                ('ALL', 'Все', ''), ('RELIABILITY', 'Reliability', ''),
                ('BRICK_HOLE', 'Brick Hole', ''), ('UV_SEAMS', 'UV Seam', ''),
                ('COLLISION', 'Collision', '')),
            default='ALL', update=_on_filter_changed)
        bpy.types.WindowManager.ima_unified_issue_hud_position = bpy.props.EnumProperty(
            name='Положение HUD', items=(('CENTER', 'Центр', ''),
                                         ('LEFT', 'Слева', ''), ('RIGHT', 'Справа', ''),
                                         ('CUSTOM', 'Свободно', 'Перемещённая панель')), 
            default='CENTER', update=_on_filter_changed)
        bpy.types.WindowManager.ima_unified_issue_hud_locked = bpy.props.BoolProperty(
            name='Закрепить HUD', default=True, description='Запретить перемещение нижней панели')
        bpy.types.WindowManager.ima_unified_issue_hud_motion = bpy.props.BoolProperty(
            name='Анимация HUD', default=True, description='Мягкий hover, без движения кликабельных кнопок')
        bpy.types.WindowManager.ima_unified_issue_custom_x = bpy.props.FloatProperty(
            name='X панели', default=.5, min=0., max=1., update=_on_filter_changed)
        bpy.types.WindowManager.ima_unified_issue_custom_y = bpy.props.FloatProperty(
            name='Y панели', default=.18, min=0., max=1., update=_on_filter_changed)
        _register_event_hooks()
        _install_gpu()
        _install_bottom_gpu()
    except Exception:
        _unregister_event_hooks()
        _uninstall_gpu()
        _uninstall_bottom_gpu()
        for prop in ('ima_unified_issue_kind', 'ima_unified_issue_source', 'ima_unified_issue_hud_position',
                     'ima_unified_issue_hud_locked', 'ima_unified_issue_hud_motion',
                     'ima_unified_issue_custom_x', 'ima_unified_issue_custom_y'):
            if hasattr(bpy.types.WindowManager, prop):
                delattr(bpy.types.WindowManager, prop)
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError, ReferenceError):
                pass
        raise


def unregister():
    close()
    owns_ui = lifecycle_lease.alive(_HOOK_OWNER, _HOOK_TOKEN)
    _unregister_event_hooks()
    _uninstall_gpu()
    _uninstall_bottom_gpu()
    if owns_ui:
        for prop in ('ima_unified_issue_kind', 'ima_unified_issue_source', 'ima_unified_issue_hud_position',
                     'ima_unified_issue_hud_locked', 'ima_unified_issue_hud_motion',
                     'ima_unified_issue_custom_x', 'ima_unified_issue_custom_y'):
            if hasattr(bpy.types.WindowManager, prop):
                delattr(bpy.types.WindowManager, prop)
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError, ReferenceError):
            pass
