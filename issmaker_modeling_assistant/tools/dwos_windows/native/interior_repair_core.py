"""DWOS 3.5.5 topology-aware Adaptive Interior self-intersection repair.

Pure Python.  The repair intentionally works from Adaptive Opening semantics,
not from an already broken triangle soup:

* each S-segment owns one plan strip between the Interior contact depth and the
  requested target depth;
* overlapping strips are partitioned by a deterministic ownership separator;
* a small physical seam gap keeps repaired components from touching;
* every result is rebuilt as a clean open-shell prism whose Frame-contact side
  stays open.

The normal 3.5.4 builder remains the primary path.  This module is a fallback
used only after a strict staged self-check has found a real intersection.
"""
from __future__ import annotations

import math

try:
    from .adaptive_opening_core import map_svd_local
    from .interior_boundary_core import map_reveal_local
except ImportError:  # direct pure-test execution
    from adaptive_opening_core import map_svd_local
    from interior_boundary_core import map_reveal_local

EPS = 1.0e-9
POLICY = 'ADAPTIVE_SEGMENT_NONCONTACT_V1'
DEFAULT_SEAM_GAP_M = 0.002


def _add(a,b): return (a[0]+b[0], a[1]+b[1])
def _sub(a,b): return (a[0]-b[0], a[1]-b[1])
def _mul(a,s): return (a[0]*s, a[1]*s)
def _dot(a,b): return a[0]*b[0] + a[1]*b[1]
def _len(a): return math.hypot(a[0],a[1])

def _norm(a):
    ll=_len(a)
    if ll<=EPS: raise ValueError('INTERIOR_REPAIR: zero 2D direction')
    return (a[0]/ll,a[1]/ll)


def _xz(p): return (float(p[0]),float(p[2]))
def _xyz(p,v): return (float(p[0]),float(v),float(p[1]))


def _area(poly):
    if len(poly)<3:return 0.0
    return 0.5*sum(poly[i][0]*poly[(i+1)%len(poly)][1]-poly[(i+1)%len(poly)][0]*poly[i][1]
                   for i in range(len(poly)))


def _centroid(poly):
    if not poly:return (0.0,0.0)
    return (sum(p[0] for p in poly)/len(poly),sum(p[1] for p in poly)/len(poly))


def _dedupe(poly,tol=1.0e-8):
    out=[]
    for p in poly:
        q=(float(p[0]),float(p[1]))
        if not out or _len(_sub(q,out[-1]))>tol: out.append(q)
    if len(out)>1 and _len(_sub(out[0],out[-1]))<=tol: out.pop()
    return out


def _clip_halfplane(poly, origin, normal, threshold, keep_le, tol=1.0e-9):
    """Sutherland-Hodgman clipping against dot(P-origin,N) <=/>= threshold."""
    if not poly:return []
    def value(p): return _dot(_sub(p,origin),normal)-float(threshold)
    out=[]
    rows=list(zip(poly,poly[1:]+poly[:1]))
    for a,b in rows:
        va,vb=value(a),value(b)
        ina=(va<=tol) if keep_le else (va>=-tol)
        inb=(vb<=tol) if keep_le else (vb>=-tol)
        if ina: out.append(a)
        if ina!=inb:
            den=va-vb
            if abs(den)<=EPS: continue
            t=max(0.0,min(1.0,va/den))
            out.append((a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t))
    return _dedupe(out)


def _positive_overlap_convex(a,b,tol=1.0e-8):
    """Positive-area overlap test for convex plan polygons.

    Mere boundary contact returns False; that is important because untouched
    adjacent strips may legitimately meet at their Frame-contact boundary.
    """
    if len(a)<3 or len(b)<3:return False
    span=max(1.0,max(abs(x) for p in a+b for x in p))
    eps=max(tol,span*1.0e-10)
    for poly in (a,b):
        for p,q in zip(poly,poly[1:]+poly[:1]):
            e=_sub(q,p);ll=_len(e)
            if ll<=eps:continue
            axis=(-e[1]/ll,e[0]/ll)
            pa=[_dot(x,axis) for x in a];pb=[_dot(x,axis) for x in b]
            overlap=min(max(pa),max(pb))-max(min(pa),min(pb))
            if overlap<=eps:return False
    return True


def _distance_to_line(p,a,b):
    ab=_sub(b,a);ll=_len(ab)
    if ll<=EPS:return float('inf')
    return abs(ab[0]*(a[1]-p[1])-(a[0]-p[0])*ab[1])/ll


