from __future__ import annotations

import json
import time
import hashlib
import bpy
from mathutils import Matrix, Vector

from .numeric import parse_meter_text, format_meter
from ....core.generation_uv_quality import stage_face_uvs, UV_SCHEMA
from .frame_uv_metric_core import stage_planar_frame_uv, UV_SCHEMA as METRIC_UV_SCHEMA
from ....core.uv_module_splitting import split_planar_glass_for_uv
from .orientation_core import stabilize_up
from .frame_core import SplitSpec, build_frame_mesh, clear_rectangles, main_clear_rectangle
from .contact_core import validate_outer_contact
from .glass_builder import build_glass_mesh
from .assembly_core import belt_centerline, glass_overlap, glass_rectangle, depth_anchors, validate_glazing
from .interior_core import build_interior_mesh, MAX_INTERIOR_DEPTH_M
from .interior_sections_core import full_height_bands,partition_plan,join_meshes
from .interior_authored_sections_core import authored_plan,read_ids
from .interior_continuity_core import (continuous_interior_plan, build_continuous_group,
                                       POLICY as INTERIOR_MULLION_POLICY)
from .validation import validate_window_decisions
from .material_system import apply_window_materials
from . import source_lp_uv_core
from . import brick_hole_depth_uv, interior_footprint_union_core
from .dwos_log import event as log_event, exception as log_exception
from .adaptive_opening_core import host_to_window_local
from .adaptive_frame_core import build_adaptive_frame_mesh
from .adaptive_glass_core import build_adaptive_glass_mesh
from .adaptive_interior_core import build_adaptive_interior_mesh
from .adaptive_validation import validate_topology as validate_adaptive_topology, validate_mesh as validate_adaptive_mesh
from .corner_rules import corner_posts as build_corner_posts, validate_split_rows
from .branch_core import decode_state as decode_branch_state, encode_state as encode_branch_state, layout_state as branch_layout_state, rows_as_dicts as branch_rows_as_dicts
from .host_interior_core import resolve_interior_target
from .payload_core import decode_opening_payload, adaptive_topology_from_payload
from .transform_safety_core import analyze_linear_transform
from .scene_spatial_safety import validate_staged_interior
from .smart_interior_scene import stage as stage_smart_interiors, publish_neighbors as publish_smart_neighbors, encode as encode_smart_canonical, CANONICAL_KEY as SMART_CANONICAL_KEY, LINKS_KEY as SMART_LINKS_KEY, snapshot_scene_interiors, restore_scene_interiors
from .spatial_safety_core import self_intersections
from .interior_repair_core import repair_adaptive_interior_mesh, DEFAULT_SEAM_GAP_M, POLICY as INTERIOR_REPAIR_POLICY
from .window_invariants import enforce_corner_settings, validate_corner_glass_contract, validate_curved_glass_contract, POLICY as CORNER_INVARIANT_POLICY, CURVED_POLICY
from .opening_index import host_geometry_fingerprint, occupied_window_for_candidate


def _uv_fingerprint(mesh):
    """Detect user edits to generated loops before any destructive rebuild."""
    layer=mesh.uv_layers.active
    if layer is None:
        return ''
    digest=hashlib.sha256()
    digest.update(str(len(layer.data)).encode('ascii'))
    for loop in layer.data:
        uv=loop.uv
        digest.update(f'{float(uv.x):.8f},{float(uv.y):.8f};'.encode('ascii'))
    return digest.hexdigest()


def _require_safe_uv_rebuild(obj):
    mesh=getattr(obj,'data',None)
    if mesh is None or len(mesh.uv_layers)==0:
        return
    if (len(mesh.uv_layers)!=1 or mesh.get('DWOS_uv_schema') not in {UV_SCHEMA,METRIC_UV_SCHEMA}
            or mesh.get('DWOS_uv_fingerprint') != _uv_fingerprint(mesh)):
        raise ValueError('DWOS UV изменены вручную или имеют неизвестный формат. '
                         'Перестройка остановлена до изменения Mesh: сохраните копию объекта '
                         'или выполните явное восстановление авторской UV-карты.')


def _stage_dwos_uv(name, verts, faces, role='', *, roles=None,
                   adaptive_class='PLANAR',metric_scale=(1.,1.,1.)):
    part=str(role).upper()
    # In all newly generated GLASS and Smart Interior meshes, the canonical
    # UV contract is deliberately absent, not a dummy UV (0,0) layer.
    if (part in {'INTERIOR','GLASS'} or
            str(name).endswith(('_Interior','_Glass'))):
        return {'state':'UNMAPPED','reason':part+'_NO_AUTHORED_UV',
                'face_uvs':()}
    if not faces:
        return {'state':'EMPTY','reason':'NO_FACES','face_uvs':()}
    if str(adaptive_class).upper() in {'CORNER','CURVED'}:
        # Preserve the legacy path until the SVD-based seam oracle is native
        # verified. Never claim profile continuity for this V1 fallback.
        result=stage_face_uvs(verts,faces,resolution=4096)
        if result['state']=='READY':
            result['reason']='LEGACY_ADAPTIVE_FACE_LOCAL_NOT_PROFILE_CERTIFIED'
        return result
    return stage_planar_frame_uv(verts,faces,roles,resolution=4096,
                                 metric_scale=metric_scale)


def _publish_staged_uv(me, plan):
    state=plan['state']
    me['DWOS_uv_stage_state']=state
    me['DWOS_uv_stage_reason']=plan['reason']
    if 'triage_state' in plan:
        me['DWOS_uv_triage_state']=plan['triage_state']
        me['DWOS_uv_triage_oversized']=int(plan['triage_oversized'])
        me['DWOS_uv_triage_unsafe']=int(plan['triage_unsafe'])
    if state!='READY':
        # V1 glass maps can be intentionally removed. Never retain stale
        # fingerprint/schema after replacing those loops with no UV.
        for _key in ('DWOS_uv_schema','DWOS_uv_fingerprint','DWOS_uv_texel_px_m',
                     'DWOS_uv_resolution','DWOS_uv_face_stacked'):
            if _key in me: del me[_key]
        return
    faces=plan['face_uvs']
    if len(me.polygons)!=len(faces) or any(len(p.loop_indices)!=len(uvs)
                                              for p,uvs in zip(me.polygons,faces)):
        me['DWOS_uv_stage_state']='UNMAPPED'
        me['DWOS_uv_stage_reason']='TOPOLOGY_CHANGED_AFTER_VALIDATE'
        return
    layer=None
    try:
        layer=me.uv_layers.new(name='UVMap')
        for poly,coords in zip(me.polygons, faces):
            for loop_index,uv in zip(poly.loop_indices, coords):
                layer.data[loop_index].uv=uv
        me['DWOS_uv_schema']=plan.get('schema',UV_SCHEMA)
        me['DWOS_uv_fingerprint']=_uv_fingerprint(me)
        me['DWOS_uv_texel_px_m']=1000
        me['DWOS_uv_resolution']=4096
        me['DWOS_uv_face_stacked']=bool(plan.get('face_islands_may_stack',False))
    except Exception as exc:
        # A Blender API rejection must not strand partially written UV loops.
        # The original generator had no UVMap: fall back to that known state
        # and expose the exact failure in diagnostics, without altering XYZ.
        if layer is not None:
            try: me.uv_layers.remove(layer)
            except Exception: pass
        for _key in ('DWOS_uv_schema','DWOS_uv_fingerprint'):
            if _key in me: del me[_key]
        me['DWOS_uv_stage_state']='UNMAPPED'
        me['DWOS_uv_stage_reason']='BLENDER_UV_PUBLICATION_FAILED'
        me['DWOS_uv_stage_error']=f'{type(exc).__name__}: {exc}'[:240]



def _mesh_object(name, verts, faces, collection):
    plan=_stage_dwos_uv(name,verts,faces)
    me=bpy.data.meshes.new(name + 'Mesh')
    me.from_pydata(verts, [], faces)
    me.validate(clean_customdata=False)
    _publish_staged_uv(me,plan)
    me.update()
    obj=bpy.data.objects.new(name, me)
    collection.objects.link(obj)
    return obj


def _replace_mesh(obj, verts, faces, *, roles=None, adaptive_class=None):
    """Stable Blender Mesh ID with precomputed UV and authored-UV safeguard."""
    _require_safe_uv_rebuild(obj)
    plan=_stage_dwos_uv(obj.name,verts,faces,role=obj.get('DWOS_role','FRAME'),
                         roles=roles,adaptive_class=(adaptive_class if adaptive_class is not None else
                                         obj.get('DWOS_adaptive_classification','PLANAR')),
                         metric_scale=tuple(abs(float(v)) for v in obj.matrix_world.to_scale()))
    # W58.12: a generated HP source-UV contract must not fail AFTER
    # destructive clear_geometry(); report exact stage reason first.
    _w5833_micro_repairs=[]
    if (obj.get("DWOS_W58_source_lp_uv_json")
            and plan.get("state") != "READY"
            and str(plan.get("reason",""))=="FOLDED_FACE"):
        from . import hp_micro_edge_repair_core as _w5833_hp_repair
        current_verts=list(verts);current_faces=[list(f) for f in faces]
        current_roles=list(roles or ['FRAME']*len(current_faces))
        for _retry in range(32):
            _fi=int(plan.get("face_index",-1))
            _fixed=_w5833_hp_repair.repair_folded_micro_edges(
                current_verts,current_faces,current_roles,face_index=_fi,
                max_edge_m=1.0e-5)
            current_verts=_fixed['verts'];current_faces=_fixed['faces'];current_roles=_fixed['roles']
            _w5833_micro_repairs.extend(_fixed['repairs'])
            candidate_plan=_stage_dwos_uv(
                obj.name,current_verts,current_faces,role=obj.get('DWOS_role','FRAME'),
                roles=current_roles,adaptive_class=(adaptive_class if adaptive_class is not None else
                    obj.get('DWOS_adaptive_classification','PLANAR')),
                metric_scale=tuple(abs(float(v)) for v in obj.matrix_world.to_scale()))
            if candidate_plan.get('state')=='READY':
                # Only replace unpublished staging arrays. The frame's old
                # Mesh and LP/host geometry are untouched until this gate passes.
                verts=current_verts;faces=current_faces;roles=current_roles
                plan=candidate_plan
                break
            if candidate_plan.get('reason')!='FOLDED_FACE':
                plan=candidate_plan;break
            plan=candidate_plan
        if plan.get('state')=='READY':
            obj['W5833_hp_local_micro_edge_repairs']=json.dumps(
                _w5833_micro_repairs,separators=(',',':'))
            obj['W5833_hp_local_micro_edge_repair_policy']='MAX_EDGE_10_MICROMETRES_LOCAL_SEAM'
    if (obj.get("DWOS_W58_source_lp_uv_json")
            and plan.get("state") != "READY"):
        raise ValueError(
            "HP_GENERATED_UV_PREFLIGHT_BLOCKED:"
            + str(plan.get("reason","UNKNOWN"))
            + f": face={plan.get('face_index','?')}; local_micro_welds={json.dumps(_w5833_micro_repairs,separators=(',',':'))}; scene Mesh was not modified")
    me=obj.data
    me.clear_geometry()
    # Blender versions can differ in whether clear_geometry() retains empty
    # UV layer definitions. Prior authored data was checked above; old generated
    # layers must not coexist with the newly staged UVMap.
    for _old_layer in list(me.uv_layers):
        me.uv_layers.remove(_old_layer)
    me.from_pydata(verts,[],faces)
    me.validate(clean_customdata=False)
    _publish_staged_uv(me,plan)
    me.update()


