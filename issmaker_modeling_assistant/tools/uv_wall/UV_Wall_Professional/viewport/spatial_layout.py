"""Deterministic, Blender-independent spatial overlay geometry and layout.

Every card belongs to a projected handle/semantic anchor. Projection is owned by
controller; this module only solves screen placement and is side-effect free.
"""
from math import isfinite


def polygon_area(points):
    points = list(points)
    if len(points) < 3:
        return 0.0
    return 0.5 * sum(p[0]*q[1] - q[0]*p[1]
                     for p,q in zip(points,points[1:]+points[:1]))


def inside_polygon(point, points):
    x,y=point;inside=False
    for a,b in zip(points,points[1:]+points[:1]):
        if ((a[1] > y) != (b[1] > y)):
            xx=(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]
            if x < xx:inside=not inside
    return inside


def interior_xy(points):
    """Return a point in a simple ring, including concave rings.

    No fragile 'vertex average' assumption. The scanline fallback is used when
    the area centroid lies in the exterior of a concave ring.
    """
    pts=[(float(x),float(y)) for x,y in points]
    if len(pts)<3:return None
    a=polygon_area(pts)
    if abs(a)<1.e-10:return None
    cx=sum((p[0]+q[0])*(p[0]*q[1]-q[0]*p[1]) for p,q in zip(pts,pts[1:]+pts[:1]))/(6*a)
    cy=sum((p[1]+q[1])*(p[0]*q[1]-q[0]*p[1]) for p,q in zip(pts,pts[1:]+pts[:1]))/(6*a)
    if inside_polygon((cx,cy),pts):return (cx,cy)
    # Scan the widest interior horizontal interval, avoiding polygon edges.
    min_y,max_y=min(y for _,y in pts),max(y for _,y in pts)
    best=None
    for i in range(1,32):
        y=min_y+(max_y-min_y)*(i/32)
        xs=[]
        for (ax,ay),(bx,by) in zip(pts,pts[1:]+pts[:1]):
            if (ay <= y < by) or (by <= y < ay):
                xs.append(ax+(y-ay)*(bx-ax)/(by-ay))
        xs.sort()
        for l,r in zip(xs[::2],xs[1::2]):
            if r-l>1.e-8 and (best is None or r-l>best[0]):
                best=(r-l,(l+r)*.5,y)
    return (best[1],best[2]) if best else None


def intersection_area(a,b):
    return max(0.0,min(a[2],b[2])-max(a[0],b[0]))*max(0.0,min(a[3],b[3])-max(a[1],b[1]))


def stable_segment(chains):
    """Choose a semantic segment invariant under cyclic start-vertex changes.

    The longest non-degenerate XY segment wins, with deterministic world-space
    tie-breaking. Return original endpoint vectors (orientation is preserved
    for wall-side drag behavior), rather than mesh face/vertex indices.
    """
    winner = None
    for chain in chains or ():
        pts = chain.get("points", ())
        for a, b in zip(pts, pts[1:]):
            dx, dy = float(b.x-a.x), float(b.y-a.y)
            d2 = dx*dx + dy*dy
            if not isfinite(d2) or d2 <= 1.e-12:
                continue
            ends = tuple(sorted(((round(float(a.x), 6),round(float(a.y), 6)),
                                 (round(float(b.x), 6),round(float(b.y), 6)))))
            score = (-round(d2, 8), ends)
            if winner is None or score < winner[0]:
                winner = (score, (a,b))
    return None if winner is None else winner[1]



# One source of truth for the visual emphasis of geometry-anchored cards.
# This module deliberately has no Blender/GPU dependency or model mutations.
CARD_EDITOR = {
    "W_HEIGHT": "HEIGHT", "W_THICKNESS": "THICKNESS", "W_CAP": "CAP",
    "W_PLANT": "PLANT", "W_WALL": "WALL", "S_Z": "SURFACE",
    "S_ROT": "SURFACE", "S_SETTINGS": "SURFACE",
    "C_PROFILE": "CORNICE", "C_ROT": "CORNICE", "C_SETTINGS": "CORNICE",
}
DRAG_CARD = {"HEIGHT": "W_HEIGHT", "BOTTOM_LEVEL": "W_HEIGHT",
             "THICKNESS": "W_THICKNESS", "OFFSET": "W_WALL",
             "SURFACE_Z": "S_Z"}


