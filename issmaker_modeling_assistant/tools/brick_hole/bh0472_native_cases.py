"""BH0472 real Blender / robust-math regression cases.

Invoked only by AUTO TEST BRICK HOLE. No GPU/mouse proof is claimed.
"""
import traceback


def run_native_geometry_decision_cases():
    rows=[]
    def run(case,fn):
        try:
            evidence=fn()
            rows.append({'case':'BH0472_'+case,'status':'PASS','evidence':evidence})
        except Exception as exc:
            rows.append({'case':'BH0472_'+case,'status':'FAIL','evidence':{
                'reason':str(exc),'traceback':traceback.format_exc()[-8000:]}})

    from .robust_predicates import exact_segment_contact_3d
    from .tessellation_reliability import verify_collinear_tessellation
    from .swept_collision_guard import _tri_intersection

    def near_miss():
        a=(0.,0.,0.);b=(1.,0.,0.)
        c=(.5,5e-7,-1.);d=(.5,5e-7,1.)
        assert not exact_segment_contact_3d(a,b,c,d)
        return {'distance_m':5e-7,'decision':'NEAR_MISS_NOT_BLOCKED'}
    run('EXACT_SEGMENT_NEAR_MISS',near_miss)

    def crossing():
        assert exact_segment_contact_3d((0,0,0),(1,0,0),(.5,0,-1),(.5,0,1))
        return {'decision':'PROVEN_CONTACT'}
    run('EXACT_SEGMENT_CROSSING',crossing)

    def overlap():
        assert exact_segment_contact_3d((0,0,0),(2,0,0),(1,0,0),(3,0,0))
        return {'decision':'PROVEN_COLLINEAR_OVERLAP'}
    run('EXACT_SEGMENT_OVERLAP',overlap)

    def large_translation():
        o=1e12
        assert not exact_segment_contact_3d((o,o,0),(o+1,o,0),(o+.5,o+1e-3,-1),(o+.5,o+1e-3,1))
        return {'translation_m':o,'decision':'DISJOINT'}
    run('EXACT_SEGMENT_LARGE_TRANSLATION',large_translation)

    def coarse_tessellation():
        o=1e15
        world=[(o,o,0),(o+2,o,0),(o+4,o,0),(o+4,o+4,0),(o,o+4,0)]
        uv=[(0,0),(.5,0),(1,0),(1,1),(0,1)]
        tris=[(0,2,3),(0,3,4)]
        diag={}
        assert verify_collinear_tessellation(world,uv,tris,4096,diagnostics=diag)==tris
        assert diag.get('precision_uncertain') is True,diag
        return {'decision':'ACCEPTED_BY_AREA_BOUNDARY_TOPOLOGY','diagnostics':diag}
    run('PRECISION_UNCERTAIN_NOT_AUTOBLOCK',coarse_tessellation)

    def translated_triangle_contact():
        a=((0,0,0),(2,0,0),(0,2,0))
        b=((.5,.5,-1),(.5,.5,1),(1.5,.5,0))
        p0,h0=_tri_intersection(a,b,1e-7)
        o=1e9
        ta=tuple((x+o,y+o,z+o) for x,y,z in a)
        tb=tuple((x+o,y+o,z+o) for x,y,z in b)
        p1,h1=_tri_intersection(ta,tb,1e-7)
        assert bool(h0 or p0)==bool(h1 or p1)
        assert h0 and h1
        return {'translation_m':o,'contacts_origin':len(h0),'contacts_translated':len(h1)}
    run('TRIANGLE_CONTACT_TRANSLATION_INVARIANT',translated_triangle_contact)

    def invariant_bmesh():
        import bmesh
        from mathutils import Matrix,Vector
        from . import non_target_mesh_invariants
        bm=bmesh.new()
        try:
            verts=[bm.verts.new((0,0,0)),bm.verts.new((4,0,0)),bm.verts.new((4,4,0)),bm.verts.new((0,4,0))]
            face=bm.faces.new(verts)
            uv=bm.loops.layers.uv.new('UVMap')
            for loop,co in zip(face.loops,((0,0),(1,0),(1,1),(0,1))):loop[uv].uv=co
            layer=bm.faces.layers.int.new('result')
            # Blender 5.2 invalidates cached BMFace references when creating
            # a new custom-data Face layer. Re-fetch the same face by index.
            bm.faces.ensure_lookup_table()
            face=bm.faces[0]
            face[layer]=0
            base=non_target_mesh_invariants(bm,uv,layer,Matrix.Identity(4))
            moved=non_target_mesh_invariants(bm,uv,layer,Matrix.Translation(Vector((1e12,1e12,1e12))))
            for x,y in zip(base[0][:6],moved[0][:6]):assert abs(x-y)<1e-9,(base,moved)
            return {'translation_m':1e12,'invariants':'IDENTICAL'}
        finally:bm.free()
    run('NON_TARGET_INVARIANT_TRANSLATION',invariant_bmesh)

    return rows
