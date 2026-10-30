"""Safe Blender-free helpers for full-path, non-committal UI ghost guides.

Preview guides are not production topology. They never replace/edit source meshes.
"""
import math


def central_segment(chains, project=None):
    """Choose a middle-by-arc-length segment, never an endpoint or world Z=0."""
    segments = []
    for chain in chains:
        points = list(chain.get('points', ()))
        for a, b in zip(points, points[1:]):
            length = math.dist(tuple(a), tuple(b))
            if length > 1.e-8:
                segments.append((a, b, length))
    if not segments:
        return None
    length_sum = sum(s[2] for s in segments)
    walked = 0.
    choices = []
    for a, b, length in segments:
        t = min(1., max(0., (length_sum*.5 - walked) / length))
        mid = tuple(float(x)*(1-t) + float(y)*t for x,y in zip(a,b))
        screen = project(mid) if project is not None else mid
        if screen is not None:
            choices.append((abs((walked+length*.5) / length_sum-.5),mid,screen))
        walked += length
    return min(choices,key=lambda row:row[0])[1] if choices else None


def _point_in_poly(x,y,poly):
    inside=False
    for (ax,ay),(bx,by) in zip(poly,poly[1:]+poly[:1]):
        if (ay>y)!=(by>y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:
            inside=not inside
    return inside


def surface_rotation_ghost(chains, degrees, *, spacing=2., max_lines=140):
    """Whole closed-path rotation guides, clipped to contour, including concave outlines.

    Works on WORLD XY, z per chain; guide lines only (no triangulation/mesh writes).
    """
    if not math.isfinite(float(degrees)) or spacing<=0:
        return []
    polygons=[]
    for chain in chains:
        if not chain.get('closed'):continue
        pts=[tuple(p) for p in chain.get('points',())]
        if len(pts)>3 and math.dist(pts[0],pts[-1])<1e-7:pts.pop()
        if len(pts)>=3: polygons.append(pts)
    if not polygons:return []
    result=[]
    angle=math.radians(float(degrees));c,s=math.cos(angle),math.sin(angle)
    for pts in polygons:
        if len(result)>=max_lines:break
        center=(sum(p[0] for p in pts)/len(pts),sum(p[1] for p in pts)/len(pts))
        # Local coordinates rotated with the *module* orientation, but all
        # visible endpoints remain clipped to the actual authored perimeter.
        poly=[((p[0]-center[0])*c+(p[1]-center[1])*s,
               -(p[0]-center[0])*s+(p[1]-center[1])*c) for p in pts]
        xmin=min(x for x,y in poly);xmax=max(x for x,y in poly)
        ymin=min(y for x,y in poly);ymax=max(y for x,y in poly)
        z=sum(p[2] for p in pts)/len(pts)+0.035
        for axis in (0,1):
            lo,hi=(xmin,xmax) if axis==0 else (ymin,ymax)
            count=min(36,max(0,int(math.ceil((hi-lo)/spacing))))
            for idx in range(count+1):
                if len(result)>=max_lines:break
                constant=(math.floor(lo/spacing)+idx)*spacing
                if constant<lo or constant>hi:continue
                crossings=[]
                for (ax,ay),(bx,by) in zip(poly,poly[1:]+poly[:1]):
                    aa,bb=(ax,bx) if axis==0 else (ay,by)
                    if abs(bb-aa)<1.e-8:continue
                    t=(constant-aa)/(bb-aa)
                    if 0<=t<1:
                        crossings.append(ay+t*(by-ay) if axis==0 else ax+t*(bx-ax))
                crossings.sort()
                for k in range(0,len(crossings)-1,2):
                    first,second=crossings[k:k+2]
                    if second-first<1.e-6:continue
                    coords=[(constant,first),(constant,second)] if axis==0 else [(first,constant),(second,constant)]
                    world=[]
                    for u,v in coords:
                        world.append((center[0]+u*c-v*s,center[1]+u*s+v*c,z))
                    result.append(tuple(world))
                    if len(result)>=max_lines:break
    return result


def cornice_full_path_ghost(chains, profile, degrees, *, flip=False, flip_x=False, flip_z=False, reverse_path=False, max_edges=2500):
    """Non-destructive full polyline section sweep guide (corners are provisional).

    Each segment uses its own frame, deliberately does not invent sharp-joint trim.
    Real production validation MUST run before commit.
    """
    if not profile or len(profile)>512 or not math.isfinite(float(degrees)):
        return []
    angle=math.radians(float(degrees));ca,sa=math.cos(angle),math.sin(angle)
    lines=[]
    for chain in chains:
        pts=[tuple(p) for p in chain.get('points',())]
        if len(pts)<2:continue
        if reverse_path:pts.reverse()
        for a,b in zip(pts,pts[1:]):
            dx,dy,dz=(b[i]-a[i] for i in range(3))
            length=math.sqrt(dx*dx+dy*dy+dz*dz)
            horizontal=math.hypot(dx,dy)
            if length<1.e-8 or horizontal<1.e-8:continue
            normal=(-dy/horizontal,dx/horizontal)
            if flip:normal=(-normal[0],-normal[1])
            rings=[]
            for center in (a,b):
                ring=[]
                for px,pz in profile:
                    px=(-px if flip_x else px);pz=(-pz if flip_z else pz)
                    lateral=px*ca-pz*sa;vertical=px*sa+pz*ca
                    ring.append((center[0]+normal[0]*lateral,
                                 center[1]+normal[1]*lateral,center[2]+vertical))
                rings.append(ring)
                lines.extend((p,q) for p,q in zip(ring,ring[1:]))
            lines.extend(zip(*rings))
            if len(lines)>=max_edges:return lines[:max_edges]
    return lines


def place_info_popup(point, size, region, obstacles=(), margin=7):
    """Reserve a selected-point information card without hiding important HUDs."""
    x,y=point; w,h=size; W,H=region
    candidates=((x+18,y+12),(x-w-18,y+12),(x+18,y-h-18),(x-w-18,y-h-18))
    for cx,cy in candidates:
        if cx<margin or cy<margin or cx+w>W-margin or cy+h>H-margin:continue
        rect=(cx,cy,cx+w,cy+h)
        if all(not (rect[0]<r[2] and rect[2]>r[0] and rect[1]<r[3] and rect[3]>r[1]) for r in obstacles if r):
            return rect
    return None


def center_height_anchor(chains,center):
    """Closest point of any authored segment to object's XY center; local interpolated Z."""
    cx,cy=float(center[0]),float(center[1]);best=None
    for chain in chains:
        pts=list(chain.get('points',()))
        for a,b in zip(pts,pts[1:]):
            ax,ay,az=map(float,a);bx,by,bz=map(float,b)
            dx,dy=bx-ax,by-ay;length2=dx*dx+dy*dy
            if length2<1e-12:continue
            t=max(0.,min(1.,((cx-ax)*dx+(cy-ay)*dy)/length2))
            x=ax+dx*t;y=ay+dy*t;z=az+(bz-az)*t
            key=(x-cx)**2+(y-cy)**2
            if best is None or key<best[0]:best=(key,(x,y,z))
    return best[1] if best else None


def bbox_center(points):
    """Visual center independent of point density and point insertion order."""
    pts = [tuple(float(v) for v in point[:3]) for point in points]
    if not pts or any(len(v)<3 for v in pts):
        return None
    if not all(math.isfinite(x) for v in pts for x in v):
        return None
    return tuple((min(v[i] for v in pts) + max(v[i] for v in pts)) * 0.5 for i in range(3))
