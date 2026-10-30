"""BH-04A: read-only 3D Smart Corner intersection witness.

This is NOT a boolean mesh operation or a claim of production-ready trim.
Any required original-wall split, coplanar overlap or ambiguous contact fails
closed. Uses exactly the existing BH Collision Guard triangle intersection.
"""
import math
from .swept_collision_guard import (_strip_triangles, _tri_intersection,
    _on_segment, _norm, _sub)


def _xyz(p):
    q=tuple(float(v) for v in p)
    if len(q)!=3 or not all(math.isfinite(t) for t in q):
        raise ValueError('CORNER: неверная координата XYZ')
    return q


def _distance(a,b):
    return _norm(_sub(a,b))


def _point_line_distance(p,a,b):
    v=_sub(b,a); w=_sub(p,a)
    den=sum(t*t for t in v)
    if den<=1e-24:return _distance(p,a)
    t=sum(v[i]*w[i] for i in range(3))/den
    projected=tuple(a[i]+v[i]*t for i in range(3))
    return _distance(p,projected)


def analyze_corner_pair(a,b,*,tol=1e-7):
    """Read-only result dict. Only a single connected pair is supported.

    status SAFE_* means the NEW strips do not need trimming against each
    other, NOT that they passed existing-mesh/UV validation. CUT_CANDIDATE
    only identifies a plausible intersection line and NEVER authorizes Apply.
    """
    if not (math.isfinite(tol) and 1e-10<=tol<=1e-4):
        raise ValueError('CORNER: недопустимый геометрический допуск')
    if any(len(p.get('edge',()))!=2 or len(p.get('quad',()))!=4 for p in (a,b)):
        raise ValueError('CORNER: неверная схема рёбер/поверхностей')
    qa=tuple(_xyz(p) for p in a['quad']); qb=tuple(_xyz(p) for p in b['quad'])
    common=[i for i,v in enumerate(a['edge']) if v in b['edge']]
    base={'schema':'issmaker.bh04a.corner_witness.v1',
          'mode':'READ_ONLY_NOT_APPLY','tolerance_m':tol,
          'source_edges_xyz':[qa[:2],qb[:2]],
          'proposed_quads_xyz':[qa,qb], 'shared_topology_count':len(common)}
    def result(status,reason,**kw):return {**base,'status':status,'reason':reason,**kw}
    if len(common)!=1 or a['edge'][0]==a['edge'][1] or b['edge'][0]==b['edge'][1]:
        return result('BLOCK_UNRELATED','Требуются ровно два ребра с одной общей BMesh-вершиной')
    ai=common[0]; shared=a['edge'][ai]; bi=b['edge'].index(shared)
    seam_a=(qa[ai],qa[3-ai]); seam_b=(qb[bi],qb[3-bi])
    if max(_distance(seam_a[0],seam_b[0]),_distance(seam_a[1],seam_b[1]))>2*tol:
        return result('BLOCK_OFFSET_MISMATCH','Общая вершина имеет несовместимые смещения',
                      proposed_seam_xyz=[seam_a,seam_b])
    try:
        ta=_strip_triangles({'quad':qa,'edge':a['edge']},tol)
        tb=_strip_triangles({'quad':qb,'edge':b['edge']},tol)
    except ValueError as exc:
        return result('BLOCK_WARPED','Плоскость/триангуляция полосы не подтверждена: '+str(exc))
    contacts=[]; pairs=0; coplanar=False
    for x in ta:
        for y in tb:
            pairs+=1
            positive,hits=_tri_intersection(x,y,tol)
            if positive:coplanar=True
            for p in hits:
                if not any(_distance(p,q)<=4*tol for q in contacts):
                    contacts.append(tuple(p))
    data={'triangle_pairs_checked':pairs,'contact_xyz':contacts,
          'proposed_common_seam_xyz':seam_a}
    if coplanar:
        return result('BLOCK_COPLANAR_OVERLAP',
                      'Новые поверхности перекрывают площадь; простой разрез по линии недостаточен',**data)
    if not contacts:
        return result('SAFE_DISJOINT','Пересечения между двумя новыми полосами не обнаружены',**data)
    if all(_on_segment(p,*seam_a,tol) for p in contacts):
        return result('SAFE_SHARED_SEAM','Только допустимое общее ребро новых полос',**data)
    if len(contacts)<2:
        return result('BLOCK_POINT_CONTACT','Изолированное касание вне общего шва не является допустимым разрезом',**data)
    p0,p1=max(((x,y) for i,x in enumerate(contacts) for y in contacts[i+1:]),
              key=lambda xy:_distance(*xy))
    if _distance(p0,p1)<=8*tol:
        return result('BLOCK_POINT_CONTACT','Контакт слишком мал для устойчивого разреза',**data)
    if any(_point_line_distance(p,p0,p1)>8*tol for p in contacts):
        return result('BLOCK_NONLINEAR','Несколько несовместимых линий пересечения',**data)
    for p in contacts:
        for src0,src1 in ((qa[0],qa[1]),(qb[0],qb[1])):
            if _on_segment(p,src0,src1,tol) and min(_distance(p,src0),_distance(p,src1))>8*tol:
                return result('BLOCK_SOURCE_BOUNDARY',
                              'Разрез требует менять исходное ребро/авторскую поверхность',**data)
    return result('CUT_CANDIDATE_NOT_APPLIED',
                  'Кандидат линии найден; выбор удаляемых граней, сварка и UV ещё не доказаны',
                  cut_line_xyz=[p0,p1],**data)
