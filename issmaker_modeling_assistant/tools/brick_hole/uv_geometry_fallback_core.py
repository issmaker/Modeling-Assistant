"""Metric UV fallback for provable micro-seam trapezoid/triangle reveals.

Shared by Brick Hole and DWOS.  A physically valid but near-triangular reveal
may have no unique legacy front-edge anchor when its final edge is a CAD
microcontact. Never invent a UV by copying two identical coordinates.
Instead, create one *explicit new UV island* at source texel density. Caller
must mark the seam for inspection. This is a fallback ONLY for <=10um collapsed
edge, not general profile reparameterisation. Existing authored UV untouched.
"""
from __future__ import annotations
import math
def calculate_micro_reveal_metric_uv(points, *, resolution=4096,
                                     density=1000, margin_px=8,
                                     micro_edge_limit_m=1.0e-5):
    xyz=[tuple(float(t) for t in p) for p in points]
    if len(xyz)!=4 or not all(len(p)==3 and all(math.isfinite(c) for c in p)
                              for p in xyz):
        raise ValueError('BH_UV_INVALID_REVEAL_INPUT')
    def sub(a,b):return tuple(x-y for x,y in zip(a,b))
    def dot(a,b):return sum(x*y for x,y in zip(a,b))
    def cross(a,b):return (a[1]*b[2]-a[2]*b[1],
                           a[2]*b[0]-a[0]*b[2],
                           a[0]*b[1]-a[1]*b[0])
    def norm(a):return math.sqrt(dot(a,a))
    edge=[norm(sub(xyz[i],xyz[(i+1)%4])) for i in range(4)]
    if min(edge)>micro_edge_limit_m:
        raise ValueError('BH_UV_NO_PROVEN_MICROCONTACT')
    origin=xyz[0]
    best=max((norm(sub(xyz[k],origin)),k) for k in range(1,4))
    if best[0]<1.e-5:raise ValueError('BH_UV_FACE_COLLINEAR')
    u=tuple(v/best[0] for v in sub(xyz[best[1]],origin))
    candidates=[]
    for j in range(1,4):
        vv=sub(xyz[j],origin)
        cr=cross(u,vv)
        candidates.append((norm(cr),cr))
    bestnorm,normal=max(candidates,key=lambda x:x[0])
    if bestnorm<=1.e-9:raise ValueError('BH_UV_FACE_COLLINEAR')
    n=tuple(v/bestnorm for v in normal)
    v=cross(n,u)
    offplane=max(abs(dot(sub(p,origin),n)) for p in xyz)
    if offplane>1.e-4:
        raise ValueError('BH_UV_ORPHAN_NONPLANAR')
    uv=[(dot(sub(p,origin),u),dot(sub(p,origin),v)) for p in xyz]
    min_u=min(p[0] for p in uv);min_v=min(p[1] for p in uv)
    scale=float(density)/float(resolution)
    margin=float(margin_px)/float(resolution)
    final=[((a-min_u)*scale+margin,(b-min_v)*scale+margin)
           for a,b in uv]
    if any(a<0 or b<0 or a>1 or b>1 for a,b in final):
        raise ValueError('BH_UV_SINGLE_TILE_CAPACITY')
    area=sum(final[i][0]*final[(i+1)%4][1]-final[(i+1)%4][0]*final[i][1] for i in range(4))*.5
    if abs(area)<1.e-12:
        raise ValueError('BH_UV_FACE_ZERO_AREA')
    max_error=0
    for i,d in enumerate(edge):
        if d<=1.e-5:continue
        a,b=final[i],final[(i+1)%4]
        target=math.dist(a,b)*resolution/d
        err=abs(target-density)
        max_error=max(max_error,err)
        if err>5.:
            raise ValueError(f'BH_UV_METRIC_ERROR_PX_M:{err:.6g}')
    return final,{'policy':'EXPLICIT_MICROCONTACT_METRIC_UV_ISLAND',
                  'minimum_edge_m':min(edge),'max_density_error_px_m':max_error,
                  'offplane_error_m':offplane,'intentional_seam':True}


