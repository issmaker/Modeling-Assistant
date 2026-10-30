"""DWOS 3.2 pure facade orientation intelligence.

The solver keeps exterior/interior semantics coherent across a segmented facade.
It deliberately does not trust one averaged normal.  Boundary normal evidence is
attached to its spatial sample and voted against the local S tangent field.
"""
from __future__ import annotations
from math import sqrt

EPS=1e-9

def add(a,b): return (a[0]+b[0],a[1]+b[1],a[2]+b[2])
def sub(a,b): return (a[0]-b[0],a[1]-b[1],a[2]-b[2])
def mul(a,s): return (a[0]*s,a[1]*s,a[2]*s)
def dot(a,b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def cross(a,b): return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def length(a): return sqrt(max(0.0,dot(a,a)))
def norm(a):
    n=length(a)
    if n<=EPS: raise ValueError('zero vector')
    return mul(a,1.0/n)

def _dist2_point_segment(p,a,b):
    ab=sub(b,a); den=dot(ab,ab)
    if den<=EPS:return dot(sub(p,a),sub(p,a))
    t=max(0.0,min(1.0,dot(sub(p,a),ab)/den))
    q=add(a,mul(ab,t));d=sub(p,q)
    return dot(d,d)

def _confidence(score,total,samples):
    if total<=EPS or samples<=0:return 'LOW',0.0
    c=max(0.0,min(1.0,abs(score)/total))
    if c<0.18:return 'AMBIGUOUS',c
    if c<0.55:return 'LOW',c
    if c<0.82:return 'MEDIUM',c
    return 'HIGH',c

def solve_facade_orientation(centers, tangents, up, samples=()):
    """Return coherent exterior normals for centerline segments.

    ``samples`` items are dicts containing ``point`` and ``normal`` in the same
    coordinate system as ``centers``.  A sample is assigned to the nearest
    centerline segment.  We trust host winding as evidence, but never permit
    isolated face flips to reverse a single facade patch: one robust global
    exterior sign is chosen and confidence tells the caller how strong the
    evidence was.
    """
    centers=[tuple(map(float,p)) for p in centers]
    tangents=[norm(tuple(map(float,t))) for t in tangents]
    up=norm(tuple(map(float,up)))
    if not tangents:
        return {'segment_normals':[], 'global_sign':1.0,
                'confidence':'LOW','confidence_value':0.0,
                'sample_count':0,'positive_weight':0.0,'negative_weight':0.0}
    provisional=[norm(cross(up,t)) for t in tangents]
    score=0.0; total=0.0; pos=0.0; neg=0.0; used=0
    for rec in samples or ():
        try:
            p=tuple(map(float,rec['point'])); n=norm(tuple(map(float,rec['normal'])))
        except Exception:
            continue
        if len(centers)<2:idx=0
        else:
            idx=min(range(len(tangents)),key=lambda i:_dist2_point_segment(p,centers[i],centers[i+1]))
        alignment=dot(n,provisional[idx])
        # Ignore near-perpendicular evidence (e.g. reveal faces accidentally
        # attached to a selected edge); strongly parallel samples vote more.
        weight=abs(alignment)
        if weight<0.20:continue
        vote=weight if alignment>=0 else -weight
        score+=vote;total+=weight;used+=1
        if vote>=0:pos+=weight
        else:neg+=weight
    sign=1.0 if score>=0.0 else -1.0
    label,value=_confidence(score,total,used)
    return {'segment_normals':[mul(n,sign) for n in provisional],
            'global_sign':sign,'confidence':label,'confidence_value':value,
            'sample_count':used,'positive_weight':pos,'negative_weight':neg}

def corner_kind(tangent_before,tangent_after,up,exterior_before):
    """Classify a hard junction as exterior-convex / exterior-concave.

    This is diagnostic semantics, not a mesh-building shortcut.
    """
    a=norm(tuple(tangent_before));b=norm(tuple(tangent_after));u=norm(tuple(up))
    turn=dot(cross(a,b),u)
    # For a right-handed S/V facade basis, the side indicated by the exterior
    # normal decides whether a turn wraps around the building or into a recess.
    side=dot(norm(tuple(exterior_before)),norm(cross(u,a)))
    signed=turn*(1.0 if side>=0 else -1.0)
    if abs(signed)<1e-7:return 'STRAIGHT'
    return 'CONVEX' if signed<0 else 'CONCAVE'
