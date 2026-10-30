from __future__ import annotations

"""DWOS 3.5.9 geometry-reference logger.

The two field buttons are deliberately diagnostics only:
- МОЯ РАМА records the currently active Mesh exactly as the user's reference geometry.
- ТВОЯ РАМА records the generated DWOS CORNER frame.

They NEVER create, hide, replace, rebuild, or otherwise mutate frame geometry.  Both
snapshots are written into one compare-friendly TXT/JSON log so the file can be sent
back for offline reconstruction of the intended Corner junction.
"""

from datetime import datetime
import json
import math
from pathlib import Path

import bpy
import bmesh
from bpy.types import Operator
from mathutils import Vector

from .batch_edit import resolve_window
from .dwos_log import log_dir, event as log_event, exception as log_exception

DWOS_VERSION = '3.5.11'
REFERENCE_LOG_NAME = 'dwos-frame-reference-latest.txt'
SCENE_LAST_CORNER = 'DWOS_reference_corner_window_name'
FORMAT = 'DWOS_FRAME_REFERENCE_V3'


def frame_reference_log_path() -> str:
    return str(Path(log_dir()) / REFERENCE_LOG_NAME)


def _is_corner(window) -> bool:
    return bool(window is not None and str(window.get('DWOS_adaptive_classification', '') or '').upper() == 'CORNER')


def _last_corner(context):
    name = str(context.scene.get(SCENE_LAST_CORNER, '') or '')
    obj = bpy.data.objects.get(name) if name else None
    if obj is not None and _is_corner(obj):
        return obj
    return None


def _remember_corner(context, window) -> None:
    if window is not None and _is_corner(window):
        context.scene[SCENE_LAST_CORNER] = window.name


def _corner_from_context(context, active=None):
    active = active or context.active_object
    window = resolve_window(active)
    if _is_corner(window):
        _remember_corner(context, window)
        return window
    for obj in getattr(context, 'selected_objects', ()):
        window = resolve_window(obj)
        if _is_corner(window):
            _remember_corner(context, window)
            return window
    return _last_corner(context)


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        if hasattr(value, 'name'):
            return value.name
    except Exception:
        pass
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _dwos_props(obj):
    out = {}
    try:
        for key in obj.keys():
            if str(key).startswith('DWOS_'):
                out[str(key)] = _jsonable(obj.get(key))
    except Exception:
        pass
    return out


def _mesh_data_live(obj):
    """Read current geometry without changing modes or committing Edit Mode."""
    if obj.type != 'MESH':
        raise ValueError('Для записи рамы нужен активный Mesh объект')
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        verts = list(bm.verts); edges = list(bm.edges); faces = list(bm.faces)
        vi = {v: i for i, v in enumerate(verts)}
        local = [[float(c) for c in v.co] for v in verts]
        edge_rows = [[vi[v] for v in e.verts] for e in edges]
        polygons = [[vi[v] for v in f.verts] for f in faces]
        normals_local = [[float(c) for c in f.normal] for f in faces]
        source = 'EDIT_BMESH'
        material_indices = [int(f.material_index) for f in faces]
    else:
        me = obj.data
        local = [[float(c) for c in v.co] for v in me.vertices]
        edge_rows = [[int(i) for i in e.vertices] for e in me.edges]
        polygons = [[int(i) for i in p.vertices] for p in me.polygons]
        normals_local = [[float(c) for c in p.normal] for p in me.polygons]
        source = 'OBJECT_MESH'
        material_indices = [int(p.material_index) for p in me.polygons]
    mw = obj.matrix_world
    world = [[float(c) for c in (mw @ Vector(p))] for p in local]
    normal_matrix = mw.to_3x3().inverted().transposed()
    normals_world = []
    for n in normals_local:
        q = normal_matrix @ Vector(n)
        normals_world.append([float(c) for c in (q.normalized() if q.length_squared > 1e-20 else q)])
    return {
        'source': source,
        'vertices_local': local,
        'vertices_world': world,
        'edges': edge_rows,
        'polygons': polygons,
        'face_normals_local': normals_local,
        'face_normals_world': normals_world,
        'material_indices': material_indices,
    }


def _bounds(points):
    if not points:
        return {'min':[0.0,0.0,0.0], 'max':[0.0,0.0,0.0], 'size':[0.0,0.0,0.0], 'center':[0.0,0.0,0.0]}
    lo = [min(float(p[i]) for p in points) for i in range(3)]
    hi = [max(float(p[i]) for p in points) for i in range(3)]
    return {
        'min': lo,
        'max': hi,
        'size': [hi[i]-lo[i] for i in range(3)],
        'center': [(hi[i]+lo[i])*0.5 for i in range(3)],
    }


