"""RC38: width-aware continuous Interior without visible faces behind an unselected V.

Only user-selected scissors create an Interior boundary. At an unselected V,
we omit the FRONT jambs (RC36) and the full-height transverse V BACK RETURN
(RC37), which remained visible as a white strip from the room side. The
horizontal top/bottom and far rear reveal bands remain connected *behind*
the frame depth. Open seams at the metal and at the V bridge mouth are
intentional: this is an open-front cavity, not a watertight solid.
"""
from __future__ import annotations
import math
from .interior_authored_sections_core import authored_plan
from .adaptive_opening_core import map_svd_local

POLICY='SELECTED_ONLY_OPEN_V_BRIDGE_RC37'
EPS=1e-8


def continuous_interior_plan(rect,bands,mode='CUSTOM',requested=1,selected=(),joint_gap=.01,
                             metal_clearance=.0005,min_span=.003):
    base=authored_plan(rect,bands,mode,requested,selected,joint_gap,min_span)
    ordered=sorted(((str(sid),float(c),float(t)) for sid,c,t in bands),key=lambda v:v[1])
    selected_ids=set(base['active_ids'])
    x0,x1=map(float,rect[:2])
    if not math.isfinite(float(metal_clearance)) or not 0<=float(metal_clearance)<=.025:
        raise ValueError('Interior: защитный зазор вокруг V должен быть 0–0,025 м')
    blocked=[]
    for sid,c,t in ordered:
        if sid in selected_ids:continue
        lo=c-t*.5-metal_clearance;hi=c+t*.5+metal_clearance
        if lo<x0+min_span or hi>x1-min_span:
            raise ValueError(f'Interior: импост {sid} слишком близко к краю рамы')
        blocked.append({'id':sid,'center':c,'thickness':t,'left':lo,'right':hi,
                        'clearance_m':float(metal_clearance),
                        'protected_span_m':hi-lo})
    by_group=[]
    for i,(a,b) in enumerate(base['intervals']):
        here=[p for p in blocked if a<p['center']<b]
        for p in here:
            if p['left']<=a+min_span or p['right']>=b-min_span:
                raise ValueError(f'Interior: невыбранный V {p["id"]} пересекает границу выбранной секции')
        for p,q in zip(here,here[1:]):
            if p['right']+min_span>=q['left']:
                raise ValueError('Interior: недостаточно места между соседними V')
        by_group.append(here)
    return {**base,'intervals':list(base['intervals']),
            'groups':list(range(base['group_count'])),
            'physical_shell_count':base['group_count'],
            'protected_unselected_count':len(blocked),
            'protected_unselected_posts':blocked,'visible_v_return_faces':0,
            'per_group_posts':by_group,'policy':POLICY}


def _sample_stations(topology, lo, hi):
    if not topology:return []
    w=float(topology['width'])
    return sorted({float(r['s'])-w*.5 for r in topology.get('stations',())
                   if lo+EPS<float(r['s'])-w*.5<hi-EPS})


