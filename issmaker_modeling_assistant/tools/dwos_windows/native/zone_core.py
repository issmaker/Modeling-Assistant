"""Pure Python recursive zones and finite geometry rules for DWOS 1.6.

Coordinates are local window X/Y. Each split occupies its own band; descendants
live strictly inside the clear sub-regions. No topology mutation during preview.
"""
from __future__ import annotations
import copy
import json
import math
import uuid

EPS = 1e-6
MAX_NODES = 256


def _finite(*values):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def root_node():
    return {'id': 'root', 'axis': None}


def decode(raw):
    if not raw:
        return root_node()
    data = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
    if not isinstance(data, dict) or data.get('id') != 'root':
        raise ValueError('Повреждённое дерево зон')
    return data


def encode(tree):
    return json.dumps(tree, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _walk(node, rect, leaves, splitters, min_size, seen=None, depth=0):
    if seen is None:seen=set()
    if not isinstance(node,dict) or not isinstance(node.get('id'),str) or not node.get('id'):
        raise ValueError('Zone ID должен быть непустой строкой')
    if node['id'] in seen:
        raise ValueError('Обнаружены повторяющиеся Zone ID')
    seen.add(node['id'])
    if depth>MAX_NODES or len(seen)>MAX_NODES:
        raise ValueError('Слишком много зон')
    x0,x1,y0,y1 = rect
    if not _finite(*rect) or x1-x0 <= EPS or y1-y0 <= EPS:
        raise ValueError('Нулевая или некорректная область окна')
    axis = node.get('axis')
    if axis is None:
        leaves.append((node['id'], rect))
        return
    if axis not in ('V','H'):
        raise ValueError('Некорректная ориентация Split')
    ratio = node.get('ratio')
    thickness = node.get('thickness')
    if not _finite(ratio, thickness) or thickness <= EPS or not (0 < ratio < 1):
        raise ValueError('Некорректные параметры Split')
    lo, hi = (x0,x1) if axis == 'V' else (y0,y1)
    center = lo + (hi-lo)*ratio
    low, high = center - thickness/2, center + thickness/2
    if low-lo < min_size-EPS or hi-high < min_size-EPS:
        raise ValueError('Split закрывает область или не оставляет безопасного зазора')
    band = (low,high,y0,y1) if axis == 'V' else (x0,x1,low,high)
    splitters.append((node['id'], axis, center, thickness, rect, band))
    a = (x0,low,y0,y1) if axis == 'V' else (x0,x1,y0,low)
    b = (high,x1,y0,y1) if axis == 'V' else (x0,x1,high,y1)
    children = node.get('children')
    if not isinstance(children,list) or len(children)!=2:
        raise ValueError('У Split нет двух дочерних зон')
    _walk(children[0], a, leaves, splitters, min_size, seen, depth+1)
    _walk(children[1], b, leaves, splitters, min_size, seen, depth+1)


def layout(tree, width, height, frame_width, min_size=0.001):
    if not _finite(width,height,frame_width,min_size):
        raise ValueError('NaN/Infinity в размерах окна')
    if min(width,height) <= 2*frame_width+2*min_size or frame_width <= EPS:
        raise ValueError('Основная рама закрывает область окна')
    rect=(-width/2+frame_width,width/2-frame_width,-height/2+frame_width,height/2-frame_width)
    leaves, bands = [], []
    _walk(tree,rect,leaves,bands,min_size)
    return leaves,bands


def find(node, node_id):
    if node.get('id') == node_id:
        return node
    for child in node.get('children',[]):
        match = find(child,node_id)
        if match is not None: return match
    return None


def add(tree, zone_id, axis, thickness, width, height, frame_width):
    clone=copy.deepcopy(tree)
    leaves,_=layout(clone,width,height,frame_width)
    if zone_id not in {z[0] for z in leaves}:
        raise ValueError('Выбранная Zone более не существует')
    if axis not in ('V','H') or not _finite(thickness) or thickness<=EPS:
        raise ValueError('Некорректная толщина или направление Split')
    leaf=find(clone,zone_id)
    leaf.update({'axis':axis,'ratio':0.5,'thickness':thickness,
                 'children':[{'id':uuid.uuid4().hex[:12], 'axis':None},
                             {'id':uuid.uuid4().hex[:12], 'axis':None}]})
    layout(clone,width,height,frame_width)
    return clone,zone_id


def update(tree, split_id, width, height, frame_width, *, center=None, thickness=None):
    clone=copy.deepcopy(tree)
    leaves,bands=layout(clone,width,height,frame_width)
    entry=next((b for b in bands if b[0]==split_id),None)
    if entry is None: raise ValueError('Split не найден')
    _,axis,old,thick,rect,_=entry
    node=find(clone,split_id)
    lo,hi=(rect[0],rect[1]) if axis=='V' else (rect[2],rect[3])
    if center is not None:
        if not _finite(center): raise ValueError('Неверная позиция Split')
        node['ratio']=(center-lo)/(hi-lo)
    if thickness is not None:
        if not _finite(thickness) or thickness<=EPS: raise ValueError('Нулевая толщина Split')
        node['thickness']=thickness
    layout(clone,width,height,frame_width)
    return clone


def remove(tree,split_id,width,height,frame_width):
    if split_id=='root':
        return root_node()
    clone=copy.deepcopy(tree)
    target=find(clone,split_id)
    if target is None or target.get('axis') is None:
        raise ValueError('Split не найден')
    target.clear();target.update({'id':split_id,'axis':None})
    layout(clone,width,height,frame_width)
    return clone


def from_global(splits,width,height,fw,default_thickness):
    """Migrate full-height/width historical splits into independent leaf regions."""
    root=root_node()
    for sp in splits:
        axis=sp.axis
        position=sp.position
        thickness=sp.thickness or default_thickness
        leaves,_=layout(root,width,height,fw)
        for zid,rect in list(leaves):
            lo,hi=(rect[0],rect[1]) if axis=='V' else (rect[2],rect[3])
            if lo+thickness/2+0.001 < position < hi-thickness/2-0.001:
                root,newid=add(root,zid,axis,thickness,width,height,fw)
                root=update(root,newid,width,height,fw,center=position)
    return root


def active_band_rectangles(tree,width,height,fw):
    _,bands=layout(tree,width,height,fw)
    return [r[-1] for r in bands]
