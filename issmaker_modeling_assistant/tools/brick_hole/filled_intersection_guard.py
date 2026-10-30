"""Brick Hole: fail-closed spatial self-intersection audit for FILLED extrusions.

Runs after Blender's native region extrusion and UV splitting, but before the
protected transaction commits. Authorised boundary contacts remain legal.
"""
import math
from .swept_collision_guard import _proven_tri_intersection, _on_segment


def _inside_triangle(point, tri, tolerance):
    a,b,c=tri
    u=tuple(b[i]-a[i] for i in range(3))
    v=tuple(c[i]-a[i] for i in range(3))
    w=tuple(point[i]-a[i] for i in range(3))
    uu=sum(x*x for x in u); vv=sum(x*x for x in v)
    uv=sum(u[i]*v[i] for i in range(3))
    wu=sum(w[i]*u[i] for i in range(3))
    wv=sum(w[i]*v[i] for i in range(3))
    d=uu*vv-uv*uv
    if d<=1e-20:
        return False
    s=(vv*wu-uv*wv)/d
    t=(uu*wv-uv*wu)/d
    eps=max(1e-7,min(1e-3,2*tolerance/max(math.sqrt(uu),math.sqrt(vv),1e-6)))
    if min(s,t,1-s-t)<=eps:
        return False
    n=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
    norm=math.sqrt(sum(x*x for x in n))
    return norm>1e-16 and abs(sum(n[i]*w[i] for i in range(3)))<=2*tolerance*norm


def _penetration(first,second,hits,area_overlap,tolerance):
    if area_overlap:
        return True
    if not hits:
        return False
    probes=list(hits)
    for i in range(len(hits)):
        for j in range(i+1,len(hits)):
            if sum((hits[i][k]-hits[j][k])**2 for k in range(3))>(2*tolerance)**2:
                probes.append(tuple((hits[i][k]+hits[j][k])*0.5 for k in range(3)))
    return any(_inside_triangle(p,first,tolerance) or
               _inside_triangle(p,second,tolerance) for p in probes)


def validate_filled_result_intersections(bm,result_layer,min_result_id,
                                         cap_faces=(),statistics=None,
                                         max_candidate_pairs=50000,tolerance=1e-7):
    """Check every new FILLED side or cap against all surface triangles.

    Pairs with only a boundary-to-boundary contact are legal; penetrations and
    overlapping face interiors fail. Exhausted candidate budget fails closed.
    """
    if max_candidate_pairs<1:
        raise ValueError('RESOURCE_LIMIT: неверный лимит проверки FILLED')
    caps={face for face in cap_faces if getattr(face,'is_valid',False)}
    triangles=[]
    for loops in bm.calc_loop_triangles():
        face=loops[0].face
        xyz=tuple(tuple(float(x) for x in loop.vert.co) for loop in loops)
        uv=tuple(xyz[1][i]-xyz[0][i] for i in range(3))
        vw=tuple(xyz[2][i]-xyz[0][i] for i in range(3))
        normal=(uv[1]*vw[2]-uv[2]*vw[1],
                uv[2]*vw[0]-uv[0]*vw[2],uv[0]*vw[1]-uv[1]*vw[0])
        if sum(x*x for x in normal)<1e-20:
            raise ValueError('COLLISION: вырожденный треугольник FILLED')
        bounds=tuple((min(v[i] for v in xyz),max(v[i] for v in xyz))
                     for i in range(3))
        changed=(face in caps or
                 (result_layer is not None and
                  face[result_layer]>0 and face[result_layer]>=min_result_id))
        triangles.append((face,xyz,bounds,changed,tuple(loop.vert for loop in loops)))
    triangles.sort(key=lambda x:x[2][0][0])
    stats={'state':'VALID','triangles':len(triangles),
           'new_triangles':sum(1 for t in triangles if t[3]),
           'candidate_pairs':0,'boundary_contacts':0}
    active=[]
    try:
        for current in triangles:
            active=[other for other in active
                    if other[2][0][1]+tolerance>=current[2][0][0]]
            for other in active:
                if current[0] is other[0] or not(current[3] or other[3]):
                    continue
                if any(current[2][k][0]>other[2][k][1]+tolerance or
                       other[2][k][0]>current[2][k][1]+tolerance
                       for k in (1,2)):
                    continue
                if stats['candidate_pairs']>=max_candidate_pairs:
                    stats['state']='UNCERTAIN'
                    raise ValueError('RESOURCE_LIMIT: проверка пересечений FILLED неполная')
                stats['candidate_pairs']+=1
                area,hits,proof=_proven_tri_intersection(current[1],other[1],tolerance)
                if not area and not hits:
                    continue
                # Adjacent triangulation pieces may share the SAME BMesh edge.
                # Numerical hit points can look interior under barycentric
                # floating error; a topologically shared segment is legal.
                shared=set(current[4]).intersection(other[4])
                if not area and len(shared)>=2 and hits:
                    a,b=tuple(shared)[:2]
                    if all(_on_segment(p,tuple(a.co),tuple(b.co),tolerance)
                           for p in hits):
                        stats['boundary_contacts']+=1
                        continue
                if _penetration(current[1],other[1],hits,area,tolerance):
                    stats['state']='PROVEN_INVALID'
                    raise ValueError(
                        'COLLISION: пересечение поверхностей FILLED '
                        f'(грани {current[0].index} и {other[0].index})')
                stats['boundary_contacts']+=1
            active.append(current)
    finally:
        if statistics is not None:
            statistics.update(stats)
    return stats['candidate_pairs']
