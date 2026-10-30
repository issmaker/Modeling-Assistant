"""BH0474 native regression: authenticated welded boundary contact, no Blender scene edits."""
from .swept_collision_guard import validate_swept_edges

def run_native_z_edge_contact_cases():
    rows = []
    def check(name, strips, obstacles, method, expected, expected_authorized=False):
        stats = {}
        witnesses=[]
        error=None
        try:
            validate_swept_edges(strips, obstacles, statistics=stats,
                                 witness=witnesses.append, broadphase=method)
        except ValueError as e: error=str(e)
        outcome=('ALLOW' if error is None else
                 'BLOCK' if error.startswith('COLLISION:') else 'OTHER')
        ok=(outcome==expected)
        if expected_authorized:
            ok = ok and stats.get('authorized_generated_edge_touches',0)>0
        rows.append({'case':'BH0474_'+name+'_'+method,
                     'status':'PASS' if ok else 'FAIL',
                     'evidence': {'expected':expected,'actual':outcome,'error':error,
                                  'allowed_edge_touches':stats.get(
                                      'authorized_generated_edge_touches',0),
                                  'witness_types':[w.get('kind') for w in witnesses]}})
    for method in ('X_SWEEP','BVH'):
        a,b,x,y=object(),object(),object(),object()
        quad=((0.,0.,2.),(.03,-.01,2.),(.03,-.01,1.7),(0.,0.,1.7))
        strip={'edge':(a,b),'quad':quad}
        def obs(tri,vert_ids=None):
            return {'face_index':10,'tri':tri,
                    'verts':vert_ids if vert_ids is not None else (x,a,y),
                    'linked_edges':()}
        legal=obs(((-.03,.01,2.),(0.,0.,2.),(0.,0.,1.)))
        check('WELDED_Z_SEAM',[strip],[legal],method,'ALLOW',True)
        reversed_strip={'edge':(b,a),'quad':(quad[1],quad[0],quad[3],quad[2])}
        check('REVERSED_SOURCE_EDGE',[reversed_strip],[legal],method,'ALLOW',True)
        unwelded=obs(legal['tri'],(x,object(),y))
        check('UNWELDED_NOT_AUTHORIZED',[strip],[unwelded],method,'BLOCK')
        overlapping=obs(((-.03,.01,2.),(0.,0.,2.),(.015,-.005,1.8)))
        check('TRUE_COPLANAR_AREA_OVERLAP',[strip],[overlapping],method,'BLOCK')
        interior=obs(((0.,0.,2.),(.03,.03,2.),(.015,-.005,1.8)),(a,x,y))
        check('INTERIOR_SOURCE_CONTACT',[strip],[interior],method,'BLOCK')
        intrusion=obs(((.015,-.5,1.85),(.015,.5,1.85),(.015,0.,2.05)),
                      (object(),object(),object()))
        check('INDEPENDENT_INTRUSION',[strip],[intrusion],method,'BLOCK')
        def trans(p):return (p[2]+9.,p[0]-20.,p[1]+3.)
        rotated={'edge':(a,b),'quad':tuple(trans(q) for q in quad)}
        rotated_source=obs(tuple(trans(q) for q in legal['tri']))
        check('ROTATED_Z_AXIS_AND_TRANSLATED',[rotated],[rotated_source],method,'ALLOW',True)
    return rows
