"""W54: exact union of two perpendicular *open* planar Interior sleeves.

The legacy Smart Interior miter expects 5-face closed-back prism; our W49
4-face sleeves must not be forwarded to it or reshaped by its fallback clip.
This solver is deliberately narrow and fail-closed. It merges equal-height
axis-aligned 90-degree two-sleeve groups by partitioning their XY union.
Return Mesh polygon arrays plus metric UV, without altering either source.
The resulting shared L-shaped cavity has no duplicate intersecting top or
bottom surfaces, and remains open at original long front/rear boundaries.
"""
from __future__ import annotations
import math

EPS=1.0e-5
def _near(a,b,tol=EPS):return abs(a-b)<tol
def _rectangle(mesh):
    verts=mesh['verts'];faces=mesh['faces']
    if len(verts)!=8 or len(faces)!=4 or any(len(f)!=4 for f in faces):
        raise ValueError("W54_EXPECT_EIGHT_VERTEX_FOUR_QUAD_OPEN_SLEEVE")
    if any(not math.isfinite(float(v)) for p in verts for v in p):
        raise ValueError("W54_NONFINITE")
    zlo=min(v[2] for v in verts);zhi=max(v[2] for v in verts)
    if zhi-zlo<.05:raise ValueError("W54_BAD_VERTICAL_SPAN")
    xs=sorted(set(round(float(v[0]),6) for v in verts))
    ys=sorted(set(round(float(v[1]),6) for v in verts))
    if len(xs)!=2 or len(ys)!=2:
        raise ValueError("W54_ONLY_AXIS_ALIGNED_PERPENDICULAR_SLEEVES")
    if any(min(abs(v[2]-zlo),abs(v[2]-zhi))>EPS for v in verts):
        raise ValueError("W54_SLEEVE_VERTICAL_TWIST")
    if len({(round(v[0],6),round(v[1],6),round(v[2],6)) for v in verts})!=8:
        raise ValueError("W54_DUPLICATE_SOURCE_VERTICES")
    dx=xs[1]-xs[0];dy=ys[1]-ys[0]
    if max(dx,dy)<1. or min(dx,dy)<.003 or max(dx,dy)/min(dx,dy)<4:
        raise ValueError("W54_NOT_A_LONG_OPEN_REVEAL_SLEEVE")
    axis='X' if dx>dy else 'Y'
    return {'axis':axis,'xmin':xs[0],'xmax':xs[1],'ymin':ys[0],'ymax':ys[1],
            'zlo':zlo,'zhi':zhi,'length':max(dx,dy),'depth':min(dx,dy)}
def _inside(x,y,r):
    return r['xmin']-EPS<x<r['xmax']+EPS and r['ymin']-EPS<y<r['ymax']+EPS
def _cap_boundary(a,b,r):
    # True when this oriented outer-boundary segment is one physical TERMINAL
    # face of a source sleeve, not its open front or rear long boundary.
    if r['axis']=='X':
        if not _near(a[0],b[0]):return False
        if not (_near(a[0],r['xmin']) or _near(a[0],r['xmax'])):return False
        return min(a[1],b[1])>=r['ymin']-EPS and max(a[1],b[1])<=r['ymax']+EPS
    if not _near(a[1],b[1]):return False
    if not (_near(a[1],r['ymin']) or _near(a[1],r['ymax'])):return False
    return min(a[0],b[0])>=r['xmin']-EPS and max(a[0],b[0])<=r['xmax']+EPS