def _point_on_segment_projection(p,a,b,tol=1.0e-6):
    ab=_sub(b,a);den=_dot(ab,ab)
    if den<=EPS:return False
    t=_dot(_sub(p,a),ab)/den
    return -tol<=t<=1.0+tol and _distance_to_line(p,a,b)<=tol


def _station_samples(topology,s0,s1):
    """Normalize numerical duplicate *sampling stations* without editing the Host.

    Blender float32 Host coords can produce two S stations separated by just
    1-13 micrometres (observed in the 2026-10-16 CURVED field fixture).  These
    are not architectural ribbon segments: treating them as separate quadrilaterals
    creates virtually zero-area owner strips and destroys the contact boundary
    when overlap clipping runs.  Keep exact endpoints; collapse only INTERIOR
    sampling stations closer than 50 micrometres to one another.
    """
    width=float(topology.get('width',0.0))
    lo,hi=sorted((float(s0),float(s1)))
    vals=[lo,hi]
    for st in topology.get('stations') or []:
        s=float(st['s'])-width*.5
        if lo+1.0e-8<s<hi-1.0e-8: vals.append(s)
    vals=sorted(set(round(x,10) for x in vals))
    min_step=5.0e-5  # 50 µm; well below the authored 1 mm/px regime
    normalized=[vals[0]]
    for value in vals[1:-1]:
        if value-normalized[-1] >= min_step and vals[-1]-value >= min_step:
            normalized.append(value)
    normalized.append(vals[-1])
    return normalized if s0<=s1 else normalized[::-1]


def _segment_depth_axis(topology,a,b):
    mid=(float(a)+float(b))*0.5
    p=_xz(map_reveal_local(topology,mid,0.0,0.0,miter_at_hard=False))
    q=_xz(map_reveal_local(topology,mid,0.0,1.0,miter_at_hard=False))
    return _norm(_sub(q,p))


def _segment_strip(topology,a,b,start,end):
    """One semantic segment-owned strip in X/Z plan."""
    n=_segment_depth_axis(topology,a,b)
    # Author contact/end with one branch-local normal.  This deliberately avoids
    # reusing a hard-corner miter point that can make two branch prisms overlap.
    ba=_xz(map_svd_local(topology,a,0.0,0.0));bb=_xz(map_svd_local(topology,b,0.0,0.0))
    ca=_add(ba,_mul(n,float(start)));cb=_add(bb,_mul(n,float(start)))
    ea=_add(ba,_mul(n,float(end)));  eb=_add(bb,_mul(n,float(end)))
    poly=[ca,cb,eb,ea]
    if _area(poly)<0.0: poly=list(reversed(poly))
    return {
        's0':float(a),'s1':float(b),'normal':n,
        'contact_a':ca,'contact_b':cb,'end_a':ea,'end_b':eb,
        'contact_mid':_mul(_add(ca,cb),0.5),'poly':_dedupe(poly),
    }


def _smooth_curved_rear_stations(topology, segments, end):
    """Blend *Interior-only* rear normals at smooth CURVED stations.

    Frame contact edges are untouched.  This is a geometric rear-section
    refinement, not a topology edit of the source wall or opening.  Each
    modified strip is later clipped and checked by the strict spatial solver.
    Acute/profile reversals are deliberately left to the robust fallback.
    """
    if topology.get('classification') != 'CURVED':
        return 0
    count = 0
    for previous, following in zip(segments, segments[1:]):
        if abs(previous['s1'] - following['s0']) > 2.0e-5:
            continue
        a, b = previous['normal'], following['normal']
        cosine = _dot(a, b)
        if cosine <= 0.65 or cosine >= 0.999999999:
            continue
        avg = _norm(_add(a, b))
        joint_s = (previous['s1']+following['s0']) * 0.5
        base = _xz(map_svd_local(topology, joint_s, 0.0, 0.0))
        rear = _add(base, _mul(avg, float(end)))
        # Limit rounding to a small part of Interior depth.  Keep sharp
        # architectural turns as authored rather than trying to round them.
        allowed = max(0.005, min(0.08, abs(float(end)) * 0.25))
        if max(_len(_sub(rear, previous['end_b'])),
               _len(_sub(rear, following['end_a']))) > allowed:
            continue
        previous['end_b'] = rear
        following['end_a'] = rear
        for strip in (previous, following):
            poly = [strip['contact_a'], strip['contact_b'],
                    strip['end_b'], strip['end_a']]
            if _area(poly) < 0.0:
                poly.reverse()
            strip['poly'] = _dedupe(poly)
        count += 1
    return count


