"""Deterministic, Blender-independent gallery of real facade apertures.

One disconnected *mesh object*, 15 connected surface islands, 15 actual holes.
Each island is a single two-manifold-with-boundary facade sheet.  It is
intentionally not an artificial solid: adaptive SURFACE classification requires
real hole boundary edges and supports curved/corner windows.
"""
from __future__ import annotations
from collections import Counter, defaultdict, deque
from math import cos, sin, radians, pi, isfinite

SCHEMA = 'DWOS_fixture_gallery_v1'
SCHEMA_LONG = 'DWOS_fixture_gallery_long_v1'
FIXTURE_TYPES = ('PLANAR', 'CURVED', 'CORNER')


def gallery_specs(*, long=False):
    """Immutable, human-readable test cases; internal angle vs change-of-heading is explicit."""
    cases = []
    for i, yaw in enumerate((0.0, 12.0, -18.0, 27.0, -34.0)):
        cases.append(dict(id=f'P{i+1:02}', type='PLANAR', title=f'Straight {i+1}',
                          yaw_deg=yaw, span_m=(4.0,4.5,5.0,4.2,5.4)[i],
                          opening_bottom_m=(0.78,0.90,0.72,1.05,0.82)[i],
                          opening_height_m=(1.65,1.8,2.1,1.6,1.95)[i]))
    for i, sweep in enumerate((30.0,45.0,60.0,80.0,110.0)):
        cases.append(dict(id=f'R{i+1:02}', type='CURVED', title=f'Rounded {i+1}',
                          sweep_deg=sweep, facets=(8,10,12,12,16)[i],
                          span_m=(4.3,4.6,4.8,4.6,5.0)[i],
                          opening_bottom_m=(0.85,0.90,0.75,1.0,0.8)[i],
                          opening_height_m=(1.8,1.65,1.9,1.7,1.85)[i]))
    # All five are *interior* acute angles. Turn = 180 - interior.
    for i, internal in enumerate((35.0,45.0,55.0,70.0,85.0)):
        cases.append(dict(id=f'A{i+1:02}', type='CORNER', title=f'Acute {i+1}',
                          interior_angle_deg=internal, turn_deg=180.0-internal,
                          span_m=(4.6,4.8,4.8,5.0,5.2)[i],
                          opening_bottom_m=(0.82,0.78,0.95,0.85,0.90)[i],
                          opening_height_m=(1.75,1.95,1.7,1.85,2.0)[i]))
    if long:
        lengths = (24.0, 48.0, 75.0, 120.0, 200.0)
        for item in cases:
            idx = int(item['id'][1:]) - 1
            item['span_m'] = lengths[idx]
            item['title'] = 'VERY LONG ' + item['title']
            item['fixture_scale'] = 'LONG'
            item['opening_height_m'] = (2.4, 2.8, 3.1, 3.5, 4.0)[idx]
            item['opening_bottom_m'] = 0.8
    return cases


