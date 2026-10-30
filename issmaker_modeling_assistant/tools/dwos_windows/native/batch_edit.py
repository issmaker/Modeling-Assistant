from __future__ import annotations

import json
import bpy
from bpy.types import Operator
from bpy.props import BoolProperty, EnumProperty

from .numeric import parse_meter_text, format_meter
from .glass_fit_core import fit_to_frame_depth
from .batch_core import WindowRecord, scope_ids, summarize_scope
from .dwos_log import event as log_event, exception as log_exception
from .dwos_history import push as history_push
from .window_invariants import enforce_corner_settings, POLICY as CORNER_INVARIANT_POLICY
from .copy_layout_core import plan_copy_layout, partition_copy_targets
from .copy_orientation_core import needs_horizontal_reflection, mirror_split_rows
from .payload_core import decode_opening_payload, adaptive_topology_from_payload


_LAST_COPY_RESULT = {'applied': 0, 'skipped': 0, 'total': 0, 'skip_names': ()}


def last_copy_result():
    """Snapshot for Composer feedback; never estimate applied from scope."""
    return dict(_LAST_COPY_RESULT)


def _remember_copy_result(*, applied=0, skipped=(), total=0):
    _LAST_COPY_RESULT.update(applied=int(applied), skipped=len(skipped), total=int(total),
                             skip_names=tuple(obj.name for obj, _why in skipped))


def resolve_window(obj):
    # 3.5.7: a manually edited reference frame still resolves to its source
    # DWOS controller, so the sidebar can always offer «МОЯ/ТВОЯ РАМА».
    if obj is not None:
        source_name = str(obj.get('DWOS_reference_source', '') or '')
        if source_name:
            source = bpy.data.objects.get(source_name)
            settings = getattr(source, 'dwos_window', None) if source is not None else None
            if settings and settings.is_window:
                return source
    cur = obj
    for _ in range(8):
        if cur is None:
            break
        settings = getattr(cur, 'dwos_window', None)
        if settings and settings.is_window:
            return cur
        cur = cur.parent
    return None


def _live_windows(scene, host_name=None):
    out = []
    for obj in scene.objects:
        settings = getattr(obj, 'dwos_window', None)
        if not settings or not settings.is_window or settings.finalized or obj.get('DWOS_archived_source'):
            continue
        if host_name and settings.host_name != host_name:
            continue
        out.append(obj)
    return out


def _record(obj):
    s = obj.dwos_window
    return WindowRecord(
        window_id=s.window_id or obj.name,
        host_name=s.host_name,
        width=parse_meter_text(s.width_text),
        height=parse_meter_text(s.height_text),
    )


def target_windows(context, active=None, scope=None):
    active = resolve_window(active or context.active_object)
    if active is None:
        return []
    scope = scope or context.scene.dwos.batch_scope
    windows = _live_windows(context.scene, active.dwos_window.host_name)
    records = [_record(obj) for obj in windows]
    selected_ids = []
    for obj in context.selected_objects:
        ctrl = resolve_window(obj)
        if ctrl is not None and ctrl.dwos_window.host_name == active.dwos_window.host_name:
            selected_ids.append(ctrl.dwos_window.window_id or ctrl.name)
    try:
        tolerance = parse_meter_text(context.scene.dwos.same_size_tolerance_text)
    except Exception:
        tolerance = 0.01
    ids = set(scope_ids(records, active.dwos_window.window_id or active.name, selected_ids, scope, tolerance))
    return [obj for obj in windows if (obj.dwos_window.window_id or obj.name) in ids]




def _classification(obj):
    cls=str(obj.get('DWOS_adaptive_classification','') or '').upper()
    if cls:
        return cls
    try:
        payload=json.loads(obj.dwos_window.opening_payload_json or '{}')
        topo=payload.get('adaptive_topology') or {}
        return str(topo.get('classification') or payload.get('adaptive_classification') or 'PLANAR').upper()
    except Exception:
        return 'PLANAR'


def _enforce_target_invariants(obj, source):
    changes=enforce_corner_settings(obj.dwos_window,_classification(obj))
    if changes:
        log_event('CORNER_INVARIANT_PROTECTED',window=obj.name,source=source,policy=CORNER_INVARIANT_POLICY,
                  changes=','.join(f'{k}:{old}->{new}' for k,(old,new) in changes.items()))
    return changes

