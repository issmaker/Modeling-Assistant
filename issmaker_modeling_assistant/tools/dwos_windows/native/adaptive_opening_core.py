"""DWOS 3.1 pure adaptive opening topology.

This module deliberately has no bpy/mathutils dependency.  It converts an ordered
3D boundary loop into an architectural ribbon model used by PLANAR, CURVED and
CORNER windows.  Coordinates are in the caller's space (normally Host local).
"""
from __future__ import annotations

from math import acos, degrees, isfinite, sqrt

try:
    from .facade_orientation_core import solve_facade_orientation, corner_kind
except ImportError:
    from facade_orientation_core import solve_facade_orientation, corner_kind

EPS = 1.0e-9
SCHEMA = "DWOS_opening_topology_v1"


def _v(a):
    return (float(a[0]), float(a[1]), float(a[2]))


def add(a, b): return (a[0]+b[0], a[1]+b[1], a[2]+b[2])
def sub(a, b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def mul(a, s): return (a[0]*s, a[1]*s, a[2]*s)
def dot(a, b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def cross(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def length(a): return sqrt(max(0.0, dot(a, a)))


def norm(a):
    n = length(a)
    if n <= EPS:
        raise ValueError("zero vector")
    return mul(a, 1.0/n)


def lerp(a, b, t): return add(a, mul(sub(b, a), t))


def distance(a, b): return length(sub(a, b))


def _clamp(x, lo=-1.0, hi=1.0): return max(lo, min(hi, x))


def angle_deg(a, b):
    try:
        return degrees(acos(_clamp(dot(norm(a), norm(b)))))
    except ValueError:
        return 180.0


def canonical_dir(a):
    a = norm(a)
    for c in a:
        if abs(c) > 1.0e-7:
            return mul(a, -1.0) if c < 0.0 else a
    return a


def _polyline_cumulative(points):
    if len(points) < 2:
        return [0.0], 0.0
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + distance(a, b))
    return cumulative, cumulative[-1]


def _sample_polyline(points, cumulative, total, t):
    if not points:
        raise ValueError("empty polyline")
    if len(points) == 1 or total <= EPS:
        return points[0]
    target = max(0.0, min(1.0, float(t))) * total
    if target <= 0.0:
        return points[0]
    if target >= total:
        return points[-1]
    for i in range(len(cumulative)-1):
        if cumulative[i] - EPS <= target <= cumulative[i+1] + EPS:
            span = cumulative[i+1]-cumulative[i]
            local = 0.0 if span <= EPS else (target-cumulative[i])/span
            return lerp(points[i], points[i+1], local)
    return points[-1]


def _cyclic_run(indices, count):
    """Order one contiguous cyclic subset, returning [] when it is fragmented."""
    if not indices:
        return []
    ids = sorted(set(int(i) % count for i in indices))
    if len(ids) == count:
        return ids
    # Rotate after the largest cyclic gap.  A true contiguous run then has unit gaps.
    gaps = []
    for j, cur in enumerate(ids):
        nxt = ids[(j+1) % len(ids)]
        gap = (nxt-cur) % count
        gaps.append((gap, j))
    _, cut = max(gaps)
    ordered = ids[cut+1:] + ids[:cut+1]
    for a, b in zip(ordered, ordered[1:]):
        if (b-a) % count != 1:
            return []
    return ordered


def _unique_sorted(values, eps=1.0e-9):
    out = []
    for x in sorted(float(v) for v in values):
        if not out or abs(x-out[-1]) > eps:
            out.append(x)
    return out


def _interp_scalar(xs, ys, x):
    if len(xs) != len(ys) or not xs:
        raise ValueError("invalid interpolation data")
    if x <= xs[0]: return float(ys[0])
    if x >= xs[-1]: return float(ys[-1])
    for i in range(len(xs)-1):
        if xs[i] <= x <= xs[i+1]:
            d = xs[i+1]-xs[i]
            t = 0.0 if abs(d) <= EPS else (x-xs[i])/d
            return float(ys[i])*(1.0-t)+float(ys[i+1])*t
    return float(ys[-1])


def _average(vectors):
    vectors = [v for v in vectors if length(v) > EPS]
    if not vectors:
        return None
    total = (0.0, 0.0, 0.0)
    for v in vectors:
        total = add(total, norm(v))
    if length(total) <= EPS:
        return None
    return norm(total)


def _orient_chain_pair(top, bottom):
    same = distance(top[0], bottom[0]) + distance(top[-1], bottom[-1])
    crossed = distance(top[0], bottom[-1]) + distance(top[-1], bottom[0])
    if crossed < same:
        bottom = list(reversed(bottom))
    c0 = mul(add(top[0], bottom[0]), 0.5)
    c1 = mul(add(top[-1], bottom[-1]), 0.5)
    chord = sub(c1, c0)
    if length(chord) <= EPS:
        raise ValueError("Opening horizontal span is zero")
    if dot(canonical_dir(chord), chord) < 0.0:
        top = list(reversed(top)); bottom = list(reversed(bottom))
    return top, bottom


def _normal_sign(normals, candidate):
    avg = _average(normals or [])
    if avg is None:
        return 1.0
    return -1.0 if dot(avg, candidate) < 0.0 else 1.0


def _source_contact_params(chain, width, sample_t_to_s):
    cumulative, total = _polyline_cumulative(chain)
    if total <= EPS:
        return []
    out = []
    for point, c in zip(chain, cumulative):
        t = c/total
        s_abs = _interp_scalar(sample_t_to_s[0], sample_t_to_s[1], t)
        out.append({"s": s_abs-width*0.5, "point": [float(x) for x in point]})
    return out




def _source_side_contacts(loop_points, top, bottom, up, width):
    """Recover Host-authored side-chain vertices as centred S/V contacts.

    Top/bottom vertices are already represented by their own contact chains. Any
    remaining boundary vertex belongs to the left or right side of the facade-like
    aperture. Keeping these coordinates prevents an adaptive rebuild from silently
    deleting Host topology subdivisions on the jambs.
    """
    def same_point(a,b):
        return distance(a,b) <= 1.0e-7
    horizontal=list(top)+list(bottom)
    left_mid=mul(add(top[0],bottom[0]),0.5)
    right_mid=mul(add(top[-1],bottom[-1]),0.5)
    left=[bottom[0],top[0]]; right=[bottom[-1],top[-1]]
    for point in loop_points:
        if any(same_point(point,q) for q in horizontal):
            continue
        # Source boundary is already known to be one simple loop; nearest jamb is
        # deterministic and avoids relying on original loop winding.
        if distance(point,left_mid) <= distance(point,right_mid):
            left.append(point)
        else:
            right.append(point)
    def records(points,scoord,mid):
        uniq=[]
        for q in points:
            if not any(same_point(q,x) for x in uniq):uniq.append(q)
        out=[]
        for q in uniq:
            out.append({'s':float(scoord),'v':float(dot(sub(q,mid),up)),
                        'point':[float(x) for x in q]})
        return sorted(out,key=lambda x:x['v'])
    return records(left,-width*.5,left_mid),records(right,width*.5,right_mid)

def build_opening_topology(
    points,
    *,
    vertex_ids=None,
    adjacent_normals=None,
    adjacent_normal_samples=None,
    up_hint=(0.0, 0.0, 1.0),
    source="AUTO_DETECTED",
    hard_corner_deg=35.0,
    planar_turn_deg=1.0,
    patch_merge_deg=2.0,
    max_height_variation_m=0.005,
    max_height_variation_ratio=0.005,
    max_side_skew_m=0.005,
    max_side_skew_ratio=0.005,
    max_miter_scale=8.0, # W58.22 lab only: allow 20-degree experimental miter under exact audits
):
    """Build a serializable adaptive opening ribbon from an ordered closed loop.

    Current production contract intentionally targets facade-like apertures: two
    approximately parallel top/bottom chains connected by two side chains.  This
    covers planar, polygonal-curved and hard-corner architectural windows without
    silently flattening them.  Unsupported spatial loops return COMPLEX/INVALID
    with explicit reasons rather than fabricated geometry.
    """
    pts = [_v(p) for p in points]
    if len(pts) > 1 and distance(pts[0], pts[-1]) <= 1.0e-8:
        pts = pts[:-1]
    n = len(pts)
    if n < 4:
        return {"schema": SCHEMA, "classification": "INVALID", "reasons": ["BOUNDARY_TOO_SMALL"], "build_supported": False}
    if any(not all(isfinite(x) for x in p) for p in pts):
        return {"schema": SCHEMA, "classification": "INVALID", "reasons": ["NONFINITE_COORDINATE"], "build_supported": False}
    if any(distance(pts[i], pts[(i+1)%n]) <= 1.0e-8 for i in range(n)):
        return {"schema": SCHEMA, "classification": "INVALID", "reasons": ["ZERO_LENGTH_BOUNDARY_EDGE"], "build_supported": False}

    try:
        up0 = norm(_v(up_hint))
    except ValueError:
        up0 = (0.0, 0.0, 1.0)
    heights = [dot(p, up0) for p in pts]
    hmin, hmax = min(heights), max(heights)
    span = hmax-hmin
    if span <= 1.0e-5:
        return {"schema": SCHEMA, "classification": "INVALID", "reasons": ["NO_VERTICAL_SPAN"], "build_supported": False}
    level_tol = max(0.001, span*2.5e-4)
    top_ids = _cyclic_run([i for i,h in enumerate(heights) if hmax-h <= level_tol], n)
    bottom_ids = _cyclic_run([i for i,h in enumerate(heights) if h-hmin <= level_tol], n)
    reasons = []
    if len(top_ids) < 2 or len(bottom_ids) < 2:
        reasons.append("TOP_BOTTOM_CHAINS_NOT_FOUND")
        return {
            "schema": SCHEMA, "classification": "COMPLEX", "reasons": reasons,
            "build_supported": False, "source": source,
            "loop_points": [list(p) for p in pts], "loop_vertex_ids": list(vertex_ids or range(n)),
        }

    top = [pts[i] for i in top_ids]
    bottom = [pts[i] for i in bottom_ids]
    try:
        top, bottom = _orient_chain_pair(top, bottom)
    except ValueError:
        return {"schema": SCHEMA, "classification": "INVALID", "reasons": ["ZERO_HORIZONTAL_SPAN"], "build_supported": False}

    side0 = sub(top[0], bottom[0]); side1 = sub(top[-1], bottom[-1])
    up = _average([side0, side1]) or up0
    if dot(up, up0) < 0.0:
        up = mul(up, -1.0)
    side0_h = dot(side0, up); side1_h = dot(side1, up)
    if min(side0_h, side1_h) <= 1.0e-5:
        reasons.append("SIDE_DIRECTION_INVALID")

    # Re-evaluate top/bottom levels in the recovered architectural up axis.
    top_heights = [dot(p, up) for p in top]
    bottom_heights = [dot(p, up) for p in bottom]
    top_level = sum(top_heights)/len(top_heights)
    bottom_level = sum(bottom_heights)/len(bottom_heights)
    height = top_level-bottom_level
    if height <= 1.0e-5:
        reasons.append("HEIGHT_INVALID")

    top_cum, top_len = _polyline_cumulative(top)
    bot_cum, bot_len = _polyline_cumulative(bottom)
    if min(top_len, bot_len) <= 1.0e-5:
        reasons.append("HORIZONTAL_CHAIN_TOO_SHORT")
    top_ts = [0.0 if top_len <= EPS else c/top_len for c in top_cum]
    bot_ts = [0.0 if bot_len <= EPS else c/bot_len for c in bot_cum]
    ts = _unique_sorted(top_ts + bot_ts)
    if len(ts) < 2:
        reasons.append("NO_SURFACE_STATIONS")

    samples = []
    heights_sample = []
    skew_sample = []
    for t in ts:
        tp = _sample_polyline(top, top_cum, top_len, t)
        bp = _sample_polyline(bottom, bot_cum, bot_len, t)
        side = sub(tp, bp)
        hv = dot(side, up)
        lateral = sub(side, mul(up, hv))
        center = mul(add(tp, bp), 0.5)
        samples.append({"t": t, "top": tp, "bottom": bp, "center": center})
        heights_sample.append(hv)
        skew_sample.append(length(lateral))
    mean_height = sum(heights_sample)/max(1,len(heights_sample))
    height_var = (max(heights_sample)-min(heights_sample)) if heights_sample else 0.0
    max_skew = max(skew_sample) if skew_sample else 0.0
    allowed_height_var = max(max_height_variation_m, abs(mean_height)*max_height_variation_ratio)
    allowed_skew = max(max_side_skew_m, abs(mean_height)*max_side_skew_ratio)
    if height_var > allowed_height_var:
        reasons.append("VARIABLE_HEIGHT_UNSUPPORTED")
    if max_skew > allowed_skew:
        reasons.append("SIDE_SKEW_UNSUPPORTED")

    centers = [s["center"] for s in samples]
    cumulative, width = _polyline_cumulative(centers)
    if width <= 1.0e-5:
        reasons.append("CENTERLINE_TOO_SHORT")
    t_to_s = ([s["t"] for s in samples], cumulative)

    segment_tangents = []
    for a,b in zip(centers, centers[1:]):
        tangent = sub(b,a)
        # Remove numerical vertical component; S is along the facade.
        tangent = sub(tangent, mul(up, dot(tangent, up)))
        if length(tangent) <= 1.0e-8:
            reasons.append("DEGENERATE_SURFACE_SEGMENT")
            tangent = (1.0,0.0,0.0)
        else:
            tangent = norm(tangent)
        segment_tangents.append(tangent)

    source_normals = [_v(v) for v in (adjacent_normals or []) if len(v) >= 3 and length(_v(v)) > EPS]
    provisional = cross(up, segment_tangents[0]) if segment_tangents else (0.0,1.0,0.0)
    # 3.2: normals are no longer reduced to one anonymous average.  Evidence is
    # attached to the boundary location and voted against the local facade
    # tangent.  A single coherent exterior sign is then propagated through all
    # patches so one bad Host face cannot flip only half of a window.
    orientation_samples=[]
    for rec in (adjacent_normal_samples or []):
        try:
            orientation_samples.append({'point':_v(rec['point']),'normal':_v(rec['normal'])})
        except Exception:
            pass
    if not orientation_samples and source_normals:
        # Backward-compatible low-confidence evidence when the caller has no
        # spatial samples (legacy/manual fixtures).
        orientation_samples=[{'point':samples[min(i,len(samples)-1)]['center'], 'normal':n}
                             for i,n in enumerate(source_normals)]
    orientation=solve_facade_orientation(centers,segment_tangents,up,orientation_samples)
    segment_normals=list(orientation['segment_normals'])
    sign=float(orientation.get('global_sign',1.0))

    turns = [angle_deg(segment_tangents[i], segment_tangents[i+1]) for i in range(max(0,len(segment_tangents)-1))]
    if any(a > 179.5 for a in turns): # W58.22 lab: defer steep reversals to real mesh/collision gates
        reasons.append("SURFACE_FOLDS_BACK")
    # Two straight facade branches meeting at a single explicit CAD vertex
    # are an architectural CORNER even when the turn is obtuse / shallow.
    # A many-segment tessellation remains CURVED unless it has a genuinely
    # sharp kink; this avoids turning approximated arcs into hard corners.
    two_branch_cad_joint=(len(segment_tangents)==2 and len(top)==3 and len(bottom)==3)
    # Semantic path is only for classification; all authored Host stations are
    # retained unchanged for geometry and contact. Short collinear sections
    # must not stop recognition of an otherwise genuine two-branch junction.
    semantic_turns=[i for i,a in enumerate(turns) if a>float(patch_merge_deg)]
    two_branch_semantic_joint=(len(semantic_turns)==1 and
        turns[semantic_turns[0]]>=12.0 and
        all(a<=float(patch_merge_deg) for i,a in enumerate(turns) if i!=semantic_turns[0]))
    corner_min_deg=min(float(hard_corner_deg),12.0) if (
        two_branch_cad_joint or two_branch_semantic_joint) else float(hard_corner_deg)
    hard_junctions = [i+1 for i,a in enumerate(turns) if a+1.0e-7 >= corner_min_deg]
    if len(hard_junctions)>1:
        # One CORNER controller currently guarantees exactly two GLASS panels
        # per layer and one integrated corner junction. Inventing a third pane
        # or collapsing two kinks into one would silently corrupt the facade.
        reasons.append('MULTI_HARD_JUNCTION_REQUIRES_MULTIPLE_WINDOWS')
    max_turn = max(turns) if turns else 0.0

    # Build station tangents, normals and proper offset/miter direction.
    stations = []
    miter_fail = False
    for i, sample in enumerate(samples):
        if not segment_tangents:
            tangent = (1.0,0.0,0.0); normal = provisional; depth_vector = normal; miter_scale = 1.0
        elif i == 0:
            tangent = segment_tangents[0]; normal = segment_normals[0]; depth_vector = normal; miter_scale = 1.0
        elif i == len(samples)-1:
            tangent = segment_tangents[-1]; normal = segment_normals[-1]; depth_vector = normal; miter_scale = 1.0
        else:
            a = segment_tangents[i-1]; b = segment_tangents[i]
            tangent = _average([a,b]) or b
            na = segment_normals[i-1]; nb = segment_normals[i]
            bis = _average([na,nb])
            if bis is None:
                bis = nb; scale = float("inf")
            else:
                den = dot(bis,na)
                scale = float("inf") if den <= 1.0e-6 else 1.0/den
            if not isfinite(scale) or scale > float(max_miter_scale):
                miter_fail = True
                scale = min(float(max_miter_scale), 4.0)
            normal = bis
            depth_vector = mul(bis, scale)
            miter_scale = scale
        stations.append({
            "index": i, "t": float(sample["t"]), "s": float(cumulative[i]),
            "center": [float(x) for x in sample["center"]],
            "tangent": [float(x) for x in tangent],
            "normal": [float(x) for x in normal],
            "depth_vector": [float(x) for x in depth_vector],
            "miter_scale": float(miter_scale),
        })
    for i,st in enumerate(stations):
        if segment_normals:
            if i<=0:en=segment_normals[0]
            elif i>=len(segment_normals):en=segment_normals[-1]
            else:en=_average([segment_normals[i-1],segment_normals[i]]) or segment_normals[i]
            st['exterior_normal']=[float(x) for x in en]
            st['interior_normal']=[float(-x) for x in en]
    if miter_fail:
        reasons.append("MITER_LIMIT_EXCEEDED")

    # Logical planar patches over centerline segments.
    patches = []
    if segment_normals:
        start = 0
        current = [segment_normals[0]]
        for i in range(1,len(segment_normals)):
            if angle_deg(segment_normals[i-1],segment_normals[i]) <= float(patch_merge_deg):
                current.append(segment_normals[i])
            else:
                pn=_average(current) or current[0]
                patches.append({"id": len(patches), "segment_start": start, "segment_end": i-1,
                                "normal": [float(x) for x in pn],
                                "exterior_normal": [float(x) for x in pn],
                                "interior_normal": [float(-x) for x in pn]})
                start=i; current=[segment_normals[i]]
        pn=_average(current) or current[0]
        patches.append({"id": len(patches), "segment_start": start, "segment_end": len(segment_normals)-1,
                        "normal": [float(x) for x in pn],
                        "exterior_normal": [float(x) for x in pn],
                        "interior_normal": [float(-x) for x in pn]})

    critical = {"TOP_BOTTOM_CHAINS_NOT_FOUND","SIDE_DIRECTION_INVALID","HEIGHT_INVALID","HORIZONTAL_CHAIN_TOO_SHORT",
                "NO_SURFACE_STATIONS","CENTERLINE_TOO_SHORT","VARIABLE_HEIGHT_UNSUPPORTED","SIDE_SKEW_UNSUPPORTED",
                "DEGENERATE_SURFACE_SEGMENT","SURFACE_FOLDS_BACK","MITER_LIMIT_EXCEEDED",
                "MULTI_HARD_JUNCTION_REQUIRES_MULTIPLE_WINDOWS"}
    if any(r in critical for r in reasons):
        classification = "COMPLEX"
        supported = False
    elif max_turn <= float(planar_turn_deg):
        classification = "PLANAR"
        supported = True
    elif hard_junctions:
        classification = "CORNER"
        supported = True
    else:
        classification = "CURVED"
        supported = True

    # A deterministic local frame at the aperture centre.  Generated adaptive Mesh
    # can stay in an ordinary Blender object's local coordinates.
    mid_s = width*0.5
    def _station_interp(field):
        if not stations:
            return (0.0,0.0,0.0)
        if mid_s <= stations[0]["s"]: return tuple(stations[0][field])
        for a,b in zip(stations,stations[1:]):
            if a["s"] <= mid_s <= b["s"]:
                d=b["s"]-a["s"]; tt=0.0 if d<=EPS else (mid_s-a["s"])/d
                return lerp(tuple(a[field]),tuple(b[field]),tt)
        return tuple(stations[-1][field])
    origin = _station_interp("center")
    axis_x = norm(_station_interp("tangent"))
    axis_y = up
    axis_z = norm(cross(axis_y,axis_x))
    # Match the recovered facade orientation field, not an unrelated average.
    if segment_normals:
        mid_seg=min(len(segment_normals)-1,max(0,len(segment_normals)//2))
        if dot(axis_z,segment_normals[mid_seg])<0:
            axis_z=mul(axis_z,-1.0)

    top_contact = _source_contact_params(top, width, t_to_s)
    bottom_contact = _source_contact_params(bottom, width, t_to_s)
    left_contact, right_contact = _source_side_contacts(pts, top, bottom, up, width)

    # Side-chain vertices come from the original Host boundary while ``mean_height``
    # is reconstructed from all S stations.  Real CAD/FBX meshes routinely differ by
    # a few microns at the semantic jamb corners.  Keeping both the measured endpoint
    # and the reconstructed +/-height/2 value creates two almost-coincident contact
    # vertices; the perimeter zipper then produces overlapping skinny triangles and a
    # non-manifold radial corner edge.  Snap ONLY semantic side endpoints to the exact
    # parametric limits.  Genuine intermediate Host jamb vertices are preserved 1:1.
    side_endpoint_snap = max(1.0e-5, abs(mean_height) * 1.0e-5)
    half_height = mean_height * 0.5
    for contact_chain in (left_contact, right_contact):
        for rec in contact_chain:
            value = float(rec.get("v", 0.0))
            if abs(value + half_height) <= side_endpoint_snap:
                rec["v"] = -half_height
            elif abs(value - half_height) <= side_endpoint_snap:
                rec["v"] = half_height

    semantic = [bottom[0], bottom[-1], top[-1], top[0]]
    corner_junctions=[]
    for station_index in hard_junctions:
        if 0 < station_index < len(stations)-1:
            st=stations[station_index]
            before=segment_tangents[station_index-1]; after=segment_tangents[station_index]
            exterior_before=segment_normals[station_index-1]
            corner_junctions.append({
                'station_index':int(station_index),
                's':float(st['s']),
                'center':[float(x) for x in st['center']],
                'angle_deg':float(turns[station_index-1]),
                'kind':corner_kind(before,after,up,exterior_before),
                'incoming_tangent':[float(x) for x in before],
                'outgoing_tangent':[float(x) for x in after],
                'incoming_exterior_normal':[float(x) for x in segment_normals[station_index-1]],
                'outgoing_exterior_normal':[float(x) for x in segment_normals[station_index]],
                'depth_vector':[float(x) for x in st['depth_vector']],
            })
    glass_policy = {"PLANAR":"PLANAR_SINGLE","CURVED":"SEGMENTED_PLANAR","CORNER":"CORNER_SEGMENTED"}.get(classification,"UNSUPPORTED")

    return {
        "schema": SCHEMA,
        "classification": classification,
        "build_supported": bool(supported),
        "source": str(source),
        "reasons": list(dict.fromkeys(reasons)),
        "loop_points": [[float(x) for x in p] for p in pts],
        "loop_vertex_ids": list(vertex_ids or range(n)),
        "semantic_corners": [[float(x) for x in p] for p in semantic],
        "top_chain": [[float(x) for x in p] for p in top],
        "bottom_chain": [[float(x) for x in p] for p in bottom],
        "top_contact": top_contact,
        "bottom_contact": bottom_contact,
        "left_contact": left_contact,
        "right_contact": right_contact,
        "width": float(width),
        "height": float(mean_height),
        "height_variation": float(height_var),
        "side_skew_max": float(max_skew),
        "up_axis": [float(x) for x in up],
        "origin": [float(x) for x in origin],
        "axis_x": [float(x) for x in axis_x],
        "axis_y": [float(x) for x in axis_y],
        "axis_z": [float(x) for x in axis_z],
        "stations": stations,
        "segment_exterior_normals": [[float(x) for x in n] for n in segment_normals],
        "segment_interior_normals": [[float(-x) for x in n] for n in segment_normals],
        "patches": patches,
        "hard_junction_station_indices": hard_junctions,
        "corner_junctions": corner_junctions,
        "orientation_confidence": orientation.get('confidence','LOW'),
        "orientation_confidence_value": float(orientation.get('confidence_value',0.0)),
        "orientation_sample_count": int(orientation.get('sample_count',0)),
        "orientation_global_sign": float(sign),
        "max_turn_deg": float(max_turn),
        "hard_corner_threshold_deg": float(corner_min_deg),
        "architectural_two_branch_joint": bool(two_branch_cad_joint),
        "planar_turn_threshold_deg": float(planar_turn_deg),
        "glass_policy": glass_policy,
        "coordinate_system": "SVD_RIBBON",
    }


def _frame_basis(topology):
    return (tuple(topology["origin"]), tuple(topology["axis_x"]),
            tuple(topology["axis_y"]), tuple(topology["axis_z"]))


def host_to_window_local(topology, point):
    origin, x, y, z = _frame_basis(topology)
    d = sub(_v(point), origin)
    return (dot(d,x), dot(d,y), dot(d,z))


def window_to_host_local(topology, point):
    origin, x, y, z = _frame_basis(topology)
    p = _v(point)
    return add(origin, add(mul(x,p[0]), add(mul(y,p[1]), mul(z,p[2]))))


def map_svd_host(topology, s, v, d=0.0):
    """Map centred S/V/D coordinates into the source Host-local coordinate system."""
    stations = topology.get("stations") or []
    width = float(topology.get("width",0.0))
    if not stations or width <= EPS:
        raise ValueError("Adaptive topology has no stations")
    u = max(0.0, min(width, float(s)+width*0.5))
    a=stations[0]; b=stations[-1]; t=0.0
    if u <= a["s"]:
        b=a
    elif u >= b["s"]:
        a=b
    else:
        for aa,bb in zip(stations,stations[1:]):
            if aa["s"]-EPS <= u <= bb["s"]+EPS:
                a,b=aa,bb
                span=b["s"]-a["s"]
                t=0.0 if span<=EPS else (u-a["s"])/span
                break
    center=lerp(tuple(a["center"]),tuple(b["center"]),t)
    up=norm(tuple(topology["up_axis"]))
    depth=lerp(tuple(a["depth_vector"]),tuple(b["depth_vector"]),t)
    return add(center, add(mul(up,float(v)), mul(depth,float(d))))


def map_svd_local(topology, s, v, d=0.0):
    return host_to_window_local(topology, map_svd_host(topology,s,v,d))


def contact_error(topology):
    """Return max source-boundary mismatch of the ribbon map at stored top/bottom points."""
    h=float(topology.get("height",0.0)); worst=0.0
    for key, vv in (("top_contact",h*0.5),("bottom_contact",-h*0.5)):
        for item in topology.get(key,[]):
            predicted=map_svd_host(topology,float(item["s"]),vv,0.0)
            worst=max(worst,distance(predicted,tuple(item["point"])))
    return worst
