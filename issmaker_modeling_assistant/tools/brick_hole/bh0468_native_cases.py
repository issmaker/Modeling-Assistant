"""BH0468 native scene-independent regression, executed from Brick Hole AUTO TEST.

No real-wall rebuild, GPU/mouse, or visual seams are claimed by these tests.
"""
import traceback
from .tessellation_reliability import (_segments_conflict,
                                        verify_collinear_tessellation,
                                        TessellationCoverageError)


def run_native_tessellation_cases():
    results=[]
    def check(name,func):
        try:
            evidence=func()
            results.append({'case':'BH0468_'+name,'status':'PASS',
                            'evidence':evidence if isinstance(evidence,dict) else {}})
        except Exception as exc:
            results.append({'case':'BH0468_'+name,'status':'FAIL',
                            'evidence':{'error':str(exc),'traceback':traceback.format_exc()[-6000:]}})

    def noncontact():
        a,b=(0.,0.),(2.,0.)
        c,d=(1.,1.0e-9),(1.,1.)
        assert not _segments_conflict(a,b,c,d,1e-7)
        assert not _segments_conflict(a,b,c,d,1e-5)
        return {'minimum_gap_m':1e-9,'actual_contact':False}
    check('SUBTOLERANCE_NEAR_MISS',noncontact)

    def real_cross():
        assert _segments_conflict((0.,0.),(2.,2.),(0.,2.),(2.,0.),1e-7)
        return {'contact':True}
    check('PROPER_CROSSING_REJECT',real_cross)

    def tangency():
        assert _segments_conflict((0.,0.),(2.,0.),(1.,0.),(1.,2.),1e-7)
        return {'contact':True}
    check('EXACT_T_CONTACT_REJECT',tangency)

    def exact_overlap():
        assert _segments_conflict((0.,0.),(2.,0.),(1.,0.),(3.,0.),1e-7)
        assert not _segments_conflict((0.,0.),(2.,0.),(2.00000001,0.),(3.,0.),1e-7)
        return {'overlap_rejected':True,'disjoint_accepted':True}
    check('COLLINEAR_OVERLAP',exact_overlap)

    def flat_five():
        world=[(0.,0.,0.),(2.,0.,0.),(2.,2.,0.),(0.,2.,0.),(0.,1.,0.)]
        uv=[(a/4.,b/4.) for a,b,_ in world]
        triangles=[(0,1,2),(0,2,3)]
        assert verify_collinear_tessellation(world,uv,triangles,4096)==triangles
        return {'loops':5,'blender_triangles':2,'coverage':'EXACT'}
    check('COLLINEAR_FIVE_LOOP_PROOF',flat_five)

    def uv_kink_rejected():
        world=[(0.,0.,0.),(2.,0.,0.),(2.,2.,0.),(0.,2.,0.),(0.,1.,0.)]
        uv=[(a/4.,b/4.) for a,b,_ in world]
        uv[-1]=(0.1,0.25)
        try: verify_collinear_tessellation(world,uv,[(0,1,2),(0,2,3)],4096)
        except TessellationCoverageError as exc:
            assert 'UV_NONLINEAR' in str(exc),str(exc)
            return {'protected_error':str(exc)}
        raise AssertionError('UV kink was accepted')
    check('AUTHOR_UV_KINK_REJECT',uv_kink_rejected)
    return results