def _edge_lengths(points, edges):
    out=[]
    for a,b in edges:
        if 0 <= a < len(points) and 0 <= b < len(points):
            out.append(math.dist(points[a], points[b]))
    return out


def _window_context(window):
    if window is None:
        return None
    s = window.dwos_window
    try:
        payload = json.loads(s.opening_payload_json or '{}')
    except Exception:
        payload = {'_decode_error': True}
    topology = payload.get('adaptive_topology') or {}
    settings = {}
    for name in (
        'window_id','host_name','width_text','height_text','frame_width_text','internal_width_text',
        'frame_depth_text','frame_offset_text','optimization','interior_mode','interior_depth_text','interior_groups_count','interior_partition_mode','interior_dividers_json','interior_joint_gap_text',
        'interior_clearance_text','interior_offset_text','glass_offset_text','glass_edge_offset_text',
        'glass_gap_text','flip_interior','corner_post_mode','corner_interior_variant','branch_state_json',
        'zone_tree_json','mullion_selection_json'):
        try:
            settings[name] = _jsonable(getattr(s, name))
        except Exception:
            pass
    return {
        'name': window.name,
        'matrix_world': [[float(c) for c in row] for row in window.matrix_world],
        'classification': str(window.get('DWOS_adaptive_classification','') or topology.get('classification','')),
        'settings': settings,
        'corner_invariants': {
            'optimization_required': 'FULL',
            'corner_post_required': True,
            'glass_panel_count_required': 2,
            'current_glass_panel_count': int(window.get('DWOS_corner_glass_panel_count', 0) or 0),
            'current_invariant_policy': str(window.get('DWOS_corner_invariant_policy','') or ''),
        },
        'topology': {
            'classification': topology.get('classification'),
            'width': topology.get('width'), 'height': topology.get('height'),
            'axis_x': topology.get('axis_x'), 'axis_y': topology.get('axis_y'), 'axis_z': topology.get('axis_z'),
            'origin': topology.get('origin'),
            'hard_junction_station_indices': topology.get('hard_junction_station_indices', []),
            'corner_junctions': topology.get('corner_junctions', []),
            'stations': topology.get('stations', []),
        },
    }


def _capture(obj, label, window_context=None):
    snap = _mesh_data_live(obj)
    edges = snap['edges']; local = snap['vertices_local']; world = snap['vertices_world']
    lengths = _edge_lengths(world, edges)
    return {
        'label': label,
        'captured_at': datetime.now().isoformat(timespec='seconds'),
        'object': {
            'name': obj.name,
            'type': obj.type,
            'mode': obj.mode,
            'matrix_world': [[float(c) for c in row] for row in obj.matrix_world],
            'dimensions': [float(c) for c in obj.dimensions],
            'dwos_props': _dwos_props(obj),
            'materials': [slot.material.name if slot.material else None for slot in obj.material_slots],
        },
        'geometry': {
            **snap,
            'counts': {'vertices': len(local), 'edges': len(edges), 'faces': len(snap['polygons'])},
            'bounds_local': _bounds(local),
            'bounds_world': _bounds(world),
            'edge_lengths_world': lengths,
            'edge_length_min': min(lengths) if lengths else 0.0,
            'edge_length_max': max(lengths) if lengths else 0.0,
        },
        'window_context': window_context,
    }


def _centered_local(cap):
    pts=cap['geometry'].get('vertices_local') or []
    center=cap['geometry'].get('bounds_local',{}).get('center') or [0.0,0.0,0.0]
    return [[float(p[i])-float(center[i]) for i in range(3)] for p in pts]


