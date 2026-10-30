"""DWOS NEXT facade-zone layout bridge. Geometry stays authored, not guessed.

Uses the already proven recursive zone_core split predicates. Maps leaf zones
and splitter bands into the global LP-FIRST facade coordinate system, retaining
module-cut topology. Only rectilinear LP-FIRST wall regions are supported here.
Other contours remain explicitly blocked by existing engine preflight.
"""
from __future__ import annotations
import math
from . import zone_core
from . import surface_step_core as geom

def _shift(r, x, y):
    return (r[0]+x,r[1]+x,r[2]+y,r[3]+y)

def zone_layout(outer,inner,tree):
    from .surface_graph import _orthogonal_bounds
    ob=geom.rectangle_axes(outer) or _orthogonal_bounds(outer)
    ib=geom.rectangle_axes(inner) or _orthogonal_bounds(inner)
    ox0,ox1,oy0,oy1=ob;ix0,ix1,iy0,iy1=ib
    borders=(ix0-ox0,ox1-ix1,iy0-oy0,oy1-iy1)
    tolerance=max(1e-5,0.000001*max(ox1-ox0,oy1-oy0))
    if min(borders)<=tolerance or max(borders)-min(borders)>tolerance:
        raise ValueError('ZONE_NEXT_NONUNIFORM_FRAME_INSET')
    width=ox1-ox0;height=oy1-oy0
    origin_x=(ox0+ox1)*.5;origin_y=(oy0+oy1)*.5
    layout_tree=zone_core.decode(tree)
    leaves, bands=zone_core.layout(layout_tree,width,height,sum(borders)/4)
    zone_rects=[(name,_shift(rect,origin_x,origin_y)) for name,rect in leaves]
    zone_fills={}
    for zone_id,_rect in leaves:
        node=zone_core.find(layout_tree,zone_id)
        fill=str(node.get('fill','GLASS')).upper()
        panel_mode=str(node.get('panel_mode','OPT')).upper()
        if fill not in {'GLASS','PANEL','EMPTY'}:
            raise ValueError('ZONE_FILL_NOT_SUPPORTED_YET:'+fill)
        if fill=='PANEL' and panel_mode!='OPT':
            raise ValueError('ZONE_PANEL_FULL_NOT_SUPPORTED_YET')
        zone_fills[zone_id]=fill
    bars=[(name,_shift(band,origin_x,origin_y)) for name,axis,center,thickness,rect,band in bands]
    if len(zone_rects)>128 or len(bars)>128:
        raise ValueError('ZONE_NEXT_RESOURCE_BUDGET')
    area=sum((v[1]-v[0])*(v[3]-v[2]) for _,v in zone_rects+bars)
    expected=(ix1-ix0)*(iy1-iy0)
    if not math.isclose(area,expected,rel_tol=1e-7,abs_tol=1e-6):
        raise ValueError('ZONE_NEXT_AREA_CONSERVATION')
    return {'zones':zone_rects,'bars':bars,'zone_count':len(zone_rects),
            'splitter_count':len(bars),'zone_fills':zone_fills,'area':area,
            'expected_area':expected,
             'physical_inner_area':geom.signed_area(inner)}

def assign_zone(cell,zones):
    if cell['role']!='GLASS':return None
    x0,x1,y0,y1=cell['rect'];cx=(x0+x1)*.5;cy=(y0+y1)*.5
    hits=[name for name,(a,b,c,d) in zones if a+1e-7<cx<b-1e-7 and c+1e-7<cy<d-1e-7]
    if len(hits)!=1:
        raise ValueError('ZONE_NEXT_CELL_NOT_UNIQUELY_ASSIGNED')
    return hits[0]
