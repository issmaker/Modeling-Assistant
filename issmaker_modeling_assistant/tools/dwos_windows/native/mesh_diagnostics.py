from __future__ import annotations

import json
from collections import Counter, deque
from datetime import datetime
import bpy
from mathutils import Vector

from .dwos_log import event as log_event, exception as log_exception, block as log_block, write_mesh_report, mesh_report_path
from .material_system import material_summary

EPS = 1e-10


def _connected_components(mesh):
    n=len(mesh.vertices)
    if n == 0:
        return []
    adj=[[] for _ in range(n)]
    for e in mesh.edges:
        a,b=e.vertices
        adj[a].append(b); adj[b].append(a)
    seen=set(); sizes=[]
    for i in range(n):
        if i in seen: continue
        q=[i]; seen.add(i); count=0
        while q:
            v=q.pop(); count += 1
            for nb in adj[v]:
                if nb not in seen:
                    seen.add(nb); q.append(nb)
        sizes.append(count)
    return sorted(sizes, reverse=True)


def _edge_face_incidence(mesh):
    counts=Counter()
    for p in mesh.polygons:
        for key in p.edge_keys:
            counts[tuple(sorted(key))] += 1
    # Mesh edges not referenced by any polygon are wire edges.
    all_edges={tuple(sorted(e.vertices)) for e in mesh.edges}
    wire=sum(1 for e in all_edges if counts.get(e,0)==0)
    boundary=sum(1 for e in all_edges if counts.get(e,0)==1)
    manifold=sum(1 for e in all_edges if counts.get(e,0)==2)
    overused=sum(1 for e in all_edges if counts.get(e,0)>2)
    return {'wire':wire,'boundary':boundary,'manifold':manifold,'overused':overused}


def _normal_bucket(n: Vector):
    vals=[abs(n.x),abs(n.y),abs(n.z)]
    i=max(range(3), key=lambda k: vals[k])
    if vals[i] < 0.85:
        return 'OTHER'
    axis='XYZ'[i]
    sign='+' if (n.x,n.y,n.z)[i] >= 0 else '-'
    return sign+axis


def _role_data(obj):
    raw=''
    key=''
    if obj.get('DWOS_face_roles'):
        raw=obj.get('DWOS_face_roles'); key='DWOS_face_roles'
    elif obj.get('DWOS_glass_roles'):
        raw=obj.get('DWOS_glass_roles'); key='DWOS_glass_roles'
    try:
        roles=json.loads(raw or '[]')
        if not isinstance(roles,list): roles=[]
    except Exception:
        roles=[]
    return key,roles


def _normal_role_mismatches(obj, roles):
    if not roles or obj.type!='MESH':
        return []
    s=getattr(obj,'dwos_window',None)
    # Children carry no settings; walk to controller.
    c=obj
    while c is not None and not (getattr(c,'dwos_window',None) and c.dwos_window.is_window):
        c=c.parent
    interior_sign=1 if (c and c.dwos_window.flip_interior) else -1
    exterior_sign=-interior_sign
    adaptive_class=str((c.get('DWOS_adaptive_classification','') if c else '') or '')
    # CURVED/CORNER faces legitimately have branch-local facade normals.
    # Comparing them to controller-local +/-Z creates large false positives.
    # Their winding is already validated by adaptive_validation against topology.
    if adaptive_class in {'CURVED','CORNER'}:
        return []
    out=[]
    for p in obj.data.polygons:
        if p.index >= len(roles): continue
        role=str(roles[p.index]).upper(); n=p.normal.normalized()
        ok=True; expected=''
        if role in {'FRAME_EXTERIOR','GLASS_EXTERIOR','GLASS_SINGLE'}:
            target=Vector((0,0,exterior_sign)); expected=f'normal·Z={exterior_sign:+d}'
            ok=n.dot(target) > 0.75
        elif role in {'FRAME_INTERIOR_BACK','GLASS_INTERIOR','INTERIOR_V_BACK_RETURN'}:
            target=Vector((0,0,interior_sign)); expected=f'normal·Z={interior_sign:+d}'
            ok=n.dot(target) > 0.75
        elif role == 'INTERIOR_BACK':
            target=Vector((0,0,-interior_sign)); expected=f'normal·Z={-interior_sign:+d} (toward aperture)'
            ok=n.dot(target) > 0.75
        elif role in {'FRAME_REVEAL','FRAME_CONTACT','INTERIOR_REVEAL'}:
            expected='side normal (|Z|<0.35)'; ok=abs(n.z)<0.35
        if not ok:
            out.append({'face':p.index,'role':role,'normal':tuple(round(v,5) for v in n),'expected':expected})
    return out


