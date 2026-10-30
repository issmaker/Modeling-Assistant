"""Bounded, no-Mesh advisory for alternate seams on closed authored loops.

Not a generic fix for a UV orientation failure: changing the phase origin
cannot turn reversed UV winding into valid physical parity. Candidate scores
support later targeted seam fallback only if a genuine near-module failure
has been separately identified. NEVER rotate a valid authored Path here.
"""
from math import fmod,isfinite


def recommend_closed_seam_start(stations, module, *, clearance=0.002, max_candidates=64):
    vals=tuple(float(s) for s in stations)
    if len(vals)<4 or not all(isfinite(s) for s in vals) or any(b<=a for a,b in zip(vals,vals[1:])):
        raise ValueError('closed loop requires increasing finite distances')
    if not isfinite(module) or module<=0 or not 0<=clearance<module/4:
        raise ValueError('invalid module/clearance')
    total=vals[-1]
    vertices=vals[:-1]
    # Sample at most 64 starts, O(n * min(n,64)), no quadratic all-pairs.
    n=len(vertices)
    count=min(n,max(1,int(max_candidates)))
    candidates=sorted({0, *(min(n-1, (j*n)//count) for j in range(count))})
    def score(k):
        origin=vertices[k]
        near=0
        min_gap=float('inf')
        for j,s in enumerate(vertices):
            if j==k:continue
            d=(s-origin)%total
            phase=d%module
            gap=min(phase,module-phase)
            if gap>1e-9:
                min_gap=min(min_gap,gap)
                if gap<clearance: near+=1
        return near, min_gap
    original=score(0)
    best_index=0;best=original
    for k in candidates[1:]:
        current=score(k)
        if current[0]<best[0] or (current[0]==best[0] and current[1]>best[1]):
            best=current;best_index=k
    return {'index':best_index,'original_near':original[0],
            'candidate_near':best[0], 'original_clearance_m':original[1],
            'candidate_clearance_m':best[1], 'candidates_checked':len(candidates),
            'applied':False}