def card_attention(key, hover=None, open_editors=(), focused_editor=None, drag=None):
    """Return focused / hover / open / quiet / idle without changing hitboxes.

    Hover is never confused with an editor: focused has a distinct border and
    a solid leading accent, while hover is a lightweight outline. Non-focused
    cards remain interactive, even while a sibling editor is open.
    """
    if DRAG_CARD.get(drag) == key:
        return "focused"
    editor = CARD_EDITOR.get(key)
    if editor is not None and editor == focused_editor:
        return "focused"
    if hover == key:
        return "hover"
    if editor is not None and editor in open_editors:
        return "open"
    if focused_editor is not None or drag is not None:
        return "quiet"
    return "idle"


def tether_distance(anchor, rect):
    """Pixel distance between a projected world anchor and its card."""
    from math import hypot
    ax,ay=map(float,anchor)
    x0,y0,x1,y1=map(float,rect)
    return hypot(ax-max(x0,min(ax,x1)), ay-max(y0,min(ay,y1)))

def layout_cards(cards, region, obstacles=(), previous=None):
    """Place cards around projected geometry without covering modal editors.

    Constraints: finite anchors, fully visible cards, deterministic priority,
    no major collision with handles/windows/cards. If no sensible space remains,
    *hide* the lower-priority label instead of covering an editor's controls.
    previous uses anchor-relative coordinates to prevent navigation jitter.
    """
    rw, rh = map(float, region)
    if not all(isfinite(v) for v in (rw,rh)) or rw < 165 or rh < 100:
        return {}
    previous = previous or {}
    occupied = []
    for rect in obstacles:
        if len(rect) == 4 and all(isfinite(float(v)) for v in rect):
            occupied.append(tuple(map(float,rect)))
    placed = {}
    for key,anchor,size,priority,offset in sorted(cards,key=lambda c:(-c[3],str(c[0]))):
        if anchor is None:
            continue
        ax,ay = map(float,anchor)
        w,h = map(float,size)
        ox,oy = map(float,offset)
        if not all(isfinite(v) for v in (ax,ay,w,h,ox,oy)):
            continue
        if ax < 0 or ay < 0 or ax >= rw or ay >= rh or w <= 0 or h <= 0:
            continue
        w = min(w,rw-12.0);h = min(h,rh-12.0)
        if w <= 0 or h <= 0:
            continue
        proposed = [(ax+ox,ay+oy),(ax-w-20,ay+oy),
                    (ax+ox,ay-h-18),(ax-w-20,ay-h-18),
                    (ax-w*.5,ay+24),(ax-w*.5,ay-h-24),
                    (ax-w*.5,ay+h+28),(ax-w*.5,ay-h-38)]
        prior = previous.get(key)
        if prior:
            old_rect,old_anchor = prior
            if all(isfinite(float(v)) for v in (*old_rect,*old_anchor)):
                proposed.insert(0,(old_rect[0]+ax-old_anchor[0],
                                   old_rect[1]+ay-old_anchor[1]))
        best, best_score, best_overlap = None, float('inf'), float('inf')
        for index,(px,py) in enumerate(proposed):
            x = max(6.,min(px,rw-w-6.))
            y = max(6.,min(py,rh-h-6.))
            rect = (x,y,x+w,y+h)
            # Leave breathing room for other cards, editor windows and gizmos;
            # collision boxes include a 6 px safety gutter, but the actual
            # rendered and clickable rectangles are never enlarged.
            gap=6.0
            padded=(x-gap,y-gap,x+w+gap,y+h+gap)
            overlap = sum(intersection_area(padded,o) for o in occupied)
            reach=tether_distance((ax,ay),rect)
            shift = abs(x-(ax+ox))*.15+abs(y-(ay+oy))*.15
            # A visible label is useful only near the related geometry.
            # Avoid choosing a distant empty viewport corner just to dodge
            # a small overlay. The previous-anchor preference remains.
            reach_penalty=max(0.0,reach-75.0)*1.2
            score = overlap*120+shift+reach_penalty+index*.14
            if score < best_score:
                best_score, best_overlap, best = score, overlap, rect
        if best is None:
            continue
        if best_overlap > (w*h)*.22 or (priority <= 2 and tether_distance((ax,ay),best)>190.):
            # A hidden card still has a clickable 3D gizmo and can reappear as
            # soon as the user moves the camera or closes overlapping windows.
            continue
        placed[key]=best
        occupied.append(best)
    return placed