def analyze_object(obj):
    rec={
        'name': getattr(obj,'name','<none>'),
        'type': getattr(obj,'type',''),
        'parent': getattr(getattr(obj,'parent',None),'name',''),
        'role': obj.get('DWOS_role','') if obj else '',
    }
    if obj is None or obj.type!='MESH':
        return rec
    me=obj.data
    inc=_edge_face_incidence(me)
    components=_connected_components(me)
    local_buckets=Counter(_normal_bucket(p.normal.normalized()) for p in me.polygons if p.normal.length_squared>EPS)
    mw3=obj.matrix_world.to_3x3()
    world_buckets=Counter(_normal_bucket((mw3 @ p.normal).normalized()) for p in me.polygons if p.normal.length_squared>EPS)
    zero_faces=sum(1 for p in me.polygons if p.area <= 1e-12)
    zero_edges=sum(1 for e in me.edges if (me.vertices[e.vertices[0]].co-me.vertices[e.vertices[1]].co).length <= 1e-10)
    nonfinite=sum(1 for v in me.vertices if not all(float(c)==float(c) and abs(float(c)) != float('inf') for c in v.co))
    key,roles=_role_data(obj)
    rec.update({
        'mesh': me.name,
        'verts': len(me.vertices), 'edges': len(me.edges), 'faces': len(me.polygons),
        'wire_edges': inc['wire'], 'boundary_edges': inc['boundary'], 'manifold_edges': inc['manifold'], 'overused_edges': inc['overused'],
        'zero_edges': zero_edges, 'zero_faces': zero_faces, 'nonfinite_verts': nonfinite,
        'components': components,
        'normal_local': dict(sorted(local_buckets.items())),
        'normal_world': dict(sorted(world_buckets.items())),
        'multi_plane_host': bool(not obj.get('DWOS_role') and len([axis for axis,count in local_buckets.items() if axis!='OTHER' and count>0])>=3),
        'materials': material_summary(obj),
        'role_key': key,
        'role_counts': dict(Counter(str(r) for r in roles)),
        'role_count_matches_faces': (len(roles)==len(me.polygons)) if roles else None,
        'normal_role_mismatches': _normal_role_mismatches(obj,roles),
        'dimensions': tuple(round(float(v),6) for v in obj.dimensions),
        'scale': tuple(round(float(v),6) for v in obj.scale),
        'matrix_det': round(float(obj.matrix_world.to_3x3().determinant()),8),
    })
    if obj.get('DWOS_last_weld_mode'):
        rec['weld_host']=(obj.get('DWOS_last_weld_mode'), obj.get('DWOS_last_weld_shared_edges'))
    settings=getattr(obj,'dwos_window',None)
    if settings and settings.is_window:
        try:
            from .numeric import parse_meter_text
            roles_list=roles
            exterior_faces=[p for p in me.polygons if p.index < len(roles_list) and str(roles_list[p.index]).upper()=='FRAME_EXTERIOR']
            zs=[]
            for poly in exterior_faces:
                zs.extend(float(me.vertices[i].co.z) for i in poly.vertices)
            expected_front=parse_meter_text(settings.frame_offset_text,allow_negative=True)
            adaptive_class=str(obj.get('DWOS_adaptive_classification','') or '')
            actual_front=(sum(zs)/len(zs)) if zs else None
            flush_error=(abs(actual_front-expected_front) if actual_front is not None else None)
            anchor_metric='LOCAL_Z_PLANE'
            if adaptive_class in {'CURVED','CORNER'}:
                # One controller-local Z plane does not exist on a folded facade.
                # Use the stored adaptive topology's Host-contact reconstruction
                # error instead of reporting a meaningless 0.6 m "flush error".
                try:
                    from .payload_core import decode_opening_payload, adaptive_topology_from_payload
                    from .adaptive_opening_core import contact_error
                    topo=adaptive_topology_from_payload(decode_opening_payload(settings.opening_payload_json))
                    flush_error=float(contact_error(topo)) if topo else None
                    actual_front=None
                    anchor_metric='ADAPTIVE_HOST_CONTACT'
                except Exception:
                    flush_error=None;actual_front=None;anchor_metric='ADAPTIVE_HOST_CONTACT_UNAVAILABLE'
            rec['window_meta']={
                'window_id':settings.window_id,
                'host':settings.host_name,
                'size':(settings.width_text,settings.height_text),
                'frame_width':settings.frame_width_text,
                'internal_width':settings.internal_width_text,
                'corner_post_variant':getattr(settings,'corner_post_variant','LEGACY'),
                'corner_post_width_requested':getattr(settings,'corner_post_width_text','0,06'),
                'corner_post_width_effective_m':float(obj.get('DWOS_corner_post_width_effective_m',0.0)),
                'frame_depth':settings.frame_depth_text,
                'frame_offset':settings.frame_offset_text,
                'optimization':settings.optimization,
                'splits':len(settings.splits),
                'front_expected':round(expected_front,6),
                'front_actual':round(actual_front,6) if actual_front is not None else None,
                'flush_error':round(flush_error,9) if flush_error is not None else None,
                'anchor_metric':anchor_metric,
                'adaptive_classification':adaptive_class,
                'anchor':obj.get('DWOS_anchor_mode',''),
                'welded':bool(obj.get('DWOS_welded_to_host',False)),
                'material_name':settings.frame_material.name if settings.frame_material else 'DWOS_Frame_Default',
                'interior_mode':settings.interior_mode,
                'interior_depth':settings.interior_depth_text,
                'interior_offset':settings.interior_offset_text,
                'interior_clearance':settings.interior_clearance_text,
                'glass_edge_offset':settings.glass_edge_offset_text,
                'glass_anchor':'FRAME_BELT_CENTERLINE_HALF_DEPTH',
                'glass_overlap_metres':obj.get('DWOS_glass_overlap_metres',None),
                'interior_anchor':'FRAME_BELT_CENTERLINE_QUARTER_DEPTH',
                'weld_strategy':obj.get('DWOS_weld_strategy','LIVE'),
                'corner_mullion_policy':obj.get('DWOS_corner_mullion_envelope_policy',''),
                'branch_profile_policy':obj.get('DWOS_branch_profile_attachment_policy',''),
                'h_contact_caps_removed':obj.get('DWOS_branch_profile_h_caps_removed',0),
                'v_contact_caps_suppressed':obj.get('DWOS_branch_profile_v_caps_suppressed',0),
                'corner_assembly_policy':obj.get('DWOS_corner_assembly_policy',''),
                'corner_assembly_components':obj.get('DWOS_corner_assembly_components',0),
                'corner_assembly_profiles':obj.get('DWOS_corner_assembly_profiles',0),
                'corner_assembly_graph_edges':obj.get('DWOS_corner_assembly_graph_edges',0),
                'corner_assembly_host_faces_removed':obj.get('DWOS_corner_assembly_host_faces_removed',0),
                'corner_assembly_vertices_welded':obj.get('DWOS_corner_assembly_vertices_welded',0),
                'corner_assembly_duplicate_faces_removed':obj.get('DWOS_corner_assembly_duplicate_faces_removed',0),
                'corner_branch_back_rim_vertices_moved':obj.get('DWOS_corner_branch_back_rim_vertices_moved',0),
                'corner_v_profile_policy':obj.get('DWOS_corner_v_profile_policy',''),
                'corner_v_profile_count':obj.get('DWOS_corner_v_profile_count',0),
                'corner_v_max_branch_angle_error_deg':obj.get('DWOS_corner_v_max_branch_angle_error_deg',0.0),
                'corner_v_max_straightness_error':obj.get('DWOS_corner_v_max_straightness_error',0.0),
                'corner_v_max_section_error_m':obj.get('DWOS_corner_v_max_section_error_m',0.0),
                'corner_v_max_orthogonality_error':obj.get('DWOS_corner_v_max_orthogonality_error',0.0),
                'corner_v_corner_shape_propagation':bool(obj.get('DWOS_corner_v_corner_shape_propagation',False)),
            }
        except Exception as exc:
            rec['window_meta_error']=repr(exc)
    return rec


