"""W58.33 Simple Interior Shell: one low-complexity, UV-free tunnel.

The front footprint follows the source opening; a measured RDP tolerance removes
only redundant S stations. HP/LP and source UV are not mutated. No ornamental
strips, per-station caps or triangles. Output is accepted only with a clean
independent self-intersection oracle and a manifold open-front shell.
"""
from __future__ import annotations
import copy, math, json
import bpy, bmesh
from mathutils import Vector

def _dist_point_line(p,a,b):
    dx=b[0]-a[0];dz=b[1]-a[1]
    ll=dx*dx+dz*dz
    if ll<=1e-18:return math.hypot(p[0]-a[0],p[1]-a[1])
    t=max(0.,min(1.,((p[0]-a[0])*dx+(p[1]-a[1])*dz)/ll))
    return math.hypot(p[0]-(a[0]+t*dx),p[1]-(a[1]+t*dz))

def _rdp(points,tolerance):
    if len(points)<=2:return list(range(len(points)))
    stack=[(0,len(points)-1)];keep={0,len(points)-1}
    while stack:
        a,b=stack.pop();best=(-1.,None)
        for i in range(a+1,b):
            p=points[i][1];d=_dist_point_line(p,points[a][1],points[b][1])
            if d>best[0]:best=(d,i)
        if best[1] is not None and best[0]>tolerance:
            keep.add(best[1]);stack.extend(((a,best[1]),(best[1],b)))
    return sorted(keep)

def _station_record(topology,u):
    rows=topology.get('stations') or []
    if not rows:raise ValueError('W5833_SIMPLE_INTERIOR_NO_STATIONS')
    u=float(u)
    if u<=float(rows[0]['s']):base=rows[0]
    elif u>=float(rows[-1]['s']):base=rows[-1]
    else:
        base=None
        for a,b in zip(rows,rows[1:]):
            if float(a['s'])<=u<=float(b['s']):
                span=float(b['s'])-float(a['s'])
                t=0. if span<=1e-12 else (u-float(a['s']))/span
                base=dict(a)
                for key in ('center','depth_vector'):
                    base[key]=[float(a[key][k])+(float(b[key][k])-float(a[key][k]))*t for k in range(3)]
                break
    rec=dict(base);rec['s']=u
    return rec

def _rdp_topology(topology,s0,s1,tolerance):
    from .adaptive_opening_core import map_svd_local
    width=float(topology['width'])
    candidates=sorted({float(s0),float(s1)}|
       {float(row['s'])-width*.5 for row in topology.get('stations',[])
        if float(s0)+1e-8<float(row['s'])-width*.5<float(s1)-1e-8})
    if len(candidates)<2:raise ValueError('W5833_SIMPLE_INTERIOR_NOT_ENOUGH_STATIONS')
    pts=[]
    for s in candidates:
        q=map_svd_local(topology,s,0.,0.)
        pts.append((s,(float(q[0]),float(q[2]))))
    kept=_rdp(pts,float(tolerance))
    simple=dict(topology)
    simple['stations']=[_station_record(topology,candidates[i]+width*.5) for i in kept]
    max_deviation=0.
    for i in range(len(pts)):
        if i in kept:continue
        j=next((k for k in range(len(kept)-1) if kept[k]<i<kept[k+1]),None)
        if j is not None:
            max_deviation=max(max_deviation,_dist_point_line(pts[i][1],pts[kept[j]][1],pts[kept[j+1]][1]))
    return simple,[candidates[i] for i in kept],{'input_stations':len(candidates),
        'output_stations':len(kept),'removed_stations':len(candidates)-len(kept),
        'max_plan_deviation_m':max_deviation,'tolerance_m':float(tolerance)}

