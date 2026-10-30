"""RC23 pure experimental open four-quad CORNER POST, independent mesh island.

These are comparison candidates, NOT a watertight/manufactured-glazing proof.
Does not mutate base mesh. All vertices are generated from the actual host angle.
"""
from __future__ import annotations
from math import acos, sin, cos, sqrt, isfinite
try:
    from .adaptive_opening_core import map_svd_local
    from .corner_assembly_core import _junction_contract
except ImportError:
    from adaptive_opening_core import map_svd_local
    from corner_assembly_core import _junction_contract

MODES=('BISECTOR','ANGLE','TAPER')
ROLE='FRAME_CORNER_POST_SHELL'

def _vadd(a,b):return tuple(x+y for x,y in zip(a,b))
def _vsub(a,b):return tuple(x-y for x,y in zip(a,b))
def _mul(a,k):return tuple(float(x)*k for x in a)
def _norm(a):
    length=sqrt(sum(x*x for x in a))
    if length<1.e-10:raise ValueError('Central post axis undefined')
    return _mul(a,1.0/length)
def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _xz(p):return (float(p[0]),float(p[2]))
def _signed_area(poly):return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(poly,poly[1:]+poly[:1]))*.5

def plan_open_post(topology, posts, frame_width, depth, front_offset, interior_sign, requested_width, mode):
    """Return four-sided open post prism; reject invalid widths before Blender mutation.

    Adaptation is deterministic: requested_width is kept in user settings; physical
    width is bounded by available frame belt, corner angle and depth. A user can
    compare variants without changing global internal mullion width.
    """
    mode=str(mode).upper()
    if mode not in MODES:raise ValueError('Unsupported experimental Corner Post variant')
    if len(posts)!=1:raise ValueError('Experimental post requires exactly one CORNER junction')
    jc=_junction_contract(topology,posts,depth)
    if jc is None:raise ValueError('Missing true CORNER geometry')
    a=_norm(_xz(jc['axis_a']));b=_norm(_xz(jc['axis_b']))
    cosine=max(-1.,min(1.,_dot(a,b)))
    angle=acos(cosine)
    if angle<.10 or angle>3.04:raise ValueError('Corner angle unsuitable for open post')
    # Hard miter line defines post's stable depth axis; no shared frame verts.
    s=float(posts[0]['s'])
    p_front=_xz(map_svd_local(topology,s,0.0,float(front_offset)))
    p_back=_xz(map_svd_local(topology,s,0.0,float(front_offset)+int(interior_sign)*float(depth)))
    tangent=_vsub(p_back,p_front)
    length=sqrt(_dot(tangent,tangent))
    if length<1.e-7:raise ValueError('Central post depth collapsed')
    longitudinal=_norm(tangent)
    transverse=(-longitudinal[1],longitudinal[0])
    midpoint=_mul(_vadd(p_front,p_back),.5)
    requested=float(requested_width);fw=float(frame_width);dep=float(depth)
    if not all(isfinite(v) for v in (requested,fw,dep)) or min(requested,fw,dep)<=0:
        raise ValueError('Central post width and frame dimensions must be positive')
    # Dynamic cap shrinks at acute angles, and stays within the frame belt.
    cap=min(fw*.87,dep*.88*max(.08,sin(angle*.5)),length*.55,
            float(topology['width'])*.08)
    if cap<.002:raise ValueError('No safe width for the experimental post at this angle')
    width=min(requested,cap)
    # Mode ANGL(E): deliberately skewed parallelogram between the true rays.
    half_len=min(length*.36,dep*.42)
    if mode=='ANGLE':
        ca=_vadd(a,b)
        if sqrt(_dot(ca,ca))<1.e-8:raise ValueError('Angle profile cannot bisect antiparallel rays')
        side=_norm(_vsub(a,b))
        # The skew is restrained to preserve a non-inverted convex quad.
        shear=min(width*.22,half_len*.24)
        p0=_vadd(_vadd(midpoint,_mul(longitudinal,-half_len)),_mul(transverse,-width*.5))
        p1=_vadd(_vadd(midpoint,_mul(longitudinal,-half_len)),_mul(transverse,width*.5))
        p2=_vadd(_vadd(midpoint,_mul(longitudinal,half_len)),_mul(transverse,width*.5))
        p3=_vadd(_vadd(midpoint,_mul(longitudinal,half_len)),_mul(transverse,-width*.5))
        p0=_vadd(p0,_mul(side,-shear));p1=_vadd(p1,_mul(side,-shear))
        p2=_vadd(p2,_mul(side,shear));p3=_vadd(p3,_mul(side,shear))
    else:
        back_width=width*(.62 if mode=='TAPER' else 1.0)
        p0=_vadd(_vadd(midpoint,_mul(longitudinal,-half_len)),_mul(transverse,-width*.5))
        p1=_vadd(_vadd(midpoint,_mul(longitudinal,-half_len)),_mul(transverse,width*.5))
        p2=_vadd(_vadd(midpoint,_mul(longitudinal,half_len)),_mul(transverse,back_width*.5))
        p3=_vadd(_vadd(midpoint,_mul(longitudinal,half_len)),_mul(transverse,-back_width*.5))
    footprint=[p0,p1,p2,p3]
    area=_signed_area(footprint)
    if not isfinite(area) or abs(area)<1.e-9:raise ValueError('Experimental post is degenerate')
    if area<0:footprint=list(reversed(footprint))
    # Four side quads only; intentionally uncapped at both ends.
    # Top/bottom terminate at the midplane of main frame horizontal belts.
    half_height=float(topology['height'])*.5
    v_lo=-half_height+fw*.5;v_hi=half_height-fw*.5
    if v_hi<=v_lo:raise ValueError('No height between frame belt midlines')
    verts=[(q[0],v_lo,q[1]) for q in footprint]+[(q[0],v_hi,q[1]) for q in footprint]
    faces=[(i,(i+1)%4,(i+1)%4+4,i+4) for i in range(4)]
    return {'verts':verts,'faces':faces,'roles':[ROLE]*4,'footprint':footprint,
            'mode':mode,'requested_width':requested,'effective_width':width,'max_width':cap,
            'angle_deg':angle*180/3.141592653589793,'height_range':(v_lo,v_hi),
            'expected_components':1,'expected_boundary_edges':8,'expected_open_ends':2}

