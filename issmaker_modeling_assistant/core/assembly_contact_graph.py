"""Assembly Contact Graph / v1: explicit, testable interface contracts.

A candidate surface touching a witness guide is NOT a production Glass fit.
This module deliberately reports actual contact checks separately from facts
we cannot yet certify (glazing frame overlap/clearance, mullions, LP/Interior).
Pure Python, deterministic, no Blender or source modification.
"""
from __future__ import annotations
from collections import Counter
from math import dist,isfinite

SCHEMA='ISSMAKER_ASSEMBLY_CONTACT_GRAPH_V1'


def _point(point):
    try:out=tuple(float(x) for x in point)
    except (TypeError,ValueError):raise ValueError('ACG_INVALID_POINT') from None
    if len(out)!=3 or any(not isfinite(x) for x in out):
        raise ValueError('ACG_INVALID_POINT')
    return out


def _link(a,b,relation,state,measured,limit,reason):
    return {'source':a,'target':b,'relation':relation,'state':state,
            'measured_error_m':measured,'tolerance_m':limit,'reason':reason}


def evaluate(source_points,volume_vertices,volume_faces,volume_roles,
             source_vertex_count,inner_guide_indices,guide_points,
             *,seat_fraction=0.5,contact_tolerance_m=1e-5,
             seat_tolerance_m=1e-5):
    """Verify exact source rim and a *virtual* glazing seating guide.

    volume_vertices has N front positions and N back positions, where the
    initial source_vertex_count points are the SOURCE author's outer contour.
    guide_points are on the inner reveal between front[i] and back[i]. They
    DO NOT stand for actual installed production glazing.
    """
    source=[_point(pt) for pt in source_points]
    vertices=[_point(pt) for pt in volume_vertices]
    guide=[_point(pt) for pt in guide_points]
    n=len(vertices)
    count=int(source_vertex_count)
    if n<12 or n%2 or count<3 or len(source)!=count or count>=n//2:
        raise ValueError('ACG_INVALID_COMPONENT_SIZES')
    ids=[int(i) for i in inner_guide_indices]
    if len(ids)<3 or len(ids)!=len(guide) or len(set(ids))!=len(ids):
        raise ValueError('ACG_INVALID_GUIDE_RING')
    if any(i<count or i>=n//2 for i in ids):
        raise ValueError('ACG_INVALID_GUIDE_IDS')
    if not 0.<float(seat_fraction)<1. or not all(
        isfinite(x) and x>0. for x in (contact_tolerance_m,seat_tolerance_m)):
        raise ValueError('ACG_INVALID_TOLERANCE_OR_SEAT')
    if len(volume_roles)!=len(volume_faces) or not volume_faces:
        raise ValueError('ACG_INVALID_FRAME_FACES')
    role_counts=Counter(str(x) for x in volume_roles)
    if sum(role_counts[k] for k in role_counts if k=='OUTER_WALL_CONTACT')<count:
        raise ValueError('ACG_MISSING_WALL_CONTACT_FACES')
    if not any(k=='INNER_GLAZING_REVEAL' for k in role_counts):
        raise ValueError('ACG_MISSING_GLAZING_REVEAL')
    max_host=max(dist(a,b) for a,b in zip(source,vertices[:count]))
    half=n//2
    t=float(seat_fraction)
    max_seat=0.
    for i,p in zip(ids,guide):
        expected=tuple(vertices[i][axis]*(1-t)+vertices[i+half][axis]*t
                       for axis in range(3))
        max_seat=max(max_seat,dist(expected,p))
    host_state='PASS' if max_host<=contact_tolerance_m else 'FAIL'
    seat_state='PASS' if max_seat<=seat_tolerance_m else 'FAIL'
    links=[
       _link('HOST','FRAME_CANDIDATE','EXACT_SOURCE_RIM',host_state,
             max_host,contact_tolerance_m,
             'Внешняя граница рамы должна совпадать с исходным контуром стены'),
       _link('FRAME_CANDIDATE','GLASS_SEAT_GUIDE','INNER_REVEAL_SEAT',seat_state,
             max_seat,seat_tolerance_m,
             'Контрольная линия лежит на внутренней боковой поверхности рамы'),
       _link('FRAME_CANDIDATE','PRODUCTION_GLASS','PHYSICAL_GLAZING_FIT',
             'NOT_CERTIFIED',None,None,
             'Фактические зазоры, перекрытия и посадка объекта Glass ещё не проверены'),
       _link('FRAME_CANDIDATE','PRODUCTION_INTERIOR','INTERIOR_REVEAL_CONTACT',
             'NOT_CERTIFIED',None,None,'Физическое сопряжение Interior не проверено'),
       _link('FRAME_CANDIDATE','PRODUCTION_LP','LOD_CONSISTENCY',
             'NOT_CERTIFIED',None,None,'Проверка соответствия HP/LP не выполнена'),
    ]
    return {'schema':SCHEMA,'state':'PREVIEW_VALIDATED' if
            host_state==seat_state=='PASS' else 'CONTACT_CONFLICT',
            'nodes':['HOST','FRAME_CANDIDATE','GLASS_SEAT_GUIDE',
                     'PRODUCTION_GLASS','PRODUCTION_INTERIOR','PRODUCTION_LP'],
            'links':links,'source_vertex_count':count,
            'glazing_guide_vertex_count':len(ids),
            'glazing_guide_seat_fraction':t,
            'contact_links_checked':2,'contact_links_passed':sum(
                link['state']=='PASS' for link in links),
            'production_glazing_fit_certified':False,
            'assembly_fit_certified':False,'source_geometry_modified':False}