def _format_object(rec):
    lines=[]
    lines.append(f"OBJECT {rec.get('name')} | role={rec.get('role') or '-'} | parent={rec.get('parent') or '-'}")
    if rec.get('type')!='MESH':
        lines.append(f"  type={rec.get('type')}")
        return lines
    lines.append(f"  mesh={rec.get('mesh')} | V/E/F={rec.get('verts')}/{rec.get('edges')}/{rec.get('faces')} | components={rec.get('components')}")
    lines.append(f"  edges: boundary={rec.get('boundary_edges')} manifold={rec.get('manifold_edges')} wire={rec.get('wire_edges')} overused={rec.get('overused_edges')}")
    lines.append(f"  degenerate: zeroEdges={rec.get('zero_edges')} zeroFaces={rec.get('zero_faces')} nonFiniteVerts={rec.get('nonfinite_verts')}")
    lines.append(f"  dimensions={rec.get('dimensions')} scale={rec.get('scale')} matrixDet={rec.get('matrix_det')}")
    lines.append(f"  normals local={rec.get('normal_local')} world={rec.get('normal_world')}")
    if rec.get('multi_plane_host'):
        lines.append('  host topology: MULTI_PLANE_CANDIDATE; opposite facades are not proof of physical thickness')
    if rec.get('role_key'):
        lines.append(f"  semantic={rec.get('role_key')} countMatchesFaces={rec.get('role_count_matches_faces')} roles={rec.get('role_counts')}")
        mism=rec.get('normal_role_mismatches') or []
        lines.append(f"  semanticNormalMismatches={len(mism)}")
        for m in mism[:24]:
            lines.append(f"    face={m['face']} role={m['role']} normal={m['normal']} expected={m['expected']}")
        if len(mism)>24:
            lines.append(f"    ... +{len(mism)-24} more")
    mats=rec.get('materials') or []
    lines.append(f"  materials={len(mats)}")
    for m in mats:
        lines.append(f"    slot={m.get('slot')} name={m.get('name')} role={m.get('role') or '-'} source={m.get('source') or '-'} faces={m.get('faces')}")
    if rec.get('weld_host'):
        lines.append(f"  WELD_PROOF: mode={rec['weld_host'][0]} sharedWallFrameEdges={rec['weld_host'][1]}")
    if rec.get('window_meta'):
        w=rec['window_meta']
        lines.append(f"  window: id={w.get('window_id')} host={w.get('host')} size={w.get('size')} splits={w.get('splits')} optimization={w.get('optimization')}")
        lines.append(f"  frame: width={w.get('frame_width')} internal={w.get('internal_width')} depth={w.get('frame_depth')} offset={w.get('frame_offset')} anchor={w.get('anchor')}")
        lines.append(f"  facadeAnchor: metric={w.get('anchor_metric')} class={w.get('adaptive_classification')} expected={w.get('front_expected')} actual={w.get('front_actual')} flushError={w.get('flush_error')} welded={w.get('welded')}")
        lines.append(f"  frameMaterial: selected={w.get('material_name')}")
        lines.append(f"  interior: mode={w.get('interior_mode')} depth={w.get('interior_depth')} offset={w.get('interior_offset')} clearance={w.get('interior_clearance')}")
        lines.append(f"  geometryAnchors: glass={w.get('glass_anchor')} glassEdgeOffset={w.get('glass_edge_offset')} interior={w.get('interior_anchor')}")
        lines.append(f"  weldStrategy: {w.get('weld_strategy')}")
        if w.get('adaptive_classification')=='CORNER':
            lines.append(f"  cornerMullions: policy={w.get('corner_mullion_policy')} branchProfile={w.get('branch_profile_policy')} hCapsRemoved={w.get('h_contact_caps_removed')} vCapsSuppressed={w.get('v_contact_caps_suppressed')}")
            lines.append(f"  cornerAssembly: policy={w.get('corner_assembly_policy')} components={w.get('corner_assembly_components')} profiles={w.get('corner_assembly_profiles')} graphEdges={w.get('corner_assembly_graph_edges')} hostFacesOpened={w.get('corner_assembly_host_faces_removed')} verticesWelded={w.get('corner_assembly_vertices_welded')} duplicateFacesRemoved={w.get('corner_assembly_duplicate_faces_removed')}")
            lines.append(f"  cornerVBasis: policy={w.get('corner_v_profile_policy')} count={w.get('corner_v_profile_count')} branchAngleDeg={w.get('corner_v_max_branch_angle_error_deg')} straight={w.get('corner_v_max_straightness_error')} section={w.get('corner_v_max_section_error_m')} orth={w.get('corner_v_max_orthogonality_error')} cornerPropagation={w.get('corner_v_corner_shape_propagation')} backRimMoved={w.get('corner_branch_back_rim_vertices_moved')}")
    if rec.get('window_meta_error'):
        lines.append(f"  windowMetaError={rec.get('window_meta_error')}")
    return lines