def append_open_post(frame, post):
    """Return a new mesh dict, with no shared vertices or edits to base faces."""
    target={k:(v.copy() if isinstance(v,list) else v) for k,v in frame.items()}
    base_n=len(target['verts']); base_f=len(target['faces'])
    target['verts'] += list(post['verts'])
    target['faces'] += [tuple(base_n+i for i in face) for face in post['faces']]
    target['roles'] += list(post['roles'])
    if 'vertex_params' in target:
        target['vertex_params'] += [(0.0,float(v[1]),0.0) for v in post['verts']]
    target['independent_corner_post']={'mode':post['mode'],'vertex_start':base_n,'face_start':base_f,
        'vertex_count':8,'face_count':4,'expected_boundary_edges':8,
        'effective_width':post['effective_width'],'max_width':post['max_width']}
    return target

def audit_open_shell(mesh):
    """Verify exactly one separate open, manifold-with-boundary quad tube."""
    roles=mesh.get('roles',()); faces=mesh.get('faces',()); verts=mesh.get('verts',())
    ids=[i for i,r in enumerate(roles) if r==ROLE]
    issues=[]
    if len(ids)!=4:issues.append('CENTRAL_POST_NOT_FOUR_QUADS')
    shell_faces=[faces[i] for i in ids]
    if any(len(f)!=4 for f in shell_faces):issues.append('CENTRAL_POST_NON_QUAD_FACE')
    used={x for f in shell_faces for x in f}; base={x for i,f in enumerate(faces) if i not in ids for x in f}
    if used & base:issues.append('CENTRAL_POST_SHARED_FRAME_VERTICES')
    if len(used)!=8:issues.append('CENTRAL_POST_EXPECTED_EIGHT_VERTICES')
    incidence={}
    for f in shell_faces:
        for a,b in zip(f,f[1:]+f[:1]):
            k=tuple(sorted((a,b)));incidence[k]=incidence.get(k,0)+1
    if sum(v==1 for v in incidence.values())!=8 or any(v>2 for v in incidence.values()):
        issues.append('CENTRAL_POST_OPEN_RIM_TOPOLOGY')
    if any(any(not isfinite(float(x)) for x in verts[i]) for i in used):
        issues.append('CENTRAL_POST_NONFINITE')
    return {'status':'FAIL' if issues else 'PASS','issues':issues,'faces':len(ids),
            'boundary_edges':sum(v==1 for v in incidence.values()),'independent':not bool(used&base)}
