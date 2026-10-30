"""Pure deterministic mullion constraints for DWOS 3.0.1.

A logical mullion is one zone-tree split ID even when it intersects several
other splitters. All target positions are staged together before one validation.
No Blender objects or BMesh data are referenced by this module.
"""
from __future__ import annotations
import copy
import json
import math
from .zone_core import decode, layout, find, EPS


def parse_ids(raw):
    try:
        value = json.loads(raw or '[]') if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(v) for v in value if isinstance(v, str) and v))


def encode_ids(ids):
    return json.dumps(list(dict.fromkeys(ids)), ensure_ascii=False, separators=(',', ':'))


def clean_ids(ids, available):
    present = set(available)
    return [item for item in parse_ids(ids) if item in present]


def select_ids(ids, ident, *, extend=False):
    old = parse_ids(ids)
    if extend:
        return [x for x in old if x != ident] if ident in old else old + [ident]
    return [ident]


def entries(tree, width, height, frame_width):
    return [{'id': sid, 'axis': axis, 'position': pos, 'thickness': thickness,
             'rect': rect} for sid, axis, pos, thickness, rect, _band
            in layout(decode(tree), width, height, frame_width)[1]]


def _selected(rows, ids):
    ids = parse_ids(ids)
    lookup = {r['id']: r for r in rows}
    if not ids or any(i not in lookup for i in ids):
        raise ValueError('Выберите существующие импосты во Viewport (Shift+ЛКМ)')
    selected = [lookup[i] for i in ids]
    if len({r['axis'] for r in selected}) > 1:
        raise ValueError('Групповая операция требует импосты одной ориентации')
    return selected


def targets(rows, ids, operation, *, value=0.0):
    """Return target centers for a selected set (same axis).

    ALIGN is only permitted for independent parallel bands whose spans
    do not overlap. Different physical profiles may never collapse together.
    """
    selected = _selected(rows, ids)
    pos = [float(r['position']) for r in selected]
    if not math.isfinite(value):
        raise ValueError('Значение должно быть конечным числом')
    if operation == 'MOVE':
        new = [x+value for x in pos]
    elif operation == 'DISTRIBUTE':
        if len(pos)<3:
            raise ValueError('Для распределения нужно минимум три импоста')
        sorted_rows=sorted(selected, key=lambda r:(r['position'],r['id']))
        lo=float(sorted_rows[0]['position']);hi=float(sorted_rows[-1]['position'])
        return {r['id']:lo+(hi-lo)*i/(len(sorted_rows)-1) for i,r in enumerate(sorted_rows)}
    elif operation == 'STEP':
        if len(selected)<2:
            raise ValueError('Для шага выберите минимум два параллельных импоста')
        if value<=0:
            raise ValueError('Шаг должен быть больше нуля')
        sorted_rows=sorted(selected, key=lambda r:(r['position'],r['id']))
        first=float(sorted_rows[0]['position'])
        return {r['id']: first+i*value for i,r in enumerate(sorted_rows)}
    elif operation == 'CENTER':
        bounds = selected[0]['rect']
        axis = selected[0]['axis']
        lo,hi=(bounds[0],bounds[1]) if axis=='V' else (bounds[2],bounds[3])
        mid=(min(pos)+max(pos))/2
        new=[x+(lo+hi)/2-mid for x in pos]
    elif operation == 'ALIGN':
        if len(selected)<2:raise ValueError('Выберите минимум два импоста одного направления')
        axis=selected[0]['axis']
        branch_ids=[str(r.get('branch_id','')) for r in selected]
        cross_branch=all(branch_ids) and len(set(branch_ids))>1
        if not cross_branch:
            # Preserve the original safety rule inside one editing region: two
            # full profiles occupying the same transverse span may not collapse
            # onto each other. Different CORNER branches are independent solids
            # and are intentionally allowed to share one H level.
            spans=sorted((r['rect'][2],r['rect'][3]) if axis=='V' else
                         (r['rect'][0],r['rect'][1]) for r in selected)
            if any(b[0]<a[1]-1e-7 for a,b in zip(spans,spans[1:])):
                raise ValueError('Эти импосты занимают одну область: используйте распределение или шаг')
        new=[pos[-1]]*len(pos)
    else:
        raise ValueError('Неизвестная групповая операция')
    return {r['id']:x for r,x in zip(selected,new)}


def apply_zone_targets(tree, width, height, fw, positions):
    """Change all requested positions in one parent-first traversal.

    Children are measured from their NEW parent cell, not old zone bounds.
    No tree mutation escapes unless the final layout validates.
    """
    staged = copy.deepcopy(decode(tree))
    original = {r['id']: r for r in entries(staged, width, height, fw)}
    if any(sid not in original for sid in positions):
        raise ValueError('Выбранный Split более не существует')
    frame_rect = (-width/2+fw, width/2-fw, -height/2+fw, height/2-fw)

    def visit(node, rect):
        axis=node.get('axis')
        if axis is None:return
        lo,hi=(rect[0],rect[1]) if axis=='V' else (rect[2],rect[3])
        center=float(positions.get(node['id'],original[node['id']]['position']))
        if not math.isfinite(center):raise ValueError('Недопустимая координата Split')
        node['ratio']=(center-lo)/(hi-lo)
        t=float(node['thickness']);low=center-t/2;high=center+t/2
        if axis=='V':
            left=(rect[0],low,rect[2],rect[3]);right=(high,rect[1],rect[2],rect[3])
        else:
            left=(rect[0],rect[1],rect[2],low);right=(rect[0],rect[1],high,rect[3])
        children=node['children']
        visit(children[0],left);visit(children[1],right)

    visit(staged,frame_rect)
    layout(staged,width,height,fw)
    return staged


def legacy_targets(rows, ids, operation, value, *, width, height, frame_width):
    """Legacy global split geometry in [0,extent] coordinates."""
    selected=_selected(rows,ids)
    rect={'V':(frame_width,width-frame_width), 'H':(frame_width,height-frame_width)}
    norm=[dict(r,position=r['position']- (width/2 if r['axis']=='V' else height/2),
               rect=(-width/2+frame_width,width/2-frame_width,
                     -height/2+frame_width,height/2-frame_width)) for r in rows]
    wanted=targets(norm,ids,operation,value=value)
    target={r['id']:float(wanted.get(r['id'],r['position']- (width/2 if r['axis']=='V' else height/2)))+
            (width/2 if r['axis']=='V' else height/2) for r in rows}
    for r in rows:
        coord=target[r['id']]; lo,hi=rect[r['axis']]
        if coord-r['thickness']/2<=lo+EPS or coord+r['thickness']/2>=hi-EPS:
            raise ValueError('Импост вышел за полезный контур рамы')
    for axis in ('V','H'):
        same=sorted((target[r['id']],r['thickness']) for r in rows if r['axis']==axis)
        for (a,at),(b,bt) in zip(same,same[1:]):
            if b-a <= (at+bt)/2 + EPS:
                raise ValueError('Импосты пересекаются либо касаются друг друга')
    return {ident:target[ident] for ident in wanted}


def decode_groups(raw):
    try:
        d=json.loads(raw or '{}') if isinstance(raw,str) else raw
    except (ValueError,TypeError):return {}
    if not isinstance(d,dict):return {}
    return {str(k):parse_ids(v) for k,v in d.items() if isinstance(k,str) and isinstance(v,list)}


def encode_groups(groups):
    return json.dumps(groups,ensure_ascii=False,separators=(',', ':'),sort_keys=True)
