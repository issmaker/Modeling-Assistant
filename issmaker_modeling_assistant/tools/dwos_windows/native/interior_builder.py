from __future__ import annotations


def _cross(a,b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _sub(a,b):
    return (a[0]-b[0],a[1]-b[1],a[2]-b[2])


def _dot(a,b):
    return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def build_interior_reveals(rects, z_start, z_end):
    """Build four reveal surfaces per clear aperture.

    Normals point into the clear opening regardless of which depth direction is interior.
    """
    verts=[]; faces=[]; roles=[]
    for x0,x1,y0,y1 in rects:
        # endpoints + desired inward normal
        sides=[
            ((x0,y0),(x1,y0),(0,1,0)),   # bottom -> +Y
            ((x1,y0),(x1,y1),(-1,0,0)),  # right  -> -X
            ((x1,y1),(x0,y1),(0,-1,0)),  # top    -> -Y
            ((x0,y1),(x0,y0),(1,0,0)),   # left   -> +X
        ]
        for a,b,desired in sides:
            pts=[(a[0],a[1],z_start),(b[0],b[1],z_start),(b[0],b[1],z_end),(a[0],a[1],z_end)]
            i=len(verts); verts += pts
            n=_cross(_sub(pts[1],pts[0]),_sub(pts[2],pts[0]))
            face=(i,i+1,i+2,i+3)
            if _dot(n,desired)<0:
                face=tuple(reversed(face))
            faces.append(face); roles.append('INTERIOR_REVEAL')
    return verts,faces,roles