def merge_two(a,b,*,resolution=4096):
    ra=_rectangle(a);rb=_rectangle(b)
    if ra['axis']==rb['axis']:
        raise ValueError('W54_PARALLEL_SLEEVES_NOT_SUPPORTED_BY_CORNER_UNION')
    if abs(ra['zlo']-rb['zlo'])>EPS or abs(ra['zhi']-rb['zhi'])>EPS:
        raise ValueError('W54_DIFFERENT_VERTICAL_SPANS_NEED_EXPLICIT_ALIGNMENT')
    ovx=min(ra['xmax'],rb['xmax'])-max(ra['xmin'],rb['xmin'])
    ovy=min(ra['ymax'],rb['ymax'])-max(ra['ymin'],rb['ymin'])
    if ovx<EPS or ovy<EPS:
        raise ValueError('W54_NO_POSITIVE_OVERLAP_TO_RESOLVE')
    xs=sorted(set([ra['xmin'],ra['xmax'],rb['xmin'],rb['xmax']]))
    ys=sorted(set([ra['ymin'],ra['ymax'],rb['ymin'],rb['ymax']]))
    nx=len(xs)-1;ny=len(ys)-1
    occupied=set()
    for ix in range(nx):
        for iy in range(ny):
            x=(xs[ix]+xs[ix+1])*.5;y=(ys[iy]+ys[iy+1])*.5
            if _inside(x,y,ra) or _inside(x,y,rb):
                occupied.add((ix,iy))
    if not occupied:raise ValueError('W54_EMPTY_UNION')
    scale=1000.0/float(resolution)
    if abs(scale-1000./resolution)>1e-9:raise ValueError('W54_UV_SCALE_INVALID')
    z0=ra['zlo'];z1=ra['zhi']
    x0=min(xs);y0=min(ys)
    vertices=[];lookup={};faces=[];uvs=[];roles=[]
    def vid(x,y,z):
        pos=(float(x),float(y),float(z))
        key=tuple(round(v,7) for v in pos)
        i=lookup.get(key)
        if i is None:
            i=len(vertices);lookup[key]=i;vertices.append(pos)
        return i
    def polygon(points,uv,role):
        if len(points)!=4 or len(set(tuple(round(v,7) for v in p) for p in points))!=4:
            raise ValueError('W54_UNION_DEGENERATE_QUAD')
        if any(not all(math.isfinite(float(v)) for v in p) for p in points):
            raise ValueError('W54_UNION_NONFINITE')
        if any(not(-1e-6<=u<=1+1e-6 and -1e-6<=v<=1+1e-6) for u,v in uv):
            raise ValueError('W54_UV_OUTSIDE_ONE_TILE')
        faces.append(tuple(vid(*p) for p in points));uvs.append(tuple(uv));roles.append(role)
    boundary=[]
    for ix,iy in sorted(occupied):
        xa,xb=xs[ix],xs[ix+1];ya,yb=ys[iy],ys[iy+1]
        xy=[(xa,ya),(xb,ya),(xb,yb),(xa,yb)]
        t=[(x,y,z1) for x,y in xy]
        uv=[((x-x0)*scale,(y-y0)*scale) for x,y in xy]
        polygon(t,uv,'INTERIOR_TOP')
        polygon(list(reversed([(x,y,z0) for x,y in xy])),list(reversed(uv)),'INTERIOR_BOTTOM')
        # Occupied cell is always LEFT of each outward perimeter edge.
        for idir,(di,dj) in enumerate(((0,-1),(1,0),(0,1),(-1,0))):
            if (ix+di,iy+dj) in occupied:continue
            axy=xy[idir];bxy=xy[(idir+1)%4]
            boundary.append((axy,bxy))
            if _cap_boundary(axy,bxy,ra) or _cap_boundary(axy,bxy,rb):
                dist=math.dist(axy,bxy)
                pts=[(*axy,z0),(*bxy,z0),(*bxy,z1),(*axy,z1)]
                uv2=[(0,0),(dist*scale,0),(dist*scale,(z1-z0)*scale),(0,(z1-z0)*scale)]
                polygon(pts,uv2,'INTERIOR_TERMINAL')
    if len(faces)<6:raise ValueError('W54_UNION_HAS_NO_GEOMETRY')
    area_a=(ra['xmax']-ra['xmin'])*(ra['ymax']-ra['ymin'])
    area_b=(rb['xmax']-rb['xmin'])*(rb['ymax']-rb['ymin'])
    union_area=area_a+area_b-ovx*ovy
    actual=sum((xs[ix+1]-xs[ix])*(ys[iy+1]-ys[iy]) for ix,iy in occupied)
    if abs(actual-union_area)>2e-5:
        raise ValueError('W54_UNION_AREA_MISMATCH')
    return {'verts':vertices,'faces':faces,'uvs':uvs,'roles':roles,
            'metric':{'intersection_area_m2':ovx*ovy,'footprint_union_area_m2':union_area,
                      'cells':len(occupied),'terminal_faces':roles.count('INTERIOR_TERMINAL'),
                      'source_axes':(ra['axis'],rb['axis']),'x_bounds':(x0,max(xs)),
                      'y_bounds':(y0,max(ys)), 'z_bounds':(z0,z1),
                      'resolution':resolution,'source_depths_m':(ra['depth'],rb['depth'])}}
