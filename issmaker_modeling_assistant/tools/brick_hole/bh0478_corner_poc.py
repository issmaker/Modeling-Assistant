"""BH0478 TEST 1: read-only experimental three-face micro-corner builder.

This is a proof of concept, NOT a production operator. Geometry is built in
a private BMesh cloned from the supplied object. It never writes its source.
Scope: three edge-connected vertical quads in a chain, with a short middle
transition and a non-collinear offset of the two main faces.
"""
import bpy
import bmesh
import math
from mathutils import Vector
from . import face_uv_density_gate, UV_SAFE_MIN, UV_SAFE_MAX
from .filled_topology_guard import validate_filled_result_topology
from .filled_intersection_guard import validate_filled_result_intersections

SCALE = 1000.0 / 4096.0


def _cross(a, b):
    return a.x * b.y - a.y * b.x


def _line_intersection(a, b, c, d):
    u, v = b-a, d-c
    determinant = _cross(u, v)
    if abs(determinant) < 1.0e-8:
        raise ValueError('Основные стороны угла почти параллельны')
    return a + u * (_cross(c-a, v) / determinant)


def _uv_orientation(face, layer):
    points = [loop[layer].uv for loop in face.loops]
    return 0.5 * sum(points[i].x*points[(i+1)%len(points)].y
                     - points[(i+1)%len(points)].x*points[i].y
                     for i in range(len(points)))


