"""Pure planar DWOS facade grid: one 2D arrangement, no stacked faces.

Only PLANAR geometry is certified here. No implicit projection of CORNER or
CURVED windows to a plane is permitted. All coordinates are local metric units.
"""
from __future__ import annotations

from math import isfinite

try:
    from .frame_core import frame_grid
except ImportError:
    from frame_core import frame_grid


class LPUnsupported(ValueError):
    """Unsupported geometry rather than an invented/incorrect LP."""


def build_planar_lp(width, height, frame_width, splits, *, front=0.0,
                    zone_tree=None, outward_sign=-1, mapper=None,
                    cell_limit=100000):
    values = (width, height, frame_width, front)
    if not all(isfinite(float(x)) for x in values):
        raise ValueError('Non-finite LP parameters')
    if width <= 0 or height <= 0 or frame_width <= 0 or 2*frame_width >= min(width, height):
        raise ValueError('Invalid frame dimensions')
    if outward_sign not in (-1, 1):
        raise ValueError('Invalid normal sign')
    xs, ys, occupied = frame_grid(width, height, frame_width, splits, zone_tree)
    if len(occupied) > cell_limit:
        raise LPUnsupported('LP_GRID_LIMIT')
    coords=[]
    metric_params=[]
    vertex_map={}
    faces=[]
    roles=[]
    face_keys=set()
    for ix in range(len(xs)-1):
        for iy in range(len(ys)-1):
            if xs[ix+1]-xs[ix]<=1.e-8 or ys[iy+1]-ys[iy]<=1.e-8:
                raise ValueError('Degenerate LP grid cell')
            indices=[]
            for key in ((ix,iy),(ix+1,iy),(ix+1,iy+1),(ix,iy+1)):
                if key not in vertex_map:
                    x,y=xs[key[0]],ys[key[1]]
                    p=(x,y,float(front)) if mapper is None else tuple(map(float,mapper(x,y,float(front))))
                    if len(p)!=3 or not all(isfinite(v) for v in p):
                        raise ValueError('LP mapper returned invalid vertex')
                    vertex_map[key]=len(coords)
                    coords.append(p)
                    metric_params.append((x,y))
                indices.append(vertex_map[key])
            if outward_sign<0:
                indices.reverse()
            face_key=frozenset(indices)
            if face_key in face_keys:
                raise ValueError('Duplicate LP cell')
            face_keys.add(face_key)
            faces.append(tuple(indices))
            roles.append('FRAME' if occupied[(ix,iy)] else 'GLASS')
    edge_uses={}
    for face in faces:
        for a,b in zip(face,face[1:]+face[:1]):
            edge=(a,b) if a<b else (b,a)
            edge_uses[edge]=edge_uses.get(edge,0)+1
            if edge_uses[edge]>2:
                raise ValueError('Nonmanifold LP cell edge')
    coverage=sum((xs[ix+1]-xs[ix])*(ys[iy+1]-ys[iy])
                 for ix in range(len(xs)-1) for iy in range(len(ys)-1))
    if abs(coverage-width*height)>1.e-7*max(1.,width*height):
        raise ValueError('LP grid coverage is incomplete')
    return {'verts':tuple(coords),'faces':tuple(faces),'roles':tuple(roles),
            'face_count':len(faces),'frame_faces':roles.count('FRAME'),
            'glass_faces':roles.count('GLASS'),'coverage_m2':coverage,
            'metric_params':tuple(metric_params),
            'version':'DWOS_LP_PLANAR_SHARED_GRID_V1'}