def _separator_for_pair(sa,sb,adjacent):
    """Return (origin,axis) with A on the <= side and B on the >= side."""
    if adjacent:
        # Equal-depth ownership at a bend: dot(P-J, nA-nB)=0.
        axis_vec=_sub(sa['normal'],sb['normal'])
        if _len(axis_vec)>1.0e-7:
            axis=_norm(axis_vec)
            # Use the midpoint of the two branch-local contact endpoints at the
            # junction.  They can differ when start depth is non-zero.
            ja=sa['contact_b'];jb=sb['contact_a'];origin=_mul(_add(ja,jb),0.5)
        else:
            axis=None
    else:
        axis=None
    if axis is None:
        # Conservative Voronoi separator for non-adjacent deep-overlap cases.
        ma,mb=sa['contact_mid'],sb['contact_mid']
        dv=_sub(mb,ma)
        if _len(dv)<=1.0e-8: raise ValueError('INTERIOR_REPAIR: coincident segment owners')
        axis=_norm(dv);origin=_mul(_add(ma,mb),0.5)

    fa=_dot(_sub(sa['contact_mid'],origin),axis)
    fb=_dot(_sub(sb['contact_mid'],origin),axis)
    if fa>fb: axis=_mul(axis,-1.0);fa,fb=-fa,-fb
    if fb-fa<=1.0e-8:
        raise ValueError('INTERIOR_REPAIR: cannot separate segment ownership')
    return origin,axis


def _repair_polygons(segments,seam_gap):
    gap=max(1.0e-5,float(seam_gap))
    overlap_pairs=0;clip_events=0;passes=0
    max_passes=max(2,min(8,len(segments)+1))
    for pass_index in range(max_passes):
        changed=False;passes=pass_index+1
        for i in range(len(segments)):
            for j in range(i+1,len(segments)):
                pa,pb=segments[i]['poly'],segments[j]['poly']
                if not _positive_overlap_convex(pa,pb):continue
                overlap_pairs+=1
                origin,axis=_separator_for_pair(segments[i],segments[j],j==i+1)
                # Signed distance uses unit axis, so gap is a real metric gap.
                half=gap*.5
                na=_clip_halfplane(pa,origin,axis,-half,True)
                nb=_clip_halfplane(pb,origin,axis,+half,False)
                if len(na)<3 or abs(_area(na))<=1.0e-10 or len(nb)<3 or abs(_area(nb))<=1.0e-10:
                    raise ValueError(f'INTERIOR_REPAIR: overlap {i}/{j} cannot be clipped without deleting a branch')
                if _area(na)<0:na=list(reversed(na))
                if _area(nb)<0:nb=list(reversed(nb))
                segments[i]['poly']=na;segments[j]['poly']=nb
                clip_events+=1;changed=True
        if not changed:break
    unresolved=[]
    for i in range(len(segments)):
        for j in range(i+1,len(segments)):
            if _positive_overlap_convex(segments[i]['poly'],segments[j]['poly']):unresolved.append((i,j))
    if unresolved:
        raise ValueError(f'INTERIOR_REPAIR: unresolved overlap pairs {unresolved[:4]}')
    return {'overlap_pairs':int(overlap_pairs),'clip_events':int(clip_events),'passes':int(passes)}


