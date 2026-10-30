"""Cross-tool deterministic Reliability Lab.

Runs inside Blender but never mutates scene data.  It exercises the common
kernel plus pure adapters used by all geometry-producing ISSMAKER tools.
"""
from __future__ import annotations
import math, traceback
from .architecture import snapshot as architecture_snapshot
from .predicates import (
    exact_segment_contact_2d,exact_segment_contact_3d,
    exact_segment_triangle_crossing_3d,exact_coplanar_triangle_positive_overlap_3d,
    signed_area_2d,
)
from .precision import policy_from_points
from .decision import proven_invalid, uncertain


def run_lab():
    rows=[]
    def case(name,fn):
        try:
            evidence=fn()
            rows.append({'case':name,'status':'PASS','evidence':evidence})
        except Exception as exc:
            rows.append({'case':name,'status':'FAIL','evidence':{'error':f'{type(exc).__name__}: {exc}','traceback':traceback.format_exc()[-1800:]}})
    case('CORE_SEGMENT_2D_CROSS',lambda: {'hit':exact_segment_contact_2d((0,0),(2,2),(0,2),(2,0))} if exact_segment_contact_2d((0,0),(2,2),(0,2),(2,0)) else (_ for _ in()).throw(AssertionError('miss')))
    case('CORE_SEGMENT_2D_NEAR_MISS',lambda: {'hit':False} if not exact_segment_contact_2d((0,0),(2,0),(0,1e-12),(2,1e-12)) else (_ for _ in()).throw(AssertionError('false hit')))
    case('CORE_SEGMENT_3D_CROSS',lambda: {'hit':True} if exact_segment_contact_3d((0,0,0),(2,0,0),(1,-1,0),(1,1,0)) else (_ for _ in()).throw(AssertionError('miss')))
    case('CORE_TRIANGLE_CROSS',lambda: {'hit':True} if exact_segment_triangle_crossing_3d((.25,.25,-1),(.25,.25,1),(0,0,0),(1,0,0),(0,1,0)) else (_ for _ in()).throw(AssertionError('miss')))
    case('CORE_COPLANAR_OVERLAP',lambda: {'overlap':True} if exact_coplanar_triangle_positive_overlap_3d(((0,0,0),(2,0,0),(0,2,0)),((.25,.25,0),(1.5,.25,0),(.25,1.5,0))) else (_ for _ in()).throw(AssertionError('miss')))
    case('CORE_SHARED_EDGE_NOT_AREA',lambda: {'overlap':False} if not exact_coplanar_triangle_positive_overlap_3d(((0,0,0),(1,0,0),(0,1,0)),((0,0,0),(1,0,0),(1,-1,0))) else (_ for _ in()).throw(AssertionError('false area overlap')))
    def translation():
        base=[(0.,0.),(4.,0.),(4.,4.),(0.,4.)];shift=[(x+1e9,y-1e9) for x,y in base]
        a=signed_area_2d(base);b=signed_area_2d(shift)
        assert abs(a-16.0)<1e-12 and abs(b-16.0)<1e-9,(a,b)
        p=policy_from_points(shift)
        return {'area_base':a,'area_shifted':b,'linear_tol':p.linear,'coord_floor':p.coordinate_floor}
    case('CORE_CAD_TRANSLATION',translation)
    def taxonomy():
        hard=proven_invalid('X','x',proof='explicit witness')
        soft=uncertain('Y','y')
        assert hard.blocking and hard.is_geometry_proof and not soft.blocking
        return {'hard':hard.as_dict(),'soft':soft.as_dict()}
    case('CORE_DECISION_TAXONOMY',taxonomy)

    # Cross-tool pure adapters. Imports are delayed so this module remains a
    # lightweight core component during addon registration.
    def collision_shared():
        from ...tools.collision_inspector.self_contact_core import shared_edge_only_contact
        ids_a=(0,1,2);ids_b=(1,0,3)
        a=((0,0,0),(1,0,0),(0,1,0));b=((1,0,0),(0,0,0),(0,-1,0))
        assert shared_edge_only_contact(ids_a,a,ids_b,b)
        b_bad=((1,0,0),(0,0,0),(.25,.25,0))
        assert not shared_edge_only_contact(ids_a,a,ids_b,b_bad)
        return {'edge_only':True,'same_side_overlap_rejected':True}
    case('COLLISION_SHARED_EDGE_PROOF',collision_shared)

    def dwos_validation():
        from ...tools.dwos_windows.native.validation import validate_window_decisions
        class S:
            def __init__(self,axis,position,thickness):self.axis=axis;self.position=position;self.thickness=thickness
        rows=validate_window_decisions(4,3,.08,.2,[S('V',0,.05),S('V',1,.05)])
        assert not any(r.kind=='PROVEN_INVALID' for r in rows),[r.as_dict() for r in rows]
        return {'decisions':[r.as_dict() for r in rows]}
    case('DWOS_DECISION_ADAPTER',dwos_validation)

    def dwos_spatial():
        from ...tools.dwos_windows.native.spatial_safety_core import triangles_intersect
        v1=[(0,0,0),(2,0,0),(0,2,0)];v2=[(.5,.5,-1),(.5,.5,1),(1.5,.5,0)]
        assert triangles_intersect(v1,(0,1,2),v2,(0,1,2))
        v3=[(0,0,1e-7),(2,0,1e-7),(0,2,1e-7)]
        assert not triangles_intersect(v1,(0,1,2),v3,(0,1,2))
        return {'cross':True,'parallel_near_miss':False}
    case('DWOS_SPATIAL_EXACT_HARD_PHASE',dwos_spatial)

    def uvwall_planar():
        from ...tools.uv_wall.UV_Wall_Professional.production.planar_integrity import signed_area,interior_side
        base=[(0,0),(4,0),(4,4),(0,4),(0,0)]
        shifted=[(x+1e9,y+1e9) for x,y in base]
        assert abs(signed_area(shifted)-16)<1e-8
        assert interior_side(shifted)=='LEFT'
        return {'area':signed_area(shifted),'side':interior_side(shifted)}
    case('UVWALL_PLANAR_TRANSLATION',uvwall_planar)

    def brick_wrapper():
        from ...tools.brick_hole.robust_predicates import exact_segment_contact_3d as fn
        assert fn((0,0,0),(1,0,0),(.5,-1,0),(.5,1,0))
        return {'shared_core':True}
    case('BRICK_HOLE_SHARED_CORE',brick_wrapper)

    passed=sum(r['status']=='PASS' for r in rows)
    return {'schema':'ISSMAKER_RELIABILITY_LAB_V1','architecture':architecture_snapshot(),
            'passed':passed,'failed':len(rows)-passed,'total':len(rows),'rows':rows}