def _split_snapshot(settings):
    return [
        {
            'split_id': sp.split_id,
            'axis': sp.axis,
            'position_text': sp.position_text,
            'thickness_text': sp.thickness_text,
            'override_thickness': bool(sp.override_thickness),
        }
        for sp in settings.splits
    ]


def _restore_splits(settings, rows):
    settings.splits.clear()
    for row in rows:
        sp = settings.splits.add()
        sp.split_id = row['split_id']
        sp.axis = row['axis']
        sp.position_text = row['position_text']
        sp.thickness_text = row['thickness_text']
        sp.override_thickness = row['override_thickness']
    settings.active_split_index = -1


COPY_PROPS = (
    'frame_width_text', 'internal_width_text', 'frame_depth_text', 'frame_offset_text',
    'optimization', 'interior_mode', 'interior_depth_text', 'interior_groups_count','interior_partition_mode','interior_joint_gap_text', 'interior_clearance_text',
    'interior_dividers_json', 'interior_offset_text', 'glass_offset_text', 'glass_edge_offset_text', 'glass_gap_text',
    'flip_interior', 'corner_post_mode','corner_post_variant','corner_post_width_text', 'frame_material',
)


def _snapshot(obj):
    s = obj.dwos_window
    return {'props': {name: getattr(s, name) for name in COPY_PROPS}, 'splits': _split_snapshot(s), 'zone_tree': s.zone_tree_json, 'branch_state': getattr(s, 'branch_state_json', '')}


def _restore(obj, snapshot):
    s = obj.dwos_window
    for name, value in snapshot['props'].items():
        setattr(s, name, value)
    _restore_splits(s, snapshot['splits'])
    s.zone_tree_json=snapshot['zone_tree']
    s.branch_state_json=snapshot.get('branch_state', '')


def _horizontal_copy_flip(source, target):
    """Read frame basis only: never use selected face or viewport projection."""
    try:
        a=source.matrix_world.to_3x3()
        b=target.matrix_world.to_3x3()
        return needs_horizontal_reflection(tuple(a.col[0]),tuple(b.col[0]),
                                           tuple(a.col[1]),tuple(b.col[1]))
    except (AttributeError,ReferenceError,RuntimeError,ValueError,TypeError):
        # Missing/stale objects must not trigger an invented reflection.
        return False


def _copy_layout_plan(source, target, *, future_props=None):
    ss=source.dwos_window;ts=target.dwos_window
    st=adaptive_topology_from_payload(decode_opening_payload(ss.opening_payload_json))
    tt=adaptive_topology_from_payload(decode_opening_payload(ts.opening_payload_json))
    future_props=future_props or {}
    mirror = _horizontal_copy_flip(source,target)
    plan = plan_copy_layout(
        st,tt,source_zone_tree=ss.zone_tree_json,
        source_branch_state=getattr(ss,'branch_state_json',''),
        source_splits=_split_snapshot(ss),
        target_width=parse_meter_text(ts.width_text),
        target_height=parse_meter_text(ts.height_text),
        target_frame_width=parse_meter_text(ts.frame_width_text),
        target_internal_width=parse_meter_text(future_props.get('internal_width_text',ts.internal_width_text)),
        target_frame_depth=parse_meter_text(future_props.get('frame_depth_text',ts.frame_depth_text)),
        target_post_mode=future_props.get('corner_post_mode',ts.corner_post_mode),
        mirror_horizontal=mirror,
    )
    if mirror and plan['mode']=='STANDARD':
        # Legacy Split positions are distances from the *left* opening edge.
        # Mirror on target width after successful topology preflight.
        plan['splits']=mirror_split_rows(plan['splits'],parse_meter_text(ts.width_text),
                                         parse_meter_text,format_meter,
                                         source_width=parse_meter_text(ss.width_text))
    return plan


def _apply_layout_plan(settings,plan):
    _restore_splits(settings,plan['splits'])
    settings.zone_tree_json=plan['zone_tree_json']
    settings.branch_state_json=plan['branch_state_json']


def _depth_fitted_updates(settings, new_depth_text, desired_optimization=None):
    """Preflight the *future* state of each window, before any mesh mutation."""
    d = parse_meter_text(new_depth_text)
    gap = parse_meter_text(settings.glass_gap_text)
    glass_offset = parse_meter_text(settings.glass_offset_text, allow_negative=True)
    interior_offset = parse_meter_text(settings.interior_offset_text, allow_negative=True)
    full = (desired_optimization or settings.optimization) == 'FULL'
    fit = fit_to_frame_depth(d, gap, glass_offset, interior_offset, full)
    updates = {
        'glass_gap_text': format_meter(fit.gap),
        'glass_offset_text': format_meter(fit.glass_offset),
        'interior_offset_text': format_meter(fit.interior_offset),
    }
    return updates, fit.warnings


