"""W58.33 local HP repair for float32 micro-edge folded UV faces.

Only weld an adjacent edge whose true 3D length is <=10 micrometres, and only
when the generation-time UV oracle specifically identifies FOLDED_FACE.
No source LP, host or source UV is modified. This is a local topology seam.
"""
from __future__ import annotations
import math
from collections import Counter

def _vsub(a,b):return tuple(float(a[i])-float(b[i]) for i in range(3))
def _length(v):return math.sqrt(sum(x*x for x in v))
def _area2(points):
    if len(points)<3:return 0.
    origin=points[0]
    n=[0.,0.,0.]
    for a,b in zip(points,points[1:]+points[:1]):
        x=_vsub(a,origin);y=_vsub(b,origin)
        n[0]+=x[1]*y[2]-x[2]*y[1]
        n[1]+=x[2]*y[0]-x[0]*y[2]
        n[2]+=x[0]*y[1]-x[1]*y[0]
    return _length(n)

def _clean_cycle(ids):
    result=[]
    for idx in ids:
        if not result or result[-1]!=idx:result.append(idx)
    if len(result)>1 and result[0]==result[-1]:result.pop()
    # A non-adjacent duplicate is not a valid face after local collapse.
    if len(set(result))!=len(result):return None
    return result

def repair_folded_micro_edges(vertices,faces,roles,*,face_index,max_edge_m=1.0e-5,
                              max_repairs=8,min_face_area2=2.0e-12):
    """Weld a proven microscopic boundary edge and revalidate all staged faces."""
    verts=[tuple(float(c) for c in p) for p in vertices]
    polys=[list(map(int,f)) for f in faces]
    face_roles=list(roles or ['FRAME']*len(polys))
    if len(polys)!=len(face_roles):raise ValueError('W5833_HP_REPAIR_ROLE_COUNT')
    if not 0<=int(face_index)<len(polys):raise ValueError('W5833_HP_REPAIR_FACE_INDEX')
    initial=int(face_index);events=[]
    for _ in range(max_repairs):
        if initial>=len(polys):raise ValueError('W5833_HP_REPAIR_FACE_REMOVED')
        face=polys[initial]
        candidates=[]
        for j in range(len(face)):
            a,b=face[j],face[(j+1)%len(face)]
            dist=_length(_vsub(verts[a],verts[b]))
            if dist<=max_edge_m:candidates.append((dist,min(a,b),max(a,b),j))
        if not candidates:
            raise ValueError('W5833_HP_REPAIR_NO_PROVEN_MICRO_EDGE')
        dist,a,b,edge_index=min(candidates)
        # Keep the older/lower index for stable correspondence.
        keep,drop=(a,b) if a<b else (b,a)
        old_faces=len(polys)
        rebuilt=[];rebuilt_roles=[];new_face_index=None
        for fi,(source,role) in enumerate(zip(polys,face_roles)):
            mapped=[keep if v==drop else v for v in source]
            cleaned=_clean_cycle(mapped)
            if cleaned is None:
                raise ValueError(f'W5833_HP_REPAIR_NONADJACENT_DUPLICATE: face={fi}')
            if len(cleaned)<3:
                if fi==initial:
                    raise ValueError('W5833_HP_REPAIR_TARGET_FACE_COLLAPSED')
                # Remove only an actually degenerate face caused by the same
                # welded micro-edge; its geometric area must also be negligible.
                old_pts=[verts[v] for v in source]
                if _area2(old_pts)>min_face_area2*10:
                    raise ValueError(f'W5833_HP_REPAIR_WOULD_DELETE_REAL_FACE:{fi}')
                events.append({'kind':'removed_micro_face','old_face':fi,'role':role})
                continue
            if fi==initial:new_face_index=len(rebuilt)
            rebuilt.append(cleaned);rebuilt_roles.append(role)
        polys,face_roles=rebuilt,rebuilt_roles
        initial=new_face_index if new_face_index is not None else initial
        # Compact unused vertices only after all face references were remapped.
        used=sorted({v for f in polys for v in f})
        remap={old:new for new,old in enumerate(used)}
        verts=[verts[i] for i in used]
        polys=[[remap[i] for i in f] for f in polys]
        initial=next((i for i,f in enumerate(polys) if len(f)>=3 and
                      face_roles[i]==roles[face_index] and
                      len(f)==len(set(f))),initial)
        # Every retained face must remain geometrically meaningful.
        for fi,f in enumerate(polys):
            if len(f)<3 or _area2([verts[i] for i in f])<min_face_area2:
                raise ValueError(f'W5833_HP_REPAIR_DEGENERATE_FACE_REMAINED:{fi}')
        edge_uses=Counter(tuple(sorted((a,b))) for f in polys
                          for a,b in zip(f,f[1:]+f[:1]))
        if any(n>2 for n in edge_uses.values()):
            raise ValueError('W5833_HP_REPAIR_NONMANIFOLD_EDGE_CREATED')
        _ka=verts[a] if a<len(verts) else ()
        _rb=verts[b] if b<len(verts) else ()
        events.append({'kind':'welded_micro_edge','distance_m':dist,
                       'kept_old_vertex':a,'removed_old_vertex':b,
                       'kept_xyz':list(_ka),'removed_xyz':list(_rb),
                       'midpoint_xyz':[(float(_ka[k])+float(_rb[k]))*.5 for k in range(3)] if _ka and _rb else [],
                       'old_face_index':face_index,'target_role':roles[face_index],
                       'remaining_faces':len(polys)})
        return {'verts':verts,'faces':polys,'roles':face_roles,
                'repairs':events,'max_weld_distance_m':max(e.get('distance_m',0.) for e in events)}
    raise ValueError('W5833_HP_REPAIR_LIMIT')

