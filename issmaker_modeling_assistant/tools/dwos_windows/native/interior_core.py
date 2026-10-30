"""Pure-python production interior envelope; zero-safe, no Blender dependencies."""
from __future__ import annotations
import math

TOL = 1e-7
MAX_INTERIOR_DEPTH_M = 1.5  # strictly enforced at every mesh-generation entry point

def validate_interior(rect, start, depth, direction, clearance, mode, side_offsets=(0,0,0,0)):
    # Read-only legacy REVEALS support: new UI exposes only OFF/BOX.
    if mode not in {'OFF', 'REVEALS', 'BOX'}:
        raise ValueError('Неизвестный режим Interior')
    if mode == 'OFF':
        return None
    x0,x1,y0,y1 = rect
    values = (x0,x1,y0,y1,start,depth,direction,clearance,*side_offsets)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError('Interior: размеры должны быть конечными')
    if direction not in (-1,1):
        raise ValueError('Interior: неверное направление')
    if depth <= TOL:
        raise ValueError('Глубина Interior должна быть больше нуля')
    if depth > MAX_INTERIOR_DEPTH_M + TOL:
        raise ValueError('Глубина Interior не может превышать 1,5 м')
    if clearance < 0 or clearance >= min(x1-x0,y1-y0)*0.5 - TOL:
        raise ValueError('Interior: отступ закрывает чистый проём')
    if len(side_offsets)!=4:
        raise ValueError('Interior: нужны отступы четырёх сторон')
    left,right,bottom,top=side_offsets
    rect2=(x0+clearance+left,x1-clearance-right,
           y0+clearance+bottom,y1-clearance-top)
    if rect2[1]-rect2[0] <= TOL or rect2[3]-rect2[2] <= TOL:
        raise ValueError('Interior: нулевая площадь проёма')
    end = start + direction * depth
    if abs(end - start) <= TOL:
        raise ValueError('Interior: нулевая глубина создаёт вырожденные грани')
    return rect2, end


def build_interior_mesh(rect, start, depth, direction, clearance=0.0, mode='BOX', side_offsets=(0,0,0,0)):
    checked=validate_interior(rect,start,depth,direction,clearance,mode,side_offsets)
    if checked is None:
        return [], [], []
    (x0,x1,y0,y1), end=checked
    verts=[(x0,y0,start),(x1,y0,start),(x1,y1,start),(x0,y1,start),
           (x0,y0,end),(x1,y0,end),(x1,y1,end),(x0,y1,end)]
    # Front edge direction is clockwise/CCW adjusted for inward normals.
    # Derived by checking face winding against desired aperture-facing normal.
    desired=((0,1,0),(-1,0,0),(0,-1,0),(1,0,0))
    faces=[]; roles=[]
    for k in range(4):
        n=(k+1)%4
        face=(k,n,n+4,k+4)
        a,b,c=(verts[i] for i in face[:3])
        ab=[b[i]-a[i] for i in range(3)]
        ac=[c[i]-a[i] for i in range(3)]
        normal=(ab[1]*ac[2]-ab[2]*ac[1],ab[2]*ac[0]-ab[0]*ac[2],ab[0]*ac[1]-ab[1]*ac[0])
        if sum(normal[i]*desired[k][i] for i in range(3))<0:
            face=tuple(reversed(face))
        faces.append(face);roles.append('INTERIOR_REVEAL')
    if mode=='BOX':
        # The BOX is a single *open-front cavity*.  Reveal normals point into
        # that cavity; its rear cap must therefore face back toward the open
        # aperture (-direction).  The former +direction cap had the SAME
        # directed boundary edges as every side face (four inverted seams).
        # Matching winding is critical for weld, baking and Blender face
        # orientation display, even though this is an open, non-solid shell.
        cap=(7,6,5,4) if direction>0 else (4,5,6,7)
        faces.append(cap); roles.append('INTERIOR_BACK')
    return verts,faces,roles
