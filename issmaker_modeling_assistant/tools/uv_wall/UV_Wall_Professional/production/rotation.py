"""Finite, face-local rotated modular clipping for Wall (along-path × world-Z).

No cap-to-wall vertical propagation. Coordinates and UV are clipped to the 2D
orthogonal module grid. Blender vectors enter/leave this boundary module;
array creation and scene mutation remain with the production generators.
"""
from math import cos, sin, floor


def rotated_wall_cells(points, segment_start, segment_end, distance_start,
                       story_anchor_z, tile_length, angle_rad, min_area=1e-9):
    """Yield (world polygon, normalized UV factors) for a wall slab polygon.

    Coordinates are interpolated on existing planar wall faces; clipping is
    local to the slab, never a global wall station. The strict area test avoids
    degenerate faces along grid boundaries.
    """
    length = ((segment_end.x - segment_start.x)**2 +
              (segment_end.y - segment_start.y)**2)**0.5
    if length < 1e-10:
        return
    direction = ((segment_end.x-segment_start.x)/length,
                 (segment_end.y-segment_start.y)/length)
    c, s = cos(angle_rad), sin(angle_rad)
    original=[]
    for point in points:
        d = distance_start + ((point.x - segment_start.x)*direction[0] +
                              (point.y - segment_start.y)*direction[1])
        h = point.z - story_anchor_z
        original.append((point, c*d+s*h, -s*d+c*h))
    def clip(poly, component, edge, keep_greater):
        if not poly:
            return []
        output=[]
        def inside(v):
            return v>=edge-1e-8 if keep_greater else v<=edge+1e-8
        for prev, cur in zip(poly[-1:]+poly[:-1],poly):
            pv,cv=prev[component],cur[component]
            pi,ci=inside(pv),inside(cv)
            if pi != ci and abs(cv-pv)>1e-12:
                t = max(0.0,min(1.0,(edge-pv)/(cv-pv)))
                position=prev[0].lerp(cur[0],t)
                output.append((position,prev[1]+(cur[1]-prev[1])*t,
                               prev[2]+(cur[2]-prev[2])*t))
            if ci:
                output.append(cur)
        return output
    min_u,min_v=min(p[1] for p in original),min(p[2] for p in original)
    max_u,max_v=max(p[1] for p in original),max(p[2] for p in original)
    lo_u,hi_u=floor(min_u/tile_length),floor((max_u-1e-8)/tile_length)
    lo_v,hi_v=floor(min_v/tile_length),floor((max_v-1e-8)/tile_length)
    if (hi_u-lo_u+1)*(hi_v-lo_v+1)>10000:
        raise ValueError("Rotated wall grid exceeds per-face safety limit")
    for ui in range(lo_u,hi_u+1):
        low_u=ui*tile_length
        high_u=low_u+tile_length
        half=clip(clip(original,1,low_u,True),1,high_u,False)
        for vi in range(lo_v,hi_v+1):
            low_v=vi*tile_length
            high_v=low_v+tile_length
            poly=clip(clip(half,2,low_v,True),2,high_v,False)
            if len(poly)<3:
                continue
            area2=0.0
            # Projected 2D area is nonzero for valid, finite planar polygons.
            for a,b in zip(poly,poly[1:]+poly[:1]):
                area2+=a[1]*b[2]-b[1]*a[2]
            if abs(area2)*0.5<min_area:
                continue
            uv=[(max(0.,min(1.,(p[1]-low_u)/tile_length)),
                 max(0.,min(1.,(p[2]-low_v)/tile_length))) for p in poly]
            yield [p[0] for p in poly],uv
