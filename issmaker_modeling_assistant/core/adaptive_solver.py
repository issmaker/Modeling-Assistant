"""Adaptive Geometry Solver v2: bounded, deterministic candidate search.

No mutation of the source contour or Blender scene. Geometry/UV validators
are supplied by the caller and must run before accepting any candidate.
"""
from __future__ import annotations
import math
from . import adaptive_construction as ac

SCHEMA='ISSMAKER_ADAPTIVE_SOLVER_V2'


def analyze(points, vectors, depth_m):
    pts=[ac._point(p) for p in points]
    vec=[ac._point(v) for v in vectors]
    if len(pts)!=len(vec):raise ValueError('SOLVER_VECTOR_COUNT_MISMATCH')
    if not math.isfinite(float(depth_m)) or abs(float(depth_m))<=1.e-8:
        raise ValueError('SOLVER_BAD_DEPTH')
    lengths=[ac._dist(a,b) for a,b in zip(pts,pts[1:])]
    angles=[ac._turn_angle(pts[i-1],pts[i],pts[i+1])
            for i in range(1,len(pts)-1)]
    nonzero=sorted(d for d in lengths if d>1.e-8)
    median=nonzero[len(nonzero)//2] if nonzero else 0.
    tiny=[i for i,dist in enumerate(lengths) if dist<=max(1.e-5,min(.08,.12*median))]
    return {'stations':len(pts),'minimum_edge_m':min(lengths) if lengths else None,
            'median_edge_m':median,'max_turn_deg':max(angles,default=0.),
            'short_edges':tiny,'short_count':len(tiny),
            'depth_m':abs(float(depth_m)),
            'difficulty':('SHORT_EDGES' if tiny else 'ORDINARY'),
            'source_intact':True}


def _candidate_plans(points, metrics, *, max_error_m=.004, protect=(), max_trials=16):
    """Bounded beam search through actually measured redundant stations.

    Unlike fixed tolerance presets, each expansion evaluates the effect of
    removing THAT source station. The search prioritizes reduced short-edge
    risk, minimal geometric error and minimal source-normal perturbation.
    """
    pts=[ac._point(p) for p in points]
    n=len(pts)
    baseline={'schema':ac.SCHEMA,'source_count':n,
              'kept_indices':list(range(n)),'removed_indices':[],
              'max_error_m':0.,'status':'UNMODIFIED'}
    yield 'ORIGINAL',baseline
    if n<3 or n>256 or max_trials<=1 or not metrics['short_count']:return
    median=max(float(metrics['median_edge_m']),1.e-5)
    # The threshold depends on the actual mesh scale, not a hardcoded 4 cm.
    # The upper cap is a physical safety bound, not an automatic deletion.
    close_threshold=min(.12,max(.02,median*.18))
    protected={0,n-1}
    protected.update(int(i) for i in protect if 0<=int(i)<n)
    for i in range(1,n-1):
        # Structural CAD turns must never be removed for cosmetic regularity.
        if ac._turn_angle(pts[i-1],pts[i],pts[i+1])>=45.:
            protected.add(i)
    visited={tuple(range(n))}
    frontier=[tuple(range(n))]
    max_depth=min(12,n//2)
    emitted=1
    for depth in range(max_depth):
        pool=[]
        for source in frontier:
            for j in range(1,len(source)-1):
                middle=source[j]
                if middle in protected:continue
                left,right=source[j-1],source[j+1]
                tiny=min(ac._dist(pts[left],pts[middle]),
                         ac._dist(pts[middle],pts[right]))
                if tiny>=close_threshold:continue
                turn=ac._turn_angle(pts[left],pts[middle],pts[right])
                if turn>=45.:continue
                current=source[:j]+source[j+1:]
                if current in visited:continue
                visited.add(current)
                maxerr=0.
                for lo,hi in zip(current,current[1:]):
                    for index in range(lo+1,hi):
                        maxerr=max(maxerr,
                            ac._segment_deviation(pts[index],pts[lo],pts[hi]))
                        if maxerr>max_error_m+1.e-9:break
                    if maxerr>max_error_m+1.e-9:break
                if maxerr>max_error_m+1.e-9:continue
                residual=sum(ac._dist(pts[l],pts[r])<close_threshold
                             for l,r in zip(current,current[1:]))
                # Prefer effective changes first; preserve exactness second.
                score=(residual,round(maxerr,9),round(turn,4),current)
                pool.append((score,current,maxerr))
        if not pool:break
        pool.sort(key=lambda item:item[0])
        frontier=[]
        for rank,(_score,kept,error) in enumerate(pool):
            if kept in frontier:continue
            if len(frontier)<3:frontier.append(kept)
            removed=sorted(set(range(n))-set(kept))
            if not removed:continue
            plan={'schema':ac.SCHEMA,'source_count':n,
                  'kept_indices':list(kept),'removed_indices':removed,
                  'max_error_m':error,'budget_m':max_error_m,
                  'min_edge_m':close_threshold,'status':'SIMPLIFIED',
                  'candidate_count':len(pool)}
            yield f'BEAM_D{depth+1}_R{rank+1}',plan
            emitted+=1
            if emitted>=max_trials:return


def generate(points, vectors, depth_m, *, max_error_m=.004,
             max_adjust_m=.005, protect=(), max_trials=16):
    """Produce immutable candidate descriptions, never a mesh edit.

    Each generated vector covers an original source station. Differences in
    back profile are bounded by max_adjust_m independently of depth.
    """
    pts=[ac._point(p) for p in points]
    vec=[ac._point(v) for v in vectors]
    info=analyze(pts,vec,depth_m)
    for name,plan in _candidate_plans(pts,info,max_error_m=max_error_m,
                                      protect=protect,max_trials=max_trials):
        if plan['removed_indices']:
            trial=ac.interpolate_vectors(pts,vec,plan)
            trial,difference=ac.bounded_offset_vectors(
                vec,trial,depth_m,max_adjust_m=max_adjust_m)
        else:
            trial=[list(v) for v in vec];difference=0.
        yield {'name':name,'plan':plan,'vectors':trial,
               'max_offset_adjustment_m':difference}


def resolve(points, vectors, depth_m, validate, *, max_error_m=.004,
            max_adjust_m=.005, protect=(), max_trials=16):
    """Run successive candidates against a real, caller-owned validator.

    The validator MUST be nonmutating and MUST raise ValueError or
    RuntimeError on geometric collision, invalid UV or topology. The old
    untouched profile is attempted first; alternative constructions run if
    it fails. Never suppress exceptions indicating a code defect.
    """
    info=analyze(points,vectors,depth_m)
    history=[]
    for candidate in generate(points,vectors,depth_m,max_error_m=max_error_m,
                              max_adjust_m=max_adjust_m,protect=protect,
                              max_trials=max_trials):
        try:
            outcome=validate(candidate['vectors'])
        except (ValueError,RuntimeError) as exc:
            history.append({'candidate':candidate['name'],'status':'REJECTED',
                            'reason':str(exc)[:240],
                            'removed':len(candidate['plan']['removed_indices'])})
            continue
        if outcome is False:
            history.append({'candidate':candidate['name'],'status':'REJECTED',
                            'reason':'VALIDATOR_RETURNED_FALSE',
                            'removed':len(candidate['plan']['removed_indices'])})
            continue
        history.append({'candidate':candidate['name'],'status':'ACCEPTED',
                        'reason':'VALIDATED',
                        'removed':len(candidate['plan']['removed_indices'])})
        receipt={'schema':SCHEMA,'status':'SOLVED',
                 'selected_strategy':candidate['name'],
                 'attempts':len(history),'history':history,
                 'analysis':info,'source_contact_preserved':True,
                 'source_uv_preserved':True,
                 'max_contour_error_m':candidate['plan']['max_error_m'],
                 'max_offset_adjustment_m':candidate['max_offset_adjustment_m'],
                 'source_stations':len(points),
                 'virtual_stations':len(candidate['plan']['kept_indices']),
                 'virtual_ignored_indices':candidate['plan']['removed_indices']}
        return candidate['vectors'],receipt
    raise ValueError('SOLVER_EXHAUSTED: '+str(history[-5:])[:1000])