def apply_property_to_scope(context, active, prop_name, value, *, explicit_targets=None):
    from .window_builder import rebuild_window
    targets = list(explicit_targets) if explicit_targets is not None else target_windows(context, active)
    if not targets:
        raise ValueError('Нет окон в выбранной области')
    snapshots = {obj.name: _snapshot(obj) for obj in targets}
    plan = {}
    # A depth or FULL conversion can make old glass offsets impossible.
    # Fit the dependent properties first for the entire batch, then mutate once.
    for obj in targets:
        # PURE PREFLIGHT: must not modify a target before the rollback-protected try.
        # Mandatory CORNER/CURVED invariants are enforced only during commit.
        effective=value
        if prop_name=='optimization' and _classification(obj) in {'CORNER','CURVED'}:
            effective='FULL'
        if prop_name in {'frame_depth_text', 'optimization'}:
            new_depth = effective if prop_name == 'frame_depth_text' else obj.dwos_window.frame_depth_text
            new_mode = effective if prop_name == 'optimization' else ('FULL' if _classification(obj) in {'CORNER','CURVED'} else None)
            extra, warnings = _depth_fitted_updates(obj.dwos_window, new_depth, new_mode)
        else:
            extra, warnings = {}, ()
        # A mode switch must preserve existing native CORNER profile positions;
        # never let branch-span changes silently rescale existing mullions.
        branch_plan=None
        if prop_name=='corner_post_mode' and _classification(obj)=='CORNER':
            from .corner_mode_core import plan_corner_mode_change
            sett=obj.dwos_window
            topology=adaptive_topology_from_payload(decode_opening_payload(sett.opening_payload_json))
            branch_plan=plan_corner_mode_change(
                topology,sett.branch_state_json,sett.corner_post_mode,effective,
                height=parse_meter_text(sett.height_text),
                frame_width=parse_meter_text(sett.frame_width_text),
                internal_width=parse_meter_text(sett.internal_width_text),
                frame_depth=parse_meter_text(sett.frame_depth_text),
                legacy_zone_tree=sett.zone_tree_json,legacy_splits=list(sett.splits),
                old_published_post_count=int(obj.get('DWOS_corner_post_count',0)))
        plan[obj.name] = (effective, extra, warnings, branch_plan)
    try:
        for obj in targets:
            effective, extra, warnings, branch_plan = plan[obj.name]
            setattr(obj.dwos_window, prop_name, effective)
            if branch_plan is not None:
                obj.dwos_window.branch_state_json=branch_plan
            _enforce_target_invariants(obj,'BATCH_PROPERTY_APPLY')
            for key, fitted in extra.items():
                if getattr(obj.dwos_window, key) != fitted:
                    setattr(obj.dwos_window, key, fitted)
            if warnings:
                log_event('GLASS_AUTOFIT', window=obj.name, reasons=','.join(warnings), depth=obj.dwos_window.frame_depth_text,
                          gap=obj.dwos_window.glass_gap_text, glass_offset=obj.dwos_window.glass_offset_text)
        for obj in targets:
            rebuild_window(obj)
    except Exception:
        for obj in targets:
            snap = snapshots.get(obj.name)
            if snap:
                _restore(obj, snap)
                try:
                    rebuild_window(obj)
                except Exception:
                    pass
        raise
    log_event('BATCH_PROPERTY_APPLY', scope=context.scene.dwos.batch_scope, property=prop_name, targets=len(targets), value=value)
    return targets


def apply_frame_material_to_scope(context, active, material):
    """Native Material pointer, no rebuilding meshes or modifying user shaders."""
    from .material_system import assign_frame_material
    targets = target_windows(context, active)
    if not targets:
        raise ValueError('Нет окон в выбранной области')
    old = {obj.name: obj.dwos_window.frame_material for obj in targets}
    try:
        for obj in targets:
            obj.dwos_window.frame_material = material
            assign_frame_material(obj)
    except Exception:
        for obj in targets:
            obj.dwos_window.frame_material = old[obj.name]
            assign_frame_material(obj)
        raise
    log_event('BATCH_FRAME_MATERIAL', scope=context.scene.dwos.batch_scope, targets=len(targets),
              material=(material.name if material else 'DWOS_Frame_Default'))
    return targets


