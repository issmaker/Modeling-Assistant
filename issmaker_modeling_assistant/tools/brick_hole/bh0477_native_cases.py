"""BH0477: Blender-native read-only BMesh tests for FILLED seam topology."""
import bmesh
from .filled_topology_guard import validate_filled_result_topology


def run_native_filled_topology_cases():
    outcomes = []

    def make_case(case):
        bm = bmesh.new()
        tag = bm.faces.layers.int.new('brick_hole_result')
        v = {}
        def vert(coord):
            if coord not in v:
                v[coord] = bm.verts.new(coord)
            return v[coord]
        points = [(0.,0.,0.),(1.,0.,0.),(2.,0.,0.),
                  (0.,1.,0.),(1.,1.,0.),(2.,1.,0.)]
        coords1 = (points[0],points[1],points[4],points[3])
        coords2 = (points[1],points[2],points[5],points[4])
        f1 = bm.faces.new(tuple(vert(q) for q in coords1))
        if case == 'SPLIT_CAP_EDGE':
            f2 = bm.faces.new(tuple(bm.verts.new(q) for q in coords2))
        else:
            f2 = bm.faces.new(tuple(vert(q) for q in coords2))
        caps = (f1,f2)
        capset = set(caps)
        borders = list({e for f in caps for e in f.edges
                        if sum(other in capset for other in e.link_faces) == 1})
        side_faces = []
        top = {}
        for edge in borders:
            a,b = edge.verts
            def upper(vtx):
                if case == 'DUPLICATED_SIDE_VERTS':
                    return bm.verts.new((float(vtx.co.x), float(vtx.co.y), float(vtx.co.z+.3)))
                if vtx not in top:
                    top[vtx] = bm.verts.new((float(vtx.co.x), float(vtx.co.y), float(vtx.co.z+.3)))
                return top[vtx]
            face = bm.faces.new((a,b,upper(b),upper(a)))
            face[tag] = 1
            side_faces.append(face)
        if case == 'MISSING_SIDE':
            bm.faces.remove(side_faces[0])
        if case == 'FLOATING_FRAGMENT':
            vs = [bm.verts.new(q) for q in
                  ((9.,0.,0.),(10.,0.,0.),(9.,1.,0.))]
            extra = bm.faces.new(vs)
            extra[tag] = 1
        bm.faces.index_update()
        return bm, tag, caps

    for name, expected in (
        ('WELDED_TWOFACE_CORNER','VALID'),
        ('SPLIT_CAP_EDGE','TOPOLOGY'),
        ('MISSING_SIDE','TOPOLOGY'),
        ('FLOATING_FRAGMENT','TOPOLOGY'),
        ('DUPLICATED_SIDE_VERTS','TOPOLOGY'),
        ('WRONG_BOUNDARY_COUNT','TOPOLOGY'),
        ('WRONG_INTERNAL_EDGE_COUNT','TOPOLOGY'),
    ):
        bm, tag, caps = make_case(name)
        try:
            before = (len(bm.verts),len(bm.edges),len(bm.faces))
            evidence = {}
            state = 'VALID'
            try:
                validate_filled_result_topology(
                    bm, tag, 1, caps, 2,
                    7 if name == 'WRONG_BOUNDARY_COUNT' else 6,
                    0 if name == 'WRONG_INTERNAL_EDGE_COUNT' else 1,
                    statistics=evidence)
            except ValueError as e:
                state = 'TOPOLOGY' if str(e).startswith('TOPOLOGY:') else 'OTHER'
            unchanged = before == (len(bm.verts),len(bm.edges),len(bm.faces))
            outcomes.append({
                'case':'BH0477_'+name,
                'status':'PASS' if state == expected and unchanged else 'FAIL',
                'evidence':{'actual':state, 'expected':expected,
                            'unchanged':unchanged, 'statistics':evidence},
            })
        finally:
            bm.free()
    return outcomes