def run_corner_test_one(obj, face_indices, depth=-0.491817831993103, output_mesh=None):
    if obj.type != 'MESH' or len(face_indices) != 3:
        raise ValueError('Нужны три полигона Mesh')
    mesh = obj.data
    before_counts = (len(mesh.vertices), len(mesh.edges), len(mesh.polygons))
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        selected = [bm.faces[i] for i in face_indices]
        face_set = set(selected)
        # Traverse faces over exact shared BMEdges, not coincident positions.
        graph = {f: {other for edge in f.edges for other in edge.link_faces
                     if other in face_set and other is not f} for f in selected}
        tips = [f for f in selected if len(graph[f]) == 1]
        if len(tips) != 2:
            raise ValueError('Три грани должны образовывать одну цепочку')
        mid = next(f for f in selected if len(graph[f]) == 2)
        order = (tips[0], mid, tips[1])
        common01 = list(set(order[0].edges) & set(order[1].edges))
        common12 = list(set(order[1].edges) & set(order[2].edges))
        if len(common01) != 1 or len(common12) != 1:
            raise ValueError('Стык граней должен состоять из одного ребра')
        def vertical(edge):
            a, b = edge.verts
            return ((a.co.xy-b.co.xy).length < 1e-5 and
                    abs(a.co.z-b.co.z) > 1e-3)
        if not all(vertical(e) for e in common01+common12):
            raise ValueError('Тест 1 поддерживает вертикальную стену')
        def outer_edge(face, excluding):
            matches = [e for e in face.edges if e is not excluding and vertical(e)]
            if len(matches) != 1:
                raise ValueError('Неоднозначная внешняя вертикальная граница')
            return matches[0]
        path_edges = (outer_edge(order[0], common01[0]), common01[0],
                      common12[0], outer_edge(order[2], common12[0]))
        pbot = [min(e.verts, key=lambda v: v.co.z) for e in path_edges]
        ptop = [max(e.verts, key=lambda v: v.co.z) for e in path_edges]
        zlo = pbot[0].co.z
        zhi = ptop[0].co.z
        if any(abs(v.co.z-zlo)>1e-5 for v in pbot) or any(
                abs(v.co.z-zhi)>1e-5 for v in ptop):
            raise ValueError('В этом прототипе высоты ребер должны совпадать')
        if abs((zhi-zlo)-4.0)>1e-4:
            raise ValueError('TEST 1 requires existing four-metre faces')
        xy = [v.co.xy.copy() for v in pbot]
        normal0 = order[0].normal.xy.copy()
        normal2 = order[2].normal.xy.copy()
        if normal0.length < .9 or normal2.length < .9:
            raise ValueError('Грани должны быть вертикальными')
        corner = _line_intersection(
            xy[0] + normal0*depth, xy[1] + normal0*depth,
            xy[2] + normal2*depth, xy[3] + normal2*depth)
        points = (xy[0]+normal0*depth, corner, xy[3]+normal2*depth)
        originals = [f for f in bm.faces if f not in face_set]
        uv = bm.loops.layers.uv.get('UVMap')
        if uv is None:
            raise ValueError('UVMap отсутствует')
        source_uv = {f: tuple((loop.vert, tuple(loop[uv].uv))
                              for loop in f.loops) for f in originals}
        borders_before = sum(e.is_boundary for e in bm.edges)
        qbot = [bm.verts.new((v.x,v.y,zlo)) for v in points]
        qtop = [bm.verts.new((v.x,v.y,zhi)) for v in points]
        for face in selected:
            bm.faces.remove(face)
        caps = (
            bm.faces.new((qtop[1],qbot[1],qbot[0],qtop[0])),
            bm.faces.new((qtop[2],qbot[2],qbot[1],qtop[1])),
        )
        side = (
            bm.faces.new((ptop[0],ptop[1],qtop[1],qtop[0])),
            bm.faces.new((ptop[1],ptop[2],ptop[3],qtop[2],qtop[1])),
            bm.faces.new((pbot[1],pbot[0],qbot[0],qbot[1])),
            bm.faces.new((pbot[3],pbot[2],pbot[1],qbot[1],qbot[2])),
            bm.faces.new((pbot[0],ptop[0],qtop[0],qbot[0])),
            bm.faces.new((ptop[3],pbot[3],qbot[2],qtop[2])),
        )
        tag = bm.faces.layers.int.get('brick_hole_result')
        if tag is None:
            tag = bm.faces.layers.int.new('brick_hole_result')
        new_tag = max((f[tag] for f in bm.faces), default=0)+1
        for i, f in enumerate(side):
            f[tag] = new_tag+i
        for face in caps + side[4:]:
            low = min(v.co.z for v in face.verts)
            bottom = [v for v in face.verts if abs(v.co.z-low)<1e-5]
            origin = bottom[0].co.copy()
            direction = (bottom[1].co-origin).normalized()
            for loop in face.loops:
                delta = loop.vert.co-origin
                loop[uv].uv = (UV_SAFE_MIN + delta.dot(direction)*SCALE,
                               UV_SAFE_MIN + delta.z*SCALE)
        for face in side[:4]:
            xmin = min(v.co.x for v in face.verts)
            ymin = min(v.co.y for v in face.verts)
            for loop in face.loops:
                xyz = loop.vert.co
                loop[uv].uv = (UV_SAFE_MIN+(xyz.x-xmin)*SCALE,
                               UV_SAFE_MIN+(xyz.y-ymin)*SCALE)
        # Each new face is independently unwrapped. Prefer positive UV
        # winding, without changing the face vertices or original host UV.
        for face in caps+side:
            signed = _uv_orientation(face, uv)
            if abs(signed)<1e-10:
                raise ValueError('Дегенеративная UV-проекция новой грани')
            if signed < 0:
                left = min(loop[uv].uv.x for loop in face.loops)
                right = max(loop[uv].uv.x for loop in face.loops)
                for loop in face.loops:
                    loop[uv].uv.x = left+right-loop[uv].uv.x
        bm.normal_update()
        bm.faces.index_update()
        topology = validate_filled_result_topology(bm, tag, new_tag,
                                                   caps, 2, 6, 1)
        collision = validate_filled_result_intersections(
            bm, tag, new_tag, cap_faces=caps)
        for face in caps+side:
            if face.calc_area()<=1e-9:
                raise ValueError('Zero area new surface')
            face_uv_density_gate(face, uv, obj.matrix_world, 4096)
            for loop in face.loops:
                pos = loop[uv].uv
                if not (UV_SAFE_MIN-2e-6<=pos.x<=UV_SAFE_MAX+2e-6 and
                        UV_SAFE_MIN-2e-6<=pos.y<=UV_SAFE_MAX+2e-6):
                    raise ValueError('UV outside 4-metre tile')
            if _uv_orientation(face,uv)<=0:
                raise ValueError('Negative UV orientation')
        host_unchanged = all(
            f.is_valid and len(f.loops)==len(saved) and all(
                loop.vert is v and
                max(abs(loop[uv].uv[i]-co[i]) for i in (0,1))<1e-7
                for loop,(v,co) in zip(f.loops,saved))
            for f,saved in source_uv.items())
        boundary_preserved = sum(e.is_boundary for e in bm.edges)==borders_before
        if not host_unchanged or not boundary_preserved:
            raise ValueError('Other host UV or mesh boundary changed')
        if any(len(e.link_faces)>2 for e in bm.edges):
            raise ValueError('Non-manifold edge created')
        if output_mesh is not None:
            if output_mesh is mesh or len(output_mesh.vertices) or len(output_mesh.polygons):
                raise ValueError('Нужно пустое отдельное Mesh для TEST 1')
            bm.to_mesh(output_mesh)
            output_mesh.update()
        return {
            'case': 'MICRO_CORNER_THREE_QUADS',
            'status': 'PASS', 'depth_m': depth,
            'source_faces':list(face_indices),
            'source_micro_edge_m': (xy[2]-xy[1]).length,
            'new_cap_faces':len(caps),
            'new_reveal_faces':len(side),
            'new_faces_total':len(bm.faces),
            'total_faces_before':before_counts[2],
            'boundary_edges_unchanged':boundary_preserved,
            'host_uv_preserved':host_unchanged,
            'target_uv_density':1000,
            'uv_safe_range':'PASS',
            'positive_uv_winding':len(caps)+len(side),
            'topology_pairs_checked':topology,
            'collision_pairs_checked':collision,
            'miter_xy':tuple(corner),
        }
    finally:
        bm.free()


def run_native_corner_poc_cases():
    obj=bpy.data.objects.get('UV_Wall')
    if obj is None or obj.type != 'MESH':
        raise RuntimeError('Тест 1 требует текущий UV_Wall')
    face_ids=(31,36,41)
    cases=[]
    for d in (-0.05,-0.491817831993103):
        try:
            cases.append(run_corner_test_one(obj,face_ids,d))
        except Exception as exc:
            cases.append({'case':'MICRO_CORNER_THREE_QUADS',
                          'depth_m':d,'status':'FAIL',
                          'reason':type(exc).__name__+': '+str(exc)})
    return cases