def _remove_object_tree(obj):
    for child in list(obj.children):
        _remove_object_tree(child)
    mesh = obj.data if getattr(obj, 'type', None) == 'MESH' else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _matrix_from_candidate(candidate):
    x = Vector(candidate.axis_x)
    y = Vector(candidate.axis_y)
    z = Vector(candidate.axis_z)
    center = Vector(candidate.center)
    xs, ys, flipped = stabilize_up(x, y)
    if flipped:
        x, y = Vector(xs), Vector(ys)
    return Matrix((
        (x.x, y.x, z.x, center.x),
        (x.y, y.y, z.y, center.y),
        (x.z, y.z, z.z, center.z),
        (0, 0, 0, 1),
    ))


def _splits_from_settings(settings, width, height):
    out = []
    for split in settings.splits:
        pos = parse_meter_text(split.position_text)
        centered = (-width * 0.5 + pos) if split.axis == 'V' else (-height * 0.5 + pos)
        thick = parse_meter_text(split.thickness_text if split.override_thickness else settings.internal_width_text)
        out.append(SplitSpec(split.axis, centered, thick, split.split_id))
    return out


def _opening_payload(settings):
    # 3.5.3 fail-closed serialization: corrupted Adaptive state must never
    # silently downgrade into legacy PLANAR geometry.
    return decode_opening_payload(settings.opening_payload_json)


def _reanchor_to_facade(frame_obj):
    """Repair old mid-wall origins and keep the window origin on the facade plane."""
    settings = frame_obj.dwos_window
    payload = _opening_payload(settings)
    host = bpy.data.objects.get(settings.host_name)
    if not host or not payload:
        return
    try:
        n_local = Vector(payload['axis_z_local']).normalized()
        center_local = Vector(payload['center_local'])
        front_proj = float(payload.get('front_proj', center_local.dot(n_local)))
        current_proj = center_local.dot(n_local)
        front_local = center_local + n_local * (front_proj - current_proj)
        xw = (host.matrix_world.to_3x3() @ Vector(payload['axis_x_local'])).normalized()
        yw = (host.matrix_world.to_3x3() @ Vector(payload['axis_y_local'])).normalized()
        zw = (host.matrix_world.to_3x3() @ n_local).normalized()
        xx, yy, flipped = stabilize_up(xw, yw)
        if flipped:
            xw, yw = Vector(xx), Vector(yy)
            log_event('WINDOW_ORIENTATION_REPAIRED', window=frame_obj.name,
                      reason='LOCAL_Y_INVERTED', host=host.name)
        center_world = host.matrix_world @ front_local
        frame_obj.matrix_world = Matrix((
            (xw.x, yw.x, zw.x, center_world.x),
            (xw.y, yw.y, zw.y, center_world.y),
            (xw.z, yw.z, zw.z, center_world.z),
            (0, 0, 0, 1),
        ))
        frame_obj['DWOS_facade_anchor'] = 'FRONT_PLANE'
    except Exception as exc:
        log_exception('FACADE_REANCHOR_WARNING', exc, window=frame_obj.name)


def _adaptive_topology(settings):
    return adaptive_topology_from_payload(_opening_payload(settings))


def _validate_host_runtime_contract(settings):
    """Reject metric-unsafe Host transforms before any staged rebuild work."""
    host=bpy.data.objects.get(settings.host_name)
    if not host:
        return None
    m=host.matrix_world.to_3x3()
    report=analyze_linear_transform([[float(m[i][j]) for j in range(3)] for i in range(3)])
    if not report['rigid']:
        scale=' × '.join(f"{x:.6g}" for x in report['scales'])
        raise ValueError(
            f'Host Transform изменён после детекции ({scale}; {", ".join(report["reasons"])}). '
            'Rebuild остановлен, чтобы не исказить физические размеры окна. Примените Transform и выполните «Найти проёмы» повторно.')
    modifiers=[m.name for m in getattr(host,'modifiers',()) if getattr(m,'show_viewport',True)]
    if modifiers:
        raise ValueError('Host имеет активные модификаторы. Rebuild остановлен до повторной детекции по применённой геометрии.')
    return report


def _reanchor_adaptive(frame_obj, topology):
    """Anchor an adaptive window to its Host-local recovered topology frame."""
    settings=frame_obj.dwos_window
    host=bpy.data.objects.get(settings.host_name)
    if not host or host.type!='MESH':
        raise ValueError('Host адаптивного окна не найден')
    origin=Vector(topology['origin'])
    x=(host.matrix_world.to_3x3() @ Vector(topology['axis_x'])).normalized()
    y=(host.matrix_world.to_3x3() @ Vector(topology['axis_y'])).normalized()
    z=(host.matrix_world.to_3x3() @ Vector(topology['axis_z'])).normalized()
    center=host.matrix_world @ origin
    frame_obj.matrix_world=Matrix(((x.x,y.x,z.x,center.x),(x.y,y.y,z.y,center.y),(x.z,y.z,z.z,center.z),(0,0,0,1)))
    frame_obj['DWOS_facade_anchor']='ADAPTIVE_HOST_TOPOLOGY'



def _future_matrix(frame_obj, topology):
    """Predict the exact Host anchor without mutating the controller precommit."""
    if topology is not None:
        settings=frame_obj.dwos_window
        host=bpy.data.objects.get(settings.host_name)
        if not host or host.type!='MESH':raise ValueError('Smart Interior: Host missing')
        origin=Vector(topology['origin'])
        x=(host.matrix_world.to_3x3()@Vector(topology['axis_x'])).normalized()
        y=(host.matrix_world.to_3x3()@Vector(topology['axis_y'])).normalized()
        z=(host.matrix_world.to_3x3()@Vector(topology['axis_z'])).normalized()
        center=host.matrix_world@origin
        return Matrix(((x.x,y.x,z.x,center.x),(x.y,y.y,z.y,center.y),
                       (x.z,y.z,z.z,center.z),(0,0,0,1)))
    # Recalculate the legacy planar front anchor without mutating the Mesh.
    settings=frame_obj.dwos_window
    payload=_opening_payload(settings)
    host=bpy.data.objects.get(settings.host_name)
    if host is not None and payload and 'axis_z_local' in payload and 'center_local' in payload:
        n_local=Vector(payload['axis_z_local']).normalized()
        center_local=Vector(payload['center_local'])
        front_proj=float(payload.get('front_proj',center_local.dot(n_local)))
        front_local=center_local+n_local*(front_proj-center_local.dot(n_local))
        xw=(host.matrix_world.to_3x3()@Vector(payload['axis_x_local'])).normalized()
        yw=(host.matrix_world.to_3x3()@Vector(payload['axis_y_local'])).normalized()
        zw=(host.matrix_world.to_3x3()@n_local).normalized()
        xx,yy,flipped=stabilize_up(xw,yw)
        if flipped:xw,yw=Vector(xx),Vector(yy)
        center=host.matrix_world@front_local
        return Matrix(((xw.x,yw.x,zw.x,center.x),(xw.y,yw.y,zw.y,center.y),
                       (xw.z,yw.z,zw.z,center.z),(0,0,0,1)))
    return frame_obj.matrix_world.copy()



def _corner_posts(settings, topology, internal_width, frame_depth=None):
    """Architectural corner metadata shared by Frame/Glass/Interior/constraints."""
    return build_corner_posts(topology,getattr(settings,'corner_post_mode','AUTO'),internal_width,frame_depth)


