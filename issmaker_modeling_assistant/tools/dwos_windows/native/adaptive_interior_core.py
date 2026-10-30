"""DWOS 3.3 adaptive Interior BOX with Corner Reveal Boundary Solver.

3.1.1 established topology-first winding. 3.3 separates Frame Contact
Boundary from Interior Reveal Boundary. Architectural corner semantics: when a fixed Corner Post is enabled, the Interior is not a blind
extrusion through that solid post.  S is split at the post band and each facade
side gets its own offset-plane BOX shell terminating cleanly against the post.
With Corner Post OFF the previous continuous mitered offset shell is preserved.
"""
from __future__ import annotations
try:
    from .interior_core import MAX_INTERIOR_DEPTH_M
except ImportError:  # standalone geometry QA
    from interior_core import MAX_INTERIOR_DEPTH_M

try:
    from .adaptive_opening_core import map_svd_local, cross, sub, dot, length, norm
    from .interior_boundary_core import map_reveal_local, boundary_samples
    from .corner_interior_lock_core import build_locked_corner_interior
except ImportError:
    from adaptive_opening_core import map_svd_local, cross, sub, dot, length, norm
    from interior_boundary_core import map_reveal_local, boundary_samples
    from corner_interior_lock_core import build_locked_corner_interior

EPS=1e-9


def _uniq(values,eps=1e-9):
    out=[]
    for v in sorted(float(x) for x in values):
        if not out or abs(v-out[-1])>eps:out.append(v)
    return out


def _face_normal(verts,indices):
    a,b,c=(verts[indices[i]] for i in range(3))
    n=cross(sub(b,a),sub(c,a))
    if length(n)<=1e-12:
        raise ValueError('Adaptive Interior zero-area face')
    return norm(n)


def _reverse_face(face):
    return tuple(reversed(face))


def _subtract_post_ranges(s0,s1,corner_posts):
    ranges=[(float(s0),float(s1))]
    for post in corner_posts or ():
        ps=float(post['s']);half=max(0.0,float(post['thickness'])*.5)
        c0,c1=ps-half,ps+half
        next_ranges=[]
        for a,b in ranges:
            if c1<=a+EPS or c0>=b-EPS:
                next_ranges.append((a,b));continue
            if c0>a+EPS:next_ranges.append((a,min(b,c0)))
            if c1<b-EPS:next_ranges.append((max(a,c1),b))
        ranges=next_ranges
    return [(a,b) for a,b in ranges if b-a>EPS]


def build_adaptive_interior_mesh(topology, rect, start, depth, direction, clearance=0.0, mode='BOX', *, corner_posts=(), variant='LOCKED'):
    if mode=='OFF':return [],[],[]
    if mode not in {'BOX','REVEALS'}:raise ValueError('Неизвестный режим Interior')
    if direction not in (-1,1):raise ValueError('Interior: неверное направление')
    if float(depth)<=EPS:raise ValueError('Глубина Interior должна быть больше нуля')
    if float(depth)>MAX_INTERIOR_DEPTH_M+EPS:raise ValueError('Глубина Interior не может превышать 1,5 м')
    s0,s1,v0,v1=(float(x) for x in rect)
    c=float(clearance)
    s0+=c;s1-=c;v0+=c;v1-=c
    if s1<=s0 or v1<=v0:raise ValueError('Interior: отступ закрывает проём')
    start=float(start);end=start+direction*float(depth)
    width=float(topology['width'])
    station_s=[float(r['s'])-width*0.5 for r in topology.get('stations',[])]
    is_corner=topology.get('classification')=='CORNER'
    variant=str(variant or 'LOCKED').upper()
    # 3.5.1 Geometry Lock: the user supplied the exact top-view contract. A
    # CORNER Interior is no longer a branch-offset/back-wall experiment. It is
    # a single clean quadrilateral shell whose two Interior returns meet once.
    # Corner Post geometry does not partition or reshape this shell.
    if is_corner:
        v,f,r,_meta=build_locked_corner_interior(topology,(s0,s1,v0,v1),start,0.0,mode)
        return v,f,r
    ranges=_subtract_post_ranges(s0,s1,corner_posts)
    if not ranges:
        raise ValueError('Interior: угловой импост полностью закрыл проём')

    verts=[];faces=[];roles=[];cache={}
    def orthogonal_mapper(topo,ss,vv,dd):
        p=map_svd_local(topo,ss,vv,0.0)
        return (float(p[0]),float(p[1]),float(p[2])+float(dd))
    def vtx(s,v,d):
        key=(round(s,10),round(v,10),round(d,10),variant)
        if key not in cache:
            mapper=map_svd_local
            cache[key]=len(verts);verts.append(tuple(map(float,mapper(topology,s,v,d))))
        return cache[key]
    def addface(ids,role):
        # Preserve one coherent shell orientation. Never flip a single polygon
        # from a local normal heuristic; the global direction policy is applied
        # after every disconnected corner-side shell is authored consistently.
        _face_normal(verts,ids)
        faces.append(tuple(ids));roles.append(role)

    for ri,(ra,rb) in enumerate(ranges):
        ss=(boundary_samples(topology,ra,rb) if is_corner else _uniq([ra,rb]+[x for x in station_s if ra+EPS<x<rb-EPS]))
        if len(ss)<2:continue
        # Bottom / top reveal strips for this facade-side interval.
        for a,b in zip(ss,ss[1:]):
            addface((vtx(a,v0,start),vtx(b,v0,start),vtx(b,v0,end),vtx(a,v0,end)),
                    'INTERIOR_REVEAL')
            addface((vtx(b,v1,start),vtx(a,v1,start),vtx(a,v1,end),vtx(b,v1,end)),
                    'INTERIOR_REVEAL')
        # Every disconnected range is closed at its S ends.  Internal range ends
        # created by a corner post are architectural jambs against the post, not
        # a diagonal bridge through the corner.
        lab_jamb='INTERIOR_BRANCH_JAMB' if is_corner and variant=='C' else 'INTERIOR_CORNER_JAMB'
        left_role=lab_jamb if ri>0 or ra>s0+EPS else 'INTERIOR_REVEAL'
        right_role=lab_jamb if ri<len(ranges)-1 or rb<s1-EPS else 'INTERIOR_REVEAL'
        addface((vtx(ra,v1,start),vtx(ra,v0,start),vtx(ra,v0,end),vtx(ra,v1,end)),left_role)
        addface((vtx(rb,v0,start),vtx(rb,v1,start),vtx(rb,v1,end),vtx(rb,v0,end)),right_role)
        if mode=='BOX':
            for a,b in zip(ss,ss[1:]):
                addface((vtx(a,v0,end),vtx(b,v0,end),vtx(b,v1,end),vtx(a,v1,end)),
                        'INTERIOR_BACK')

    if direction > 0:
        faces=[_reverse_face(face) for face in faces]

    # Global semantic sanity check only.  If the whole shell is reversed,
    # reverse all faces together.  This keeps every manifold shared edge coherent.
    if faces:
        n=_face_normal(verts,faces[0])
        if dot(n,(0.0,1.0,0.0)) < -1e-6:
            faces=[_reverse_face(face) for face in faces]
    return verts,faces,roles