def apply_optimization_to_scope(context, active, mode):
    targets=target_windows(context,active)
    if mode=='OPTIMIZED':
        compatible=[o for o in targets if _classification(o) not in {'CORNER','CURVED'}]
        skipped=[o for o in targets if _classification(o) in {'CORNER','CURVED'}]
        if skipped:
            log_event('OPTIMIZATION_SKIPPED_FULL_LOCK',targets=len(skipped),
                      names=';'.join(o.name for o in skipped))
        if not compatible:
            raise ValueError('Нет PLANAR окон для OPTIMIZED в этой области. CORNER/CURVED требуют FULL и двух слоёв стекла.')
        return apply_property_to_scope(context,active,'optimization',mode,explicit_targets=compatible)
    return apply_property_to_scope(context,active,'optimization',mode)



def copy_active_settings_to_scope(context, active, include_splits=True):
    """Copy active properties; optionally apply layouts only to compatible targets."""
    from .window_builder import rebuild_window
    active = resolve_window(active)
    if active is None:
        raise ValueError('Активное окно не найдено')
    targets = [obj for obj in target_windows(context, active) if obj is not active]
    if include_splits:
        plans, skipped = partition_copy_targets(targets, lambda obj: _copy_layout_plan(
            active, obj, future_props=_snapshot(active)['props']))
        applicable = list(plans)
    else:
        plans, skipped, applicable = {}, [], targets
    snapshots = {obj.name: _snapshot(obj) for obj in applicable}
    source = _snapshot(active)
    # Source-owned mullion IDs cannot be copied to a different window.
    # Preserve the recipient's authored selections; do not silently treat
    # unknown IDs as geometry or activate CUSTOM with an empty list.
    if source['props'].get('interior_partition_mode')=='CUSTOM':
        source['props'].pop('interior_partition_mode',None)
    source['props'].pop('interior_dividers_json',None)
    if include_splits:
        from .corner_post_policy_core import require_post_allowed
        for obj in applicable:
            if _classification(obj)=='CORNER':
                topo=adaptive_topology_from_payload(decode_opening_payload(obj.dwos_window.opening_payload_json))
                require_post_allowed(topo, source['props'].get('corner_post_mode',obj.dwos_window.corner_post_mode))
    # COPY Settings does NOT copy the Split tree. A changed CORNER separator
    # would otherwise change branch spans and silently move existing profiles.
    junction_replans={}
    if not include_splits:
        from .corner_post_policy_core import require_post_allowed
        for obj in applicable:
            if _classification(obj)=='CORNER':
                topo=adaptive_topology_from_payload(decode_opening_payload(obj.dwos_window.opening_payload_json))
                require_post_allowed(topo, source['props'].get('corner_post_mode',obj.dwos_window.corner_post_mode))
        from .corner_mode_core import plan_corner_mode_change
        for obj in applicable:
            if _classification(obj)!='CORNER':continue
            old=obj.dwos_window
            new_mode=source['props'].get('corner_post_mode',old.corner_post_mode)
            if new_mode==old.corner_post_mode:continue
            topo=adaptive_topology_from_payload(decode_opening_payload(old.opening_payload_json))
            before_width=parse_meter_text(old.frame_width_text)
            after_width=parse_meter_text(source['props'].get('frame_width_text',old.frame_width_text))
            before_depth=parse_meter_text(old.frame_depth_text)
            after_depth=parse_meter_text(source['props'].get('frame_depth_text',old.frame_depth_text))
            # Handle a simultaneous frame-size transfer separately: keeping
            # profile positions would require a full parameter migration.
            if (abs(before_width-after_width)>1e-8 or abs(before_depth-after_depth)>1e-8):
                from .branch_core import decode_state, rows_as_dicts
                state=decode_state(old.branch_state_json,topo)
                from .corner_rules import corner_posts
                previous_posts=corner_posts(topo,old.corner_post_mode,
                    parse_meter_text(old.internal_width_text),before_depth)
                if rows_as_dicts(state,topo,parse_meter_text(old.height_text),before_width,previous_posts):
                    raise ValueError('COPY настроек отклонён: смена угла и размеров рамы требует отдельного переноса раскладки')
            junction_replans[obj.name]=plan_corner_mode_change(
                topo,old.branch_state_json,old.corner_post_mode,new_mode,
                height=parse_meter_text(old.height_text),frame_width=before_width,
                internal_width=parse_meter_text(old.internal_width_text),frame_depth=before_depth,
                legacy_zone_tree=old.zone_tree_json,legacy_splits=list(old.splits),
                old_published_post_count=int(obj.get('DWOS_corner_post_count',0)))
    try:
        for obj in applicable:
            settings=obj.dwos_window
            for name, value in source['props'].items():
                setattr(settings, name, value)
            if include_splits:
                _apply_layout_plan(settings, plans[obj])
            elif obj.name in junction_replans:
                settings.branch_state_json=junction_replans[obj.name]
            _enforce_target_invariants(obj, 'BATCH_COPY')
        for obj in applicable:
            _enforce_target_invariants(obj, 'BATCH_COPY_PREFLIGHT')
            updates, warnings = _depth_fitted_updates(obj.dwos_window, obj.dwos_window.frame_depth_text)
            for key, fitted in updates.items():
                setattr(obj.dwos_window, key, fitted)
            if warnings:
                log_event('GLASS_AUTOFIT', window=obj.name, reasons=','.join(warnings), source='BATCH_COPY')
            rebuild_window(obj)
    except Exception:
        for obj in applicable:
            snap=snapshots.get(obj.name)
            if snap:
                _restore(obj,snap)
                try: rebuild_window(obj)
                except Exception: pass
        raise
    _remember_copy_result(applied=len(applicable), skipped=skipped, total=len(targets))
    log_event('BATCH_COPY_ACTIVE', scope=context.scene.dwos.batch_scope,
              applied=len(applicable), skipped=len(skipped), total=len(targets),
              include_splits=include_splits, source=active.name)
    return applicable


