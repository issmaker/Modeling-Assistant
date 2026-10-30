"""Conservative profile-oriented, globally metric DWOS frame UV staging.

All PLANAR frame faces share the same global local-metric coordinates within
surface-oriented families (front, back, XY reveal). This deliberately does NOT
claim validated curved/corner mapping or arbitrary world non-unit transforms.
"""
from __future__ import annotations

from math import sqrt, isfinite

UV_SCHEMA='DWOS_PROFILE_METRIC_1000_PX_M_V2'


def cross(a,b):
    return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])


def dot(a,b):
    return sum(x*y for x,y in zip(a,b))


def normal(points):
    n=(0.,0.,0.)
    for p,q in zip(points,points[1:]+points[:1]):
        c=cross(p,q)
        n=(n[0]+c[0],n[1]+c[1],n[2]+c[2])
    length=sqrt(dot(n,n))
    if length<1e-12:
        raise ValueError('Degenerate polygon')
    return tuple(x/length for x in n)


def uv_edge_ratios(vertices,faces,face_uvs,*,resolution=4096):
    ratios=[]
    for face,coords in zip(faces,face_uvs):
        for a,b,uv_a,uv_b in zip(face,face[1:]+face[:1],coords,coords[1:]+coords[:1]):
            p,q=vertices[a],vertices[b]
            d=sqrt(sum((x-y)**2 for x,y in zip(p,q)))
            u=sqrt(sum((x-y)**2 for x,y in zip(uv_a,uv_b)))
            if d>1.e-10:
                ratios.append(u*resolution/d)
    return ratios


def stage_planar_frame_uv(vertices,faces,roles=None,*,resolution=4096,
                          pixels_per_meter=1000,allow_repeat=False,
                          margin_pixels=8,metric_scale=(1.,1.,1.)):
    if resolution not in (2048,4096) or pixels_per_meter!=1000:
        raise ValueError('Unsupported metric UV contract')
    if margin_pixels<0 or margin_pixels>24:
        raise ValueError('Invalid margin')
    if any(abs(float(s)-1.)>1e-6 for s in metric_scale):
        return {'state':'UNMAPPED','reason':'NON_UNIT_OBJECT_SCALE', 'face_uvs':()}
    verts=tuple(tuple(float(x) for x in v) for v in vertices)
    polys=tuple(tuple(int(i) for i in f) for f in faces)
    if not polys:
        return {'state':'EMPTY','reason':'NO_FACES','face_uvs':()}
    if roles is not None and len(roles)!=len(polys):
        return {'state':'UNMAPPED','reason':'ROLE_COUNT_MISMATCH','face_uvs':()}
    if len(polys)>250000 or len(verts)>500000:
        return {'state':'UNMAPPED','reason':'TOO_MANY_FACES','face_uvs':()}
    if any(len(v)!=3 or not all(isfinite(x) for x in v) for v in verts):
        return {'state':'UNMAPPED','reason':'INVALID_VERTEX','face_uvs':()}
    span=pixels_per_meter/float(resolution)
    pad=margin_pixels/float(resolution)
    output=[]
    family_count={}
    for fi,face in enumerate(polys):
        if len(face)<3 or len(face)>128 or len(set(face))!=len(face) or any(i<0 or i>=len(verts) for i in face):
            return {'state':'UNMAPPED','reason':'INVALID_FACE','face_index':fi,'face_uvs':()}
        if roles is not None and not str(roles[fi]).upper().startswith(('FRAME','MULLION','CORNER')):
            return {'state':'UNMAPPED','reason':'UNKNOWN_PROFILE_ROLE','face_index':fi,'face_uvs':()}
        pts=[verts[i] for i in face]
        try:
            n=normal(pts)
        except ValueError:
            return {'state':'UNMAPPED','reason':'DEGENERATE_FACE','face_index':fi,'face_uvs':()}
        if abs(n[2]) >= 1.-1e-5:
            # Same exact planar metric projection for every adjacent front/back
            # triangle and quad; only global orientation differs by side.
            family='FRONT' if n[2]>0 else 'BACK'
            metric=[(n[2]*p[0],p[1]) for p in pts]
        elif abs(n[2])<=1e-5:
            # Tangent perpendicular to face normal, bitangent is +depth.
            # Side profile faces use one shared metric system per normal class.
            family=f'SIDE:{round(n[0],5)}:{round(n[1],5)}'
            tangent=(-n[1],n[0],0.)
            metric=[(dot(tangent,p),p[2]) for p in pts]
        else:
            return {'state':'UNMAPPED','reason':'OBLIQUE_PROFILE_REQUIRES_SVD',
                    'face_index':fi,'face_uvs':()}
        # Use global family origin rather than per-face bounding boxes: shared
        # vertex on coplanar faces always receives identical UV coordinates.
        if family in ('FRONT','BACK'):
            origin_u=(min(v[0] for v in verts) if n[2]>0
                      else -max(v[0] for v in verts))
            origin_v=min(v[1] for v in verts)
        else:
            origin_u=min(dot(tangent,v) for v in verts)
            origin_v=min(v[2] for v in verts)
        projected=tuple(((u-origin_u)*span+pad,(v-origin_v)*span+pad) for u,v in metric)
        # Tiled UV beyond [0,1] is never silently called an ordinary atlas.
        if not allow_repeat and any(u < -1.e-8 or v < -1.e-8 or u > 1.+1.e-8 or v > 1.+1.e-8
                                    for u,v in projected):
            return {'state':'UNMAPPED','reason':'FRAME_EXCEEDS_TILE',
                    'face_index':fi,'face_uvs':()}
        # All polygon points must lie on one plane.
        if any(abs(dot(tuple(p[i]-pts[0][i] for i in range(3)),n))>1.e-4 for p in pts):
            return {'state':'UNMAPPED','reason':'NONPLANAR_FACE','face_index':fi,'face_uvs':()}
        # Determinant > 0: avoid mirrored islands.
        signed_area=sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(projected,projected[1:]+projected[:1]))
        if signed_area<=1.e-13:
            return {'state':'UNMAPPED','reason':'MIRROR_OR_COLLAPSED_UV',
                    'face_index':fi,'face_uvs':()}
        family_count[family]=family_count.get(family,0)+1
        output.append(projected)
    ratios=uv_edge_ratios(verts,polys,tuple(output),resolution=resolution)
    if ratios and any(abs(r-1000)>1e-5 for r in ratios):
        return {'state':'UNMAPPED','reason':'DENSITY_ORACLE_FAILED','face_uvs':()}
    return {'state':'READY','reason':'OK','face_uvs':tuple(output),'faces':len(output),
            'schema':UV_SCHEMA,'pixels_per_meter':1000,'resolution':resolution,
            'metric_density_min':min(ratios) if ratios else None,
            'metric_density_max':max(ratios) if ratios else None,
            'surface_families':tuple(sorted(family_count)),
            'face_islands_may_stack':True}