def _window_tree(window_obj):
    out=[window_obj]
    stack=list(window_obj.children)
    while stack:
        o=stack.pop(0); out.append(o); stack.extend(list(o.children))
    return out


def relevant_objects(scene, active=None):
    host=None; windows=[]
    controller=active
    for _ in range(8):
        if controller is None: break
        if getattr(controller,'dwos_window',None) and controller.dwos_window.is_window: break
        controller=controller.parent
    if controller and getattr(controller,'dwos_window',None) and controller.dwos_window.is_window:
        host=bpy.data.objects.get(controller.dwos_window.host_name)
        windows=[o for o in scene.objects if getattr(o,'dwos_window',None) and o.dwos_window.is_window and o.dwos_window.host_name==controller.dwos_window.host_name]
    else:
        st=getattr(scene,'dwos',None)
        host=bpy.data.objects.get(st.wall_name) if st and st.wall_name else (active if active and active.type=='MESH' else None)
        if host:
            windows=[o for o in scene.objects if getattr(o,'dwos_window',None) and o.dwos_window.is_window and o.dwos_window.host_name==host.name]
    objs=[]
    if host: objs.append(host)
    for w in windows:
        objs.extend(_window_tree(w))
    # preserve order and uniqueness
    unique=[]; seen=set()
    for o in objs:
        if o and o.name not in seen:
            seen.add(o.name); unique.append(o)
    return unique


