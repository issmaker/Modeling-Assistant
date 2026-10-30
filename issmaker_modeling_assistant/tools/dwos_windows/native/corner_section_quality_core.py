"""DWOS 3.7.18: strict *read-only* central glazing section feasibility audit.

Pure Python; a pair of planar glazing layers cannot be declared correctly
seated merely because the staged mesh has four panes. No geometry is changed.
The audit does not claim to have solved construction of a physical glazing bead.
"""
from __future__ import annotations
from math import isfinite, hypot
try:
    from .corner_flat_glass_core import postless_corner_point
    from .corner_glass_contact_core import staged_corner_glass_contacts
except ImportError:
    from corner_flat_glass_core import postless_corner_point
    from corner_glass_contact_core import staged_corner_glass_contacts

EPS=1e-9

def _p2(q):return float(q[0]),float(q[2])
def _sub(a,b):return a[0]-b[0],a[1]-b[1]
def _cross(a,b):return a[0]*b[1]-a[1]*b[0]
def _dot(a,b):return a[0]*b[0]+a[1]*b[1]

def corner_section_feasibility(frame,topology,posts,depth_center,gap,interior_sign,*,tolerance_m=0.0005):
    if not topology or topology.get('classification')!='CORNER' or not posts:
        return {'status':'NOT_APPLICABLE','issues':[],'sides':[]}
    if len(posts)!=1 or interior_sign not in (-1,1) or not all(isfinite(float(v)) for v in (depth_center,gap)) or gap<=EPS:
        return {'status':'UNVERIFIED','issues':['CORNER_SECTION_BAD_INPUT'],'sides':[]}
    try:
        physical=staged_corner_glass_contacts(frame,topology,posts)
    except Exception as exc:
        return {'status':'UNVERIFIED','issues':['CORNER_SECTION_UNREADABLE_POST_FACE'],
                'detail':f'{type(exc).__name__}: {exc}','sides':[]}
    result=[];issues=[]
    layers=(('EXTERIOR',depth_center-interior_sign*gap/2),
            ('INTERIOR',depth_center+interior_sign*gap/2))
    for side,branch in [('NEG',0),('POS',1)]:
        face=physical['side'][side];A=_p2(face['front']);B=_p2(face['back']);D=_sub(B,A)
        dd=_dot(D,D)
        if dd<=EPS*EPS:
            return {'status':'UNVERIFIED','issues':['CORNER_SECTION_COLLAPSED_FACE'],'sides':result}
        for layer,depth in layers:
            s=float(face['s']);P=_p2(postless_corner_point(topology,branch,s,0.,depth))
            direction=1 if branch==0 else -1
            T=_sub(_p2(postless_corner_point(topology,branch,s+direction*0.001,0.,depth)),P)
            ll=hypot(*T);det=_cross(T,D)
            if ll<EPS:
                return {'status':'UNVERIFIED','issues':['CORNER_SECTION_NO_TANGENT'],'sides':result}
            AP=_sub(P,A);u=max(0.,min(1.,_dot(AP,D)/dd))
            Q=(A[0]+u*D[0],A[1]+u*D[1])
            gap_at_nominal=hypot(*_sub(P,Q))
            dist_between_lines=abs(_cross(_sub(A,P),T))/ll
            parallel=abs(det)/(ll*hypot(*D))<1e-7
            in_face=False;extension_station=None
            if not parallel:
                rel=_sub(A,P)
                along=_cross(rel,D)/det
                frac=_cross(rel,T)/det
                extension_station=s+direction*0.001*along
                in_face=-1e-6<=frac<=1+1e-6
            direct_contact=(parallel and dist_between_lines<=tolerance_m) or (not parallel and in_face)
            if not direct_contact:issues.append('CORNER_GLASS_REQUIRES_PHYSICAL_STOP')
            result.append({'side':side,'layer':layer,
                           'nominal_edge_gap_m':round(gap_at_nominal,9),
                           'plane_to_post_line_m':round(dist_between_lines,9),
                           'post_side_parallel_to_pane':parallel,
                           'extension_can_contact_post':bool(direct_contact),
                           'contact_station':(round(extension_station,6) if extension_station is not None else None)})
    return {'status':'WARNING' if issues else 'PASS','issues':sorted(set(issues)),
            'sides':result,'max_nominal_gap_m':max(q['nominal_edge_gap_m'] for q in result),
            'requires_new_physical_stop':bool(issues),
            'scope':'GEOMETRIC_FEASIBILITY_ONLY_NOT_AN_INTERSECTION_CERTIFICATE',
            'tolerance_m':tolerance_m}