def build_simple_mesh(topology,rect,front_depth,back_depth,*,tolerances=(.002,.005,.01,.02,.04,.08,.12,.20),seam_gap=1.e-5):
    from .interior_repair_core import repair_adaptive_interior_mesh
    from .spatial_safety_core import self_intersections
    attempts=[];last_error=None
    for tolerance in tolerances:
        simple,stations,meta=_rdp_topology(topology,float(rect[0]),float(rect[1]),tolerance)
        if len(stations)<2:continue
        try:
            verts,faces,roles,repair=repair_adaptive_interior_mesh(
                simple,rect,float(front_depth),abs(float(back_depth)-float(front_depth)),
                -1,0.0,'BOX',seam_gap=seam_gap,simple=True)
            hits=self_intersections(verts,faces,max_hits=64)
            edge_uses={}
            for face in faces:
                for a,b in zip(face,face[1:]+face[:1]):
                    key=tuple(sorted((a,b)));edge_uses[key]=edge_uses.get(key,0)+1
            zero=[]
            for fi,face in enumerate(faces):
                pts=[Vector(verts[i]) for i in face]
                area=sum((pts[i]-pts[0]).cross(pts[(i+1)%len(pts)]-pts[0]).length for i in range(1,len(pts)-1))*.5
                if area<=1e-9:zero.append(fi)
            nonmanifold=sum(v>2 for v in edge_uses.values())
            attempt={'tolerance_m':tolerance,'stations':len(stations),
                'max_plan_deviation_m':meta['max_plan_deviation_m'],'faces':len(faces),
                'self_hits':len(hits),'zero_area':len(zero),'nonmanifold_edges':nonmanifold,
                'repair':repair}
            attempts.append(attempt)
            if hits or zero or nonmanifold:
                last_error=f'W5833_SIMPLE_INTERIOR_ORACLE_FAIL:hits={len(hits)} zero={len(zero)} nonmanifold={nonmanifold}'
                continue
            return verts,faces,meta|{'face_count':len(faces),'self_intersections':0,
                'simple_shell':'BRANCH_LOCAL_OFFSET_CLIPPED','physical_seam_gap_m':repair['seam_gap_m'],
                'repair':repair,'attempts':attempts}
        except Exception as ex:
            last_error=str(ex)
            attempts.append({'tolerance_m':tolerance,'stations':len(stations),
                'max_plan_deviation_m':meta['max_plan_deviation_m'],'error':str(ex)})
    raise ValueError(last_error or 'W5833_SIMPLE_INTERIOR_NO_VALID_TOLERANCE')

def build_and_publish(frame):
    from .numeric import parse_meter_text
    from .material_system import ensure_interior_material
    payload=json.loads(str(frame.dwos_window.opening_payload_json))
    topology=payload['adaptive_topology']
    width=float(topology['width']);height=float(topology['height'])
    fw=parse_meter_text(frame.dwos_window.frame_width_text)
    fd=parse_meter_text(frame.dwos_window.frame_depth_text)
    depth=parse_meter_text(frame.dwos_window.interior_depth_text)
    if not .8<=depth<=1.5:raise ValueError('W5833_SIMPLE_INTERIOR_DEPTH_OUT_OF_RANGE')
    rect=(-width*.5+fw,width*.5-fw,-height*.5+fw,height*.5-fw)
    start=-fd;end=-depth
    verts,faces,meta=build_simple_mesh(topology,rect,start,end)
    existing=next((o for o in frame.children if o.get('DWOS_role')=='INTERIOR'),None)
    mesh=bpy.data.meshes.new('DWOS_W5833_SIMPLE_INTERIOR_'+frame.name)
    mesh.from_pydata(verts,[],faces);mesh.update()
    bm=bmesh.new();bm.from_mesh(mesh)
    bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
    bm.to_mesh(mesh);bm.free();mesh.update()
    if len(mesh.uv_layers):bpy.data.meshes.remove(mesh);raise RuntimeError('W5833_INTERIOR_UV_PROHIBITED')
    mesh.materials.append(ensure_interior_material())
    if existing:
        old=existing.data;existing.data=mesh
        if old and old.users==0:bpy.data.meshes.remove(old)
        target=existing
    else:
        target=bpy.data.objects.new('DWOS_W5833_SimpleInterior_'+frame.name,mesh)
        collection=bpy.data.collections.get('ISSMAKER_W5833_TEXEL_BATTLE_TEST | WINDOWS')
        if collection is None:
            collection=bpy.context.collection
        collection.objects.link(target)
        target.parent=frame
        target.matrix_parent_inverse=frame.matrix_world.inverted()
        target.matrix_world=frame.matrix_world.copy()
        target['DWOS_role']='INTERIOR'
    target.hide_set(False)
    target['W5833_simple_interior']=True
    target['W5833_simple_interior_meta']=json.dumps(meta,separators=(',',':'))
    frame['W5833_simple_interior_meta']=json.dumps(meta,separators=(',',':'))
    frame['DWOS_W58_interior_faces']=len(faces)
    return target,meta