def _compare(my_cap, dwos_cap):
    if not my_cap or not dwos_cap:
        return {'ready': False}
    # 3.5.9: the user is allowed to move the reference object elsewhere in the
    # Blender scene.  Raw world coordinates are therefore diagnostic only.
    # Shape comparison happens in each object's own local basis after removing
    # the local bounds-center translation.  This fixes the false ~20 m delta
    # seen in the 3.5.8 field reference while preserving actual junction edits.
    a=_centered_local(my_cap); b=_centered_local(dwos_cap)
    result = {
        'ready': True,
        'comparison_basis': 'LOCAL_BOUNDS_CENTERED_V3',
        'my_counts': my_cap['geometry']['counts'],
        'dwos_counts': dwos_cap['geometry']['counts'],
        'bounds_center_delta_world': [
            my_cap['geometry']['bounds_world']['center'][i] - dwos_cap['geometry']['bounds_world']['center'][i]
            for i in range(3)],
        'bounds_size_delta_world': [
            my_cap['geometry']['bounds_world']['size'][i] - dwos_cap['geometry']['bounds_world']['size'][i]
            for i in range(3)],
        'bounds_size_delta_local': [
            my_cap['geometry']['bounds_local']['size'][i] - dwos_cap['geometry']['bounds_local']['size'][i]
            for i in range(3)],
    }
    if a and b and len(a) <= 2500 and len(b) <= 2500:
        nearest=[]
        for i,p in enumerate(a):
            j,d=min(((j,math.dist(p,q)) for j,q in enumerate(b)), key=lambda x:x[1])
            nearest.append({'my_vertex':i,'nearest_dwos_vertex':j,'distance':d})
        result['my_to_dwos_nearest'] = nearest
        result['nearest_max'] = max(r['distance'] for r in nearest)
        result['nearest_mean'] = sum(r['distance'] for r in nearest)/len(nearest)
    if len(a) == len(b) and a:
        indexed=[math.dist(a[i],b[i]) for i in range(len(a))]
        result['same_index_vertex_delta'] = indexed
        result['same_index_max'] = max(indexed)
        result['same_index_mean'] = sum(indexed)/len(indexed)
    return result


def _load_report(path):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('format') == FORMAT:
            return data
    except Exception:
        pass
    return {'format':FORMAT,'dwos_version':DWOS_VERSION,'captures':{}}


def _write_capture(context, kind, obj, window=None):
    path=Path(frame_reference_log_path())
    report=_load_report(path)
    report['format']=FORMAT
    report['dwos_version']=DWOS_VERSION
    report['blender_version']=bpy.app.version_string
    report['scene']=context.scene.name
    report['updated_at']=datetime.now().isoformat(timespec='seconds')
    report.setdefault('captures',{})[kind]=_capture(obj,kind,_window_context(window))
    report['comparison']=_compare(report['captures'].get('MY_FRAME'),report['captures'].get('DWOS_FRAME'))
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    cap=report['captures'][kind]
    log_event('FRAME_REFERENCE_CAPTURE',kind=kind,object=obj.name,window=(window.name if window else ''),
              vertices=cap['geometry']['counts']['vertices'],faces=cap['geometry']['counts']['faces'],path=str(path))
    if report['comparison'].get('ready'):
        log_event('FRAME_REFERENCE_COMPARE_READY',path=str(path),
                  my_vertices=report['comparison']['my_counts']['vertices'],
                  dwos_vertices=report['comparison']['dwos_counts']['vertices'],
                  nearest_max=round(float(report['comparison'].get('nearest_max',0.0)),6))
    return str(path)


class DWOS_OT_show_user_frame(Operator):
    bl_idname = 'dwos.show_user_frame'
    bl_label = 'МОЯ РАМА'
    bl_description = 'Записать активный Mesh в диагностический лог как МОЯ РАМА. Геометрия не изменяется'
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj=context.active_object
        if obj is None or obj.type!='MESH':
            self.report({'ERROR'}, 'Выделите вашу раму Mesh и нажмите «МОЯ РАМА»')
            return {'CANCELLED'}
        try:
            window=_corner_from_context(context,obj)
            path=_write_capture(context,'MY_FRAME',obj,window)
            self.report({'INFO'}, f'МОЯ РАМА записана: {Path(path).name}')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('FRAME_REFERENCE_MY_FAILED',exc,object=(obj.name if obj else ''))
            self.report({'ERROR'},f'Не удалось записать «Мою раму»: {exc}')
            return {'CANCELLED'}


class DWOS_OT_show_generated_frame(Operator):
    bl_idname = 'dwos.show_generated_frame'
    bl_label = 'ТВОЯ РАМА'
    bl_description = 'Записать текущую параметрическую CORNER раму DWOS в диагностический лог. Геометрия не изменяется'
    bl_options = {'REGISTER'}

    def execute(self, context):
        window=_corner_from_context(context,context.active_object)
        if window is None:
            self.report({'ERROR'}, 'Выберите CORNER окно DWOS и нажмите «ТВОЯ РАМА»')
            return {'CANCELLED'}
        try:
            _remember_corner(context,window)
            path=_write_capture(context,'DWOS_FRAME',window,window)
            self.report({'INFO'},f'ТВОЯ РАМА записана: {Path(path).name}')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('FRAME_REFERENCE_DWOS_FAILED',exc,window=window.name)
            self.report({'ERROR'},f'Не удалось записать «Твою раму»: {exc}')
            return {'CANCELLED'}


CLASSES=(DWOS_OT_show_user_frame,DWOS_OT_show_generated_frame)
