"""BH0480: production micro-corner FILLED solver, geometry and boundary-locked UV.

Conservative fast path: three connected vertical quads, short middle panel.
Runs ONLY inside protected Brick Hole working-Mesh transaction. It modifies
the transaction's Edit BMesh, never the original object data.
"""
import math
import bmesh
from mathutils import Vector
from mathutils.geometry import tessellate_polygon
from .filled_intersection_guard import validate_filled_result_intersections
from .filled_topology_guard import validate_filled_result_topology
from . import (UV_SAFE_MIN, UV_SAFE_MAX, PIXELS_PER_METER,
               face_uv_density_gate, triangle_uv_metric_error_pixels)


def _cross2(a, b):
    return a.x*b.y-a.y*b.x


def _line(a, b, c, d):
    u, v=b-a,d-c
    det=_cross2(u,v)
    if abs(det) < 1e-8:
        raise ValueError('Micro-Corner: главные стенки почти параллельны')
    return a+u*(_cross2(c-a,v)/det)


def _host_uv(edge_verts, original_faces, new_faces, uv):
    a,b=edge_verts
    host=next((f for f in a.link_faces
               if f in b.link_faces and f not in original_faces
               and f not in new_faces),None)
    if host is None:
        raise ValueError('Micro-Corner: нет соседней стены у границы')
    lookup={l.vert:l[uv].uv.copy() for l in host.loops}
    if a not in lookup or b not in lookup:
        raise ValueError('Micro-Corner: отсутствует исходная UV границы')
    return host,lookup[a],lookup[b]


def classify_three_face_corner(bm, faces):
    """Recognize geometry from topology and relative dimensions, not angles."""
    if len(faces)!=3 or any(len(f.verts)!=4 for f in faces):
        return None
    group=set(faces)
    links={f:{g for edge in f.edges for g in edge.link_faces
              if g in group and g is not f} for f in faces}
    ends=[f for f in faces if len(links[f])==1]
    center=[f for f in faces if len(links[f])==2]
    if len(ends)!=2 or len(center)!=1:return None
    mid=center[0]
    ends.sort(key=lambda f:f.index)
    order=(ends[0],mid,ends[1])
    shared1=list(set(order[0].edges)&set(mid.edges))
    shared2=list(set(order[2].edges)&set(mid.edges))
    if len(shared1)!=1 or len(shared2)!=1:return None
    def isvertical(e):
        a,b=e.verts
        return (a.co.xy-b.co.xy).length<1e-5 and abs(a.co.z-b.co.z)>1e-3
    def outer(face,ignore):
        choices=[e for e in face.edges if e is not ignore and isvertical(e)]
        if len(choices)!=1:return None
        return choices[0]
    if not (isvertical(shared1[0]) and isvertical(shared2[0])):return None
    edge0=outer(ends[0],shared1[0]);edge3=outer(ends[1],shared2[0])
    if edge0 is None or edge3 is None:return None
    path=(edge0,shared1[0],shared2[0],edge3)
    lower=[min(e.verts,key=lambda v:v.co.z) for e in path]
    upper=[max(e.verts,key=lambda v:v.co.z) for e in path]
    lo,hi=lower[0].co.z,upper[0].co.z
    if hi-lo<=1e-4 or any(abs(v.co.z-lo)>1e-4 for v in lower) or any(
            abs(v.co.z-hi)>1e-4 for v in upper):return None
    lengths=[(lower[i].co.xy-lower[i+1].co.xy).length for i in range(3)]
    if min(lengths[0],lengths[2])<=1e-4 or not (
            lengths[1]/min(lengths[0],lengths[2])<0.03):
        return None
    if abs(order[0].normal.z)>1e-4 or abs(order[2].normal.z)>1e-4:
        return None
    return order,lower,upper,(shared1[0],shared2[0]),lengths


def _test_metric(face,uv,world,resolution,expected_ppm):
    coords=[tuple(world.to_3x3() @ l.vert.co) for l in face.loops]
    mapped=[tuple(l[uv].uv) for l in face.loops]
    triangles=tessellate_polygon([[Vector(v) for v in coords]])
    if not triangles:raise ValueError('Micro-Corner: UV triangulation failed')
    for tri in triangles:
        indices=tuple(int(i) for i in tri)
        if len(indices)!=3 or len(set(indices))!=3 or any(
                i<0 or i>=len(coords) for i in indices):
            raise ValueError('Micro-Corner: неоднозначная триангуляция UV')
        errors=triangle_uv_metric_error_pixels(
            [coords[i] for i in indices],[mapped[i] for i in indices],
            resolution,pixels_per_meter=expected_ppm)
        for error,length in errors:
            if error>max(0.25,7.5*length):
                raise ValueError('Micro-Corner: UV distortion exceeds allowed metric')
    return True


