"""Pure deterministic connected-facade graph for DWOS rectangular windows.

No Blender dependency. Source polygon and authored UV are never mutated.
Every rectangular CELL shares index keys with adjacent cells; material roles are
labels of a single planar cell complex, not separate overlapping meshes.
"""
from __future__ import annotations
import math
from . import surface_step_core as core


def _cuts(values, eps=1e-5):
    ordered=sorted(float(v) for v in values)
    if not all(math.isfinite(x) for x in ordered): raise ValueError('GRAPH_NONFINITE_CUT')
    result=[]
    for x in ordered:
        if result and x-result[-1] <= eps:
            if x-result[-1] < 0:raise ValueError('GRAPH_UNSORTED_CUT')
            continue
        result.append(x)
    return result


def _orthogonal_bounds(poly):
    pts=core.normalized_boundary(poly)
    if any(min(abs(a[0]-b[0]),abs(a[1]-b[1]))>1e-7
           for a,b in zip(pts,pts[1:]+pts[:1])):
        raise ValueError('LP_FIRST_NON_ORTHOGONAL_COMPLEX_PATH_UNSUPPORTED')
    return min(p[0] for p in pts),max(p[0] for p in pts),min(p[1] for p in pts),max(p[1] for p in pts)


def make_graph(outer, inner, mullion_width=None, module_cuts=None, zone_tree_next=None):
    """Build a complete rectangular subdivision with structural and glass roles.

    Collinear authored vertices produce optional split lines, not overlapping
    polygons. Offset outlines remain unchanged in the source definition.
    """
    outer_rect=core.rectangle_axes(outer)
    inner_rect=core.rectangle_axes(inner)
    outer_box=outer_rect or _orthogonal_bounds(outer)
    inner_box=inner_rect or _orthogonal_bounds(inner)
    ox0,ox1,oy0,oy1=outer_box
    ix0,ix1,iy0,iy1=inner_box
    if min(ix0-ox0,ox1-ix1,iy0-oy0,oy1-iy1)<=1e-8:
        raise ValueError('GRAPH_INVALID_INSET_ORDER')
    xs=[ox0,ix0,ix1,ox1];ys=[oy0,iy0,iy1,oy1]
    xs.extend(p[0] for p in outer)
    xs.extend(p[0] for p in inner)
    ys.extend(p[1] for p in outer)
    ys.extend(p[1] for p in inner)
    xs.extend(p[0] for p in outer if ox0+1e-5<p[0]<ox1-1e-5)
    xs.extend(p[0] for p in inner if ix0+1e-5<p[0]<ix1-1e-5)
    ys.extend(p[1] for p in outer if oy0+1e-5<p[1]<oy1-1e-5)
    ys.extend(p[1] for p in inner if iy0+1e-5<p[1]<iy1-1e-5)
    bars=[];zones=[]
    explicit_zone_names=[]
    if zone_tree_next:
        from .surface_zone_next import zone_layout
        result=zone_layout(outer,inner,zone_tree_next)
        bars=[(name,rect) for name,rect in result['bars']]
        zones=[rect for name,rect in result['zones']]
        explicit_zone_names=result['zones']
        fill_by_zone=result['zone_fills']
        for name,(xa,xb,ya,yb) in bars:
            xs.extend([xa,xb]);ys.extend([ya,yb])
    elif mullion_width is not None:
        m=float(mullion_width)
        w=ix1-ix0;h=iy1-iy0
        if not math.isfinite(m) or m<=0 or m>=min(w/3.,h/6.):
            raise ValueError('MULLION_TOO_WIDE_FOR_SIX_GLASS_ZONES')
        cx=(ix0+ix1)*.5
        y_a=iy0+h/3.
        y_b=iy0+2.*h/3.
        left=cx-m/2.;right=cx+m/2.
        ya0=y_a-m/2.;ya1=y_a+m/2.
        yb0=y_b-m/2.;yb1=y_b+m/2.
        xs.extend([left,right]);ys.extend([ya0,ya1,yb0,yb1])
        bars=[('vertical',(left,right,iy0,iy1)),
              ('lower',(ix0,ix1,ya0,ya1)),
              ('upper',(ix0,ix1,yb0,yb1))]
        for xa,xb in ((ix0,left),(right,ix1)):
            for ya,yb in ((iy0,ya0),(ya1,yb0),(yb1,iy1)):
                zones.append((xa,xb,ya,yb))
    else:
        zones=[(ix0,ix1,iy0,iy1)]
    if module_cuts:
        xs.extend(v for v in module_cuts.get("x",[]) if ox0+1e-5<v<ox1-1e-5)
        ys.extend(v for v in module_cuts.get("y",[]) if oy0+1e-5<v<oy1-1e-5)
    xs=_cuts(xs);ys=_cuts(ys)
    if (len(xs)-1)*(len(ys)-1)>20000:
        raise ValueError('LP_FIRST_CELL_BUDGET_EXCEEDED: more than 20k facade cells')
    cell_grid={};cells=[]
    for j,(y0,y1) in enumerate(zip(ys,ys[1:])):
        for i,(x0,x1) in enumerate(zip(xs,xs[1:])):
            mx=(x0+x1)*.5;my=(y0+y1)*.5
            if not core.point_in_polygon((mx,my),outer):
                continue
            if not core.point_in_polygon((mx,my),inner):
                role='FRAME'
            elif (mullion_width is not None or zone_tree_next) and any(
                    a<mx<b and c<my<d for _,(a,b,c,d) in bars):
                role='MULLION'
            else:role='GLASS'
            rect=(x0,x1,y0,y1)
            cell={'i':i,'j':j,'role':role,'rect':rect,
                  'corners':[(x0,y0),(x1,y0),(x1,y1),(x0,y1)]}
            if zone_tree_next and role=='GLASS':
                from .surface_zone_next import assign_zone
                cell['zone_id']=assign_zone(cell,explicit_zone_names)
                cell['fill']=fill_by_zone[cell['zone_id']]
            cells.append(cell);cell_grid[(i,j)]=cell
    if zone_tree_next:
        active_ids={c.get('zone_id') for c in cells if c['role']=='GLASS'}
        unused=[name for name,_ in explicit_zone_names if name not in active_ids]
        if unused:
            raise ValueError('ZONE_ORTHOGONAL_EMPTY_LEAF:'+repr(unused[:6]))
    area=sum((x1-x0)*(y1-y0) for c in cells for x0,x1,y0,y1 in (c['rect'],))
    expected=core.signed_area(outer)
    if not math.isclose(area,expected,rel_tol=1e-10,abs_tol=1e-8):
        raise ValueError('GRAPH_AREA_MISMATCH')
    return {'xs':xs,'ys':ys,'cells':cells,'grid':cell_grid,'zones':zones,
            'outer_box':outer_box,'inner_box':inner_box,'bars':bars,
            'coverage_area':area,'expected_area':expected,
            'custom_zones':bool(zone_tree_next),
            'orthogonal_complex':bool(outer_rect is None or inner_rect is None),
            'zone_ids':[n for n,_ in explicit_zone_names],
            'zone_fills':dict(fill_by_zone) if zone_tree_next else {}}