def _build_shell(segments,v0,v1):
    verts=[];faces=[];roles=[];component_rows=[]
    tol=2.0e-6
    def same_point(a,b): return _len(_sub(a,b))<=tol
    def shared_with_other(si,p,q):
        # Untouched adjacent strips can share a station edge.  Authoring a jamb
        # face on both components would create duplicate coplanar geometry, so
        # keep that internal seam open. Repaired conflict seams have a physical
        # gap and therefore do not match here.
        for oi,other in enumerate(segments):
            if oi==si: continue
            op=other['poly']
            for a,b in zip(op,op[1:]+op[:1]):
                if (same_point(p,a) and same_point(q,b)) or (same_point(p,b) and same_point(q,a)):
                    return True
        return False
    for si,seg in enumerate(segments):
        poly=_dedupe(seg['poly'])
        if len(poly)<3 or abs(_area(poly))<=1.0e-10:
            raise ValueError(f'INTERIOR_REPAIR: segment {si} collapsed')
        if _area(poly)<0:poly=list(reversed(poly))
        base=len(verts);n=len(poly)
        verts.extend(_xyz(p,v0) for p in poly)
        verts.extend(_xyz(p,v1) for p in poly)
        # For CCW X/Z polygon this order gives outward -Y on bottom; top is reverse.
        faces.append(tuple(base+i for i in range(n)));roles.append('INTERIOR_REVEAL_BOTTOM')
        faces.append(tuple(base+n+i for i in reversed(range(n))));roles.append('INTERIOR_REVEAL_TOP')
        contact_edges=0;side_faces=0
        for i in range(n):
            j=(i+1)%n;p,q=poly[i],poly[j]
            on_contact=(_point_on_segment_projection(p,seg['contact_a'],seg['contact_b'],tol)
                        and _point_on_segment_projection(q,seg['contact_a'],seg['contact_b'],tol))
            if on_contact:
                contact_edges+=1
                continue
            if shared_with_other(si,p,q):
                # Continuous, non-overlapping segmentation: no duplicate internal wall.
                continue
            # Side uses the opposite bottom-edge direction to keep manifold winding.
            faces.append((base+j,base+i,base+n+i,base+n+j))
            mid=_mul(_add(p,q),0.5)
            far=(_distance_to_line(mid,seg['end_a'],seg['end_b'])<=tol)
            roles.append('INTERIOR_BACK' if far else 'INTERIOR_REPAIR_JAMB')
            side_faces+=1
        if contact_edges<1:
            raise ValueError(f'INTERIOR_REPAIR: segment {si} lost Frame-contact boundary')
        component_rows.append({'segment':si,'plan_vertices':n,'contact_edges':contact_edges,'side_faces':side_faces})
    return verts,faces,roles,component_rows



def _build_continuous_curved_shell(topology, samples, start, end, v0, v1):
    """One CURVED Interior shell, no artificial per-station jambs.

    The Frame-contact curve is sampled from SVD geometry; only the rear contour
    uses blended normals. Intermediate stations define topology (quads), NOT
    physical separations. V mullions are handled by the caller's explicit
    partition intervals, not by the number of host edges.

    Fail closed on degenerate geometry. Spatial self-intersections are checked
    by the existing staged validator before any Mesh is published.
    """
    strips = [_segment_strip(topology, a, b, start, end)
              for a, b in zip(samples, samples[1:]) if abs(b-a) > EPS]
    if not strips or len(strips) != len(samples)-1:
        raise ValueError('INTERIOR_CONTINUOUS: invalid curved sampling')
    front, rear = [], []
    smooth_joints = 0
    for i, station in enumerate(samples):
        if i == 0:
            axis = strips[0]['normal']
        elif i == len(samples)-1:
            axis = strips[-1]['normal']
        else:
            na, nb = strips[i-1]['normal'], strips[i]['normal']
            if _dot(na, nb) < 0.0:
                raise ValueError('INTERIOR_CONTINUOUS: sharp direction reversal')
            axis = _norm(_add(na, nb))
            if _dot(na, nb) < 0.999999999:
                smooth_joints += 1
        # Use the actual SVD mapping for Frame contact (including its
        # interpolation of station depth axes); unlike segment-local offsets,
        # this preserves exactly the authored continuous boundary.
        front.append(_xz(map_svd_local(topology, station, 0.0, float(start))))
        rear.append(_xz(map_svd_local(topology, station, 0.0, float(end))))
    verts, faces, roles = [], [], []
    # Four vertices per sampling station: contact lower, rear lower,
    # contact upper, rear upper. The front/Frame-contact edge remains open.
    for ca, rb in zip(front, rear):
        verts.extend((_xyz(ca, v0), _xyz(rb, v0),
                      _xyz(ca, v1), _xyz(rb, v1)))
    for i in range(len(samples)-1):
        j = i+1
        faces.extend(((4*i, 4*j, 4*j+1, 4*i+1),
                      (4*i+2, 4*i+3, 4*j+3, 4*j+2),
                      (4*i+1, 4*j+1, 4*j+3, 4*i+3)))
        roles.extend(('INTERIOR_REVEAL_BOTTOM', 'INTERIOR_REVEAL_TOP',
                      'INTERIOR_BACK'))
    last=4*(len(samples)-1)
    faces.extend(((0,1,3,2), (last,last+2,last+3,last+1)))
    roles.extend(('INTERIOR_REPAIR_JAMB','INTERIOR_REPAIR_JAMB'))
    if any(_len(_sub(ca, rb)) <= 1.0e-8 for ca, rb in zip(front, rear)):
        raise ValueError('INTERIOR_CONTINUOUS: collapsed depth')
    # One shell, exactly two architectural end-caps, zero internal dividers.
    return verts, faces, roles, [dict(segment='whole_curved_window',
                stations=len(samples), physical_shells=1)], smooth_joints