def generate_report(scene, active=None, reason='MANUAL'):
    objs=relevant_objects(scene,active)
    lines=[
        'DWOS Window Core — Mesh Analysis',
        f'Time: {datetime.now().isoformat(timespec="seconds")}',
        f'Reason: {reason}',
        f'Blender: {bpy.app.version_string}',
        f'Objects: {len(objs)}',
        '',
    ]
    for obj in objs:
        lines.extend(_format_object(analyze_object(obj))); lines.append('')
    return '\n'.join(lines).rstrip()+'\n'


def write_report(scene, active=None, reason='MANUAL'):
    report=generate_report(scene,active,reason)
    path=write_mesh_report(report)
    log_event('MESH_ANALYSIS_OK', reason=reason, objects=len(relevant_objects(scene,active)), path=path)
    log_block('DWOS MESH ANALYSIS '+reason, report)
    return path


class DWOS_OT_analyze_meshes(bpy.types.Operator):
    bl_idname='dwos.analyze_meshes'; bl_label='Записать анализ сетки'; bl_options={'REGISTER'}
    reason: bpy.props.StringProperty(default='MANUAL')
    def execute(self,context):
        try:
            path=write_report(context.scene,context.active_object,self.reason or 'MANUAL')
        except Exception as exc:
            log_exception('MESH_ANALYSIS_FAILED',exc,reason=self.reason)
            self.report({'ERROR'},f'Анализ сетки: {exc}'); return {'CANCELLED'}
        self.report({'INFO'},f'Анализ записан: {path}')
        return {'FINISHED'}
