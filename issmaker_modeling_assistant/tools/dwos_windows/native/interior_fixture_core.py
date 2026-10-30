"""Deterministic five-wall / ten-opening Smart Interior diagnostic fixture gallery.

Pure Python. Wall patches are disconnected within ONE Mesh object, but each case
is a connected SURFACE sheet with two genuine, topologically bounded holes.
The projected overlap is a *challenge*, not a claim the solver repaired meshes.
"""
from __future__ import annotations
from collections import Counter, defaultdict, deque
from math import cos, sin, radians, hypot, isfinite

SCHEMA_INTERIOR = 'DWOS_fixture_interior_contact_v1'


def scenario_specs():
    return [
      dict(id='I01', title='Parallel / narrow U', scenario='PARALLEL_NARROW', depth_m=1.0,
           opening_bottom_m=.85, opening_height_m=1.75,
           path=[(0,0),(.7,0),(3.0,0),(3.8,0),(3.8,1.20),(3.0,1.20),(.7,1.20),(0,1.20)],
           holes=((1,2),(5,6))),
      dict(id='I02', title='Parallel / staggered', scenario='PARALLEL_STAGGERED', depth_m=1.22,
           opening_bottom_m=.72, opening_height_m=2.0,
           path=[(0,0),(.6,0),(3.15,0),(4.2,0),(4.2,1.70),(3.60,1.70),(1.20,1.70),(.2,1.70)],
           holes=((1,2),(5,6))),
      dict(id='I03', title='L corner / 90 deg', scenario='RIGHT_ANGLE', depth_m=1.40,
           opening_bottom_m=.8, opening_height_m=1.9,
           path=[(0,0),(1.9,0),(3.5,0),(4.0,0),(4.0,.38),(4.0,2.05),(4.0,2.9)],
           holes=((1,2),(4,5))),
      dict(id='I04', title='Acute wall / 55 deg', scenario='ACUTE_55', depth_m=1.40,
           opening_bottom_m=.88, opening_height_m=1.8,
           path=_make_angled(55, a_start=1.8,a_end=3.45,b_start=.38,b_end=2.0),
           holes=((1,2),(4,5))),
      dict(id='I05', title='Obtuse wall / 120 deg', scenario='OBTUSE_120', depth_m=1.40,
           opening_bottom_m=.95, opening_height_m=1.65,
           path=_make_angled(120,a_start=2.15,a_end=3.62,b_start=.30,b_end=1.92),
           holes=((1,2),(4,5)))
    ]


def _make_angled(internal_angle, *, a_start,a_end,b_start,b_end):
    turn=radians(180-internal_angle)
    def at(t):return (4+cos(turn)*t,sin(turn)*t)
    return [(0,0),(a_start,0),(a_end,0),(4,0),at(b_start),at(b_end),at(2.9)]


def _polygon_area(poly):
    return abs(sum(x*poly[(i+1)%len(poly)][1]-poly[(i+1)%len(poly)][0]*y
                   for i,(x,y) in enumerate(poly)))*.5 if len(poly)>2 else 0.


def _left_interior_polygon(a,b,depth):
    dx=b[0]-a[0];dy=b[1]-a[1];size=hypot(dx,dy)
    if size<1e-8:raise ValueError('Zero-length window boundary')
    nx,ny=-dy/size*depth,dx/size*depth
    return [a,b,(b[0]+nx,b[1]+ny),(a[0]+nx,a[1]+ny)]


def _clip_convex(subject, clip):
    """Sutherland–Hodgman clipping for CCW convex footprints."""
    out=list(subject)
    for a,b in zip(clip,clip[1:]+clip[:1]):
        def side(p):return (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])
        def cross_at(s,e):
            vs=side(s);ve=side(e);den=vs-ve
            if abs(den)<1e-14:return e
            t=vs/den
            return (s[0]+t*(e[0]-s[0]),s[1]+t*(e[1]-s[1]))
        seq=out;out=[]
        if not seq:break
        for s,e in zip(seq[-1:]+seq[:-1],seq):
            inside_s=side(s)>=-1e-10;inside_e=side(e)>=-1e-10
            if inside_e:
                if not inside_s:out.append(cross_at(s,e))
                out.append(e)
            elif inside_s:out.append(cross_at(s,e))
    return out


def expected_footprint_overlap(spec):
    (a,b),(c,d)=spec['holes'];path=spec['path'];depth=spec['depth_m']
    p=_left_interior_polygon(path[a],path[b],depth)
    q=_left_interior_polygon(path[c],path[d],depth)
    return _polygon_area(_clip_convex(p,q))