def build_lp(composer,graph,z):
    """LP is an actual plane cover; no faceted glass from module grid cells."""
    from .surface_glass_planes import glass_regions
    for c in graph['cells']:
        if c['role']=='GLASS':continue
        composer.face([(*p,z) for p in c['corners']],c['role'],
                      uv=c.get('source_uv'),group='LP_FACADE',
                      part='FRONT',module=c.get('module'))
    for region in glass_regions(graph):
        fill=region['fill']
        if fill=='EMPTY':continue
        if fill not in ('GLASS','PANEL'):
            raise ValueError('W46_UNKNOWN_COVER_FILL:'+str(fill))
        polygon=region['polygon']
        min_x=min(float(p[0]) for p in polygon)
        min_y=min(float(p[1]) for p in polygon)
        cover_uv=[((p[0]-min_x)*composer.scale,(p[1]-min_y)*composer.scale) for p in polygon]
        if max(u for u,v in cover_uv)>1.+1e-6 or max(v for u,v in cover_uv)>1.+1e-6:
            # W56: GLASS is unwrapped only when physically needed. A single
            # 8/12m-wide plane must NOT be split just to fit a 4K glass UV tile.
            # FRAME/PANEL still require a real module UV layout.
            if fill=='GLASS':cover_uv=None
            else:raise ValueError('W48_LP_COVER_EXCEEDS_SINGLE_UV_TILE')
        composer.face([(*p,z) for p in polygon],fill,uv=cover_uv,
                      group='LP_FACADE',part='PANEL_FRONT' if fill=='PANEL' else 'GLASS_FRONT')


def build_hp_structure(composer,graph,z,frame_depth,mullion_depth):
    """Connected frame+mullion boundary surface, including rear-depth steps.

    Structural cells have shared XY front vertices and cross-cell rear edge
    connectivity. Each boundary front edge emits exactly one side wall; a
    structural/structural depth difference emits exactly one step wall on the
    deeper cell. GLASS cells are skipped and added as separate fill islands.
    """
    depths={'FRAME':float(frame_depth),'MULLION':float(mullion_depth)}
    if not all(math.isfinite(v) and v>1e-8 for v in depths.values()):
        raise ValueError('GRAPH_INVALID_PROFILE_DEPTH')
    grid=graph['grid'];n_x=len(graph['xs'])-1;n_y=len(graph['ys'])-1
    for c in graph['cells']:
        role=c['role']
        if role=='GLASS':continue
        d=depths[role];corners=c['corners']
        composer.face([(*p,z) for p in corners],role,group='HP_STRUCTURE',part='FRONT')
        composer.face([(*p,z-d) for p in reversed(corners)],role,
                      group='HP_STRUCTURE',part='REAR')
        i,j=c['i'],c['j']
        # Boundary follows counterclockwise CELL order: its inside lies left.
        neighbors=[(i,j-1),(i+1,j),(i,j+1),(i-1,j)]
        for e_index,(a,b) in enumerate(zip(corners,corners[1:]+corners[:1])):
            adjacent=grid.get(neighbors[e_index])
            other_role=adjacent['role'] if adjacent is not None else 'EMPTY'
            if other_role not in depths:
                # A deeper cell's side wall is segmented at the shallower
                # neighbor depth: its edges must pair with DEPTH_STEP walls
                # at every structural/glass/frame triple junction. This is
                # required to avoid hidden T-junctions/open boundary edges.
                cut_depths=[0.,d]
                for other_d in depths.values():
                    if 1e-9<other_d<d-1e-9:
                        cut_depths.append(other_d)
                cut_depths=sorted(cut_depths)
                for da,db in zip(cut_depths,cut_depths[1:]):
                    composer.face([(*a,z-da),(*a,z-db),(*b,z-db),(*b,z-da)],
                                  role,group='HP_STRUCTURE',part='PROFILE_SIDE')
            elif d>depths[other_role]+1e-9:
                other_z=z-depths[other_role]
                composer.face([(*a,other_z),(*a,z-d),(*b,z-d),(*b,other_z)],
                              role,group='HP_STRUCTURE',part='DEPTH_STEP')
    return {'structure_cells':sum(c['role']!='GLASS' for c in graph['cells']),
            'glass_cells':sum(c['role']=='GLASS' for c in graph['cells'])}