def apply_micro_corner_to_edit_mesh(obj,bm,faces,depth,resolution=4096,
                                    allow_density_override=False,material_override=-1):
    plan=classify_three_face_corner(bm,faces)
    if plan is None:return None
    bm.faces.index_update()
    face_indices=[f.index for f in faces]
    tag=bm.faces.layers.int.get('brick_hole_result')
    if tag is None:tag=bm.faces.layers.int.new('brick_hole_result')
    caplayer=bm.faces.layers.int.get('brick_hole_temp_cap')
    if caplayer is None:caplayer=bm.faces.layers.int.new('brick_hole_temp_cap')
    bm.faces.ensure_lookup_table()
    faces=[bm.faces[i] for i in face_indices]
    plan=classify_three_face_corner(bm,faces)
    if depth>=-1e-6 or resolution!=4096:
        raise ValueError('Micro-Corner: требуется внутренняя глубина и UV 4096')
    order,lower,upper,old_inner,lengths=plan
    uv=bm.loops.layers.uv.get('UVMap')
    if uv is None:raise ValueError('Micro-Corner: нужна UVMap')
    selected=set(order)
    before_non_target=[f for f in bm.faces if f not in selected]
    snap={f:tuple((l.vert,tuple(l[uv].uv)) for l in f.loops) for f in before_non_target}
    borders_before=sum(e.is_boundary for e in bm.edges)
    wires_before=sum(e.is_wire for e in bm.edges)
    nonman_before=sum(len(e.link_faces)>2 for e in bm.edges)
    lower_pts=[v.co.xy.copy() for v in lower]
    normal_a,normal_b=(order[0].normal.xy.copy(),order[2].normal.xy.copy())
    if min(normal_a.length,normal_b.length)<0.99:raise ValueError('Micro-Corner: вертикаль')
    corner=_line(lower_pts[0]+normal_a*depth,
                 lower_pts[1]+normal_a*depth,
                 lower_pts[2]+normal_b*depth,
                 lower_pts[3]+normal_b*depth)
    xy=(lower_pts[0]+normal_a*depth,corner,
        lower_pts[3]+normal_b*depth)
    qbot=[bm.verts.new((v.x,v.y,lower[0].co.z)) for v in xy]
    qtop=[bm.verts.new((v.x,v.y,upper[0].co.z)) for v in xy]
    # Keep the original source faces long enough to read their UV basis.
    original_maps=[]
    for f,a,b,t in ((order[0],lower[0],lower[1],upper[0]),
                    (order[2],lower[2],lower[3],upper[2])):
        coords={l.vert:l[uv].uv.copy() for l in f.loops}
        original_maps.append((a.co.copy(),(b.co-a.co).normalized(),
            (t.co-a.co).normalized(),coords[a],
            coords[b]-coords[a],coords[t]-coords[a],
            (b.co-a.co).length,(t.co-a.co).length,f.material_index))
    # Capture the UV edges from actual adjacent (NON-SELECTED) host wall.
    perimeter=((upper[0],upper[1]),(upper[2],upper[3]),
               (lower[0],lower[1]),(lower[2],lower[3]),
               (lower[0],upper[0]),(lower[3],upper[3]))
    host=[]
    for pair in perimeter:
        h,u0,u1=_host_uv(pair,selected,set(),uv)
        host.append((h,u0,u1))
    for f in order:bm.faces.remove(f)
    for edge in old_inner:
        if edge.is_valid and not edge.link_faces:bm.edges.remove(edge)
    caps=(bm.faces.new((qtop[1],qbot[1],qbot[0],qtop[0])),
          bm.faces.new((qtop[2],qbot[2],qbot[1],qtop[1])))
    side=(bm.faces.new((upper[0],upper[1],qtop[1],qtop[0])),
          bm.faces.new((upper[1],upper[2],upper[3],qtop[2],qtop[1])),
          bm.faces.new((lower[1],lower[0],qbot[0],qbot[1])),
          bm.faces.new((lower[3],lower[2],lower[1],qbot[1],qbot[2])),
          bm.faces.new((lower[0],upper[0],qtop[0],qbot[0])),
          bm.faces.new((upper[3],lower[3],qbot[2],qtop[2])))
    new_id=max((f[tag] for f in bm.faces),default=0)+1
    for i,f in enumerate(side):
        f[tag]=new_id+i
        f.material_index=(material_override if material_override>=0
                          else host[i][0].material_index)
    for i,f in enumerate(caps):
        f[caplayer]=1
        f.material_index=(material_override if material_override>=0
                          else original_maps[i][8])
    bm.normal_update()
    actual_ppm=[]
    for i,f in enumerate(caps):
        origin,tangent,vertical,uv0,uvec,vvec,L,H,_=original_maps[i]
        ppm_u=uvec.length/L*resolution;ppm_v=vvec.length/H*resolution
        actual_ppm.append((ppm_u+ppm_v)/2)
        for loop in f.loops:
            d=loop.vert.co-origin
            loop[uv].uv=uv0+uvec*(d.dot(tangent)/L)+vvec*(d.dot(vertical)/H)
    for i,f in enumerate(side):
        a,b=perimeter[i]
        u0,u1=host[i][1:]
        base=(b.co-a.co).normalized()
        delta=u1-u0
        scale=delta.length/(b.co-a.co).length
        ppm=scale*resolution
        actual_ppm.append(ppm)
        axis=delta.normalized()
        rotate=Vector((-axis.y,axis.x))
        normal=f.normal.cross(base).normalized()
        valid=[]
        for sign in (-1,1):
            coords=[]
            for loop in f.loops:
                d=loop.vert.co-a.co
                coords.append(u0+scale*(axis*d.dot(base)+sign*rotate*d.dot(normal)))
            bounds_ok=all(
                UV_SAFE_MIN-2e-6<=v[k]<=UV_SAFE_MAX+2e-6
                for v in coords for k in (0,1))
            if bounds_ok:valid.append((sign,coords))
        if not valid:
            raise ValueError('Micro-Corner: UV выходит за пределы тайла; нужен UV-разрез')
        # Choose a physical unfold that fits. Never reflect the already
        # authored source edge or reset its phase on the wall.
        coords=next((coords for sign,coords in valid if sign==-1),valid[0][1])
        for loop,value in zip(f.loops,coords):loop[uv].uv=value
        actual_a=next(l[uv].uv for l in f.loops if l.vert is a)
        actual_b=next(l[uv].uv for l in f.loops if l.vert is b)
        if (actual_a-u0).length>1e-6 or (actual_b-u1).length>1e-6:
            raise ValueError('Micro-Corner: входная UV не закреплена на ребре')
    deviation=max(abs(d-PIXELS_PER_METER) for d in actual_ppm)
    if deviation>7.5 and not allow_density_override:
        raise ValueError(
            'Micro-Corner UV: исходный масштаб отличается от 1000 px/м '
            f'на {deviation:.1f}; требуется ваше разрешение')
    for f in caps+side:
        if f.calc_area()<=1e-9:raise ValueError('Micro-Corner: вырожденная поверхность')
        for loop in f.loops:
            v=loop[uv].uv
            if not all(UV_SAFE_MIN-2e-6<=v[k]<=UV_SAFE_MAX+2e-6 for k in (0,1)):
                raise ValueError('Micro-Corner: UV вышла за 4K тайл')
    for f,expected in zip(caps+side,actual_ppm):
        if abs(expected-PIXELS_PER_METER)<=7.5:
            face_uv_density_gate(f,uv,obj.matrix_world,resolution)
        else:
            _test_metric(f,uv,obj.matrix_world,resolution,expected)
    for f,saved in snap.items():
        if not f.is_valid or len(f.loops)!=len(saved) or any(
                l.vert is not v or max(abs(l[uv].uv[k]-c[k]) for k in (0,1))>1e-7
                for l,(v,c) in zip(f.loops,saved)):
            raise ValueError('Micro-Corner: соседняя UV или топология изменена')
    bm.faces.index_update()
    if sum(e.is_boundary for e in bm.edges)!=borders_before:
        raise ValueError('Micro-Corner: граница исходной стены изменилась')
    if sum(e.is_wire for e in bm.edges)!=wires_before:
        raise ValueError('Micro-Corner: изолированные рёбра после построения')
    if sum(len(e.link_faces)>2 for e in bm.edges)!=nonman_before:
        raise ValueError('Micro-Corner: появилось non-manifold ребро')
    top={}
    validate_filled_result_topology(bm,tag,new_id,caps,2,6,1,statistics=top)
    collision={}
    validate_filled_result_intersections(bm,tag,new_id,cap_faces=caps,statistics=collision)
    for f in bm.faces:f.select_set(False)
    for f in caps:f.select_set(True)
    bm.select_flush(True)
    bmesh.update_edit_mesh(obj.data,loop_triangles=True,destructive=True)
    return {'caps':2,'reveals':len(side),'ppm':tuple(round(v,2) for v in actual_ppm),
            'uv_override':bool(allow_density_override),'topology':top,
            'collision':collision, 'micro_width':lengths[1]}