def repair_adaptive_interior_mesh(topology,rect,start,depth,direction,clearance=0.0,mode='BOX',*,seam_gap=DEFAULT_SEAM_GAP_M,simple=False):
    """Build a non-contact fallback Interior from Adaptive Opening segment ownership.

    Returns `(verts, faces, roles, meta)`.  It is intentionally not a replacement
    for the primary Interior builder; callers should invoke it only after a real
    staged self-intersection has been detected.
    """
    if mode=='OFF':return [],[],[],{'policy':POLICY,'repaired':False}
    if mode not in {'BOX','REVEALS'}:raise ValueError('INTERIOR_REPAIR: unknown Interior mode')
    if direction not in (-1,1):raise ValueError('INTERIOR_REPAIR: invalid direction')
    depth=float(depth)
    if not math.isfinite(depth) or depth<=EPS:raise ValueError('INTERIOR_REPAIR: depth must be positive')
    s0,s1,v0,v1=(float(x) for x in rect)
    c=float(clearance);s0+=c;s1-=c;v0+=c;v1-=c
    if s1<=s0 or v1<=v0:raise ValueError('INTERIOR_REPAIR: clearance closes opening')
    start=float(start);end=start+float(direction)*depth
    samples=_station_samples(topology,s0,s1)
    if len(samples)<2:raise ValueError('INTERIOR_REPAIR: topology has no usable S segments')
    segments=[_segment_strip(topology,a,b,start,end) for a,b in zip(samples,samples[1:]) if abs(b-a)>EPS]
    if not segments:raise ValueError('INTERIOR_REPAIR: no segment strips')
    # CURVED: always author one continuous Interior per *user-selected* physical
    # interval. Segment-level clipping produced 17 tiny individual cabinets in
    # the 2026-10-16 field scene, visible as false vertical mullions.
    # The old ownership/clipper remains for non-CURVED legacy topologies only.
    smoothed_joints = 0
    raw_overlap=sum(1 for i in range(len(segments)) for j in range(i+1,len(segments))
                    if _positive_overlap_convex(segments[i]['poly'],segments[j]['poly']))
    # This is a sampling-only normalization. It does NOT weld or move Host
    # vertices, mutate topology['stations'], or change the real opening route.
    eligible_raw=sum(1 for station in (topology.get('stations') or [])
                     if min(s0,s1)+1.0e-8 < float(station['s'])-float(topology['width'])*.5
                     < max(s0,s1)-1.0e-8)
    station_merges=max(0,eligible_raw-(len(samples)-2))
    if topology.get('classification') == 'CURVED' and not simple:
        verts, faces, roles, components, smoothed_joints = _build_continuous_curved_shell(
            topology, samples, start, end, v0, v1)
        metrics = {'overlap_pairs': 0, 'clip_events': 0, 'passes': 0}
        policy = 'ADAPTIVE_CONTINUOUS_CURVED_V2'
        physical_gap = 0.0
    else:
        # W58.33: simple Interior uses branch-local depth normals on every
        # selected wall segment, with a strict 10um contact seam at only those
        # segment footprints proven to overlap. No decorative SVD miter ribbon.
        smoothed_joints = 0 if simple else _smooth_curved_rear_stations(topology, segments, end)
        metrics = _repair_polygons(segments, seam_gap)
        verts, faces, roles, components = _build_shell(segments, v0, v1)
        policy = 'W5833_SIMPLE_BRANCH_LOCAL_INTERIOR_V1' if simple else POLICY
        physical_gap = max(1.0e-5,float(seam_gap))
    meta={
        'policy':policy,'repaired':True,'clipped_overlap':bool(metrics['clip_events']),
        'segment_count':len(segments),'physical_shells':len(components),
        'raw_overlap_pairs':int(raw_overlap),'overlap_pairs_seen':metrics['overlap_pairs'],
        'clip_events':metrics['clip_events'],'passes':metrics['passes'],
        'seam_gap_m':physical_gap,'components':components,
        'sample_station_merges':int(station_merges),
        'rear_smooth_joints':int(smoothed_joints),
    }
    return verts,faces,roles,meta
