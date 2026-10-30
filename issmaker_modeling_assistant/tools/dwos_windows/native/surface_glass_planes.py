"""W46: one planar GLASS/PANEL face per connected Zone Graph region.

Source facade grid and authored boundary are immutable. Only generated cover
polygons are dissolved; boundary split vertices remain to match reveal edges.
The independent boundary oracle refuses ambiguous / holed or duplicated loops.
"""
from __future__ import annotations
from collections import deque

_OFFSETS=((0,-1),(1,0),(0,1),(-1,0))


def _key(point):
    return (round(float(point[0]),12),round(float(point[1]),12))


def glass_regions(graph):
    """Return [{'polygon', 'fill','cells'}] for four-connected glass cells."""
    grid=graph['grid']
    visited=set()
    out=[]
    for item in graph['cells']:
        if item['role']!='GLASS':continue
        ident=(item['i'],item['j'])
        if ident in visited:continue
        tag=(item.get('fill','GLASS'),item.get('zone_id'))
        queue=deque([ident]);visited.add(ident);region=[]
        while queue:
            key=queue.popleft()
            current=grid[key]
            region.append(current)
            for di,dj in _OFFSETS:
                nk=(key[0]+di,key[1]+dj)
                neighbor=grid.get(nk)
                if (nk not in visited and neighbor is not None
                    and neighbor['role']=='GLASS'
                    and (neighbor.get('fill','GLASS'),neighbor.get('zone_id'))==tag):
                    visited.add(nk);queue.append(nk)
        region_keys={(c['i'],c['j']) for c in region}
        directed=[]
        for c in region:
            corners=c['corners']
            for edge_index,(a,b) in enumerate(zip(corners,corners[1:]+corners[:1])):
                di,dj=_OFFSETS[edge_index]
                if (c['i']+di,c['j']+dj) not in region_keys:
                    directed.append((_key(a),_key(b)))
        # One outgoing + one incoming edge at every boundary vertex.
        successors={};incoming={}
        for a,b in directed:
            if a in successors or b in incoming:
                raise ValueError('W46_GLASS_BOUNDARY_BRANCH_OR_DUPLICATE_EDGE')
            successors[a]=b;incoming[b]=a
        if len(successors)!=len(incoming) or set(successors)!=set(incoming):
            raise ValueError('W46_GLASS_BOUNDARY_NOT_CLOSED')
        start=min(successors)
        poly=[];cur=start
        for _ in range(len(directed)):
            if cur in poly:
                raise ValueError('W46_GLASS_BOUNDARY_PREMATURE_LOOP')
            poly.append(cur);cur=successors[cur]
        if cur!=start or len(poly)!=len(successors):
            raise ValueError('W46_GLASS_BOUNDARY_MULTIPLE_LOOPS')
        signed_area=sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(poly,poly[1:]+poly[:1]))*.5
        if signed_area<=1e-10:
            raise ValueError('W46_GLASS_BOUNDARY_INVALID_AREA')
        area=sum((c['rect'][1]-c['rect'][0])*(c['rect'][3]-c['rect'][2]) for c in region)
        if abs(area-signed_area)>max(1e-7,area*1e-8):
            raise ValueError('W46_GLASS_BOUNDARY_AREA_CONSERVATION')
        out.append({'polygon':poly,'fill':tag[0],'zone_id':tag[1],
                    'cell_count':len(region),'area':area})
    if sum(reg['cell_count'] for reg in out)!=sum(c['role']=='GLASS' for c in graph['cells']):
        raise ValueError('W46_GLASS_REGION_COVERAGE')
    return out