def build_continuous_group(topology,rect,posts,start,depth,direction,clearance,
                           frame_depth,facade_z,metal_depth_clearance=.003):
    """Construct one *connected* open-front cavity behind any unselected V.

    A post gap x:[lo,hi] is absent at the front. The short bottom/top
    surfaces resume behind the entire metal depth and connect through the
    shared rear cap. No vertical face is placed at the mouth of this bridge:
    the former RC36 return at that plane was visible as a white strip.
    """
    x0,x1,y0,y1=map(float,rect)
    c=float(clearance);x0+=c;x1-=c;y0+=c;y1-=c
    start=float(start);depth=float(depth);direction=int(direction)
    frame_depth=float(frame_depth);facade_z=float(facade_z)
    if (not all(math.isfinite(v) for v in (x0,x1,y0,y1,c,start,depth,frame_depth,facade_z))
            or direction not in (-1,1) or depth<=0 or x0>=x1 or y0>=y1):
        raise ValueError('Interior: invalid continuous group geometry')
    from .interior_core import MAX_INTERIOR_DEPTH_M
    if depth>MAX_INTERIOR_DEPTH_M+1e-8:
        raise ValueError('Interior: глубина не может превышать 1,5 м')
    metal_depth_clearance=float(metal_depth_clearance)
    if not math.isfinite(metal_depth_clearance) or metal_depth_clearance<.001:
        raise ValueError('Interior: неверный безопасный отступ за импостом')
    # Relative depth from the independently authored interior start plane.
    bridge_distance=frame_depth+metal_depth_clearance-direction*(start-facade_z)
    if posts and not (.001<bridge_distance<depth-.003):
        raise ValueError('Interior: не хватает глубины для непрерывного обхода V за его задней гранью; увеличьте глубину Interior')
    mid=start+direction*(bridge_distance if posts else depth*.5)
    end=start+direction*depth
    ordered=sorted(posts,key=lambda row:row['left'])
    for post in ordered:
        if post['left']<=x0+.003 or post['right']>=x1-.003:
            raise ValueError(f'Interior: импост {post["id"]} не оставляет свободной секции')
    for p,q in zip(ordered,ordered[1:]):
        if p['right']+.003>=q['left']:
            raise ValueError('Interior: пересекающиеся защитные зоны V')
    x_breaks=[x0,x1]
    for post in ordered:x_breaks.extend([post['left'],post['right']])
    x_breaks.extend(_sample_stations(topology,x0,x1))
    xs=sorted(set(round(x,8) for x in x_breaks))
    slots=[]
    for a,b in zip(xs,xs[1:]):
        if b-a<EPS:continue
        in_post=any(p['left']-EPS<=((a+b)*.5)<=p['right']+EPS for p in ordered)
        slots.append((a,b,in_post))
    if not slots:raise ValueError('Interior: empty continuous group')

    verts=[];faces=[];roles=[];cache={}
    def vertex(x,y,z):
        x=round(x,8)
        key=(x,round(y,9),round(z,9))
        if key not in cache:
            cache[key]=len(verts)
            verts.append(tuple(float(v) for v in
                               (map_svd_local(topology,x,y,z) if topology else (x,y,z))))
        return cache[key]
    def face(points,role):
        f=tuple(vertex(*p) for p in points)
        if direction==1:f=f[::-1]
        faces.append(f);roles.append(role)
    def bottom(a,b,z0,z1):
        face(((a,y0,z0),(b,y0,z0),(b,y0,z1),(a,y0,z1)),'INTERIOR_REVEAL')
    def top(a,b,z0,z1):
        face(((b,y1,z0),(a,y1,z0),(a,y1,z1),(b,y1,z1)),'INTERIOR_REVEAL')
    def rear(a,b,z,role='INTERIOR_BACK'):
        face(((a,y0,z),(b,y0,z),(b,y1,z),(a,y1,z)),role)
    def jamb(x,z0,z1,is_left):
        points=((x,y0,z0),(x,y1,z0),(x,y1,z1),(x,y0,z1))
        face(points[::-1] if is_left else points,'INTERIOR_REVEAL')
    for a,b,in_post in slots:
        if not in_post:
            bottom(a,b,start,mid);top(a,b,start,mid)
        bottom(a,b,mid,end);top(a,b,mid,end)
        rear(a,b,end)
        # Unselected V: deliberately NO full-height face at z=mid.
        # The RC36 INTERIOR_V_BACK_RETURN, though positioned behind the
        # mullion, was still visible as a white vertical patch. Bottom/top
        # strips and the shared far rear remain, preserving connectivity.
    # Outer sidewalls: two depth strips to match the U-turn strip's mesh grid.
    for x,is_left in ((x0,True),(x1,False)):
        jamb(x,start,mid,is_left);jamb(x,mid,end,is_left)
    # RC37: no vertical Interior sidewall/return anywhere across an
    # unselected post band, from the front plane through the rear bridge.
    # Top/bottom and far rear faces form one connected open-front surface;
    # they MUST NOT be capped at mid. Do not auto-fill open boundary edges.
    # Selected V boundaries are still built by the regular separate-group
    # code and continue to receive their proper Interior jambs.
    if topology:
        from .adaptive_validation import validate_mesh
        validate_mesh({'verts':verts,'faces':faces,'roles':roles})
    return verts,faces,roles