def calculate_metric_island_uv(points, *, resolution=4096, density=1000,
                               margin_px=8, planar_tolerance_m=1e-5):
    """Create a truthful independent local UV island for a proven node seam.

    Supports a triangle or quad. UV length is derived directly from 3D edge
    lengths; no fit-to-tile scaling is allowed. Oversized geometry fails closed.
    Coordinates should be object-local with the linear object transform applied,
    not large translated world coordinates.
    """
    xyz=[tuple(float(v) for v in p) for p in points]
    if len(xyz) not in (3,4) or any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in xyz):
        raise ValueError('BH_UV_METRIC_ISLAND_INVALID_POLYGON')
    def sub(a,b):return tuple(x-y for x,y in zip(a,b))
    def dot(a,b):return sum(x*y for x,y in zip(a,b))
    def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
    def norm(a):return math.sqrt(dot(a,a))
    origin=xyz[0]
    best=max(((norm(cross(sub(xyz[i],origin),sub(xyz[j],origin))),i,j)
              for i in range(1,len(xyz)) for j in range(i+1,len(xyz))),default=(0.,0,0))
    if best[0]<=1.e-12:raise ValueError('BH_UV_METRIC_ISLAND_DEGENERATE')
    normal=tuple(v/best[0] for v in cross(sub(xyz[best[1]],origin),sub(xyz[best[2]],origin)))
    offplane=max(abs(dot(sub(p,origin),normal)) for p in xyz)
    if offplane>planar_tolerance_m:raise ValueError('BH_UV_METRIC_ISLAND_NONPLANAR')
    longest=max(((norm(sub(xyz[(i+1)%len(xyz)],xyz[i])),i) for i in range(len(xyz))),default=(0.,0))
    if longest[0]<=1.e-9:raise ValueError('BH_UV_METRIC_ISLAND_COLLAPSED_EDGE')
    tangent=sub(xyz[(longest[1]+1)%len(xyz)],xyz[longest[1]])
    tangent=tuple(v/longest[0] for v in tangent)
    bitangent=cross(normal,tangent)
    metric=[(dot(sub(p,origin),tangent),dot(sub(p,origin),bitangent)) for p in xyz]
    min_u=min(p[0] for p in metric);max_u=max(p[0] for p in metric)
    min_v=min(p[1] for p in metric);max_v=max(p[1] for p in metric)
    scale=float(density)/float(resolution);pad=float(margin_px)/float(resolution)
    if (max_u-min_u)*scale+2*pad>1.000001 or (max_v-min_v)*scale+2*pad>1.000001:
        raise ValueError('BH_UV_METRIC_ISLAND_SINGLE_TILE_CAPACITY')
    uv=[((u-min_u)*scale+pad,(v-min_v)*scale+pad) for u,v in metric]
    area=abs(sum(uv[i][0]*uv[(i+1)%len(uv)][1]-uv[(i+1)%len(uv)][0]*uv[i][1]
                 for i in range(len(uv)))*.5)
    if area<=1.e-12:raise ValueError('BH_UV_METRIC_ISLAND_ZERO_AREA')
    max_error=0.
    for i,a in enumerate(xyz):
        j=(i+1)%len(xyz)
        d=norm(sub(a,xyz[j]));du=math.dist(uv[i],uv[j])
        if d<=1.e-8:raise ValueError('BH_UV_METRIC_ISLAND_MICRO_EDGE')
        error=abs(du*resolution/d-density)
        max_error=max(max_error,error)
        if error>5.:raise ValueError(f'BH_UV_METRIC_ISLAND_DENSITY:{error:.6g}')
    return uv,{'policy':'W5833_LOCAL_NODE_SEAM_METRIC_ISLAND',
               'max_density_error_px_m':max_error,'max_planar_residual_m':offplane,
               'intentional_seam':True,'density_px_m':density,'resolution':resolution}
