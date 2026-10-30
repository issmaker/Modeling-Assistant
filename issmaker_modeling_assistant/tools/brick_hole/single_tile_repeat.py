"""BH0462: repeat UV **inside one physical tile** by clipping NEW face loops.

Pure math, no Blender imports. A 3D border is never moved and original source
UV loops are never touched. A repeated UV boundary is a new UV seam, not a
second UDIM; seamless texture appearance requires compatible edge pixels.
"""
import math
from dataclasses import dataclass

@dataclass(frozen=True)
class TileCell:
    vertices: tuple  # ((world XYZ, local UV), ...) cyclic, outward new-face order
    repeat: tuple     # (integer U, integer V) relative to same ONE tile


def _clip(polygon, axis, bound, keep_above, tolerance=2e-10):
    def inside(p):
        return p[1][axis] >= bound-tolerance if keep_above else p[1][axis] <= bound+tolerance
    def cross(a,b):
        d=b[1][axis]-a[1][axis]
        if abs(d)<=1e-15:return a
        t=max(0.,min(1.,(bound-a[1][axis])/d))
        xyz=tuple(a[0][j]+t*(b[0][j]-a[0][j]) for j in range(3))
        uv=tuple(a[1][j]+t*(b[1][j]-a[1][j]) for j in range(2))
        uv=tuple(bound if j==axis else uv[j] for j in range(2))
        return (xyz,uv)
    result=[]
    if not polygon:return result
    prev=polygon[-1]
    for curr in polygon:
        p,c=inside(prev),inside(curr)
        if c and not p:result.append(cross(prev,curr))
        if c:result.append(curr)
        if p and not c:result.append(cross(prev,curr))
        prev=curr
    return result


def _area2(poly):
    return sum(a[1][0]*b[1][1]-b[1][0]*a[1][1]
               for a,b in zip(poly,poly[1:]+poly[:1]))


def _squared(a,b):
    return sum((x-y)**2 for x,y in zip(a,b))


def _dedup(poly):
    out=[]
    for item in poly:
        if not out or _squared(out[-1][0],item[0])>1e-18:out.append(item)
    if len(out)>1 and _squared(out[0][0],out[-1][0])<=1e-18:out.pop()
    return out


def _is_on_segment(p,a,b,eps=1e-8):
    diff=tuple(b[i]-a[i] for i in range(3))
    d2=sum(x*x for x in diff)
    if d2<=1e-20:return False
    t=sum((p[i]-a[i])*diff[i] for i in range(3))/d2
    q=tuple(a[i]+t*diff[i] for i in range(3))
    return eps<t<1-eps and _squared(q,p)<=eps*eps


def clip_plan_to_single_tile(rows,tile_min,tile_max,*,max_cells=512,protect_start=True,protect_end=False):
    """Clip all new UV strip quads into one UV square by modulo-like tiling.

    The original source row must remain entirely inside the square. Subdividing
    it would introduce a T-junction on an authored source edge, so refuse.
    Optional far edge (Bridge) is equally protected. World positions of shared
    cuts match numerically; caller MUST weld by world XYZ and preserve BMesh UV
    loops per face. Results do not certify image pixel periodicity.
    """
    lo,hi=float(tile_min),float(tile_max)
    if not (math.isfinite(lo) and math.isfinite(hi) and hi-lo>0):
        raise ValueError('SINGLE_TILE: invalid UV bounds')
    if len(rows)<2 or max_cells<1:raise ValueError('SINGLE_TILE: invalid strip rows')
    for row in rows:
        if len(row)!=4:raise ValueError('SINGLE_TILE: invalid row')
        for v in row[2:]:
            if len(v)!=2 or not all(math.isfinite(float(x)) for x in v):
                raise ValueError('SINGLE_TILE: invalid source UV')
        for v in row[:2]:
            if len(v)!=3 or not all(math.isfinite(float(x)) for x in v):
                raise ValueError('SINGLE_TILE: invalid geometry')
    for edge,label,protect in ((rows[0],'исходном',protect_start),
                                (rows[-1],'конечном',protect_end)):
        if protect and any(not (lo-1e-9<=v<=hi+1e-9)
                           for point in edge[2:] for v in point):
            raise ValueError(f'SINGLE_TILE: UV на {label} ребре выходит за один тайл; '
                             'нельзя менять авторские UV или создавать T-junction')
    span=hi-lo
    out=[]
    for before,after in zip(rows,rows[1:]):
        l0,r0,ul0,ur0=before
        l1,r1,ul1,ur1=after
        polygon=[(tuple(r0),tuple(ur0)),(tuple(l0),tuple(ul0)),
                 (tuple(l1),tuple(ul1)),(tuple(r1),tuple(ur1))]
        s0=_area2(polygon)
        if abs(s0)<1e-14:raise ValueError('SINGLE_TILE: degenerate UV cell')
        uvmin=[min(x[1][axis] for x in polygon) for axis in (0,1)]
        uvmax=[max(x[1][axis] for x in polygon) for axis in (0,1)]
        start=[math.floor((v-lo+1e-10)/span) for v in uvmin]
        stop=[math.floor((v-lo-1e-10)/span) for v in uvmax]
        for axis in (0,1):
            if stop[axis]<start[axis]:stop[axis]=start[axis]
        count=(stop[0]-start[0]+1)*(stop[1]-start[1]+1)
        if len(out)+count>max_cells:
            raise ValueError('SINGLE_TILE: слишком много UV-разрезов; ограничение безопасности')
        for ix in range(start[0],stop[0]+1):
            for iy in range(start[1],stop[1]+1):
                clipped=list(polygon)
                for axis,index in ((0,ix),(1,iy)):
                    bound_lo=lo+index*span;bound_hi=hi+index*span
                    clipped=_clip(clipped,axis,bound_lo,True)
                    clipped=_clip(clipped,axis,bound_hi,False)
                clipped=_dedup(clipped)
                if len(clipped)<3 or abs(_area2(clipped))<1e-12:continue
                if s0*_area2(clipped)<0:raise ValueError('SINGLE_TILE: UV face flip')
                local=[]
                for xyz,uv in clipped:
                    if (protect_start and (_is_on_segment(xyz,rows[0][0],rows[0][1])) or
                            protect_end and _is_on_segment(xyz,rows[-1][0],rows[-1][1])):
                        raise ValueError('SINGLE_TILE: разрез требует делить исходное ребро; BLOCK')
                    u=uv[0]-ix*span;v=uv[1]-iy*span
                    # Numerical clipping noise must not escape physical tile.
                    if not (lo-2e-7<=u<=hi+2e-7 and lo-2e-7<=v<=hi+2e-7):
                        raise ValueError('SINGLE_TILE: clipped UV outside one tile')
                    local.append((xyz,(min(hi,max(lo,u)),min(hi,max(lo,v)))))
                out.append(TileCell(tuple(local),(ix,iy)))
    if not out:raise ValueError('SINGLE_TILE: empty result')
    return tuple(out)


def seam_phase_gap_px(left_uv,right_uv,resolution,period=1.0):
    """Minimum phase mismatch, not an image-color seam guarantee."""
    return max(abs((a-b)-round((a-b)/period)*period) for a,b in zip(left_uv,right_uv))*resolution