def semantic_edge_samples(chains, max_samples=40):
    """Stable source-edge midpoints; bounded projection count per viewport frame.

    Sort by geometric position instead of mesh element ID or Path start vertex.
    The sampled set is invariant to cyclic path-start changes and winding.
    Call once per *analysis revision*, not on camera navigation frames.
    """
    mids={}
    for chain in chains or ():
        pts=chain.get('points',())
        for a,b in zip(pts,pts[1:]):
            dx=float(a.x-b.x);dy=float(a.y-b.y)
            if dx*dx+dy*dy < 1e-10:continue
            mid=(a+b)*.5
            key=(round(float(mid.x),6),round(float(mid.y),6),round(float(mid.z),6))
            mids[key]=mid
    entries=[mids[k] for k in sorted(mids)]
    if not entries or max_samples<=0:return []
    if len(entries)<=max_samples:return entries
    if max_samples==1:return [entries[len(entries)//2]]
    return [entries[i*(len(entries)-1)//(max_samples-1)] for i in range(max_samples)]


def choose_semantic_anchor(candidates, avoid, region, previous=None):
    """Select a visible segment for a *label*, distinct from the active gizmo.

    Candidates are already projected semantic Path segment midpoints, never
    generated mesh polygon indices. Keep the previously selected segment when
    moving the view a little, and prefer segments apart from other controls.
    A None return permits a safe fallback on tiny/occluded viewports.
    """
    import math
    rw, rh = map(float, region)
    if rw < 170 or rh < 115:
        return None
    reserved = [tuple(map(float, p)) for p in avoid if p is not None and len(p) == 2
                and all(isfinite(float(v)) for v in p)]
    valid = []
    for index, candidate in enumerate(candidates):
        if candidate is None or len(candidate) != 2:
            continue
        cx,cy = map(float, candidate)
        if not all(isfinite(v) for v in (cx,cy)):
            continue
        if not (24. <= cx <= rw-24. and 32. <= cy <= rh-34.):
            continue
        spacing = min((math.hypot(cx-x,cy-y) for x,y in reserved),default=rw*.25)
        # Avoid placing the label at the viewport perimeter, even where the
        # segment itself is partly visible. Values measured in screen pixels.
        border = min(cx,rw-cx,cy,rh-cy)
        score = min(spacing,370.) + min(border,110.)*.30
        if previous is not None and all(isfinite(float(v)) for v in previous):
            distance = math.hypot(cx-previous[0],cy-previous[1])
            if distance < 36:
                score += 30 * (1-distance/36)
        valid.append((score,-index,(cx,cy)))
    return max(valid,default=(None,None,None))[2]


# The camera-dependent selector works on original authored path edges, not on
# generated wall polygons (whose indices change after every rebuild).
def segment_identity(a,b):
    return tuple(sorted(((round(float(a.x),5),round(float(a.y),5),round(float(a.z),5)),
                         (round(float(b.x),5),round(float(b.y),5),round(float(b.z),5)))))


def camera_segment(chains, project, camera_location, view_center, current_key=None,
                   lock=False, max_candidates=640):
    """Pick a camera-near, screen-usable visible-side segment with hysteresis.

    `project` returns a 2d position or None; `camera_location` is world-space.
    A coarse facing test rejects back-facing closed-loop sections. Full GPU
    depth occlusion is left to Blender field verification; never claim raycast.
    """
    from math import sqrt
    candidates=[]
    chains=chains or ()
    count=sum(max(0,len(c.get("points",()))-1) for c in chains)
    stride=max(1,(count+max_candidates-1)//max_candidates)
    ordinal=0
    for chain in chains:
        pts=chain.get('points',())
        closed=bool(chain.get('closed'))
        poly=pts[:-1] if closed and len(pts)>2 else pts
        area=(chain.get("signed_xy_area") if "signed_xy_area" in chain else
              polygon_area([(p.x,p.y) for p in poly])) if closed else 0.
        for a,b in zip(pts,pts[1:]):
            if ordinal % stride:
                ordinal+=1
                continue
            ordinal+=1
            dx=float(b.x-a.x);dy=float(b.y-a.y)
            len_sq=dx*dx+dy*dy
            if len_sq<1.e-10:continue
            candidates.append((segment_identity(a,b),a,b,dx,dy,area,closed))
    if not candidates:return None
    # A hard upper bound avoids Python allocations proportional to massive
    # CAD contours on *every* frame. Downsampling is deterministic.
    if len(candidates)>max_candidates:
        step=(len(candidates)+max_candidates-1)//max_candidates
        candidates=candidates[::step]
    best=None
    last=None
    vx,vy=map(float,view_center)
    for key,a,b,dx,dy,area,closed in candidates:
        p0=project(a);p1=project(b)
        if p0 is None or p1 is None:continue
        if not all(isfinite(float(v)) for v in (*p0,*p1)):continue
        # Clipping heuristic: avoid selecting an edge that cannot be interacted
        # with because its entire projected bounding interval is offscreen.
        if max(p0[0],p1[0]) < -20 or min(p0[0],p1[0]) > 2*vx+20:continue
        if max(p0[1],p1[1]) < -20 or min(p0[1],p1[1]) > 2*vy+20:continue
        ex=float(p1[0]-p0[0]);ey=float(p1[1]-p0[1]);screen_length=sqrt(ex*ex+ey*ey)
        if screen_length<12.:continue
        midpoint=a.lerp(b,0.5)
        px=(p0[0]+p1[0])*0.5;py=(p0[1]+p1[1])*0.5
        dist_screen=sqrt((px-vx)**2+(py-vy)**2)
        cam_delta=camera_location-midpoint
        camera_dist=max(.01,float(cam_delta.length))
        # In CCW loops interior is left; exterior is right; reversed in CW.
        outward_sign=-1 if area>=0 else 1
        facing=(dy*(-outward_sign)*cam_delta.x+dx*outward_sign*cam_delta.y)
        # If world camera is above, only gently penalize rear-facing pieces;
        # do not lose all handles in nearly top-down orthographic views.
        face_penalty=0. if not closed or facing>=0 else 250.
        score=(min(screen_length,320.)*.12 - camera_dist*4. - dist_screen*.06 -face_penalty)
        item=(key,a,b,score)
        if key==current_key:last=item
        if best is None or score>best[3] or (score==best[3] and key<best[0]):best=item
    if lock and last is not None:return last
    if best is None:return None
    # Switch only if advantage exceeds a stable absolute screen-space margin;
    # no oscillation between equally good sides while orbiting slowly.
    if last is not None and best[3]-last[3]<14.:return last
    return best


def floating_quick_rect(mouse,region,width=270.,height=116.):
    """Clamp a quick-mode menu to actual WINDOW-region coordinates."""
    rw,rh=region
    w=min(width,max(100.,rw-12.));h=min(height,max(56.,rh-12.))
    return (max(6.,min(mouse[0]-w*.5,rw-w-6.)),
            max(6.,min(mouse[1]-h*.5,rh-h-6.)),w,h)
