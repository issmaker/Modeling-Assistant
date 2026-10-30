"""RC32: avoid unselected full-height mullions in authored Interior meshes.

A user-authored Interior GROUP may occupy many free physical spans while still
being ONE semantic Interior. Those spans are separated inside one Blender child
object by the metal volume of a full-height vertical mullion. Never bridge the
solid mullion with a rear cap or side wall simply because it is not selected as
an Interior *group* boundary.

Selected V posts retain their independently authored centreline seam policy.
Unselected V posts are excluded by their complete physical width plus a small
safety clearance; no attempt is made to weld the disconnected shells or alter
frame, glass, and mullion vertices. Fail closed if there isn't enough space.
"""
from __future__ import annotations
import math
from .interior_authored_sections_core import authored_plan

POLICY='USER_GROUPS_PHYSICAL_MULLION_EXCLUSION_RC32'

def protected_interior_plan(rect, bands, mode='CUSTOM', requested=1, selected=(), joint_gap=.01,
                            metal_clearance=.0005, min_span=.003):
    base=authored_plan(rect,bands,mode,requested,selected,joint_gap,min_span)
    x0,x1,y0,y1=map(float,rect)
    clearance=float(metal_clearance)
    if not math.isfinite(clearance) or clearance<0 or clearance>.025:
        raise ValueError('Interior: защитный зазор V должен быть в диапазоне 0–0,025 м')
    active=set(base['active_ids'])
    posts=sorted(((str(sid),float(c),float(t)) for sid,c,t in bands),key=lambda v:v[1])
    left=x0
    gid=0
    sections=[];groups=[];guard=[]
    by_id={cut['id']:cut for cut in base['cuts']}
    for sid,c,t in posts:
        if sid in active:
            cut=by_id[sid]
            a=float(cut['left_end']); b=float(cut['right_start'])
            if b-a<1e-5:
                raise ValueError(f'Interior: зазор между секциями у импоста {sid} слишком мал')
            mode_name='SELECTED_CENTERLINE_SEAM'
        else:
            a=c-t/2-clearance
            b=c+t/2+clearance
            mode_name='UNSELECTED_METAL_EXCLUSION'
        # Avoid a tiny end-cap or a partially overlapping neighbouring V.
        if a-left<=min_span or b>=x1-min_span or a>b+1e-12:
            raise ValueError(f'Interior: у импоста {sid} нет места для безопасного обхода')
        sections.append((left,a));groups.append(gid)
        guard.append({'id':sid,'center':c,'thickness':t,'mode':mode_name,
                      'left_limit':a,'right_limit':b,
                      'clearance_m':clearance if sid not in active else 0.0})
        left=b
        if sid in active: gid+=1
    if x1-left<=min_span:
        raise ValueError('Interior: последний V слишком близко к боковой раме')
    sections.append((left,x1));groups.append(gid)
    if gid+1!=base['group_count']:
        raise ValueError('Interior: рассогласование числа групп и выбранных V')
    for idx,section in enumerate(sections):
        if section[1]-section[0]<=min_span:
            raise ValueError(f'Interior: вырожденная физическая секция №{idx+1}')
    return {**base,'intervals':sections,'groups':groups,
            'physical_shell_count':len(sections),'physical_protected_posts':guard,
            'protected_unselected_count':sum(g['mode']=='UNSELECTED_METAL_EXCLUSION' for g in guard),
            'policy':POLICY}