class DWOS_OT_batch_set_scope(Operator):
    bl_idname = 'dwos.batch_set_scope'
    bl_label = 'DWOS Scope'
    bl_options = {'INTERNAL'}
    scope: EnumProperty(items=[
        ('ACTIVE', 'ONE', ''), ('SELECTED', 'SELECTED', ''),
        ('SAME_SIZE', 'SAME SIZE', ''), ('ALL_HOST', 'ALL', ''),
    ], default='ACTIVE')

    def execute(self, context):
        context.scene.dwos.batch_scope = self.scope
        active = resolve_window(context.active_object)
        count = len(target_windows(context, active)) if active else 0
        log_event('BATCH_SCOPE', scope=self.scope, targets=count)
        return {'FINISHED'}


class DWOS_OT_batch_select_scope(Operator):
    bl_idname = 'dwos.batch_select_scope'
    bl_label = 'Выделить область'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active = resolve_window(context.active_object)
        targets = target_windows(context, active)
        try:
            for obj in context.view_layer.objects:
                if obj.select_get():
                    obj.select_set(False)
            for obj in targets:
                obj.select_set(True)
            if active:
                context.view_layer.objects.active = active
        except Exception as exc:
            log_exception('BATCH_SELECT_WARNING', exc)
        log_event('BATCH_SELECT_SCOPE', scope=context.scene.dwos.batch_scope, targets=len(targets))
        self.report({'INFO'}, f'Выбрано окон: {len(targets)}')
        return {'FINISHED'}


def scope_mixed_values(context, active):
    """Honest MIXED summary, read-only, no Blender data mutation."""
    targets=target_windows(context,active)
    attrs=('frame_width_text','internal_width_text','frame_depth_text',
           'optimization','interior_mode','glass_edge_offset_text',
           'glass_offset_text','glass_gap_text')
    result={}
    for name in attrs:
        values=[str(getattr(o.dwos_window,name)) for o in targets]
        result[name]=values[0] if values and all(v==values[0] for v in values) else ('MIXED' if values else '-')
    return result


