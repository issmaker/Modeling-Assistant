"""Blender-free semantic planner for independent cornice segment junctions.

The authored path is immutable.  Corner overlap means JOINT GROUP, never
silently move/delete the authored vertex to satisfy an isolated joint.
Conservative estimates guide validation; they are NOT Boolean outputs.
"""
from dataclasses import dataclass
from math import atan2, degrees, hypot, tan, radians, isfinite
from typing import Sequence

_EPS = 1e-8


class JointGraphError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(f'{code}: {detail}')


@dataclass(frozen=True)
class Joint:
    vertex: int
    turn_degrees: float
    kind: str
    reach_m: float
    coupled: bool = False


@dataclass(frozen=True)
class Graph:
    path: tuple
    profile: tuple
    side_sign: int
    closed: bool
    lengths: tuple
    joints: tuple
    coupling_groups: tuple
    safe_independent: bool


def _vecs(data, n):
    out = []
    for v in data:
        if len(v) != n or not all(isfinite(float(x)) for x in v):
            raise JointGraphError('BAD_COORDINATE', 'Nonfinite or malformed coordinate')
        out.append(tuple(float(x) for x in v))
    return tuple(out)


def create_joint_graph(path: Sequence, profile: Sequence, *, closed: bool,
                       side_sign: int = 1, reverse_path: bool = False,
                       flip_x: bool = False, flip_z: bool = False,
                       min_segment_m: float = .0021,
                       reserved_fraction: float = .02):
    if side_sign not in (-1, 1) or not 0 <= reserved_fraction < .25:
        raise JointGraphError('OPTIONS', 'Invalid side or safety fraction')
    original = _vecs(path, 3)
    pts = original[:-1] if closed and len(original) > 1 and original[0] == original[-1] else original
    if reverse_path:
        pts = tuple(reversed(pts))
    p = _vecs(profile, 2)
    if len(p) > 2 and p[-1] == p[0]:
        p = p[:-1]
    if len(pts) < (3 if closed else 2) or len(p) < 2:
        raise JointGraphError('TOO_SHORT', 'Insufficient path or profile')
    p = tuple(((-x if flip_x else x), (-z if flip_z else z)) for x, z in p)
    if max(abs(q[0]) for q in p) <= _EPS:
        raise JointGraphError('ZERO_WIDTH', 'Profile has no lateral reach')
    n = len(pts); segs = n if closed else n - 1
    # Non-neighbouring path segments must not cross or share a junction:
    # a self-intersecting polygon has no unambiguous inner/outer profile side.
    def _orient(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def _on(a,b,p):
        return (min(a[0],b[0])-_EPS <= p[0] <= max(a[0],b[0])+_EPS
                and min(a[1],b[1])-_EPS <= p[1] <= max(a[1],b[1])+_EPS
                and abs(_orient(a,b,p)) < 1e-8)
    def _intersects(a,b,c,d):
        x,y,z,w=_orient(a,b,c),_orient(a,b,d),_orient(c,d,a),_orient(c,d,b)
        return ((x*y < -_EPS and z*w < -_EPS) or
                (abs(x) < _EPS and _on(a,b,c)) or
                (abs(y) < _EPS and _on(a,b,d)) or
                (abs(z) < _EPS and _on(c,d,a)) or
                (abs(w) < _EPS and _on(c,d,b)))
    for i in range(segs):
        for j in range(i+1,segs):
            if j==i+1 or (closed and i==0 and j==segs-1):
                continue
            if _intersects(pts[i],pts[(i+1)%n],pts[j],pts[(j+1)%n]):
                raise JointGraphError('PATH_SELF_INTERSECTION',
                                      f'Nonadjacent edges {i} and {j} intersect in XY')
    dirs = []; lengths = []
    for i in range(segs):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b[0]-a[0], b[1]-a[1]
        d = hypot(dx, dy)
        if d < min_segment_m:
            raise JointGraphError('SHORT_SEGMENT', f'Path edge {i}, XY length={d:.6f}')
        dirs.append((dx/d, dy/d)); lengths.append(d)
    max_offset = max(abs(q[0]) for q in p)
    turns = {}; reaches = {}; kinds = {}
    for i in (range(n) if closed else range(1, n-1)):
        prev, nxt = dirs[(i-1) % segs], dirs[i % segs]
        cross = prev[0]*nxt[1] - prev[1]*nxt[0]
        dot = prev[0]*nxt[0] + prev[1]*nxt[1]
        angle = degrees(atan2(cross, dot))
        if abs(angle) > 179.5:
            raise JointGraphError('PATH_REVERSAL', f'Corner {i} is near 180 degrees')
        turns[i] = angle
        # Needed tangent reach for the widest rail in either mirror parity.
        reaches[i] = max_offset * abs(tan(radians(angle/2)))
        kinds[i] = 'INNER' if angle*side_sign > 0 else 'OUTER' if abs(angle) > 1.e-6 else 'STRAIGHT'
    # A segment can only be independently trimmed if both adjacent corner
    # cut zones fit on it, with a reserved unaffected central interval.
    conflicts = []
    for i, length in enumerate(lengths):
        j = (i+1) % n
        reach_start = reaches.get(i, 0.)
        reach_end = reaches.get(j, 0.)
        if reach_start + reach_end >= length * (1-reserved_fraction):
            conflicts.append((i,j))
    # Connected components of overlapping junctions, including closed wrap.
    parent = {j:j for j in reaches}
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]];i=parent[i]
        return i
    for a,b in conflicts:
        if a in parent and b in parent:
            parent[root(a)] = root(b)
    groups = {}
    for j in reaches:
        groups.setdefault(root(j), []).append(j)
    coupled = set()
    for a,b in conflicts:
        coupled.update((a,b))
    components = tuple(tuple(sorted(v)) for v in groups.values() if len(v)>1)
    joins = tuple(Joint(i,turns[i],kinds[i],reaches[i],i in coupled) for i in sorted(reaches))
    return Graph(tuple(pts), p, side_sign, closed, tuple(lengths), joins,
                 tuple(sorted(components)), not bool(coupled))


def graph_diagnostics(graph):
    return {'path_points':len(graph.path),'segments':len(graph.lengths),
            'side':graph.side_sign,'closed':graph.closed,'safe_independent':graph.safe_independent,
            'coupling_groups':[list(g) for g in graph.coupling_groups],
            'joints':[{'vertex':j.vertex,'turn_deg':round(j.turn_degrees,6),
                       'kind':j.kind,'reach_m':round(j.reach_m,6),'coupled':j.coupled}
                      for j in graph.joints]}