def _stations(spec):
    typ = spec['type']
    width = spec['span_m']
    if typ == 'PLANAR':
        theta = radians(spec['yaw_deg'])
        return [(width*t*cos(theta), width*t*sin(theta)) for t in (0,.2,.4,.6,.8,1)],2,4
    if typ == 'CURVED':
        n = int(spec['facets'])
        sweep = radians(spec['sweep_deg'])
        radius = width/sweep
        samples=[]
        for j in range(n+1):
            a = sweep*(j/n-.5)
            samples.append((radius*sin(a), radius*(1-cos(a))))
        left=max(1,n//4)
        right=n-left
        return samples,left,right
    if typ == 'CORNER':
        turn = radians(spec['turn_deg'])
        a=width/2
        # The aperture contour has THREE authored vertices: start, true corner, end.
        # Avoid extra collinear facade nodes *inside* the opening boundary: some
        # parametric CORNER junction algorithms rely on the exact authored triad.
        pts=[(-a,0.),(-a*.5,0.),(0.,0.),(a*.5*cos(turn),a*.5*sin(turn)),
             (a*cos(turn),a*sin(turn))]
        return pts,1,3
    raise ValueError('Unsupported fixture type')


def build_fixture_gallery(*, variant="STANDARD", spacing_x=None, spacing_y=None):
    """Return geometry and metadata, without mutating Blender state."""
    variant=str(variant).upper()
    if variant not in {'STANDARD','LONG'}: raise ValueError('Unknown gallery variant')
    long=variant=='LONG'
    specs = gallery_specs(long=long)
    if spacing_x is None:spacing_x = 450.0 if long else 11.0
    if spacing_y is None:spacing_y = 450.0 if long else 9.0
    verts=[];faces=[];face_case_ids=[];cases=[]
    for case_number,spec in enumerate(specs):
        row=case_number//5
        col=case_number%5
        positions, opening_start, opening_end = _stations(spec)
        # Place samples far apart, while maintaining one mesh datablock.
        x_offset=col*spacing_x
        y_offset=-row*spacing_y
        opening_lo=float(spec['opening_bottom_m'])
        opening_hi=opening_lo+float(spec['opening_height_m'])
        wall_height=max(3.5,opening_hi+0.55)
        levels=(0.0,opening_lo,opening_hi,wall_height)
        start=len(verts)
        for px,py in positions:
            for z in levels:
                verts.append((px+x_offset,py+y_offset,z))
        face_start=len(faces)
        def idx(st,z):return start+st*4+z
        # Facade surface: bottom and lintel band everywhere, middle only outside hole.
        for j in range(len(positions)-1):
            for zband in (0,1,2):
                if zband==1 and opening_start<=j<opening_end:
                    continue
                faces.append((idx(j,zband),idx(j+1,zband),idx(j+1,zband+1),idx(j,zband+1)))
                face_case_ids.append(case_number)
        rec={k:v for k,v in spec.items()}
        rec.update(case_index=case_number,face_start=face_start,face_count=len(faces)-face_start,
                   vertex_start=start,vertex_count=len(positions)*4,
                   opening_start_station=opening_start,opening_end_station=opening_end,
                   opening_width_straight_m=None,
                   wall_height_m=wall_height,grid_row=row,grid_column=col,
                   source='SURFACE',expected_classification=spec['type'])
        cases.append(rec)
    result=dict(schema=(SCHEMA_LONG if long else SCHEMA),variant=variant,object_role='DWOS_FIXTURE_WALL_GALLERY',
                wall_mode='SURFACE',vertices=verts,faces=faces,
                face_case_ids=face_case_ids,cases=cases)
    validate_fixture_gallery(result)
    return result


def validate_fixture_gallery(data):
    """Independent structural checks of 15 distinct closed aperture contours."""
    verts=data['vertices']; faces=data['faces']; ids=data['face_case_ids'];cases=data['cases']
    if len(cases)!=15 or len(faces)!=len(ids):
        raise ValueError('Fixture gallery must contain exactly 15 cases with indexed faces')
    if any(not all(isfinite(c) for c in v) for v in verts):
        raise ValueError('Non-finite vertex coordinate')
    component_stats=[]
    seen_faces=set()
    for case_idx,rec in enumerate(cases):
        part_faces=faces[rec['face_start']:rec['face_start']+rec['face_count']]
        if len(part_faces)<4 or any(i!=case_idx for i in ids[rec['face_start']:rec['face_start']+rec['face_count']]):
            raise ValueError(f"{rec['id']}: invalid face-to-case mapping")
        incidence=Counter();directed=defaultdict(list);adj=defaultdict(set); used=set()
        for face in part_faces:
            key=tuple(sorted(face))
            if key in seen_faces:raise ValueError(f"{rec['id']}: duplicate face")
            seen_faces.add(key)
            if len(set(face))!=4:raise ValueError(f"{rec['id']}: invalid quad")
            if not all(rec['vertex_start']<=i<rec['vertex_start']+rec['vertex_count'] for i in face):
                raise ValueError(f"{rec['id']}: case shares vertices with another case")
            a,b,c=(verts[face[i]] for i in range(3))
            u=(b[0]-a[0],b[1]-a[1],b[2]-a[2]);v=(c[0]-a[0],c[1]-a[1],c[2]-a[2])
            cr=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            if sum(z*z for z in cr)<1e-12:raise ValueError(f"{rec['id']}: zero area face")
            for a,b in zip(face,face[1:]+face[:1]):
                e=tuple(sorted((a,b)));incidence[e]+=1;directed[e].append((a,b))
                adj[a].add(b);adj[b].add(a);used.update((a,b))
        if any(c>2 for c in incidence.values()):raise ValueError(f"{rec['id']}: non-manifold edge")
        if any(len(dr)==2 and dr[0]==dr[1] for dr in directed.values()):
            raise ValueError(f"{rec['id']}: inconsistent normals")
        active=set()
        q=deque([min(used)])
        while q:
            v=q.popleft()
            if v in active:continue
            active.add(v);q.extend(adj[v]-active)
        if used!=active:raise ValueError(f"{rec['id']}: disconnected mesh patch")
        border=[e for e,count in incidence.items() if count==1]
        badj=defaultdict(list)
        for a,b in border:badj[a].append(b);badj[b].append(a)
        if not badj or any(len(neighbors)!=2 for neighbors in badj.values()):
            raise ValueError(f"{rec['id']}: branched/open boundary")
        remaining=set(badj);loops=0
        while remaining:
            loops+=1;stack=[next(iter(remaining))]
            while stack:
                v=stack.pop()
                if v not in remaining:continue
                remaining.remove(v);stack.extend(badj[v])
        if loops!=2:raise ValueError(f"{rec['id']}: expected exterior and aperture loops, got {loops}")
        component_stats.append(dict(id=rec['id'],type=rec['type'],faces=len(part_faces),
                                    boundary_loops=loops,boundary_edges=len(border)))
    return component_stats
