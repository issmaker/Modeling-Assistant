"""DWOS 3.5.1 Corner Interior Geometry Lock.

User-approved top-view contract (2026-10-13): a two-branch CORNER opening is
one clean quadrilateral plan.  The blue frame is the two adjacent sides
A->C->B; Interior is the opposite two sides A->I->B.  I is the single opposite
corner of the parallelogram.  There is no stepped branch offset chain and no
extra back-wall zig-zag through the hard corner.

The module is pure Python and intentionally independent from bpy/mathutils.
"""
from __future__ import annotations
import math
try:
    from .adaptive_opening_core import map_svd_local
except ImportError:
    from adaptive_opening_core import map_svd_local

EPS=1.0e-9
POLICY='CORNER_PARALLELOGRAM_LOCK_V1'


def hard_corner_s(topology):
    stations=topology.get('stations') or []
    width=float(topology.get('width',0.0))
    ids=[int(i) for i in (topology.get('hard_junction_station_indices') or [])
         if 0<int(i)<len(stations)-1]
    if len(ids)!=1:
        raise ValueError('Corner Interior Geometry Lock currently requires exactly one hard corner')
    return float(stations[ids[0]]['s'])-width*.5


def _xz(p): return (float(p[0]),float(p[2]))
def _v2(a,b): return (b[0]-a[0],b[1]-a[1])
def _cross2(a,b): return a[0]*b[1]-a[1]*b[0]
def _len2(v): return math.hypot(v[0],v[1])


def _segment_intersection(a,b,c,d):
    """Return true only for a proper non-adjacent 2D segment intersection."""
    ab=_v2(a,b); cd=_v2(c,d); ac=_v2(a,c)
    den=_cross2(ab,cd)
    if abs(den)<=EPS:return False
    t=_cross2(ac,cd)/den; u=_cross2(ac,ab)/den
    return EPS<t<1.0-EPS and EPS<u<1.0-EPS


def plan(topology, rect, start):
    """Return the locked top-view A,C,B,I plan in window-local coordinates.

    A/C/B lie on the two frame branches at the Interior start depth. I is the
    unique opposite corner: I = A + B - C. Therefore A-I is parallel to C-B
    and I-B is parallel to A-C, exactly matching the approved top-view sketch.
    """
    if topology.get('classification')!='CORNER':
        raise ValueError('Corner Interior Geometry Lock requires CORNER topology')
    s0,s1,v0,v1=(float(x) for x in rect)
    if s1-s0<=EPS or v1-v0<=EPS:raise ValueError('Corner Interior: empty opening rect')
    hs=hard_corner_s(topology)
    if not (s0+EPS<hs<s1-EPS):raise ValueError('Corner Interior hard corner lies outside opening rect')
    # Horizontal plan does not depend on V. Use V=0 and preserve the exact
    # Interior start depth (1/4 frame depth + user offset in the main builder).
    A=tuple(map(float,map_svd_local(topology,s0,0.0,float(start))))
    C=tuple(map(float,map_svd_local(topology,hs,0.0,float(start))))
    B=tuple(map(float,map_svd_local(topology,s1,0.0,float(start))))
    # Parallelogram closure from the user's approved drawing.
    I=(A[0]+B[0]-C[0],0.0,A[2]+B[2]-C[2])
    # Force plan Y to 0 so top/bottom layers are authored only by V.
    A=(A[0],0.0,A[2]);C=(C[0],0.0,C[2]);B=(B[0],0.0,B[2])
    ax,cz,bx,ix=_xz(A),_xz(C),_xz(B),_xz(I)
    e0=_v2(ax,cz);e1=_v2(cz,bx)
    if _len2(e0)<=1e-7 or _len2(e1)<=1e-7:raise ValueError('Corner Interior has degenerate frame branch')
    area=abs(_cross2(e0,e1))
    if area<=1e-8:raise ValueError('Corner Interior branches are nearly collinear')
    # Polygon A-C-B-I must be simple. For a parallelogram this should hold by
    # construction, but keep the explicit preflight as a release gate.
    poly=[ax,cz,bx,ix]
    if _segment_intersection(poly[0],poly[1],poly[2],poly[3]) or _segment_intersection(poly[1],poly[2],poly[3],poly[0]):
        raise ValueError('Corner Interior Geometry Lock would self-intersect')
    return {
        'policy':POLICY,'hard_s':hs,'A':A,'C':C,'B':B,'I':I,
        'branch_a_length':_len2(e0),'branch_b_length':_len2(e1),
        'return_a_length':_len2(_v2(ax,ix)),'return_b_length':_len2(_v2(ix,bx)),
        'area2':area,
    }


def preflight(topology, rect, start):
    try:
        p=plan(topology,rect,start)
        return {'ok':True,'reason':'OK',**p}
    except Exception as exc:
        return {'ok':False,'reason':str(exc),'policy':POLICY}


def build_locked_corner_interior(topology, rect, start, clearance=0.0, mode='BOX'):
    """Build user-approved CORNER Interior shell.

    Top view:
        FRAME:    A ---- C
                        |
                        B
        INTERIOR: A ---- I ---- B as the two opposite return sides A-I and I-B

    Geometry is open only on the blue frame sides A-C and C-B.  There is no
    stepped back wall and no Corner Post-dependent partition.
    """
    if mode=='OFF':return [],[],[],{'policy':POLICY}
    if mode not in {'BOX','REVEALS'}:raise ValueError('Неизвестный режим Interior')
    s0,s1,v0,v1=(float(x) for x in rect)
    c=float(clearance);s0+=c;s1-=c;v0+=c;v1-=c
    if s1<=s0 or v1<=v0:raise ValueError('Interior: отступ закрывает проём')
    P=plan(topology,(s0,s1,v0,v1),float(start))
    base=[P[k] for k in ('A','C','B','I')]
    verts=[]
    for vv in (v0,v1):
        for p in base:verts.append((float(p[0]),float(vv),float(p[2])))
    A0,C0,B0,I0=0,1,2,3;A1,C1,B1,I1=4,5,6,7
    # Coherent open-shell winding. Shared manifold edges run in opposite
    # directions; the only boundary is the frame contact L: A-C and C-B.
    faces=[
        (A0,C0,B0,I0),      # sill / bottom reveal
        (A1,I1,B1,C1),      # head / top reveal
        (A0,I0,I1,A1),      # approved green return A-I
        (I0,B0,B1,I1),      # approved green return I-B
    ]
    roles=['INTERIOR_REVEAL_BOTTOM','INTERIOR_REVEAL_TOP',
           'INTERIOR_CORNER_RETURN_A','INTERIOR_CORNER_RETURN_B']
    meta={**P,'derived_depth_a':P['return_a_length'],'derived_depth_b':P['return_b_length'],
          'requested_depth_mode':'AUTO_FROM_CORNER_GEOMETRY'}
    return verts,faces,roles,meta
