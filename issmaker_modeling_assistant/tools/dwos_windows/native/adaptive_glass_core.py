"""DWOS 3.1 adaptive glazing mapped through OpeningTopology S/V/D."""
from __future__ import annotations
try:
    from .adaptive_opening_core import map_svd_local, cross, sub, dot, length
except ImportError:
    from adaptive_opening_core import map_svd_local, cross, sub, dot, length

EPS=1e-9

def _uniq(values,eps=1e-9):
    out=[]
    for v in sorted(float(x) for x in values):
        if not out or abs(v-out[-1])>eps:out.append(v)
    return out


def _append_oriented(verts,faces,roles,indices,role,desired):
    a,b,c=(verts[indices[i]] for i in range(3))
    n=cross(sub(b,a),sub(c,a))
    if length(n)<=1e-12:raise ValueError('Adaptive glass zero-area face')
    if dot(n,desired)<0:indices=tuple(reversed(indices))
    faces.append(tuple(indices));roles.append(role)


def build_adaptive_glass_mesh(topology, rect, depth_center=0.0, offset=0.0, *, full=False, gap=0.075, interior_sign=-1, corner_posts=(), junction_positions=(),corner_contacts=None,branch_index_override=None):
    s0,s1,v0,v1=(float(x) for x in rect)
    if s1<=s0 or v1<=v0:return [],[],[]
    if interior_sign not in (-1,1):raise ValueError('interior_sign must be -1 or +1')
    center=float(depth_center)+float(offset)
    station_s=[float(r['s'])-float(topology['width'])*0.5 for r in topology.get('stations',[])]
    ranges=[(s0,s1)]
    for post in (corner_posts or ()):
        ps=float(post['s']);half=max(0.0,float(post.get('physical_branch_extent',float(post['thickness'])*0.5)))
        cut0,cut1=ps-half,ps+half
        next_ranges=[]
        for a,b in ranges:
            if cut1<=a+EPS or cut0>=b-EPS:
                next_ranges.append((a,b));continue
            if cut0>a+EPS:next_ranges.append((a,min(b,cut0)))
            if cut1<b-EPS:next_ranges.append((max(a,cut1),b))
        ranges=next_ranges
    # Postless CORNER: separate two façade panes at the actual corner station.
    # Do not make a gap where no divider exists.
    for ss in sorted(set(float(x) for x in junction_positions)):
        split=[]
        for a,b in ranges:
            if a+EPS < ss < b-EPS: split.extend(((a,ss),(ss,b)))
            else: split.append((a,b))
        ranges=split
    ranges=[r for r in ranges if r[1]-r[0]>EPS]
    exterior_sign=-interior_sign
    if full:
        planes=[(center-interior_sign*float(gap)*0.5,exterior_sign,'GLASS_EXTERIOR'),
                (center+interior_sign*float(gap)*0.5,interior_sign,'GLASS_INTERIOR')]
    else:
        planes=[(center,exterior_sign,'GLASS_SINGLE')]
    verts=[];faces=[];roles=[]
    physical_contacts={}
    planar_postless=(topology.get('classification')=='CORNER' and not corner_posts
                     and corner_contacts is None and (len(junction_positions)==1 or branch_index_override in (0,1)))
    if planar_postless:
        try:
            from .corner_flat_glass_core import postless_corner_point
        except ImportError:
            from corner_flat_glass_core import postless_corner_point
        # Each branch uses its own rigid depth axis. In this mode the true
        # corner miter belongs to both planes and is shared by the two panes.
        if branch_index_override is None and len(ranges)!=2:
            raise ValueError('CORNER postless glass requires two independent facade branches')
    if corner_contacts is not None and len(ranges)!=2:
        raise ValueError('CORNER physical contact requires two independent glass ranges')
    for depth,normal_sign,role in planes:
        for range_index,(ra,rb) in enumerate(ranges):
            ss=_uniq([ra,rb]+[x for x in station_s if ra+EPS<x<rb-EPS])
            row0=[];row1=[]
            for s in ss:
                if planar_postless:
                    bottom=postless_corner_point(topology,range_index if branch_index_override is None else branch_index_override,s,v0,depth)
                    top=postless_corner_point(topology,range_index if branch_index_override is None else branch_index_override,s,v1,depth)
                else:
                    bottom=map_svd_local(topology,s,v0,depth)
                    top=map_svd_local(topology,s,v1,depth)
                if corner_contacts is not None:
                    try:
                        from .corner_glass_contact_core import contact_point
                    except ImportError:
                        from corner_glass_contact_core import contact_point
                    at_post=(range_index==0 and abs(s-rb)<=1e-7) or (range_index==1 and abs(s-ra)<=1e-7)
                    if at_post:
                        side='NEG' if range_index==0 else 'POS'
                        bottom=contact_point(corner_contacts,side,bottom,depth)
                        top=contact_point(corner_contacts,side,top,depth)
                        physical_contacts[(role,side)]=(bottom,top)
                row0.append(len(verts));verts.append(bottom)
                row1.append(len(verts));verts.append(top)
            # W58.21: adjacent source stations separated by only micrometres
            # must join in a single glass miter, not a reversed micro-pane.
            from .glass_micro_station_core import join_coincident_stations
            skip_micro=join_coincident_stations(ss,row0,row1,verts,topology)
            for i in range(len(ss)-1):
                if i in skip_micro: continue
                ids=(row0[i],row0[i+1],row1[i+1],row1[i])
                mid=(ss[i]+ss[i+1])*0.5
                if planar_postless:
                    p0=postless_corner_point(topology,range_index if branch_index_override is None else branch_index_override,mid,0.0,depth)
                    p1=postless_corner_point(topology,range_index if branch_index_override is None else branch_index_override,mid,0.0,depth+1.0)
                else:
                    p0=map_svd_local(topology,mid,0.0,depth)
                    p1=map_svd_local(topology,mid,0.0,depth+1.0)
                d=sub(p1,p0)
                _append_oriented(verts,faces,roles,ids,role,tuple(normal_sign*x for x in d))
    if corner_contacts is not None and full:
        for side in ('NEG','POS'):
            for vaxis in (0,1):
                a=physical_contacts.get(('GLASS_EXTERIOR',side))
                b=physical_contacts.get(('GLASS_INTERIOR',side))
                if a is None or b is None or length(sub(a[vaxis],b[vaxis]))<=1e-5:
                    raise ValueError('CORNER glass: two physical layers collapsed at central junction')
    # Remove orphaned vertices of the now-collapsed micro-station strips.
    used=sorted(set(v for face in faces for v in face))
    if len(used)!=len(verts):
        convert={old:idx for idx,old in enumerate(used)}
        verts=[verts[i] for i in used]
        faces=[tuple(convert[v] for v in face) for face in faces]
    return [tuple(float(x) for x in p) for p in verts],faces,roles
