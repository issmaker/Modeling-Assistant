"""DWOS 3.3 pure corner-post and mullion envelope rules.

No bpy dependency.  Corner posts are architectural junctions, not draggable
splits.  Ordinary V mullions must never overlap their physical profile envelope.
"""
from __future__ import annotations
import math

EPS=1e-9


def corner_posts(topology, mode='AUTO', thickness=0.06, frame_depth=None):
    if not topology or topology.get('classification')!='CORNER' or str(mode)=='OFF':
        return []
    if str(mode)=='AUTO':
        try:
            from .corner_post_policy_core import evaluate_corner_post
        except ImportError:
            from corner_post_policy_core import evaluate_corner_post
        if evaluate_corner_post(topology,'AUTO')['effective_mode']=='OFF':
            return []
    t=max(EPS,float(thickness)); width=float(topology.get('width',0.0))
    fd=None
    try:
        q=float(frame_depth) if frame_depth is not None else None
        if q is not None and math.isfinite(q) and q>EPS: fd=q
    except Exception:
        fd=None
    out=[]
    junctions=list(topology.get('corner_junctions') or [])
    if not junctions:
        stations=topology.get('stations') or []
        for idx in topology.get('hard_junction_station_indices') or []:
            if 0<=int(idx)<len(stations):
                junctions.append({'station_index':int(idx),'s':float(stations[int(idx)]['s'])})
    for n,j in enumerate(junctions):
        s=float(j['s'])-width*.5
        if -width*.5+EPS < s < width*.5-EPS:
            kind=str(j.get('kind','')).upper()
            integrated=bool(kind in {'CONVEX','CONCAVE'} and fd is not None)
            extent=(fd if integrated else t*.5)
            # Integrated central post is a parallelogram built from the two
            # actual facade branch rays. For an acute interior sector its far
            # corner can project farther than one frame depth along either
            # branch. A fixed depth reserve lets a full-depth V mullion enter
            # the central post at oblique angles. Keep H contacts at the actual
            # nominal post side (physical_branch_extent), but independently
            # protect the entire projected body for V-split placement.
            protected=extent
            if integrated:
                incoming=j.get('incoming_tangent');outgoing=j.get('outgoing_tangent')
                if incoming is None or outgoing is None:
                    stations=topology.get('stations') or ()
                    idx=j.get('station_index')
                    if idx is not None and 0<int(idx)<len(stations)-1:
                        aa=stations[int(idx)-1];bb=stations[int(idx)];cc=stations[int(idx)+1]
                        if all(isinstance(x.get('center'),(tuple,list)) and len(x['center'])>=3 for x in (aa,bb,cc)):
                            incoming=[float(bb['center'][k])-float(aa['center'][k]) for k in range(3)]
                            outgoing=[float(cc['center'][k])-float(bb['center'][k]) for k in range(3)]
                if incoming is not None and outgoing is not None:
                    dot=sum(float(a)*float(b) for a,b in zip(incoming,outgoing))
                    ni=math.sqrt(sum(float(a)**2 for a in incoming))
                    no=math.sqrt(sum(float(b)**2 for b in outgoing))
                    if ni>EPS and no>EPS:
                        cos_between=max(-1.,min(1.,-dot/(ni*no)))
                        protected=extent*(1.+max(0.,cos_between))
                # Missing reliable tangents => conservative worst-case bound.
                else:
                    protected=extent*2.
            out.append({'s':s,'thickness':t,'id':f'corner:{n}','junction_kind':kind,
                        'physical_branch_extent':extent,'envelope_half_extent':protected,
                        'junction_contract':('INTEGRATED_ANGLE_V2' if integrated else 'LEGACY_INTERNAL_WIDTH_V1')})
    return out


def forbidden_center_intervals(posts, moving_thickness, clearance=0.003):
    mt=max(EPS,float(moving_thickness)); gap=max(0.0,float(clearance))
    out=[]
    for p in posts or ():
        ps=float(p['s']); pt=max(EPS,float(p['thickness']))
        extent=max(EPS,float(p.get('envelope_half_extent',pt*.5)))
        half=extent+mt*.5+gap
        out.append((ps-half,ps+half,str(p.get('id','corner'))))
    return out