def build_interior_contact_gallery(*, spacing_x=17.0):
    verts=[];faces=[];face_case_ids=[];cases=[]
    for ci,s in enumerate(scenario_specs()):
        path=s['path'];depth=float(s['depth_m'])
        overlap=expected_footprint_overlap(s)
        if overlap <= 0.02:raise ValueError(f"{s['id']}: projected pair does not overlap ({overlap:.6f} m2)")
        # Each case stays a connected wall surface with precisely TWO holes.
        xoff=ci*spacing_x;yoff=-(ci%2)*.5
        z0=s['opening_bottom_m'];z1=z0+s['opening_height_m'];wall_h=max(3.6,z1+.55)
        levels=(0.,z0,z1,wall_h)
        vs=len(verts);fs=len(faces)
        for x,y in path:
            for z in levels:verts.append((x+xoff,y+yoff,z))
        def idx(st,z):return vs+st*4+z
        for j in range(len(path)-1):
            for zband in (0,1,2):
                if zband==1 and any(a<=j<b for a,b in s['holes']):continue
                faces.append((idx(j,zband),idx(j+1,zband),idx(j+1,zband+1),idx(j,zband+1)))
                face_case_ids.append(ci)
        rec={k:v for k,v in s.items() if k!='path'}
        rec.update(case_index=ci,vertex_start=vs,vertex_count=len(path)*4,
                   face_start=fs,face_count=len(faces)-fs,
                   hole_count=2,expected_classifications=['PLANAR','PLANAR'],
                   projected_overlap_m2=round(overlap,5), expected_contact='POTENTIAL',
                   grid_column=ci,wall_mode='SURFACE',wall_height_m=wall_h,
                   opening_centers_xy=[((path[a][0]+path[b][0])/2+xoff,
                                        (path[a][1]+path[b][1])/2+yoff) for a,b in s['holes']])
        cases.append(rec)
    data=dict(schema=SCHEMA_INTERIOR,variant='INTERIOR',object_role='DWOS_INTERIOR_CONTACT_FIXTURE',
              wall_mode='SURFACE',vertices=verts,faces=faces,face_case_ids=face_case_ids,cases=cases)
    validate_interior_contact_gallery(data)
    return data


def validate_interior_contact_gallery(data):
    verts=data['vertices'];faces=data['faces'];mapping=data['face_case_ids'];cases=data['cases']
    if len(cases)!=5 or len(faces)!=len(mapping):raise ValueError('Interior gallery must contain 5 cases')
    if any(not all(isfinite(v) for v in vertex) for vertex in verts):raise ValueError('Nonfinite fixture vertex')
    stats=[];all_faces=set()
    for ci,case in enumerate(cases):
        fs=case['face_start'];fc=case['face_count'];vs=case['vertex_start'];ve=vs+case['vertex_count']
        if mapping[fs:fs+fc]!=[ci]*fc:raise ValueError(f"{case['id']}: face identity mismatch")
        incidence=Counter();directions=defaultdict(list);adj=defaultdict(set);used=set()
        for f in faces[fs:fs+fc]:
            if len(f)!=4 or len(set(f))!=4 or not all(vs<=v<ve for v in f):
                raise ValueError(f"{case['id']}: invalid quad or shared vertices")
            key=tuple(sorted(f))
            if key in all_faces:raise ValueError(f"{case['id']}: duplicate face")
            all_faces.add(key)
            a,b,c=[verts[i] for i in f[:3]]
            u=[b[k]-a[k] for k in range(3)];v=[c[k]-a[k] for k in range(3)]
            cross=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            if sum(t*t for t in cross)<1e-12:raise ValueError(f"{case['id']}: degenerate face")
            for a,b in zip(f,f[1:]+f[:1]):
                e=tuple(sorted((a,b)));incidence[e]+=1;directions[e].append((a,b))
                adj[a].add(b);adj[b].add(a);used.update((a,b))
        if any(n>2 for n in incidence.values()):raise ValueError(f"{case['id']}: overused edge")
        if any(len(ds)==2 and ds[0]==ds[1] for ds in directions.values()):raise ValueError(f"{case['id']}: inconsistent winding")
        seen=set();queue=deque([min(used)])
        while queue:
            at=queue.popleft()
            if at in seen:continue
            seen.add(at);queue.extend(adj[at]-seen)
        if seen!=used:raise ValueError(f"{case['id']}: disconnected case")
        bd=defaultdict(set)
        for (a,b),v in incidence.items():
            if v==1:bd[a].add(b);bd[b].add(a)
        if any(len(neigh)!=2 for neigh in bd.values()):raise ValueError(f"{case['id']}: non-loop boundary")
        remain=set(bd);loops=0
        while remain:
            loops+=1;queue=[next(iter(remain))]
            while queue:
                v=queue.pop()
                if v not in remain:continue
                remain.remove(v);queue.extend(bd[v])
        if loops!=3:raise ValueError(f"{case['id']}: expected 1 outer + 2 hole loops, got {loops}")
        if case['projected_overlap_m2']<=.02:raise ValueError(f"{case['id']}: no projected overlap challenge")
        stats.append(dict(id=case['id'],boundary_loops=loops,opening_count=2,projected_overlap_m2=case['projected_overlap_m2']))
    return stats
