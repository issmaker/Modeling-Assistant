"""W58.17: stage-only glazing pane layout.

No bpy datablock mutations. Panel boundaries are derived from DWOS
Zone Tree, legacy frame grid or CORNER Branch Graph. Their interiors
do not cross opaque mullion bands. All original no-split windows retain
their legacy exact geometry.
"""
from __future__ import annotations

def _clip(a, bounds, tol=1.e-6):
    l=max(float(a[0]),float(bounds[0]))
    r=min(float(a[1]),float(bounds[1]))
    b=max(float(a[2]),float(bounds[2]))
    t=min(float(a[3]),float(bounds[3]))
    if r-l<=tol or t-b<=tol:
        return None
    return l,r,b,t

def _audit_rects(records):
    if len(records)>512:
        raise ValueError('W5817_GLAZING_RESOURCE_LIMIT')
    for item in records:
        x0,x1,y0,y1=item['rect']
        if not x1>x0+1.e-5 or not y1>y0+1.e-5:
            raise ValueError('W5817_GLAZING_ZERO_APERTURE')
    for i in range(len(records)):
        a=records[i]['rect']
        for j in range(i+1,len(records)):
            b=records[j]['rect']
            over_x=min(a[1],b[1])-max(a[0],b[0])
            over_y=min(a[3],b[3])-max(a[2],b[2])
            if over_x>1.e-5 and over_y>1.e-5:
                raise ValueError('W5817_GLAZING_PANELS_OVERLAP')
    return records

def plan_panels(width,height,frame_width,splits,zone_tree,branch_state,topology,
                glass_rect,*,corner_bands=()):
    """Return None for exact unchanged legacy glazing, otherwise panel dicts.

    For CORNER, branch index is explicit; pane geometry will be mapped
    with the approved rigid postless_corner_point method. The branch count
    remains exactly two even if a user subdivides either branch further.
    """
    classification=str((topology or {}).get('classification','PLANAR')).upper()
    if classification=='CORNER':
        if not corner_bands and not zone_tree and not splits:
            return None
        if zone_tree:
            raise ValueError('W5817_LEGACY_CORNER_CROSS_BRANCH_ZONE_UNSUPPORTED')
        if splits and not corner_bands:
            raise ValueError('W5817_CORNER_SPLIT_REQUIRES_BRANCH_GRAPH')
        from .branch_core import layout_state
        state=branch_state
        if state is None:
            raise ValueError('W5817_CORNER_BRANCH_STATE_REQUIRED')
        leaves,_=layout_state(state,topology,height,frame_width)
        ordered={str(b['id']):int(b['index']) for b in state.get('branches',[])}
        if sorted(ordered.values())!=[0,1]:
            raise ValueError('W5817_CORNER_REQUIRES_TWO_BRANCHES')
        results=[]
        for leaf_id,box,branch_id in leaves:
            rect=_clip(box,glass_rect)
            if rect is not None:
                results.append({'id':str(leaf_id),'branch_index':ordered[branch_id],
                                'rect':rect})
        if len({x['branch_index'] for x in results})!=2:
            raise ValueError('W5817_CORNER_MISSING_FACADE_PANE')
        return _audit_rects(results)
    if zone_tree:
        from .zone_core import layout
        leaves,_=layout(zone_tree,width,height,frame_width)
        rects=[(str(leaf_id),box) for leaf_id,box in leaves]
    elif splits:
        from .frame_core import clear_rectangles
        rects=[('grid_'+str(i),box) for i,box in enumerate(clear_rectangles(width,height,frame_width,splits))]
    else:
        return None
    results=[]
    for leaf_id,box in rects:
        rect=_clip(box,glass_rect)
        if rect is not None:results.append({'id':leaf_id,'branch_index':None,'rect':rect})
    if len(results)<2:
        raise ValueError('W5817_GLASS_SPLITS_PRODUCED_LESS_THAN_TWO_PANELS')
    return _audit_rects(results)

def stage_glazing(panels,topology,glass_z,*,full,gap,interior_sign,
                  planar_builder,adaptive_builder):
    """Compose physically detached Mesh islands with deterministic pane IDs."""
    verts=[];faces=[];roles=[];panes=[]
    for item in panels:
        rect=item['rect']
        if topology is not None:
            p,q,tags=adaptive_builder(
                topology,rect,glass_z,0.0,full=full,gap=gap,
                interior_sign=interior_sign,corner_posts=(),junction_positions=(),
                branch_index_override=item['branch_index'])
        else:
            p,q,tags=planar_builder(rect,glass_z,0.0,full=full,
                                    gap=gap,interior_sign=interior_sign)
        if not q:
            raise ValueError('W5817_GLAZING_EMPTY_PANE')
        base=len(verts)
        verts.extend(p)
        faces.extend([tuple(base+int(v) for v in face) for face in q])
        roles.extend(tags)
        panes.append({'id':item['id'],'rect':list(rect),
                      'branch_index':item['branch_index'],
                      'faces':len(q)})
    return verts,faces,roles,panes
