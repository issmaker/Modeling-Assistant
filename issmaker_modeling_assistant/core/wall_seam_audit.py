"""Read-only seam analyzer for UV Wall meshes (no welding by threshold)."""
from math import dist, isfinite


def suspect_open_seam_pairs(vertices, edges, face_edge_usage, *, min_gap=0.0025,
                            max_gap=0.025, max_report=48):
    """Find parallel/nearby separate boundary edges, for diagnostics ONLY.

    A 2 cm gap can also be intentional geometry; positive results are suspects,
    not permission to move vertices. All coordinates are world XYZ.
    """
    boundary=[]
    for e in edges:
        a,b=tuple(e)
        key=tuple(sorted((a,b)))
        if face_edge_usage.get(key,0)!=1:
            continue
        pa,pb=vertices[a],vertices[b]
        if not all(isfinite(float(c)) for p in (pa,pb) for c in p):
            continue
        if dist(pa,pb)<min_gap:
            continue
        boundary.append((key,pa,pb))
    result=[]
    for i,(e,a,b) in enumerate(boundary):
        for f,c,d in boundary[i+1:]:
            if set(e)&set(f):continue
            direct=max(dist(a,c),dist(b,d))
            reverse=max(dist(a,d),dist(b,c))
            mismatch=min(direct,reverse)
            if min_gap<=mismatch<=max_gap and abs(dist(a,b)-dist(c,d))<=max_gap:
                result.append({'first_edge':e,'second_edge':f,
                               'max_endpoint_gap_m':round(mismatch,8)})
                if len(result)>=max_report:return result
    return result