def split_center_valid(center, axis, thickness, lo, hi, posts=(), clearance=0.003):
    c=float(center);t=max(EPS,float(thickness));lo=float(lo);hi=float(hi)
    if not all(math.isfinite(x) for x in (c,t,lo,hi)) or hi<=lo:
        return False
    if c-t*.5 < lo-EPS or c+t*.5 > hi+EPS:
        return False
    if axis!='V':
        return True
    for a,b,_ in forbidden_center_intervals(posts,t,clearance):
        if a+EPS < c < b-EPS:
            return False
    return True


def clamp_split_center(center, axis, thickness, lo, hi, posts=(), clearance=0.003, previous=None):
    """Clamp a preview center to valid geometry without jumping through a corner post.

    ``previous`` is the last valid center.  When dragging into a forbidden Corner
    Post envelope, the profile sticks to the approached side instead of teleporting
    across the junction.
    """
    t=max(EPS,float(thickness));lo=float(lo);hi=float(hi)
    minc=lo+t*.5; maxc=hi-t*.5
    if maxc<minc:
        raise ValueError('Split закрывает доступную область')
    c=max(minc,min(maxc,float(center)))
    if axis!='V':
        return c
    prev=float(previous) if previous is not None and math.isfinite(float(previous)) else c
    for a,b,_ in forbidden_center_intervals(posts,t,clearance):
        if a < c < b:
            left=max(minc,a);right=min(maxc,b)
            if prev<=a:
                c=left
            elif prev>=b:
                c=right
            else:
                c=left if abs(c-left)<=abs(right-c) else right
    return max(minc,min(maxc,c))


def validate_split_rows(rows, posts=(), clearance=0.003):
    """Validate global/zone split rows against Corner Post envelopes.

    Rows may be dictionaries or tuples from zone_core.layout.
    """
    conflicts=[]
    for row in rows or ():
        if isinstance(row,dict):
            sid=row.get('id','');axis=row.get('axis');center=row.get('position',row.get('center'))
            thickness=row.get('thickness',0.0);rect=row.get('rect')
        else:
            sid,axis,center,thickness,rect,*_rest=row
        if axis!='V':continue
        if rect is None:
            lo,hi=-float('inf'),float('inf')
        else:
            lo,hi=float(rect[0]),float(rect[1])
        for a,b,pid in forbidden_center_intervals(posts,float(thickness),clearance):
            c=float(center)
            if a+EPS<c<b-EPS:
                conflicts.append({'split_id':str(sid),'corner_id':pid,'center':c,
                                  'forbidden':(a,b),'rect':(lo,hi)})
    return conflicts


def postless_junction_safety_guards(topology, frame_depth):
    """Conservative no-post V-mullion exclusion at a sharp physical mitre.

    Off means NO physical central profile; nevertheless a full-depth vertical
    prism cannot safely be translated arbitrarily into a mitered frame joint.
    No guard is ever passed to the frame/glass builder as a physical post.
    """
    import math
    if not topology or topology.get('classification')!='CORNER':return []
    d=float(frame_depth)
    if not math.isfinite(d) or d<=0:raise ValueError('Invalid frame depth')
    w=float(topology.get('width',0.0)); out=[]
    for i,j in enumerate(topology.get('corner_junctions') or ()):
        turn=float(j.get('angle_deg',90.0))
        if not math.isfinite(turn) or not 0.0<turn<180.0:
            raise ValueError('Invalid junction turn angle')
        # Miter run of depth d: d*tan(turn/2). Lower bound reserves tool
        # tolerance for almost planar branches (12-degree gentle corner).
        reserve=d*max(0.25,math.tan(math.radians(turn)*.5))
        s=float(j['s'])-w*.5
        out.append({'id':f'postless_mitre:{i}','s':s,'thickness':0.0,
                    'envelope_half_extent':reserve,'physical_branch_extent':reserve,
                    'junction_contract':'VIRTUAL_NO_POST_SAFETY_ONLY'})
    return out