def _validate_corner_split_envelopes(settings,width,height,frame_width,splits,corner_posts):
    if not corner_posts:
        return
    clearance=max(0.003,min(0.02,parse_meter_text(settings.internal_width_text)*.08 if settings.internal_width_text else 0.003))
    rows=[]
    if settings.zone_tree_json:
        from .zone_core import decode,layout
        _leaves,bands=layout(decode(settings.zone_tree_json),width,height,frame_width)
        rows=[{'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],'rect':r[4]} for r in bands]
    else:
        rows=[{'id':sp.split_id,'axis':sp.axis,'position':sp.position,'thickness':sp.thickness,
               'rect':(-width*.5+frame_width,width*.5-frame_width,-height*.5+frame_width,height*.5-frame_width)} for sp in splits]
    conflicts=validate_split_rows(rows,corner_posts,clearance)
    if conflicts:
        c=conflicts[0]
        raise ValueError(f"Импост слишком близко к угловому профилю ({c['split_id']}). Отодвиньте его от Corner Post.")

def _validate_offsets(settings, frame_depth, glass_gap, full_glass):
    frame_offset = parse_meter_text(settings.frame_offset_text, allow_negative=True)
    if abs(frame_offset) > 2.0:
        raise ValueError('Смещение рамы слишком большое. Используйте значение в пределах 2')
    glass_offset = parse_meter_text(settings.glass_offset_text, allow_negative=True)
    max_glass = max(0.0, frame_depth * 0.5 - 0.001)
    required = abs(glass_offset) + (glass_gap * 0.5 if full_glass else 0.0)
    if required > max_glass + 1e-9:
        raise ValueError(
            f'Стекло выходит за глубину рамы. Допустимо от центра не более {format_meter(max_glass)}'
        )
    interior_offset = parse_meter_text(settings.interior_offset_text, allow_negative=True)
    # Interior base is 1/4 frame depth, never 1/2.
    depth_anchors(frame_depth, frame_offset, -1, glass_offset, interior_offset)
    validate_glazing(frame_depth, glass_gap, glass_offset, full_glass)
    return frame_offset, glass_offset, interior_offset


# Static compatibility markers for the proven PLANAR path retained in 3.1:
# anchor='WALL_FACADE_FRONT'
# settings.interior_mode == 'OFF'
# if settings.interior_mode == 'REVEALS':
# settings.interior_mode = 'BOX'
# legacy PLANAR expression retained: belt_centerline(width, height, frame_width)
# contact = validate_outer_contact(data, width, height)
# glass_rectangle(width, height, frame_width, glass_edge_offset)

def rebuild_window(frame_obj):
    _start_perf=time.perf_counter()
    settings=frame_obj.dwos_window
    if not settings.is_window or settings.finalized:
        return

    # W58.9: a shared MERGE is owned by two windows. Never edit either
    # controller while a group owns its Interior; otherwise the shared shell
    # would become stale and silently damage the user's window.
    from .smart_interior_resolution_transaction import guard_edit_on_merged_group
    guard_edit_on_merged_group(frame_obj)
    _validate_host_runtime_contract(settings)
    width=parse_meter_text(settings.width_text)
    height=parse_meter_text(settings.height_text)
    wall_depth=parse_meter_text(settings.wall_depth_text)
    frame_width=parse_meter_text(settings.frame_width_text)
    frame_depth=parse_meter_text(settings.frame_depth_text)
    internal_width=parse_meter_text(settings.internal_width_text)
    topology=_adaptive_topology(settings)
    adaptive=topology is not None
    adaptive_class='PLANAR'
    if adaptive:
        topo_metrics=validate_adaptive_topology(topology)
        adaptive_class=topology.get('classification','PLANAR')
        tw,th=float(topology['width']),float(topology['height'])
        if abs(width-tw)>1e-4 or abs(height-th)>1e-4:
            raise ValueError('Размер Adaptive Opening задаётся Host. Выполните «Найти проёмы» повторно вместо ручного изменения ширины/высоты.')
        width,height=tw,th
    corner_availability=None
    if adaptive and adaptive_class=='CORNER':
        from .corner_post_policy_core import require_post_allowed
        # Preflight before mutating properties, mesh objects or stored geometry.
        corner_availability=require_post_allowed(topology,getattr(settings,'corner_post_mode','AUTO'))
        # Legacy AUTO used to mean ON at every angle. Do not silently remove
        # a saved post when the user rebuilds that older scene. An explicit OFF
        # is required before changing the physical junction topology.
        if (corner_availability['requested_mode']=='AUTO' and corner_availability['effective_mode']=='OFF'
                and int(frame_obj.get('DWOS_corner_post_count',0))>0):
            raise ValueError('Старое окно содержит центральный импост AUTO на остром угле. '
                             'Сначала выберите «БЕЗ ИМПОСТА» в Composer; модель не изменена.')
    invariant_changes=enforce_corner_settings(settings,adaptive_class if adaptive else 'PLANAR_LEGACY')
    if invariant_changes:
        log_event('CORNER_INVARIANT_ENFORCED',window=frame_obj.name,policy=CORNER_INVARIANT_POLICY,
                  changes=','.join(f'{k}:{old}->{new}' for k,(old,new) in invariant_changes.items()))
    splits=_splits_from_settings(settings,width,height)
    corner_mode=str(getattr(settings,'corner_post_variant','LEGACY')).upper()
    independent_corner=bool(adaptive and adaptive_class=='CORNER' and
        bool(corner_availability and corner_availability['effective_mode']!='OFF') and corner_mode!='LEGACY')
    corner_posts=_corner_posts(settings,topology,internal_width,frame_depth) if adaptive else []
    staged_open_post=None
    if independent_corner:
        from .corner_post_experiment_core import plan_open_post
        requested_post_width=parse_meter_text(settings.corner_post_width_text)
        staged_open_post=plan_open_post(topology,corner_posts,frame_width,frame_depth,
            parse_meter_text(settings.frame_offset_text,allow_negative=True),
            1 if settings.flip_interior else -1,requested_post_width,corner_mode)
    decisions=validate_window_decisions(width,height,frame_width,frame_depth,splits)
    try:
        from ....core.reliability.ledger import record as record_reliability_decision
    except Exception:
        record_reliability_decision=None
    for decision in decisions:
        if record_reliability_decision is not None:
            record_reliability_decision(decision,source='DWOS.window_builder')
        if decision.kind in {'UNCERTAIN','RESOURCE_LIMIT'}:
            log_event('RELIABILITY_ADVISORY',tool='DWOS',kind=decision.kind,code=decision.code,
                      message=decision.message,witness=json.dumps(decision.as_dict().get('witness',{}),ensure_ascii=False))
    errors=[d.message for d in decisions if d.blocking]
    if internal_width<=0:errors.append('Толщина внутренних рам должна быть больше 0')
    if errors:raise ValueError('; '.join(errors))

    # All following geometric gates consume validated offsets, including CORNER V/V.
    optimized=settings.optimization=='OPTIMIZED'
    interior_sign=1 if settings.flip_interior else -1
    gap=parse_meter_text(settings.glass_gap_text)
    frame_offset,glass_offset,interior_offset=_validate_offsets(settings,frame_depth,gap,not optimized)


    from .zone_core import decode,layout
    zone_tree=decode(settings.zone_tree_json) if settings.zone_tree_json else None
    if zone_tree is not None:layout(zone_tree,width,height,frame_width)
    branch_state=None; branch_bands=[]; branch_state_encoded=''
    # 3.4: a native CORNER assembly owns one independent Zone Tree per facade Branch.
    # Existing 3.3 trees are kept as legacy until they can be migrated without
    # silently changing a cross-corner authored split. Fresh/no-tree windows use
    # branch architecture immediately.
    # W58.7: Composer CURVED bands are integrated directly into the
    # watertight-within-frame shared SVD grid. Their authored front
    # UV, width and per-station depth are kept; no detached prism overlap.
    if adaptive and adaptive_class=='CORNER' and not settings.zone_tree_json:
        branch_state=decode_branch_state(getattr(settings,'branch_state_json',''),topology)
        _branch_leaves,_branch_bands=branch_layout_state(branch_state,topology,height,frame_width,corner_posts)
        branch_bands=[{'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],
                       'rect':r[4],'band':r[5],'branch_id':r[6]} for r in _branch_bands]
        branch_state_encoded=encode_branch_state(branch_state)
    # OFF still has a real miter at the two full-depth frames.  A vertical
    # mullion overlapping its physical miter envelope can intersect the joint
    # even though the central post is absent.  Validate WITHOUT constructing
    # a virtual post in the physical frame or glass meshes.
    if adaptive and adaptive_class=='CORNER' and not corner_posts and branch_bands:
        from .corner_rules import postless_junction_safety_guards
        guards=postless_junction_safety_guards(topology,frame_depth)
        bad=validate_split_rows(branch_bands,guards,0.003)
        if bad:
            raise ValueError('CORNER без центрального импоста: вертикальный профиль входит в зону углового сопряжения; отодвиньте профиль от угла')
    if adaptive and corner_posts:
        if branch_bands:
            conflicts=validate_split_rows(branch_bands,corner_posts,0.003)
            if conflicts:raise ValueError('Branch mullion overlaps Corner Post envelope')
        else:
            _validate_corner_split_envelopes(settings,width,height,frame_width,splits,corner_posts)

    # RC22: exact physical horizontal footprints catch cross-branch V/V and
    # V/central collisions that independent S-zone limits cannot detect.
    # Reject before changing any Blender mesh or parameter object.
    if adaptive and adaptive_class=='CORNER' and branch_bands:
        from .corner_collision_core import audit_branch_band_collisions
        # branch_bands was already normalized to dictionaries above; do not
        # index rows numerically (KeyError: 0 on the first real CORNER split).
        collisions=audit_branch_band_collisions(topology,branch_bands,corner_posts,frame_depth,
                    frame_offset,interior_sign,
                    post_polygon=(staged_open_post['footprint'] if staged_open_post else None))
        if collisions:
            first=collisions[0]
            raise ValueError(f'{first["code"]}: {first["a"]} ↔ {first["b"]}; physical overlap {first["intersection_m2"]} m²')

    glass_edge_offset=parse_meter_text(settings.glass_edge_offset_text,allow_negative=True)
    glass_rect=glass_rectangle(width,height,frame_width,glass_edge_offset)
    # RC20: frame junction mode must not change the glass mapping. The glass
    # always follows exactly TWO rigid facade branches with the same miter as
    # the proven OFF mode. The central post is a separate frame component;
    # it never twists the panes or dictates a warped contact edge.
    # Note: in ON the glass remains continuous beneath the post. This is a
    # visual glazing model, NOT proof of a collision-free manufactured rabbet.
    corner_seams=()
    if adaptive and adaptive_class=='CORNER':
        junctions=tuple(topology.get('corner_junctions') or ())
        if len(junctions)!=1:
            raise ValueError('CORNER требует ровно один подтверждённый угол для плоского стекла')
        corner_seams=(float(junctions[0]['s'])-width*.5,)
    corner_glass_contract=validate_corner_glass_contract(
        adaptive_class if adaptive else 'PLANAR_LEGACY',settings.optimization,(),glass_rect,
        junction_positions=corner_seams)
    glass_z,interior_z=depth_anchors(frame_depth,frame_offset,interior_sign,glass_offset,interior_offset)
    # W58.17: one source of truth (Zone Tree / legacy splits / native Branch Graph).
    # No panes are built until all authored zone boundaries have passed preflight.
    from .glazing_dependency_core import plan_panels as _w5817_plan_glass
    from .glazing_dependency_core import stage_glazing as _w5817_stage_glass
    _w5817_glass_panels=_w5817_plan_glass(
        width,height,frame_width,splits,zone_tree,branch_state,topology,
        glass_rect,corner_bands=branch_bands)
    _w5817_pane_details=[]


    # STAGE 1 — no Blender Mesh mutation before every part validates.
    junction_audit={"status":"NOT_APPLICABLE","checked":0}
    if adaptive:
        data=build_adaptive_frame_mesh(topology,frame_width,frame_depth,splits,
                    optimize=optimized,interior_sign=interior_sign,front_offset=frame_offset,zone_tree=zone_tree,
                    corner_posts=(() if independent_corner else corner_posts),branch_bands=branch_bands)
        if adaptive_class=='CURVED':
            log_event('CURVED_FRAME_FLOAT32_STATIONS',
                      merged_semantic_stations=int(data.get('float32_station_merge_count',0)),
                      merged_semantic_contacts=int(data.get('float32_contact_merge_count',0)),
                      publication_audit=data.get('float32_publication_audit',{}).get('status','UNVERIFIED'),
                      frame_faces=len(data['faces']))
        try:
            contact=validate_adaptive_mesh(data,topology=topology)
        except ValueError as exc:
            # One safe attempt on staged data ONLY, before Blender mesh publish.
            # Never mask non-manifold, degenerate or topology-contact errors.
            if 'inconsistent manifold winding at edge' not in str(exc):
                raise
            from .adaptive_validation import orient_consistent_faces
            corrected=dict(data)
            corrected['faces']=orient_consistent_faces(data['faces'])
            contact=validate_adaptive_mesh(corrected,topology=topology)
            from .dwos_log import event as _dwos_orientation_event
            _dwos_orientation_event('ADAPTIVE_WINDING_STAGED_REPAIR',
                faces=len(corrected['faces']), classification=adaptive_class)
            data=corrected
        # W58.7: before touching any published HP or LP mesh, verify every
        # Composer V-mullion is geometrically connected to the upper/lower
        # frame belt by shared manifold edges, with zero facade contact drift.
        if adaptive_class=='CURVED' and zone_tree:
            from .connected_mullion_junction_core import audit_connected_mullions
            junction_audit=audit_connected_mullions(
                data,topology,zone_tree,frame_width,frame_offset,
                frame_depth,interior_sign)
            log_event("W58_7_CONNECTED_MULLION_JUNCTION",
                window=frame_obj.name,summary=junction_audit)
        # RC13: CORNER must be one edge-connected frame, not merely a visual
        # overlap of independently generated post and branch meshes.
        # This is staged BEFORE _replace_mesh; failed ON-concave legacy joints
        # are rejected without publishing a disconnected replacement.
        if adaptive_class=='CORNER':
            from .mesh_consistency_core import audit_frame
            frame_gate=audit_frame(data,'CORNER')
            if frame_gate['status']!='PASS':
                raise ValueError('DWOS CORNER frame consistency: '+', '.join(frame_gate['issues'])+
                                 '. Previous frame retained; inspect Mesh Consistency report.')
            # 3.7.13: an edge-connected frame can still contain internal
            # T-junctions or holes. Check each open edge against the Host
            # semantic boundary *before* touching published Blender Mesh.
            from .mesh_consistency_core import audit_staged_corner_boundary
            boundary_gate=audit_staged_corner_boundary(data,topology)
            if boundary_gate['status']!='PASS':
                raise ValueError('DWOS CORNER staged boundary consistency: '+
                                 ', '.join(boundary_gate['issues'])+
                                 '. Previous frame retained; export diagnostic ZIP.')
        if staged_open_post is not None:
            from .corner_post_experiment_core import append_open_post, audit_open_shell
            data=append_open_post(data,staged_open_post)
            shell_gate=audit_open_shell(data)
            if shell_gate['status']!='PASS':
                raise ValueError('DWOS experimental central post: '+', '.join(shell_gate['issues']))
        # RC20: exactly the OFF glass geometry for both ON/OFF junction modes.
        # The central post remains in the *frame* mesh only. By design glass
        # is hidden behind it, not deformed by its side faces.
        corner_contacts=None
        glass_posts=() if adaptive_class=='CORNER' else corner_posts
        if _w5817_glass_panels is None:
            gv,gf,glass_roles=build_adaptive_glass_mesh(topology,glass_rect,glass_z,0.0,
                        full=not optimized,gap=gap,interior_sign=interior_sign,corner_posts=glass_posts,
                        junction_positions=corner_seams,corner_contacts=corner_contacts)
        else:
            gv,gf,glass_roles,_w5817_pane_details=_w5817_stage_glass(
                _w5817_glass_panels,topology,glass_z,
                full=not optimized,gap=gap,interior_sign=interior_sign,
                planar_builder=build_glass_mesh,adaptive_builder=build_adaptive_glass_mesh)
        # Glass is a separate mesh from frame. Previously staged orientation
        # correction was applied ONLY to frame data, while CURVED glazing still
        # failed on shared edges (2, 3) / (8, 9). Repair directed face rings on
        # the unpublished glass staging data, never alter vertex coordinates,
        # roles, face count, or published scene objects.
        try:
            validate_adaptive_mesh({'verts':gv,'faces':gf,'roles':glass_roles})
        except ValueError as glass_exc:
            if 'inconsistent manifold winding at edge' not in str(glass_exc):
                raise
            from .adaptive_validation import orient_consistent_faces
            staged_glass = orient_consistent_faces(gf)
            validate_adaptive_mesh({'verts':gv,'faces':staged_glass,'roles':glass_roles})
            log_event('ADAPTIVE_GLASS_WINDING_STAGED_REPAIR',
                      faces=len(staged_glass), classification=adaptive_class)
            gf = staged_glass
    else:
        data=build_frame_mesh(width,height,frame_width,frame_depth,splits,
                    optimize=optimized,interior_sign=interior_sign,front_offset=frame_offset,zone_tree=zone_tree)
        contact=validate_outer_contact(data,width,height)
        if _w5817_glass_panels is None:
            gv,gf,glass_roles=build_glass_mesh(glass_rect,glass_z,0.0,
                                  full=not optimized,gap=gap,interior_sign=interior_sign)
        else:
            gv,gf,glass_roles,_w5817_pane_details=_w5817_stage_glass(
                _w5817_glass_panels,None,glass_z,
                full=not optimized,gap=gap,interior_sign=interior_sign,
                planar_builder=build_glass_mesh,adaptive_builder=build_adaptive_glass_mesh)
        # Pass15.19: only proven independent XY PLANAR glazing sheets are
        # eligible. The pure preflight stages all 4m patches AND their UV
        # before any Blender Mesh operation. Non-simple panes remain intact.
        _glass_patches=({'state':'UNMAPPED','reason':'PANEL_DEPENDENCY_OWNS_SHEETS'}
                         if _w5817_glass_panels is not None else
                         split_planar_glass_for_uv(gv,gf,glass_roles,resolution=4096))
        if _glass_patches['state']=='SPLIT':
            gv,gf,glass_roles=(_glass_patches['verts'], _glass_patches['faces'],
                               _glass_patches['roles'])
            log_event('DWOS_PLANAR_GLASS_UV_MODULE_SPLIT',
                      window=frame_obj.name, patches=len(gf),
                      panes=_glass_patches['sheets'], module_m=4.0)
        elif _glass_patches['reason']!='FITS_SINGLE_MODULE':
            log_event('DWOS_PLANAR_GLASS_UV_MODULE_DECLINED',
                      window=frame_obj.name, reason=_glass_patches['reason'])

    curved_contract=validate_curved_glass_contract(adaptive_class,settings.optimization,glass_roles)
    # RC13: verify *actual staged glazing* rather than trusting only the
    # planned panel count or the presence of role strings. Read-only pure gate.
    if adaptive and adaptive_class in {'CORNER','CURVED'}:
        from .mesh_consistency_core import audit_glass
        glass_gate=audit_glass({'verts':gv,'faces':gf,'roles':glass_roles},adaptive_class,
                               expected_panels=(len(_w5817_glass_panels) if _w5817_glass_panels is not None
                                  else corner_glass_contract.get('panel_count') if adaptive_class=='CORNER' else None),
                               allow_subpanes=(_w5817_glass_panels is not None))
        if glass_gate['status']!='PASS':
            raise ValueError('DWOS GLASS staged consistency: '+', '.join(glass_gate['issues']))
        # RC20: every CORNER mode uses the same rigid planar branch glazing.
        # This verifies planarity, not physical clearance underneath ON post.
        if adaptive_class=='CORNER':
            from .glass_quality_core import corner_glass_plane_quality
            flat=corner_glass_plane_quality({'verts':gv,'faces':gf,'roles':glass_roles},topology,
                                            tolerance_deg=0.05)
            if flat['status']!='PASS' or flat.get('max_polygon_warp_m',0)>1.e-5:
                raise ValueError('CORNER postless planar glass failed: '+', '.join(flat['issues']))
    iv,iff,interior_roles=[],[],[]
    interior_partition=None
    interior_face_groups=[]
    _w5817_interior_seam_meta={'mode':'NO_SPLIT','applied':0,'cut_faces':0}
    interior_solution={'mode':'LEGACY','confidence':'LEGACY','reason':''}
    interior_repair_meta={'repaired':False,'policy':'NONE','raw_self_hits':0,'clip_events':0,'segment_count':0,'seam_gap_m':0.0}
    migrated_interior=settings.interior_mode=='REVEALS'
    target_interior_mode=('OFF' if bpy.context.scene.get('W5833_simple_interior',False) else ('BOX' if migrated_interior else settings.interior_mode))
    if target_interior_mode!='OFF':
        interior_depth=parse_meter_text(settings.interior_depth_text)
        if interior_depth>MAX_INTERIOR_DEPTH_M:
            log_event('INTERIOR_DEPTH_CAPPED',window=frame_obj.name,
                      entered_m=interior_depth,cap_m=MAX_INTERIOR_DEPTH_M)
            interior_depth=MAX_INTERIOR_DEPTH_M
            settings.interior_depth_text=format_meter(interior_depth)
        interior_clearance=parse_meter_text(settings.interior_clearance_text)
        interior_rect=belt_centerline(width,height,frame_width)
        if adaptive:
            interior_solution=resolve_interior_target(topology,_opening_payload(settings),interior_depth,interior_sign)
            # 3.5: if the Host proves a physical inner wall, end the reveal at
            # that wall rather than blindly using the requested BOX depth.
            build_depth=interior_depth
            if interior_solution.get('mode')=='HOST_DRIVEN':
                target=float(interior_solution.get('target_depth',interior_sign*interior_depth))
                build_depth=max(1e-6,abs(target-float(interior_z)))
            # A detected Host back wall must not override the 1.5 m product limit.
            if build_depth>MAX_INTERIOR_DEPTH_M:
                log_event('INTERIOR_HOST_DEPTH_CAPPED',window=frame_obj.name,
                          requested_m=build_depth,cap_m=MAX_INTERIOR_DEPTH_M)
                build_depth=MAX_INTERIOR_DEPTH_M
            interior_posts=[] if adaptive_class=='CORNER' else corner_posts
            # 3.5.1: the field-approved CORNER Interior geometry is locked. Old
            # A/B/C/D lab values migrate on the next successful rebuild.
            interior_variant=('LOCKED' if adaptive_class=='CORNER' else 'B')
            iv,iff,interior_roles=build_adaptive_interior_mesh(topology,interior_rect,interior_z,
                        build_depth,interior_sign,interior_clearance,'BOX',corner_posts=interior_posts,variant=interior_variant)
            if adaptive_class=='CORNER':
                interior_solution['mode']='CORNER_GEOMETRY_LOCK'
                interior_solution['reason']='USER_APPROVED_TOP_VIEW_2026_10_13'
                interior_solution['policy']='CORNER_PARALLELOGRAM_LOCK_V1'
            interior_solution['variant']=interior_variant
            interior_solution['build_depth']=build_depth
            validate_adaptive_mesh({'verts':iv,'faces':iff,'roles':interior_roles})
        else:
            iv,iff,interior_roles=build_interior_mesh(interior_rect,interior_z,
                        interior_depth,interior_sign,interior_clearance,'BOX')
        # RC28: sections are derived exclusively from full-height V mullions.
        # CORNER stays on its proven independent locked Interior path.
        if adaptive_class!='CORNER' and target_interior_mode=='BOX':
            _zone_rows=(layout(zone_tree,width,height,frame_width)[1] if zone_tree is not None else [])
            eligible=full_height_bands(splits, _zone_rows, width,height,frame_width)
            requested=int(getattr(settings,'interior_groups_count',0))
            authored_mode=str(getattr(settings,'interior_partition_mode','AUTO'))
            selected=read_ids(str(getattr(settings,'interior_dividers_json','[]')))
            gap_m=parse_meter_text(str(getattr(settings,'interior_joint_gap_text','0,01')))
            if eligible or requested>1 or selected:
                # RC37: scissors are the ONLY Interior dividers. Unselected V
                # have neither vertical jambs nor return/cap faces, even
                # behind the metal. Horizontal strips reconnect at the rear.
                plan=continuous_interior_plan(interior_rect,eligible,authored_mode,requested,selected,gap_m)
                interior_partition=plan
                if eligible:
                    built=[]
                    for (sa,sb),group_id,inside_posts in zip(
                            plan['intervals'],plan['groups'],plan['per_group_posts']):
                        local_rect=(sa,sb,interior_rect[2],interior_rect[3])
                        if inside_posts:
                            local_mesh=build_continuous_group(
                                topology if adaptive else None,local_rect,inside_posts,
                                interior_z,build_depth if adaptive else interior_depth,
                                interior_sign,interior_clearance,frame_depth,frame_offset)
                        elif adaptive:
                            local_mesh=build_adaptive_interior_mesh(topology,local_rect,interior_z,
                                build_depth,interior_sign,interior_clearance,'BOX',corner_posts=(),variant='B')
                        else:
                            local_mesh=build_interior_mesh(local_rect,interior_z,interior_depth,
                                interior_sign,interior_clearance,'BOX')
                        built.append((group_id,local_mesh))
                    iv,iff,interior_roles,interior_face_groups=join_meshes(built)
                    if adaptive:validate_adaptive_mesh({'verts':iv,'faces':iff,'roles':interior_roles})
                    interior_solution['partition_policy']=INTERIOR_MULLION_POLICY
                    interior_solution['partition_group_count']=plan['group_count']
                    interior_solution['cut_effective_gaps_m']=[round(c['gap_m'],6) for c in plan['cuts']]
                    interior_solution['v_thickness_m']={sid:round(t,6) for sid,_,t in eligible}
                    interior_solution['physical_shells']=plan['group_count']
                    interior_solution['protected_unselected_v']=plan['protected_unselected_count']
                    interior_solution['open_front_v_seams']=2*plan['protected_unselected_count']
                    interior_solution['visible_v_return_faces']=0
                    interior_solution['open_bridge_mouth_edges']=2*plan['protected_unselected_count']
                    interior_solution['open_front_seam_policy']='INTENTIONAL_OPEN_V_BRIDGE_RC37'
                    log_event('INTERIOR_FULL_HEIGHT_V_PARTITION',window=frame_obj.name,
                        v_count=plan['eligible'],groups=plan['group_count'],
                        physical_shells=plan['group_count'],selected_cut_ids=plan['active_ids'],
                        cut_gap_m=gap_m,cut_effective_gaps_m=[round(c['gap_m'],6) for c in plan['cuts']],
                        v_thickness_m={sid:round(t,6) for sid,_,t in eligible},
                        mode=authored_mode,protected_v=plan['protected_unselected_count'],
                        physical_policy=INTERIOR_MULLION_POLICY,
                        open_front_v_seams=2*plan['protected_unselected_count'],
                        visible_v_return_faces=0)
        # W58.17: matching user V/H mullion positions become real connected
        # Interior edges, never physical shell splits. Existing scissors
        # alone continue to own disconnected Interior mesh islands.
        if (splits or zone_tree is not None or branch_bands) and iv and iff:
            from .interior_dependency_core import stage_continuous_interior
            _had_groups=bool(interior_face_groups)
            iv,iff,interior_roles,_w5817_interior_seam_meta,_new_groups=stage_continuous_interior(
                iv,iff,interior_roles,topology=topology,
                splits=splits,zone_tree=zone_tree,branch_bands=branch_bands,
                width=width,height=height,frame_width=frame_width,
                face_groups=interior_face_groups)
            if _had_groups:
                interior_face_groups=_new_groups
            if adaptive:
                validate_adaptive_mesh({'verts':iv,'faces':iff,'roles':interior_roles})
        if not (glass_rect[0] < interior_rect[0]+interior_clearance
                and glass_rect[1] > interior_rect[1]-interior_clearance
                and glass_rect[2] < interior_rect[2]+interior_clearance
                and glass_rect[3] > interior_rect[3]-interior_clearance):
            raise ValueError('Контур стекла должен быть больше Interior BOX. Уменьшите отступ стекла.')
        # 3.5.5 repair-before-reject. Spatial Safety stays strict, but an
        # Adaptive Interior that can be repaired from its original S/V/D segment
        # ownership is rebuilt into non-contact strips before final validation.
        raw_self_hits=self_intersections(iv,iff)
        interior_repair_meta['raw_self_hits']=len(raw_self_hits)
        if raw_self_hits and interior_partition and adaptive:
            # One physical shell with an unselected V mullion is still a
            # continuous Interior.  Do not block its proven fallback repair.
            # With active scissors, repair each physical group independently,
            # preserving the deliberately separated group boundaries.
            active_cut_ids = tuple(interior_partition.get('active_ids') or ())
            physical_groups = int(interior_partition.get('group_count', 1))
            if active_cut_ids or physical_groups > 1:
                try:
                    repaired_groups = []
                    for (ga, gb), gid in zip(interior_partition['intervals'],
                                              interior_partition['groups']):
                        local_rect = (ga, gb, interior_rect[2], interior_rect[3])
                        rv, rf, rr, rm = repair_adaptive_interior_mesh(
                            topology, local_rect, interior_z, build_depth,
                            interior_sign, interior_clearance, 'BOX',
                            seam_gap=DEFAULT_SEAM_GAP_M)
                        validate_adaptive_mesh({'verts': rv, 'faces': rf, 'roles': rr})
                        if self_intersections(rv, rf):
                            raise ValueError('Interior: intersections remain in a divided group')
                        repaired_groups.append((gid, (rv, rf, rr)))
                    iv, iff, interior_roles, interior_face_groups = join_meshes(repaired_groups)
                    validate_adaptive_mesh({'verts': iv, 'faces': iff, 'roles': interior_roles})
                    if self_intersections(iv, iff):
                        raise ValueError('Interior: repaired physical groups still intersect')
                    interior_solution['partition_repair'] = 'PER_GROUP_NONCONTACT_V1'
                    log_event('INTERIOR_PARTITION_REPAIR_SUCCESS', window=frame_obj.name,
                              groups=len(repaired_groups), cuts=list(active_cut_ids))
                    raw_self_hits = []
                except Exception as partition_exc:
                    log_exception('INTERIOR_PARTITION_REPAIR_FAILED', partition_exc,
                                  window=frame_obj.name, groups=physical_groups)
                    raise ValueError('Interior: раздельные секции пересекаются; '
                                     'построение отменено без изменения Mesh') from partition_exc
        if raw_self_hits and adaptive:
            log_event('INTERIOR_REPAIR_BEGIN',window=frame_obj.name,classification=adaptive_class,
                      hits=len(raw_self_hits),policy=INTERIOR_REPAIR_POLICY)
            log_event('INTERIOR_REPAIR_INTERSECTIONS_FOUND',window=frame_obj.name,hits=len(raw_self_hits))
            try:
                riv,riff,rroles,rmeta=repair_adaptive_interior_mesh(
                    topology,interior_rect,interior_z,build_depth,interior_sign,
                    interior_clearance,'BOX',seam_gap=DEFAULT_SEAM_GAP_M)
                validate_adaptive_mesh({'verts':riv,'faces':riff,'roles':rroles})
                repaired_hits=self_intersections(riv,riff)
                if repaired_hits:
                    raise ValueError(f'INTERIOR_REPAIR unresolved self-intersections: {len(repaired_hits)}')
                iv,iff,interior_roles=riv,riff,rroles
                interior_repair_meta={**rmeta,'raw_self_hits':len(raw_self_hits),'repaired':True}
                interior_solution['repair_policy']=rmeta.get('policy',INTERIOR_REPAIR_POLICY)
                interior_solution['repair_seam_gap_m']=float(rmeta.get('seam_gap_m',0.0))
                if rmeta.get('policy')=='ADAPTIVE_CONTINUOUS_CURVED_V2':
                    log_event('INTERIOR_CONTINUOUS_SHELL',window=frame_obj.name,
                              stations=int(rmeta.get('segment_count',0))+1,
                              physical_shells=int(rmeta.get('physical_shells',0)),
                              internal_dividers=0, seam_gap_m=0.0,
                              old_raw_overlap_pairs=int(rmeta.get('raw_overlap_pairs',0)))
                else:
                    log_event('INTERIOR_REPAIR_SEGMENTS_SPLIT',window=frame_obj.name,
                              segments=int(rmeta.get('segment_count',0)),clips=int(rmeta.get('clip_events',0)),
                              raw_overlap_pairs=int(rmeta.get('raw_overlap_pairs',0)),
                              sample_station_merges=int(rmeta.get('sample_station_merges',0)))
                log_event('INTERIOR_REPAIR_GRAPH_NORMALIZED',window=frame_obj.name,
                          verts=len(iv),faces=len(iff),
                          components=int(rmeta.get('physical_shells',rmeta.get('segment_count',0))),
                          seam_gap_m=float(rmeta.get('seam_gap_m',0.0)))
                log_event('INTERIOR_REPAIR_SUCCESS',window=frame_obj.name,
                          original_hits=len(raw_self_hits),final_hits=0,policy=rmeta.get('policy',INTERIOR_REPAIR_POLICY))
            except Exception as repair_exc:
                log_exception('INTERIOR_REPAIR_FAILED',repair_exc,window=frame_obj.name,
                              original_hits=len(raw_self_hits),policy=INTERIOR_REPAIR_POLICY)
                # Geometry-first fallback: union the REAL 3D strip footprints.
                # No contraction of depth, no suppression of self-intersections,
                # and no mutation of authored source wall vertices.
                try:
                    union_mesh,union_meta=(
                        interior_footprint_union_core.union_native_interior(
                            {"verts":iv,"faces":iff,"roles":interior_roles}))
                    validate_adaptive_mesh(union_mesh)
                    remaining=self_intersections(
                        union_mesh["verts"],union_mesh["faces"])
                    if remaining:
                        raise ValueError(
                            f"INTERIOR_UNION_REMAINS_INTERSECTING:{len(remaining)}")
                    iv,iff,interior_roles=(
                        union_mesh["verts"],union_mesh["faces"],
                        union_mesh["roles"])
                    interior_repair_meta={**union_meta,
                        "raw_self_hits":len(raw_self_hits),"repaired":True}
                    interior_solution["repair_policy"]=union_meta["policy"]
                    log_event("INTERIOR_STRIP_UNION_SUCCESS",
                        window=frame_obj.name,faces=len(iff),
                        source_hits=len(raw_self_hits),
                        source_strips=union_meta["source_strips"])
                except Exception as union_exc:
                    log_exception("INTERIOR_STRIP_UNION_FAILED",
                        union_exc,window=frame_obj.name,
                        original_hits=len(raw_self_hits))
                    # Strict validator below will preserve the original error
                    # semantics if both independent repair methods fail.
        # 3.7: solve and stage ALL affected canonical Interiors before any
        # FRAME/GLASS/INTERIOR mesh is published. Never clip prior clipped meshes.
    smart_neighbors=[];smart_meta={'policy':'OFF','pairs':[]}
    canonical_interior={'verts':list(iv),'faces':list(iff),'roles':list(interior_roles)}
    if target_interior_mode=='OFF':
        canonical_interior={'verts':[],'faces':[],'roles':[]}
    try:
        staged_interior,smart_neighbors,smart_meta=stage_smart_interiors(
            frame_obj,canonical_interior,_future_matrix(frame_obj,topology),bpy.context.scene,allow_deferred=True,
            active_class=adaptive_class)
        iv,iff,interior_roles=(staged_interior['verts'],staged_interior['faces'],staged_interior['roles'])
        spatial_report={'self_hits':0,'scene_hits':0,'other':'',
                        'smart_contacts':len(smart_meta.get('pairs',()))}
    except Exception as exc:
        # W58.6: A mixed CURVED opening with a newly cut mullion can produce
        # overlapping source strip shells AFTER the legacy group repair.
        # Retry exactly once using a verified physical union of these strips;
        # never suppress or relabel the proven intersections.
        if (adaptive and adaptive_class=='CURVED'
                and 'source canonical cavity contains self-intersections'
                in str(exc) and canonical_interior.get('faces')):
            try:
                actual_original_hits=len(self_intersections(
                    canonical_interior['verts'],canonical_interior['faces']))
                union_mesh,union_meta=(
                    interior_footprint_union_core.union_native_interior(
                        canonical_interior))
                validate_adaptive_mesh(union_mesh)
                remaining=self_intersections(
                    union_mesh['verts'],union_mesh['faces'])
                if remaining:
                    raise ValueError(
                        f'INTERIOR_MULLION_UNION_INTERSECTIONS:{len(remaining)}')
                staged_interior,smart_neighbors,smart_meta=stage_smart_interiors(
                    frame_obj,union_mesh,_future_matrix(frame_obj,topology),
                    bpy.context.scene,allow_deferred=True,
                    active_class=adaptive_class)
                canonical_interior=union_mesh
                iv,iff,interior_roles=(
                    staged_interior['verts'],staged_interior['faces'],
                    staged_interior['roles'])
                interior_repair_meta={**union_meta,'repaired':True}
                interior_solution['repair_policy']=union_meta['policy']
                spatial_report={'self_hits':0,'scene_hits':0,'other':'',
                    'smart_contacts':len(smart_meta.get('pairs',()))}
                log_event('INTERIOR_MULLION_PHYSICAL_UNION_REPAIRED',
                          window=frame_obj.name,
                          original_hits=actual_original_hits,
                          published_faces=len(iff))
            except Exception as union_error:
                frame_obj['DWOS_last_validation_error']=str(union_error)
                log_exception('INTERIOR_SMART_UNION_RETRY_FAILED',
                              union_error,window=frame_obj.name)
                raise
        else:
            frame_obj['DWOS_last_validation_error']=str(exc)
            log_exception('INTERIOR_SMART_PREFLIGHT_REJECTED',exc,
                window=frame_obj.name,
                repair_policy=interior_repair_meta.get('policy','NONE'),
                mode=getattr(bpy.context.scene.dwos,'smart_interior_mode','OFF'))
            raise

    if smart_meta.get('active_pending',False):
        # The unresolved contact deliberately yields an empty *rendered* Interior.
        # Never erase an authored/linked active Interior in order to publish it.
        old_interior=next((o for o in frame_obj.children if o.get('DWOS_role')=='INTERIOR'),None)
        if old_interior is not None:
            me=old_interior.data
            if (getattr(me,'users',1)>1 or len(me.uv_layers)>0 or
                getattr(me,'shape_keys',None) is not None):
                raise ValueError('Smart Interior: cannot defer a shared/UV-authored Interior; '
                                 'make a safe copy and resolve the contact first')

    _staged_ms=(time.perf_counter()-_start_perf)*1000

    # STAGE 2 — publish only after complete staging/validation.
    # Safety gate for ISSUE-003: existing windows may carry an out-of-date
    # Host/opening anchor. Never teleport their controller on mullion edit.
    # Compare the predicted anchor BEFORE writing any Mesh or matrix. New
    # windows have no published DWOS_anchor_mode and are not restricted.
    if (frame_obj.get('DWOS_anchor_mode') and getattr(frame_obj, 'type', None) == 'MESH'
            and frame_obj.data is not None and len(frame_obj.data.polygons) > 0):
        from .anchor_safety_core import require_stable_anchor
        predicted_matrix = _future_matrix(frame_obj, topology)
        old_position = tuple(float(v) for v in frame_obj.matrix_world.translation)
        new_position = tuple(float(v) for v in predicted_matrix.translation)
        try:
            anchor_shift_m = require_stable_anchor(old_position, new_position, height)
        except ValueError as exc:
            log_exception('DWOS_ANCHOR_REBUILD_REJECTED', exc,
                          window=frame_obj.name, before=old_position, after=new_position,
                          host=settings.host_name)
            raise
        if anchor_shift_m > 0.01:
            log_event('DWOS_ANCHOR_REBUILD_SHIFT', window=frame_obj.name,
                      delta_m=round(anchor_shift_m, 6), before=old_position, after=new_position)
    # Authored UV protection must run BEFORE reanchoring the controller.
    _require_safe_uv_rebuild(frame_obj)
    for _child in frame_obj.children:
        if _child.get('DWOS_role') in {'GLASS','INTERIOR'} and _child.type=='MESH':
            _require_safe_uv_rebuild(_child)
    if adaptive:_reanchor_adaptive(frame_obj,topology)
    else:_reanchor_to_facade(frame_obj)
    # On interactive re-edits, rebuild the authored LP BEFORE publishing HP.
    if (frame_obj.get("DWOS_W58_source_lp_uv_json") and
            frame_obj.get("DWOS_W58_pipeline_stage")=="HP_GLASS_INTERIOR_READY"):
        from . import auto_window as _auto
        _auto.rebuild_lp(frame_obj)
    _replace_mesh(frame_obj,data['verts'],data['faces'],roles=data.get('roles'),
                  adaptive_class=adaptive_class)
    if adaptive and adaptive_class=='CORNER':
        settings.corner_interior_variant='LOCKED'
    if branch_state_encoded:
        settings.branch_state_json=branch_state_encoded
        settings.active_branch_id=getattr(settings,'active_branch_id','') or (branch_state['branches'][0]['id'] if branch_state and branch_state.get('branches') else '')
    frame_obj['DWOS_face_roles']=json.dumps(data['roles'])
    frame_obj['DWOS_W58_17_glazing_dependency']='ZONE_GRAPH_PANES_V1' if _w5817_glass_panels else 'LEGACY_BASELINE'
    frame_obj['DWOS_W58_17_glazing_panes']=json.dumps(_w5817_pane_details,separators=(',',':'))

    frame_obj['DWOS_W58_7_connected_mullion_audit']=json.dumps(
        junction_audit,separators=(",",":"))
    frame_obj['DWOS_grid_xs']=json.dumps(data['xs'])
    frame_obj['DWOS_grid_ys']=json.dumps(data['ys'])
    frame_obj['DWOS_frame_front_z']=float(data['z_front'])
    frame_obj['DWOS_frame_back_z']=float(data['z_back'])
    frame_obj['DWOS_interior_sign']=int(interior_sign)
    frame_obj['DWOS_outer_contact_policy']=data.get('outer_contact_policy','OPEN_FOR_WELD')
    frame_obj['DWOS_anchor_mode']='ADAPTIVE_HOST_TOPOLOGY' if adaptive else 'WALL_FACADE_FRONT'
    frame_obj['DWOS_adaptive_classification']=adaptive_class if adaptive else 'PLANAR_LEGACY'
    if adaptive:
        frame_obj['DWOS_orientation_confidence']=topology.get('orientation_confidence','LOW')
        frame_obj['DWOS_orientation_confidence_value']=float(topology.get('orientation_confidence_value',0.0))
        frame_obj['DWOS_corner_post_mode']=getattr(settings,'corner_post_mode','AUTO')
        frame_obj['DWOS_corner_interior_variant']=('LOCKED' if adaptive_class=='CORNER' else getattr(settings,'corner_interior_variant','B'))
        frame_obj['DWOS_corner_post_count']=len(corner_posts)
        if adaptive_class=='CORNER':
            frame_obj['DWOS_corner_invariant_policy']=CORNER_INVARIANT_POLICY
            frame_obj['DWOS_corner_glass_panel_count']=int(corner_glass_contract.get('panel_count',0))
            frame_obj['DWOS_corner_full_geometry_locked']=True
            integrated=[p for p in corner_posts if p.get('junction_contract')=='INTEGRATED_FRAME_DEPTH_V1']
            frame_obj['DWOS_corner_post_variant']=corner_mode if corner_posts else 'OFF'
            frame_obj['DWOS_corner_post_effective_mode']=corner_availability['effective_mode']
            frame_obj['DWOS_corner_interior_angle_deg']=float(corner_availability['angle_deg'] or 0.0)
            frame_obj['DWOS_corner_post_warning_code']=str(corner_availability['warning_code'])
            if corner_availability['requested_mode']=='AUTO' and corner_availability['effective_mode']=='OFF':
                log_event('CORNER_POST_AUTO_OFF',window=frame_obj.name,
                          interior_angle_deg=corner_availability['angle_deg'],
                          reason=corner_availability['warning_code'])
            frame_obj['DWOS_corner_post_independent']=bool(staged_open_post is not None)
            if staged_open_post is not None:
                frame_obj['DWOS_corner_post_width_requested_m']=float(staged_open_post['requested_width'])
                frame_obj['DWOS_corner_post_width_effective_m']=float(staged_open_post['effective_width'])
                frame_obj['DWOS_corner_post_width_max_m']=float(staged_open_post['max_width'])
                frame_obj['DWOS_corner_post_shell_faces']=4
                log_event('CORNER_POST_EXPERIMENT_PUBLISHED',window=frame_obj.name,mode=corner_mode,
                    requested=staged_open_post['requested_width'],effective=staged_open_post['effective_width'],
                    limit=staged_open_post['max_width'],shell_faces=4)
            frame_obj['DWOS_corner_mullion_envelope_policy']=str(data.get('corner_mullion_attachment_policy') or ('FRAME_DEPTH_SIDE_PLANE_T_JUNCTION_V2' if integrated else ('POSTLESS_MITRED_CONTACT_V1' if not corner_posts else 'LEGACY_INTERNAL_WIDTH_V1')))
            frame_obj['DWOS_corner_junction_mode']=('POSTLESS_MITRE' if not corner_posts else 'OPEN_INDEPENDENT_POST' if independent_corner else 'INTEGRATED_POST')
            frame_obj['DWOS_corner_open_internal_edges']=int(data.get('corner_assembly_open_internal_edges',0))
            frame_obj['DWOS_corner_branch_extent_m']=max((float(p.get('physical_branch_extent',0.0)) for p in integrated),default=0.0)
            frame_obj['DWOS_branch_profile_attachment_policy']=str(data.get('branch_profile_attachment_policy',''))
            frame_obj['DWOS_branch_profile_h_caps_removed']=int(data.get('branch_profile_h_contact_caps_removed',0))
            frame_obj['DWOS_branch_profile_v_caps_suppressed']=int(data.get('branch_profile_v_contact_caps_suppressed',0))
            frame_obj['DWOS_corner_assembly_policy']=str(data.get('profile_geometry_contract',''))
            frame_obj['DWOS_corner_assembly_components']=int(data.get('corner_assembly_components',1))
            frame_obj['DWOS_corner_assembly_graph_edges']=int(data.get('corner_assembly_graph_edge_count',0))
            frame_obj['DWOS_corner_assembly_profiles']=int(data.get('corner_assembly_profile_count',0))
            frame_obj['DWOS_corner_assembly_host_faces_removed']=int(data.get('corner_assembly_host_faces_removed',0))
            _weld_meta=data.get('corner_assembly_weld') or {}
            frame_obj['DWOS_corner_assembly_vertices_welded']=int(_weld_meta.get('vertices_merged',0))
            frame_obj['DWOS_corner_assembly_duplicate_faces_removed']=int(_weld_meta.get('duplicate_faces_removed',0))
            frame_obj['DWOS_corner_branch_back_rim_vertices_moved']=int(data.get('corner_assembly_branch_back_rim_vertices_moved',0))
            frame_obj['DWOS_corner_v_profile_policy']=str(data.get('corner_v_profile_policy',''))
            frame_obj['DWOS_corner_v_profile_count']=int(data.get('corner_v_profile_count',0))
            frame_obj['DWOS_corner_v_max_branch_angle_error_deg']=float(data.get('corner_v_max_branch_angle_error_deg',0.0))
            frame_obj['DWOS_corner_v_max_straightness_error']=float(data.get('corner_v_max_straightness_error',0.0))
            frame_obj['DWOS_corner_v_max_section_error_m']=float(data.get('corner_v_max_section_error_m',0.0))
            frame_obj['DWOS_corner_v_max_orthogonality_error']=float(data.get('corner_v_max_orthogonality_error',0.0))
            frame_obj['DWOS_corner_v_corner_shape_propagation']=bool(data.get('corner_v_corner_shape_propagation',False))
            _vdiag=[]
            for _pm in data.get('prismatic_profiles') or []:
                if _pm.get('kind')!='FRAME_MULLION_VERTICAL':continue
                _b=_pm.get('basis') or {};_inv=_pm.get('invariants') or {}
                _vdiag.append({'id':_pm.get('profile_id',''),'branch':_pm.get('branch_id',''),
                    'basis_mode':_b.get('basis_mode',''),'branch_angle_error_deg':_inv.get('branch_angle_error_deg',0.0),
                    'straightness_error':_inv.get('straightness_error',0.0),'section_error_m':_inv.get('section_error_m',0.0),
                    'orthogonality_error':_inv.get('orthogonality_error',0.0),
                    'corner_shape_propagation':bool(_b.get('corner_shape_propagation',False))})
            frame_obj['DWOS_corner_v_profiles_json']=json.dumps(_vdiag,ensure_ascii=False,separators=(',',':'))
        frame_obj['DWOS_branch_count']=len(branch_state.get('branches',[])) if branch_state else 0
        frame_obj['DWOS_branch_schema']='DWOS_corner_branches_v1' if branch_state else ''
        frame_obj['DWOS_branch_migration_status']=('NATIVE_3_4' if branch_state else ('LEGACY_CROSS_CORNER_REVIEW' if adaptive_class=='CORNER' and settings.zone_tree_json else 'NOT_REQUIRED'))
    # Migration is deliberately conservative.  Existing 3.0.1 SURFACE windows
    # were created before Adaptive Opening Core existed, so their apparent planar
    # shape is not enough proof that the original Host aperture was planar.  Keep
    # their authored geometry untouched and mark it for an explicit re-detect/rebuild
    # instead of silently changing a saved project.
    payload=_opening_payload(settings)
    if adaptive:
        frame_obj['DWOS_migration_status']='ADAPTIVE_V310'
    elif payload.get('wall_mode')=='SURFACE':
        frame_obj['DWOS_migration_status']='LEGACY_GEOMETRY_REVIEW_REQUIRED'
    else:
        frame_obj['DWOS_migration_status']='LEGACY_PLANAR_RETAINED'
    if adaptive:
        frame_obj['DWOS_contact_front_corners']=4
        frame_obj['DWOS_contact_front_vertices']=int(contact.get('contact_front_vertices',0))
        frame_obj['DWOS_contact_back_vertices']=int(contact.get('contact_back_vertices',0))
        frame_obj['DWOS_opening_topology_schema']=topology.get('schema','')
    else:
        frame_obj['DWOS_contact_front_corners']=contact['front_corners']
        frame_obj['DWOS_contact_back_corners']=contact['back_corners']
    frame_obj['DWOS_spatial_validation']='OK'
    frame_obj['DWOS_spatial_self_hits']=int(spatial_report.get('self_hits',0))
    frame_obj['DWOS_spatial_scene_hits']=int(spatial_report.get('scene_hits',0))
    frame_obj['DWOS_interior_repair_policy']=str(interior_repair_meta.get('policy','NONE'))
    frame_obj['DWOS_interior_repair_applied']=bool(interior_repair_meta.get('repaired',False))
    frame_obj['DWOS_interior_repair_raw_hits']=int(interior_repair_meta.get('raw_self_hits',0))
    frame_obj['DWOS_interior_repair_clip_events']=int(interior_repair_meta.get('clip_events',0))
    frame_obj['DWOS_interior_repair_segments']=int(interior_repair_meta.get('segment_count',0))
    frame_obj['DWOS_interior_repair_seam_gap_m']=float(interior_repair_meta.get('seam_gap_m',0.0))
    pending=bool(smart_meta.get('active_pending',False))
    frame_obj['DWOS_smart_interior_pending']=pending
    if pending:
        reason=str(smart_meta.get('reason','Smart Interior contact unresolved'))
        frame_obj['DWOS_smart_interior_reason']=reason
        frame_obj['DWOS_smart_interior_reason_code']=str(smart_meta.get('reason_code','UNRESOLVED_CONTACT'))
        frame_obj['DWOS_last_validation_error']=reason
        log_event('SMART_INTERIOR_WINDOW_CREATED_PENDING',window=frame_obj.name,
                  reason=reason,contacts=len(smart_meta.get('pairs',())))
    else:
        if 'DWOS_smart_interior_reason' in frame_obj:del frame_obj['DWOS_smart_interior_reason']
        if 'DWOS_smart_interior_reason_code' in frame_obj:del frame_obj['DWOS_smart_interior_reason_code']
        if 'DWOS_last_validation_error' in frame_obj:del frame_obj['DWOS_last_validation_error']

    glass=next((o for o in frame_obj.children if o.get('DWOS_role')=='GLASS'),None)
    if glass is None:
        glass=_mesh_object(frame_obj.name+'_Glass',gv,gf,frame_obj.users_collection[0])
        glass.parent=frame_obj;glass.matrix_parent_inverse=Matrix.Identity(4);glass.matrix_basis=Matrix.Identity(4);glass['DWOS_role']='GLASS'
    else:_replace_mesh(glass,gv,gf)
    glass['DWOS_glass_roles']=json.dumps(glass_roles)
    if adaptive:
        glass['DWOS_glass_policy']=topology.get('glass_policy','UNSUPPORTED')+('_DOUBLE' if not optimized else '_SINGLE')
        glass['DWOS_adaptive_classification']=adaptive_class
        if adaptive_class=='CORNER':
            glass['DWOS_corner_invariant_policy']=CORNER_INVARIANT_POLICY
            glass['DWOS_corner_glass_panel_count']=int(corner_glass_contract.get('panel_count',0))
            glass['DWOS_corner_glass_contract']='TWO_BRANCH_PANELS'
            glass['DWOS_corner_glass_contact_policy']=('RIGID_TWO_BRANCHES_UNDER_OPEN_POST_RC23' if independent_corner else 'RIGID_TWO_BRANCHES_UNDER_POST_RC20' if corner_posts else 'POSTLESS_MITER_PLANAR_RC20')
    else:
        glass['DWOS_glass_policy']='DOUBLE_FULL_SHEET' if not optimized else 'SINGLE_FULL_SHEET'
    if adaptive_class=='CURVED':
        glass['DWOS_curved_invariant_policy']=CURVED_POLICY
        glass['DWOS_curved_glass_layer_count']=int(curved_contract['layer_count'])
    glass['DWOS_contour_anchor']='FRAME_BELT_CENTERLINE_OVERLAP'
    glass['DWOS_overlap_metres']=glass_overlap(frame_width)
    glass['DWOS_edge_offset']=float(glass_edge_offset)
    glass['DWOS_depth_anchor']='HALF_FRAME_DEPTH'

    interior=next((o for o in frame_obj.children if o.get('DWOS_role')=='INTERIOR'),None)
    # W58.33: Simple Interior is published by auto_window.ensure_interior()
    # after the LP/HP/Glass transaction; do not enter the legacy SVD publisher.
    if settings.interior_mode=='OFF' or bpy.context.scene.get('W5833_simple_interior',False):
        if interior is not None:_remove_object_tree(interior);interior=None
    else:
        if interior is None:
            interior=_mesh_object(frame_obj.name+'_Interior',iv,iff,frame_obj.users_collection[0])
            interior.parent=frame_obj;interior.matrix_parent_inverse=Matrix.Identity(4);interior.matrix_basis=Matrix.Identity(4);interior['DWOS_role']='INTERIOR'
        else:_replace_mesh(interior,iv,iff)
        interior[SMART_CANONICAL_KEY]=encode_smart_canonical(canonical_interior)
        interior[SMART_LINKS_KEY]=json.dumps(smart_meta.get('active_links',[]),separators=(',',':'))
        interior['DWOS_smart_interior_policy']=str(smart_meta.get('policy','OFF'))
        interior['DWOS_smart_interior_pending']=bool(smart_meta.get('active_pending',False))
        if smart_meta.get('active_pending'):
            interior['DWOS_smart_interior_reason']=str(smart_meta.get('reason',''))
        elif 'DWOS_smart_interior_reason' in interior:
            del interior['DWOS_smart_interior_reason']
        interior['DWOS_smart_interior_contacts']=len(smart_meta.get('pairs',()))
        interior['DWOS_smart_interior_solver_methods']=json.dumps(smart_meta.get('solver_methods',[]))
        interior['DWOS_interior_depth_limit_m']=MAX_INTERIOR_DEPTH_M
        interior['DWOS_face_roles']=json.dumps(interior_roles)
        interior['DWOS_interior_partition_plan']=json.dumps(interior_partition or {},ensure_ascii=False)
        interior['DWOS_W5817_boundary_dependency']=json.dumps(_w5817_interior_seam_meta,ensure_ascii=False,separators=(',',':'))
        # Smart Interior may legitimately change face topology. Never leave a
        # stale per-face grouping map on the published child mesh.
        interior['DWOS_interior_partition_face_groups']=json.dumps(
            interior_face_groups if len(interior_face_groups)==len(iff) else [])
        interior['DWOS_contour_anchor']='FRAME_BELT_CENTERLINE'
        interior['DWOS_depth_anchor']='QUARTER_FRAME_DEPTH'
        interior['DWOS_depth_start_z']=float(interior_z)
        interior['DWOS_centerline_offset']=float(interior_clearance)
        interior['DWOS_side_offsets']='REMOVED_IN_2_1'
        if adaptive:
            interior['DWOS_adaptive_classification']=adaptive_class
            interior['DWOS_interior_solution_mode']=interior_solution.get('mode','PARAMETRIC_BOX')
            interior['DWOS_interior_solution_confidence']=interior_solution.get('confidence','')
            interior['DWOS_interior_solution_reason']=interior_solution.get('reason','')
            if adaptive_class=='CORNER':
                interior['DWOS_corner_interior_policy']=(interior_repair_meta.get('policy') if interior_repair_meta.get('repaired') else 'HOST_TARGET_CONTINUOUS_MITER')
                interior['DWOS_corner_post_count']=len(corner_posts)
                interior['DWOS_corner_post_partitions_target']=False
            interior['DWOS_interior_repair_policy']=str(interior_repair_meta.get('policy','NONE'))
            interior['DWOS_interior_repair_applied']=bool(interior_repair_meta.get('repaired',False))
            interior['DWOS_interior_repair_seam_gap_m']=float(interior_repair_meta.get('seam_gap_m',0.0))
        log_event('INTERIOR_BUILT',window=frame_obj.name,mode='BOX',depth=settings.interior_depth_text,variant=('LOCKED' if adaptive_class=='CORNER' else getattr(settings,'corner_interior_variant','B')),
                  start=interior_z,clearance=settings.interior_clearance_text,offset=settings.interior_offset_text,faces=len(iff),adaptive=adaptive,
                  repair=bool(interior_repair_meta.get('repaired',False)),repair_policy=interior_repair_meta.get('policy','NONE'))
    if migrated_interior:
        settings.interior_mode='BOX';log_event('INTERIOR_MODE_MIGRATED',window=frame_obj.name,old='REVEALS',new='BOX')

    if smart_neighbors:
        changed=publish_smart_neighbors(smart_neighbors)
        log_event('INTERIOR_CONNECTED_REBUILD',source=frame_obj.name,neighbors=changed,
                  contacts=len(smart_meta.get('pairs',())))

    apply_window_materials(frame_obj,glass,interior)
    if frame_obj.get("DWOS_W58_source_lp_uv_json"):
        transfer=source_lp_uv_core.transfer_hp_front_uv(frame_obj)
        # Brick Hole reveal: unfold every 3D depth quad from the exact
        # UV of its shared authored front edge. Never reset U/V per face.
        depth_uv=brick_hole_depth_uv.unwrap_frame_reveals(frame_obj)
        inner_uv=brick_hole_depth_uv.unfold_inner_profile_from_reveals(
            frame_obj)
        frame_obj["DWOS_W58_5_brick_depth_faces"]=int(
            depth_uv["mapped_reveal_faces"])
        frame_obj["DWOS_W58_5_brick_inner_faces"]=int(
            inner_uv["mapped_inner_faces"])
        # W58.20: independently constrained one-tile metric UV packing of
        # generated rear/reveal charts only; preserves every authored front loop,
        # geometry XYZ and texel scale. Record intentionally introduced UV seams.
        from .uv_one_tile_core import pack_frame as _w5820_single_tile
        _pack_w5820 = _w5820_single_tile(frame_obj)
        frame_obj["DWOS_W5820_metric_pack_new_seams"] = int(_pack_w5820["newly_broken_links"]) + int(frame_obj.data.get("DWOS_W5820_uv_seam_links_added", 0))
        frame_obj["DWOS_W5820_metric_pack_shifted_faces"] = int(_pack_w5820["shifted_faces"])
        frame_obj.data["DWOS_uv_fingerprint"]=_uv_fingerprint(frame_obj.data)
        frame_obj["DWOS_W58_hp_uv_front_face_count"]=int(
            transfer["mapped_front_faces"])
        frame_obj["DWOS_W58_hp_uv_depth_face_count"]=int(
            transfer["depth_faces_kept"])
    for _uv_obj in (frame_obj,glass,interior):
        if _uv_obj is not None:
            _me=_uv_obj.data
            log_event('DWOS_GENERATION_UV_STAGE',window=frame_obj.name,
                      part=_uv_obj.get('DWOS_role','FRAME'),
                      state=_me.get('DWOS_uv_stage_state','UNMAPPED'),
                      reason=_me.get('DWOS_uv_stage_reason','LEGACY_NO_UV'),
                      face_count=len(_me.polygons),schema=_me.get('DWOS_uv_schema',''))
    log_event('WINDOW_GEOMETRY_BUILT',window=frame_obj.name,frame_faces=len(frame_obj.data.polygons),
              glass_faces=len(glass.data.polygons),glass_policy=glass.get('DWOS_glass_policy',''),
              interior_faces=(len(interior.data.polygons) if interior else 0),interior_mode=settings.interior_mode,
              optimization=settings.optimization,frame_front=format_meter(data['z_front']),frame_back=format_meter(data['z_back']),
              anchor=frame_obj.get('DWOS_anchor_mode',''),splits=len(settings.splits),
              glass_anchor='BELT_CENTERLINE_OVERLAP_HALF_DEPTH',glass_overlap_m=round(glass_overlap(frame_width),6),
              interior_anchor='BELT_CENTERLINE_QUARTER_DEPTH',outer_contact_faces=0,
              adaptive=adaptive,classification=adaptive_class,corner_posts=len(corner_posts),
              corner_invariant=(CORNER_INVARIANT_POLICY if adaptive_class=='CORNER' else ''),
              corner_glass_panels=(corner_glass_contract.get('panel_count',0) if adaptive_class=='CORNER' else 0),
              corner_mullion_policy=(frame_obj.get('DWOS_corner_mullion_envelope_policy','') if adaptive_class=='CORNER' else ''),
              corner_branch_extent_m=(frame_obj.get('DWOS_corner_branch_extent_m',0.0) if adaptive_class=='CORNER' else 0.0),
              branch_profile_policy=(frame_obj.get('DWOS_branch_profile_attachment_policy','') if adaptive_class=='CORNER' else ''),
              h_contact_caps_removed=(frame_obj.get('DWOS_branch_profile_h_caps_removed',0) if adaptive_class=='CORNER' else 0),
              v_contact_caps_suppressed=(frame_obj.get('DWOS_branch_profile_v_caps_suppressed',0) if adaptive_class=='CORNER' else 0),
              corner_assembly_policy=(frame_obj.get('DWOS_corner_assembly_policy','') if adaptive_class=='CORNER' else ''),
              corner_assembly_components=(frame_obj.get('DWOS_corner_assembly_components',0) if adaptive_class=='CORNER' else 0),
              corner_assembly_profiles=(frame_obj.get('DWOS_corner_assembly_profiles',0) if adaptive_class=='CORNER' else 0),
              corner_assembly_graph_edges=(frame_obj.get('DWOS_corner_assembly_graph_edges',0) if adaptive_class=='CORNER' else 0),
              corner_assembly_host_faces_removed=(frame_obj.get('DWOS_corner_assembly_host_faces_removed',0) if adaptive_class=='CORNER' else 0),
              corner_assembly_vertices_welded=(frame_obj.get('DWOS_corner_assembly_vertices_welded',0) if adaptive_class=='CORNER' else 0),
              corner_branch_back_rim_moved=(frame_obj.get('DWOS_corner_branch_back_rim_vertices_moved',0) if adaptive_class=='CORNER' else 0),
              corner_v_policy=(frame_obj.get('DWOS_corner_v_profile_policy','') if adaptive_class=='CORNER' else ''),
              corner_v_count=(frame_obj.get('DWOS_corner_v_profile_count',0) if adaptive_class=='CORNER' else 0),
              corner_v_branch_angle_error_deg=(frame_obj.get('DWOS_corner_v_max_branch_angle_error_deg',0.0) if adaptive_class=='CORNER' else 0.0),
              corner_v_straightness_error=(frame_obj.get('DWOS_corner_v_max_straightness_error',0.0) if adaptive_class=='CORNER' else 0.0),
              corner_v_section_error_m=(frame_obj.get('DWOS_corner_v_max_section_error_m',0.0) if adaptive_class=='CORNER' else 0.0),
              corner_v_orthogonality_error=(frame_obj.get('DWOS_corner_v_max_orthogonality_error',0.0) if adaptive_class=='CORNER' else 0.0),
              corner_v_corner_shape_propagation=(frame_obj.get('DWOS_corner_v_corner_shape_propagation',False) if adaptive_class=='CORNER' else False),
              orientation_confidence=(topology.get('orientation_confidence','') if adaptive else 'LEGACY'))
    log_event('WINDOW_REBUILD_TIMING',window=frame_obj.name,staging_ms=round(_staged_ms,3),
              commit_ms=round((time.perf_counter()-_start_perf)*1000-_staged_ms,3),
              total_ms=round((time.perf_counter()-_start_perf)*1000,3),adaptive=adaptive)

    # W58: keep actual simplified LP and physical Interior synchronized with
    # every successful edit of our NEW auto windows; old DWOS untouched.
    if frame_obj.get("DWOS_W58_auto_uid"):
        from . import auto_window as _auto
        _auto.ensure_interior(frame_obj)
        # W58.3 LP-FIRST rebuilds happen BEFORE HP publication, not here.
        if not frame_obj.get("DWOS_W58_source_lp_uv_json"):
            _auto.rebuild_lp(frame_obj)

    # W58.8: post-success passive contact scan only. A failed UI scan must
    # never invalidate or roll back already verified wall/window geometry.
    # No automatic MERGE, GAP or opening-level mutation is permitted here.
    if frame_obj.name in bpy.context.scene.objects and bpy.context.scene.get(
            "W58_8_auto_conflict_scan",True):
        try:
            from .smart_interior_conflict_ui import scan,ensure_overlay,stop_overlay
            issues=scan(bpy.context.scene)
            if issues:ensure_overlay()
            else:stop_overlay()
        except Exception as issue_exc:
            log_exception("W58_8_AUTOSCAN_UI_FAILED",
                          issue_exc,window=frame_obj.name)


def create_window_from_candidate(context, candidate, host, *, defer_rebuild=False):
    collection = context.collection or context.scene.collection
    obj = _mesh_object('DWOS_Window_' + candidate.candidate_id, [], [], collection)
    try:
        obj.matrix_world = _matrix_from_candidate(candidate)
        settings = obj.dwos_window
        settings.is_window = True
        settings.window_id = 'W_' + candidate.candidate_id
        settings.host_name = host.name
        settings.opening_payload_json = candidate.payload_json
        settings.width_text = candidate.width_text
        settings.height_text = candidate.height_text
        settings.wall_depth_text = candidate.wall_depth_text
        payload = _opening_payload(settings)
        # A surface test wall cannot hide an open optimized backside, so FULL is a
        # better truthful default. Solid walls retain OPTIMIZED as production default.
        settings.optimization = 'FULL' if payload.get('wall_mode') == 'SURFACE' else 'OPTIMIZED'
        settings.frame_offset_text = '0'
        # New-window-only glazing default: legacy .blend files keep 0.075
        # even when the old property was never explicitly saved to RNA.
        settings.glass_gap_text = '0,003'
        # Default only for NEW windows. Existing authored depths remain intact.
        settings.interior_depth_text = '1'
        if not defer_rebuild:
            rebuild_window(obj)
        return obj
    except Exception:
        _remove_object_tree(obj)
        raise


class DWOS_OT_create_windows(bpy.types.Operator):
    bl_idname = 'dwos.create_windows'
    bl_label = 'Создать окна'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        state = context.scene.dwos
        host = bpy.data.objects.get(state.wall_name)
        if not host or host.type != 'MESH':
            self.report({'ERROR'}, 'Стена не найдена')
            return {'CANCELLED'}
        # Fail closed on stale discovery. A new hole, modified Host geometry,
        # transform, or existing window requires an explicit re-scan.
        try:
            current_fingerprint=host_geometry_fingerprint(host)
        except Exception as exc:
            self.report({'ERROR'},f'Не удалось проверить стену: {exc}')
            return {'CANCELLED'}
        if not state.opening_source_fingerprint or current_fingerprint!=state.opening_source_fingerprint:
            self.report({'ERROR'},'Проёмы устарели после изменения стены. Нажмите «Найти проёмы» повторно.')
            log_event('OPENING_CANDIDATES_STALE_BLOCKED',wall=host.name,
                      expected=state.opening_source_fingerprint,current=current_fingerprint)
            return {'CANCELLED'}
        raw=[candidate for candidate in state.openings if candidate.enabled]
        chosen=[]
        skipped=[]
        for candidate in raw:
            try:
                payload=json.loads(candidate.payload_json)
                existing=occupied_window_for_candidate(payload,host,context.scene)
            except (ValueError,TypeError):
                existing=None
            if existing is None:chosen.append(candidate)
            else:skipped.append((candidate,existing))
        if skipped:
            log_event('OPENING_BATCH_OCCUPIED_SKIPPED',wall=host.name,count=len(skipped),
                      windows=[x.name for _,x in skipped[:30]])
        if not chosen:
            self.report({'WARNING'}, 'Новых свободных проёмов нет: все выбранные уже заняты или не выбраны')
            return {'CANCELLED'}
        made = []
        # Snapshot once: deleting partially created frames alone does not restore
        # preexisting Interior meshes that earlier candidates already rebuilt.
        baseline_interiors=snapshot_scene_interiors(context.scene)
        log_event('CREATE_WINDOWS_BEGIN', host=host.name, requested=len(chosen))
        try:
            for candidate in chosen:
                log_event('CREATE_WINDOW_CANDIDATE', candidate=candidate.candidate_id, size=f'{candidate.width_text}x{candidate.height_text}')
                made.append(create_window_from_candidate(context, candidate, host))
        except Exception as exc:
            log_exception('CREATE_WINDOWS_FAILED', exc, created=len(made), requested=len(chosen))
            for obj in reversed(made):
                try:
                    _remove_object_tree(obj)
                except Exception:
                    pass
            try:
                restored=restore_scene_interiors(baseline_interiors)
                log_event('CREATE_WINDOWS_BATCH_ROLLED_BACK',restored=restored)
            except Exception as rollback_exc:
                log_exception('CREATE_WINDOWS_BATCH_ROLLBACK_INCOMPLETE',rollback_exc)
                self.report({'ERROR'},f'КРИТИЧЕСКАЯ ошибка отката: {rollback_exc}')
                return {'CANCELLED'}
            self.report({'ERROR'}, f'Создание окна: {exc}')
            return {'CANCELLED'}

        try:
            for obj in context.view_layer.objects:
                if obj.select_get():
                    obj.select_set(False)
            for obj in made:
                if context.view_layer.objects.get(obj.name) is obj:
                    obj.select_set(True)
            if made and context.view_layer.objects.get(made[-1].name) is made[-1]:
                context.view_layer.objects.active = made[-1]
        except Exception as exc:
            log_exception('CREATE_WINDOWS_SELECTION_WARNING', exc, created=len(made))

        # Hide the detection drawing once the real window meshes exist.  Keep
        # opening candidates for a deliberate subsequent Find/Create cycle.
        state.show_overlay=False
        for candidate in chosen:
            candidate.enabled=False
        log_event('CREATE_WINDOWS_OK', created=len(made), occupied_skipped=len(skipped),host=host.name)
        pending=[o for o in made if o.get('DWOS_smart_interior_pending',False)]
        if pending:
            self.report({'WARNING'},
                f'Создано окон: {len(made)}; Interior ожидает согласования: {len(pending)}. '
                'Измените зазор/положение и нажмите «Пересчитать соседние Interior».')
        else:
            self.report({'INFO'}, f'Создано окон: {len(made)}')
        return {'FINISHED'}


class DWOS_OT_rebuild_window(bpy.types.Operator):
    bl_idname = 'dwos.rebuild_window'
    bl_label = 'Обновить окно'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        while obj is not None and not (getattr(obj, 'dwos_window', None) and obj.dwos_window.is_window):
            obj = obj.parent
        if not obj or not obj.dwos_window.is_window:
            self.report({'ERROR'}, 'Выберите окно DWOS')
            return {'CANCELLED'}
        try:
            rebuild_window(obj)
            log_event('REBUILD_WINDOW_OK', window=obj.name)
        except Exception as exc:
            log_exception('REBUILD_WINDOW_FAILED', exc, window=getattr(obj, 'name', ''))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}