class DWOS_OT_batch_preview(Operator):
    bl_idname='dwos.batch_preview'
    bl_label='Предпросмотр массового применения'
    bl_description='Показать цели и смешанные настройки без изменения объектов'
    bl_options={'REGISTER'}

    def invoke(self,context,event):
        return context.window_manager.invoke_props_dialog(self,width=410)

    def draw(self,context):
        layout=self.layout
        active=resolve_window(context.active_object)
        if active is None:
            layout.label(text='Нет выбранного DWOS окна');return
        targets=target_windows(context,active)
        layout.label(text=f'Область: {context.scene.dwos.batch_scope} · окон: {len(targets)}')
        layout.label(text='Предпросмотр без изменения геометрии')
        if not targets:return
        title={
          'frame_width_text':'Основная рама',
          'internal_width_text':'Внутренние рамы',
          'frame_depth_text':'Глубина',
          'optimization':'Геометрия',
          'interior_mode':'Interior',
          'glass_edge_offset_text':'Край стекла',
          'glass_offset_text':'Глубина стекла',
          'glass_gap_text':'Между стеклами',
        }
        mixed=scope_mixed_values(context,active)
        box=layout.box()
        for key,label in title.items():
            row=box.row(align=True);row.label(text=label)
            row.label(text=mixed[key])
        layout.label(text='Для применения используйте отдельную кнопку Apply')

    def execute(self,context):
        return {'FINISHED'}


class DWOS_OT_batch_copy_active(Operator):
    bl_idname = 'dwos.batch_copy_active'
    bl_label = 'Применить настройки активного'
    bl_options = {'REGISTER'}
    include_splits: BoolProperty(default=True)

    def execute(self, context):
        active = resolve_window(context.active_object)
        if active is None:
            self.report({'ERROR'}, 'Выберите окно DWOS')
            return {'CANCELLED'}
        # Full preflight before Undo, mutation or costly window rebuild.
        all_targets=[obj for obj in target_windows(context,active) if obj is not active]
        try:
            if self.include_splits:
                plans, skipped=partition_copy_targets(all_targets,
                    lambda target:_copy_layout_plan(active,target,future_props=_snapshot(active)['props']))
                applicable=list(plans)
            else:
                applicable=all_targets
                skipped=[]
            if not applicable:
                _remember_copy_result(applied=0,skipped=skipped,total=len(all_targets))
                self.report({'INFO'}, f'COPY: 0 применено, {len(skipped)} пропущено')
                return {'FINISHED'}
            history_push(context,'BATCH_COPY_ACTIVE')
            targets=copy_active_settings_to_scope(context,active,self.include_splits)
        except Exception as exc:
            log_exception('BATCH_COPY_FAILED',exc,scope=context.scene.dwos.batch_scope)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},f'Настройки: {len(targets)} применено · {len(skipped)} несовместимых пропущено')
        return {'FINISHED'}


class DWOS_OT_batch_copy_splits(Operator):
    bl_idname='dwos.batch_copy_splits'
    bl_label='Скопировать раскладку импостов'
    bl_options={'REGISTER'}

    def execute(self,context):
        active=resolve_window(context.active_object)
        if active is None:
            return {'CANCELLED'}
        from .window_builder import rebuild_window
        all_targets=[obj for obj in target_windows(context,active) if obj is not active]
        try:
            plans,skipped=partition_copy_targets(all_targets,
                lambda target:_copy_layout_plan(active,target))
        except Exception as exc:
            log_exception('BATCH_SPLITS_PREFLIGHT_REJECTED',exc)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        if not plans:
            _remember_copy_result(applied=0,skipped=skipped,total=len(all_targets))
            self.report({'INFO'},f'COPY: применено 0 · пропущено {len(skipped)}')
            return {'FINISHED'}
        snapshots={obj.name:_snapshot(obj) for obj in plans}
        try:
            history_push(context,'BATCH_COPY_SPLITS')
            for obj, plan in plans.items():
                _apply_layout_plan(obj.dwos_window,plan)
            for obj in plans:
                rebuild_window(obj)
        except Exception as exc:
            for obj in plans:
                _restore(obj,snapshots[obj.name])
                try: rebuild_window(obj)
                except Exception: pass
            log_exception('BATCH_SPLITS_FAILED',exc)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        _remember_copy_result(applied=len(plans),skipped=skipped,total=len(all_targets))
        log_event('BATCH_SPLITS_APPLY',scope=context.scene.dwos.batch_scope,
                  applied=len(plans),skipped=len(skipped), total=len(all_targets),
                  native_corner=bool(getattr(active.dwos_window,'branch_state_json','')))
        self.report({'INFO'},f'COPY: {len(plans)} применено · {len(skipped)} пропущено')
        return {'FINISHED'}


CLASSES = (
    DWOS_OT_batch_set_scope,
    DWOS_OT_batch_select_scope,
    DWOS_OT_batch_preview,
    DWOS_OT_batch_copy_active,
    DWOS_OT_batch_copy_splits,
)
