"""Non-destructive DWOS field geometry diagnostics for architectural hosts.

Both actions produce TXT/JSON in the DWOS log directory. Edge selections are read
from edit BMesh WITHOUT switching modes or changing the user's selection.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path
import hashlib
import math
import bpy
import bmesh
from bpy.props import BoolProperty

from .geometry_inspector_core import inspect_selected_edges
from .dwos_log import log_dir, event as log_event, exception as log_exception

VERSION = '3.7.13-rc14'
SOFT_LIMIT = (80000, 160000, 80000)
HARD_LIMIT = (650000, 1350000, 700000)


def _host(context):
    obj=context.active_object
    if obj is None or obj.type!='MESH':
        raise ValueError('Выделите объект стены Mesh (Object Mode / Edit Mode)')
    s=getattr(obj,'dwos_window',None)
    if s is not None and s.is_window:
        raise ValueError('Выбрано окно DWOS; выберите именно исходную стену Host')
    return obj


def _sizes(obj):
    if obj.mode=='EDIT':
        bm=bmesh.from_edit_mesh(obj.data)
        return (len(bm.verts),len(bm.edges),len(bm.faces))
    me=obj.data
    return (len(me.vertices),len(me.edges),len(me.polygons))


def _oversized(sizes, limit): return any(n>max_n for n,max_n in zip(sizes,limit))


def _matrix(obj): return [[float(q) for q in row] for row in obj.matrix_world]


def _normal_world(obj, local):
    try:
        v=obj.matrix_world.to_3x3().inverted().transposed() @ local
        return [round(float(t),8) for t in v.normalized()] if v.length_squared>1e-18 else [0,0,0]
    except Exception:
        return [0,0,0]


def _current_mesh(obj):
    """Freeze active Mesh, including pending Edit Mode changes, without committing."""
    mw=obj.matrix_world
    if obj.mode=='EDIT':
        bm=bmesh.from_edit_mesh(obj.data)
        verts=list(bm.verts); edges=list(bm.edges); faces=list(bm.faces)
        vind={v:i for i,v in enumerate(verts)}
        eind={e:i for i,e in enumerate(edges)}
        coords=[[float(c) for c in v.co] for v in verts]
        edge_pairs=[[vind[v] for v in edge.verts] for edge in edges]
        polygons=[[vind[v] for v in face.verts] for face in faces]
        face_normals=[_normal_world(obj,f.normal) for f in faces]
        edge_faces=[len(e.link_faces) for e in edges]
        material_indexes=[int(f.material_index) for f in faces]
        selected_vertices=[i for i,v in enumerate(verts) if v.select]
        explicit_edges=[i for i,e in enumerate(edges) if e.select]
        selected_faces=[i for i,f in enumerate(faces) if f.select]
        attribute_values={}
        layer=bm.edges.layers.int.get('DWOS_opening_id')
        if layer is not None:
            attribute_values['DWOS_opening_id']=[int(e[layer]) for e in edges]
        uv_maps={}
        for uv_layer in obj.data.uv_layers:
            layer_uv=bm.loops.layers.uv.get(uv_layer.name)
            if layer_uv is not None:
                uv_maps[uv_layer.name]=[[[float(c) for c in loop[layer_uv].uv]
                                         for loop in face.loops] for face in faces]
        source='EDIT_BMESH'
    else:
        me=obj.data
        coords=[[float(c) for c in v.co] for v in me.vertices]
        edge_pairs=[[int(i) for i in e.vertices] for e in me.edges]
        polygons=[[int(i) for i in f.vertices] for f in me.polygons]
        face_normals=[_normal_world(obj,f.normal) for f in me.polygons]
        edge_faces=[0]*len(edge_pairs)
        lookup={tuple(sorted(pair)):i for i,pair in enumerate(edge_pairs)}
        for f in me.polygons:
            for key in f.edge_keys:
                i=lookup.get(tuple(sorted(key)))
                if i is not None:edge_faces[i]+=1
        material_indexes=[int(f.material_index) for f in me.polygons]
        selected_vertices=[int(v.index) for v in me.vertices if v.select]
        explicit_edges=[int(e.index) for e in me.edges if e.select]
        selected_faces=[int(f.index) for f in me.polygons if f.select]
        attribute_values={}
        attr=me.attributes.get('DWOS_opening_id')
        if attr is not None and attr.domain=='EDGE' and attr.data_type=='INT':
            attribute_values['DWOS_opening_id']=[int(a.value) for a in attr.data]
        uv_maps={}
        for uv_layer in me.uv_layers:
            uv_maps[uv_layer.name]=[
                [[float(c) for c in uv_layer.data[li].uv]
                 for li in range(face.loop_start, face.loop_start+face.loop_total)]
                for face in me.polygons]
        source='OBJECT_MESH'
    from mathutils import Vector
    world=[[float(c) for c in (mw @ Vector(p))] for p in coords]
    return {'source':source, 'coordinates_local':coords,'coordinates_world':world,
            'edges':edge_pairs,'polygons':polygons,'face_normals_world':face_normals,
            'edge_face_counts':edge_faces,'material_indices':material_indexes,
            'selected_vertices':selected_vertices,'explicit_edges':explicit_edges,
            'selected_faces':selected_faces,'edge_attributes':attribute_values,'uv_maps':uv_maps}


def _mesh_summary(obj, snap):
    verts=snap['coordinates_world']; edges=snap['edges']; faces=snap['polygons']
    ef=snap['edge_face_counts']; incidence=Counter(ef)
    graph=[[] for _ in verts]
    for a,b in edges:
        graph[a].append(b);graph[b].append(a)
    visited=set(); components=[]
    for i in range(len(verts)):
        if i in visited: continue
        stack=[i]; visited.add(i); count=0
        while stack:
            n=stack.pop();count+=1
            for t in graph[n]:
                if t not in visited:
                    visited.add(t);stack.append(t)
        components.append(count)
    dupe=defaultdict(list)
    for i,p in enumerate(snap['coordinates_local']):
        dupe[tuple(round(x,6) for x in p)].append(i)
    duplicates=[v for v in dupe.values() if len(v)>1]
    boundary_degree=Counter()
    for ei,(a,b) in enumerate(edges):
        if ef[ei]==1: boundary_degree[a]+=1;boundary_degree[b]+=1
    bbox=([min(p[k] for p in verts) for k in range(3)],
          [max(p[k] for p in verts) for k in range(3)]) if verts else ([0]*3,[0]*3)
    bad_normal=sum(1 for n in snap['face_normals_world'] if sum(x*x for x in n)<1e-14)
    # Fan-triangulation area in world space: flags fully collapsed and zero-area faces.
    zero_area_faces=0
    for f in faces:
        if len(f)<3:
            zero_area_faces+=1;continue
        origin=verts[f[0]]
        area2=0.0
        from .geometry_inspector_core import _cross,_vecsub,_norm
        for j in range(1,len(f)-1):
            area2+=_norm(_cross(_vecsub(verts[f[j]],origin),
                                _vecsub(verts[f[j+1]],origin)))
        if area2 < 2e-12: zero_area_faces+=1
    zero_edge=sum(1 for a,b in edges if sum((verts[a][i]-verts[b][i])**2 for i in range(3))<1e-18)
    nonfinite=sum(1 for p in verts if any(not math.isfinite(c) for c in p))
    attrs=[{'name':a.name,'domain':a.domain,'data_type':a.data_type}
           for a in obj.data.attributes]
    return {
        'counts':{'vertices':len(verts),'edges':len(edges),'polygons':len(faces)},
        'edge_classification':{'wire':incidence.get(0,0),'boundary':incidence.get(1,0),
                               'manifold':incidence.get(2,0),'nonmanifold':sum(c for n,c in incidence.items() if n>2)},
        'vertex_components':len(components),'component_vertex_counts':sorted(components,reverse=True)[:30],
        'boundary_branch_vertices':[i for i,d in boundary_degree.items() if d>2][:150],
        'boundary_branch_count':sum(d>2 for d in boundary_degree.values()),
        'duplicate_coordinate_groups_sample':duplicates[:60],
        'duplicate_coordinate_group_count':len(duplicates),
        'zero_length_edges':zero_edge,'zero_area_faces':zero_area_faces,
        'nonfinite_vertices':nonfinite,
        'invalid_world_face_normals':bad_normal,
        'bounds_world':[[round(q,6) for q in side] for side in bbox],
        'material_slots':[{'index':i,'name':s.material.name if s.material else None} for i,s in enumerate(obj.material_slots)],
        'uv_layers':[l.name for l in obj.data.uv_layers], 'attributes':attrs,
        'matrix_determinant':float(obj.matrix_world.determinant()),
        'warnings':[
            'Opposite-facing facade patches alone do NOT establish solid-wall thickness',
            'Nonmanifold/boundary diagnostics do NOT classify holes automatically',
            'Duplicate-coordinate matching uses 1e-6 local-coordinate rounding and is only a hint',
            'A full mesh weld-readiness proof requires further geometry inspection',
        ]
    }


def _cached_candidates(context, host, edges):
    state=getattr(context.scene,'dwos',None)
    if not state or getattr(state,'wall_name','')!=host.name:
        return []
    edge_lookup={tuple(sorted(pair)):i for i,pair in enumerate(edges)}
    out=[]
    for candidate in state.openings:
        try:
            data=json.loads(candidate.payload_json)
            loop=data.get('front_loop') or []
            match=set()
            for a,b in zip(loop,loop[1:]+loop[:1]):
                ei=edge_lookup.get(tuple(sorted((a,b))))
                if ei is not None:match.add(ei)
            if match:out.append(match)
        except (ValueError,TypeError,KeyError):continue
    return out


def _geometry_signature(snapshot):
    """Pair selected-edge report with exactly the same mesh snapshot as host report."""
    sha=hashlib.sha256()
    for key in ('coordinates_local','edges','polygons'):
        sha.update(key.encode('ascii'))
        for row in snapshot[key]:
            sha.update(json.dumps(row,separators=(',',':'),allow_nan=False).encode('ascii'))
            sha.update(b';')
    return sha.hexdigest()


def _report_host(context,host,snapshot):
    summary=_mesh_summary(host,snapshot)
    payload={'schema':'DWOS_geometry_inspector_host_v1','dwos_version':VERSION,
             'timestamp':datetime.now().isoformat(timespec='seconds'),
             'blender_version':bpy.app.version_string,
             'object_name':host.name,'mesh_name':host.data.name,
             'source':snapshot['source'],'object_mode':host.mode,
             'geometry_sha256':_geometry_signature(snapshot),
             'matrix_world':_matrix(host),'summary':summary,
             'topology':{k:snapshot[k] for k in ('coordinates_local','coordinates_world','edges',
                    'polygons','edge_face_counts','face_normals_world','material_indices',
                    'edge_attributes','uv_maps')},
             'note':'Input snapshot for forensic analysis, NOT an inferred list of windows',
             }
    return payload


def _report_selected(context,host,snapshot):
    if host.mode!='EDIT':
        raise ValueError('Перейдите в Edit Mode стены и выделите рёбра проёмов')
    selected=list(snapshot['explicit_edges'])
    rule='EXPLICIT_SELECTED_EDGES'
    if not selected and snapshot['selected_vertices']:
        vset=set(snapshot['selected_vertices'])
        selected=[i for i,(a,b) in enumerate(snapshot['edges']) if a in vset and b in vset]
        rule='BOTH_ENDPOINT_VERTICES_SELECTED'
    if not selected:
        raise ValueError('Нет выделенных рёбер. В Edit Mode выделите контуры предполагаемых проёмов')
    analysis=inspect_selected_edges(snapshot['coordinates_world'],snapshot['edges'],selected,
             snapshot['edge_face_counts'],_cached_candidates(context,host,snapshot['edges']),
             selected_vertex_ids=snapshot['selected_vertices'])
    return {'schema':'DWOS_selected_openings_v1','dwos_version':VERSION,
            'timestamp':datetime.now().isoformat(timespec='seconds'),
            'blender_version':bpy.app.version_string,'host':host.name,
            'mesh_source':snapshot['source'],'selection_rule':rule,
            'geometry_sha256':_geometry_signature(snapshot),
            'selected_face_ids':snapshot['selected_faces'],
            'index_warning':'Indices identify snapshot elements, not persistent IDs after edits',
            'comparison_warning':'Automatic candidate matches are cached/unverified and may be stale',
            'analysis':analysis,
            'cached_automatic_comparison_status': 'AVAILABLE_CACHED_UNVERIFIED' if _cached_candidates(context,host,snapshot['edges']) else 'UNAVAILABLE_NO_HOST_MATCHING_CACHE',
            'selected_edge_coordinates_world':[
                {'edge_id':ei,'vertex_ids':snapshot['edges'][ei],
                 'endpoints':[snapshot['coordinates_world'][v] for v in snapshot['edges'][ei]],
                 'adjacent_faces':snapshot['edge_face_counts'][ei],
                 'DWOS_opening_id':snapshot['edge_attributes'].get('DWOS_opening_id',[None]*len(snapshot['edges']))[ei]}
                 for ei in selected],
            'note':'These edges are USER-selected examples, not automatically accepted openings',
            }


def _short_report(kind,payload):
    if kind=='host':
        s=payload['summary']; c=s['counts']; e=s['edge_classification']
        return ('DWOS — АНАЛИЗ СЕТКИ СТЕНЫ\n'
            f"Host: {payload['object_name']} | Blender: {payload['blender_version']}\n"
            f"Source: {payload['source']} | V/E/F: {c['vertices']}/{c['edges']}/{c['polygons']}\n"
            f"Boundary: {e['boundary']} | Manifold: {e['manifold']} | Nonmanifold: {e['nonmanifold']} | Wire: {e['wire']}\n"
            f"Vertex components: {s['vertex_components']} | Boundary branches: {s['boundary_branch_count']}\n"
            f"Duplicate-coordinate groups (approx.): {s['duplicate_coordinate_group_count']}\n"
            f"UV: {s['uv_layers']} | Materials: {len(s['material_slots'])}\n"
            'Full coordinates, connectivity, attributes and normals are in JSON.\n'
            'No mesh modification was performed.\n')
    a=payload['analysis']
    parts=[f"DWOS — АНАЛИЗ ВЫДЕЛЕННЫХ ПРОЁМОВ\nHost: {payload['host']} | Blender: {payload['blender_version']}",
           f"Selection source: {payload['selection_rule']} | Selected edges: {len(a['selected_edges'])}",
           f"Components: {a['component_count']} | Closed: {a['closed_count']} | Possible rectangles: {a['rectangular_candidate_count']}",
           f"Diagnostic groups: {a.get('diagnostic_class_counts',{})}",
           f"Auto comparison: {payload.get('cached_automatic_comparison_status','UNAVAILABLE')}",
           'Manual selection is a reference example, NOT geometric proof.']
    for c in a['components'][:100]:
        shape=c['shape']; parts.append(
            f"#{c['id']}: {c['status']} | V/E={c['vertex_count']}/{c['edge_count']} "
            f"| corners={shape.get('corner_count','?')} | planar={shape.get('plane_status','?')} "
            f"| rectangular={shape.get('rectangular_candidate',False)} | class={c.get('diagnostic_class','UNKNOWN')}"
            f" | deviation_m={shape.get('max_plane_deviation','?')} | flags={','.join(c['flags']) or '-'}")
    if len(a['components'])>100:parts.append(f"... and {len(a['components'])-100} more; full details in JSON")
    return '\n'.join(parts)+'\n'


def _write(name,payload):
    folder=Path(log_dir()); folder.mkdir(parents=True,exist_ok=True)
    (folder/f'{name}.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    text=_short_report('host' if 'host-analysis' in name else 'selected',payload)
    (folder/f'{name}.txt').write_text(text,encoding='utf8')
    return str(folder/f'{name}.json')


class _DWOS_Geometry_Operator:
    """Mixin only (NOT registered). Confirmation for exceptionally heavy meshes."""
    def invoke(self,context,event):
        try:
            n=_sizes(_host(context))
        except Exception as exc:
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        if _oversized(n,SOFT_LIMIT):
            return context.window_manager.invoke_props_dialog(self,width=430)
        return self.execute(context)
    def draw(self,context):
        self.layout.label(text='Большая сетка: диагностика может быть медленной',icon='ERROR')
        self.layout.prop(self,'confirm_large')
    def _snapshot(self,context):
        obj=_host(context); counts=_sizes(obj)
        if _oversized(counts,HARD_LIMIT):
            raise ValueError(f'Host слишком большой для безопасного экспорта: V/E/F={counts}. Отправьте .blend')
        if _oversized(counts,SOFT_LIMIT) and not self.confirm_large:
            raise ValueError('Подтвердите анализ большой сетки (поставьте галочку в диалоге)')
        snap=_current_mesh(obj)
        return obj,snap


class DWOS_OT_inspect_wall_mesh(_DWOS_Geometry_Operator,bpy.types.Operator):
    bl_idname='dwos.inspect_wall_mesh'
    bl_label='Анализ сетки стены'
    bl_description='Записать геометрию Host в JSON/TXT; не изменять Mesh'
    bl_options={'REGISTER'}
    confirm_large: BoolProperty(name='Подтверждаю большой экспорт', default=False,
       description='Подробный JSON может быть очень большим и замедлить Blender')
    def execute(self,context):
        try:
            obj,snap=self._snapshot(context)
            data=_report_host(context,obj,snap)
            path=_write('dwos-host-analysis-latest',data)
            c=data['summary']['counts']
            log_event('GEOMETRY_HOST_ANALYSIS_OK',host=obj.name,source=snap['source'],
                      verts=c['vertices'],edges=c['edges'],faces=c['polygons'],file=path)
            self.report({'INFO'},f'Анализ стены сохранён: {Path(path).name}')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('GEOMETRY_HOST_ANALYSIS_FAILED',exc)
            self.report({'ERROR'},f'Анализ стены: {exc}');return {'CANCELLED'}


class DWOS_OT_inspect_manual_openings(_DWOS_Geometry_Operator,bpy.types.Operator):
    bl_idname='dwos.inspect_manual_openings'
    bl_label='Анализ выделенных проёмов'
    bl_description='Исследовать рёбра, вручную выделенные в Edit Mode, без автоисправлений'
    bl_options={'REGISTER'}
    confirm_large: BoolProperty(name='Подтверждаю большой экспорт', default=False,
       description='Подробный JSON может быть очень большим и замедлить Blender')
    @classmethod
    def poll(cls,context):
        return context.active_object is not None and context.active_object.type=='MESH'
    def execute(self,context):
        try:
            obj,snap=self._snapshot(context)
            data=_report_selected(context,obj,snap)
            # Always capture the EXACT same edit-mode geometry as the selection.
            host=_report_host(context,obj,snap)
            host_path=_write('dwos-host-analysis-latest',host)
            path=_write('dwos-selected-openings-latest',data)
            a=data['analysis']
            log_event('GEOMETRY_SELECTED_OPENINGS_OK',host=obj.name,
                      selection_rule=data['selection_rule'],edges=len(a['selected_edges']),
                      components=a['component_count'],closed=a['closed_count'],
                      rectangles=a['rectangular_candidate_count'],
                      classifications=a.get('diagnostic_class_counts',{}),
                      host_file=host_path,selection_file=path)
            bands=a.get('diagnostic_class_counts',{})
            self.report({'INFO'},
                f"Контуров {a['component_count']}: прямоуг. {a['rectangular_candidate_count']}, "
                f"почти плоских {bands.get('NEAR_PLANAR_FOUR_CORNERS_REVIEW',0)}, "
                f"сложных {bands.get('SPATIALLY_NONPLANAR_REVIEW',0)}, "
                f"открытых {bands.get('OPEN_CHAIN_REVIEW',0)}. Экспортируйте ZIP")
            return {'FINISHED'}
        except Exception as exc:
            log_exception('GEOMETRY_SELECTED_OPENINGS_FAILED',exc)
            self.report({'ERROR'},f'Анализ выделенных проёмов: {exc}');return {'CANCELLED'}


CLASSES=(DWOS_OT_inspect_wall_mesh,DWOS_OT_inspect_manual_openings)
