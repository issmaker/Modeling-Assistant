"""DWOS 3.7.15: match both glazing layers to *actual* central-post side faces.

The historical S-interval stop was only an approximation of the post edge.
For angle-aware CORNER, central side faces are slanted in 3D; at the glass
layer's depth the projected S-stop can leave a visible gap. This module reads
staged FRAME mesh (never Blender state), and gives glazing a physical contact
point on each side face. Two glass layers remain distinct.
"""
from __future__ import annotations
from math import isfinite, sqrt
try:
    from .adaptive_opening_core import map_svd_local
except ImportError:
    from adaptive_opening_core import map_svd_local

EPS=1.e-8

def dot(a,b):return sum(float(x)*float(y) for x,y in zip(a,b))
def sub(a,b):return tuple(float(x)-float(y) for x,y in zip(a,b))
def mul(a,s):return tuple(float(x)*float(s) for x in a)
def add(a,b):return tuple(float(x)+float(y) for x,y in zip(a,b))
def norm(a):return sqrt(dot(a,a))

def _horiz(point,up):
    return sub(point,mul(up,dot(point,up)))

def staged_corner_glass_contacts(frame,topology,posts):
    """Return side-face contact loci projected perpendicular to the window V axis.

    Only use the actual integrated-post faces from a fully staged frame; never
    create an artificial central post or approximate an angle with 90 degrees.
    """
    if len(posts or ())!=1 or str(topology.get('classification'))!='CORNER':
        return None
    up=(0.,1.,0.)  # map_svd_local is already in window-local space
    if abs(norm(up)-1)>1e-4:raise ValueError('CORNER glass: invalid vertical frame basis')
    front=float(frame['z_front']);back=float(frame['z_back'])
    if abs(back-front)<=EPS:raise ValueError('CORNER glass: invalid frame depth')
    ps=float(posts[0]['s']);half=float(posts[0]['physical_branch_extent'])
    if half<=0:raise ValueError('CORNER glass: invalid physical post extent')
    out={}
    for side,role in (('NEG','FRAME_CORNER_POST_SIDE_NEG'),('POS','FRAME_CORNER_POST_SIDE_POS')):
        matches=[face for face,r in zip(frame['faces'],frame['roles']) if r==role]
        if not matches:
            raise ValueError(f'CORNER glass: missing {role}')
        horizontal=[]
        for vid in [vid for face in matches for vid in face]:
            p=_horiz(frame['verts'][vid],up)
            if all(norm(sub(p,existing))>1.e-6 for existing in horizontal):horizontal.append(p)
        if len(horizontal)!=2:raise ValueError(f'CORNER glass: {role} does not have one straight side surface')
        s=ps-half if side=='NEG' else ps+half
        expected=_horiz(map_svd_local(topology,s,0,front),up)
        ordered=sorted(horizontal,key=lambda p:norm(sub(p,expected)))
        if norm(sub(ordered[0],expected))>max(0.002,half*0.12):
            raise ValueError(f'CORNER glass: {side} post face disagrees with authorial front contour')
        out[side]={'front':ordered[0],'back':ordered[1],'s':s}
    return {'up':up,'front_depth':front,'back_depth':back,'s':ps,'side':out,
            'policy':'PHYSICAL_STAGED_CORNER_POST_SIDE_V1'}


def contact_point(contacts,side,raw_point,depth):
    if not contacts or side not in contacts['side']:raise ValueError('CORNER glass: contact side unavailable')
    front=contacts['front_depth'];back=contacts['back_depth']
    fraction=(float(depth)-front)/(back-front)
    if not isfinite(fraction) or not -1.e-7<=fraction<=1+1.e-7:
        raise ValueError('CORNER glass: glass layer outside frame physical depth')
    fraction=max(0.,min(1.,fraction))
    seg=contacts['side'][side]
    pos=add(mul(seg['front'],1-fraction),mul(seg['back'],fraction))
    up=contacts['up']
    return add(pos,mul(up,dot(raw_point,up)))
