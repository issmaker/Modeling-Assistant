"""Bounded, source-mesh-independent detection of offscreen WALL paths.

The camera projection callback maps a world point to (x, y) or returns
None for positions behind the viewer. All calculations are read-only.
"""

def _within(x, y, width, height, padding):
    return (-padding <= x <= width + padding
            and -padding <= y <= height + padding)


def _segment_hits_viewport(a, b, width, height, padding):
    """Liang-Barsky clip: avoids false offscreen for long crossing segments."""
    left,bottom=-float(padding),-float(padding)
    right,top=float(width)+padding,float(height)+padding
    x0,y0=map(float,a)
    x1,y1=map(float,b)
    dx,dy=x1-x0,y1-y0
    lo,hi=0.,1.
    for coefficient,delta in (
        (-dx,x0-left),(dx,right-x0),
        (-dy,y0-bottom),(dy,top-y0),
    ):
        if abs(coefficient) < 1.e-12:
            if delta < 0.:return False
            continue
        ratio=delta/coefficient
        if coefficient < 0.:
            lo=max(lo,ratio)
        else:
            hi=min(hi,ratio)
        if lo>hi:return False
    return True


def wall_path_visible(chains, project, width, height, *, padding=10, budget=240):
    """Conservative visibility assessment for authored WALL path chains.

    Return True for unknown/empty geometry (no false 'lost wall' warning).
    Return False only when tested path points and edges miss the region.
    The point budget caps expensive viewport projections on big meshes.
    """
    if width<=0 or height<=0:
        return True
    paths=[]
    for chain in chains:
        points=chain.get('points',())
        if not points:
            continue
        stride=max(1,(len(points)+budget-1)//budget)
        points=list(points[::stride])
        if len(points)<2 and len(chain.get('points',()))>=2:
            points.append(chain['points'][-1])
        paths.append(points)
    if not paths:return True
    remaining=budget
    for points in paths:
        last=None
        for point in points[:max(2,remaining)]:
            projected=project(point)
            if projected is not None:
                x,y=map(float,projected)
                if _within(x,y,width,height,padding):
                    return True
                if last is not None and _segment_hits_viewport(last,(x,y),width,height,padding):
                    return True
                last=(x,y)
            else:
                last=None
        remaining-=min(len(points),max(2,remaining))
        if remaining<=0:
            break
    return False


def return_control_rect(width,height,scale=1.0):
    """One reachable, minimum-sized action in an offscreen Blender viewport."""
    if width<105 or height<100:
        return None
    scale=max(.8,min(2.,float(scale)))
    bw=min(250.*scale,max(96.,width-24.*scale))
    bh=min(40.*scale,max(28.,height*.16))
    x=(float(width)-bw)*.5
    y=max(12.*scale,float(height)-94.*scale)
    y=min(y,float(height)-bh-6.)
    return (x,y,x+bw,y+bh)
