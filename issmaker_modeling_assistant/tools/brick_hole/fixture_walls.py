"""Deterministic Blender-independent Brick Hole fixture walls.

Derived from the *idea* of DWOS 3.7.16 fixture_wall_core (not a runtime
import). One Mesh, thirty distinct facade patches: fifteen aperture loops and
fifteen polygon-filled openings. No Blender state is touched by this module.
"""
from __future__ import annotations
from collections import Counter, defaultdict, deque
from math import cos, sin, radians, hypot, isfinite

SCHEMA = 'BRICK_HOLE_WALL_FIXTURES_V1'
TYPES = ('PLANAR', 'CURVED', 'CORNER')
KINDS = ('OPEN', 'FILLED')
MARGIN_PIXELS = {4096: 48, 2048: 24}
PIXELS_PER_METRE = 1000.0


def wall_fixture_specs():
    """Thirty examples; every DWOS-style shape has OPEN and FILLED variants."""
    items = []
    for kind in KINDS:
        for i, yaw in enumerate((0.0, 12.0, -18.0, 27.0, -34.0)):
            items.append(dict(id=f'P{i+1:02}_{kind}', type='PLANAR', kind=kind,
                              yaw_deg=yaw, span_m=(4.0,4.5,5.0,4.2,5.4)[i],
                              opening_bottom_m=(.78,.9,.72,1.05,.82)[i],
                              opening_height_m=(1.65,1.8,1.9,1.6,1.85)[i]))
        for i, sweep in enumerate((30.0,45.0,60.0,80.0,110.0)):
            items.append(dict(id=f'R{i+1:02}_{kind}', type='CURVED', kind=kind,
                              sweep_deg=sweep, facets=(8,10,12,12,16)[i],
                              span_m=(4.3,4.6,4.8,4.6,5.0)[i],
                              opening_bottom_m=(.85,.9,.75,1.,.8)[i],
                              opening_height_m=(1.8,1.65,1.9,1.7,1.85)[i]))
        for i, angle in enumerate((35.0,45.0,55.0,70.0,85.0)):
            items.append(dict(id=f'A{i+1:02}_{kind}', type='CORNER', kind=kind,
                              interior_angle_deg=angle, turn_deg=180.-angle,
                              span_m=(4.6,4.8,4.8,5.,5.2)[i],
                              opening_bottom_m=(.82,.78,.95,.85,.9)[i],
                              opening_height_m=(1.75,1.9,1.7,1.85,1.9)[i]))
    # Group paired variants in neighbouring rows: OPEN above, FILLED below.
    order = {'PLANAR':0,'CURVED':1,'CORNER':2}
    items.sort(key=lambda s:(order[s['type']], KINDS.index(s['kind']), int(s['id'][1:3])))
    return items


def _stations(spec):
    width = spec['span_m']
    if spec['type'] == 'PLANAR':
        theta = radians(spec['yaw_deg'])
        # A single editable quad seals the PLANAR opening; every side remains
        # shorter than the useful 2K tile (2 m at 1000 px/m).
        opening_width=min(1.8,width*.4)
        fractions=(0.,(width-opening_width)/(2*width),(width+opening_width)/(2*width),1.)
        return [(width*t*cos(theta),width*t*sin(theta)) for t in fractions],1,2
    if spec['type'] == 'CURVED':
        n=int(spec['facets']); sweep=radians(spec['sweep_deg'])
        radius=width/sweep
        pts=[(radius*sin(sweep*(j/n-.5)),radius*(1-cos(sweep*(j/n-.5)))) for j in range(n+1)]
        left=max(1,n//4)
        return pts,left,n-left
    if spec['type'] == 'CORNER':
        turn=radians(spec['turn_deg']); a=width/2
        return [(-a,0.),(-a*.5,0.),(0.,0.),(a*.5*cos(turn),a*.5*sin(turn)),
                (a*cos(turn),a*sin(turn))],1,3
    raise ValueError('Unsupported shape')


def build_wall_fixture_gallery(*, resolution=4096, spacing_x=11.0, spacing_y=10.0):
    """Generate all fixture coordinates, quads, per-face UVs and case provenance.

    Segments are deliberately shorter than 2 m, so even the 2K tiles can
    preserve 1000px/m within the useful 2000px square. Each quad is an
    independent UV island; seams are intentional to avoid out-of-tile UV.
    """
    resolution=int(resolution)
    if resolution not in MARGIN_PIXELS: raise ValueError('Resolution must be 2048 or 4096')
    if not all(isfinite(float(x)) and float(x)>0 for x in (spacing_x,spacing_y)):
        raise ValueError('Invalid spacing')
    if float(spacing_x)<8 or float(spacing_y)<6:
        raise ValueError('Spacing risks overlapping fixture cases')
    verts=[];faces=[];face_uvs=[];face_case_ids=[];case_records=[]
    low=MARGIN_PIXELS[resolution]/resolution
    for case_idx,spec in enumerate(wall_fixture_specs()):
        shape_idx=TYPES.index(spec['type'])
        kind_idx=KINDS.index(spec['kind'])
        sample_idx=int(spec['id'][1:3])-1
        row=shape_idx*2+kind_idx; col=sample_idx
        stations,opening_start,opening_end=_stations(spec)
        ox=float(col*spacing_x);oy=float(-row*spacing_y)
        zlo=float(spec['opening_bottom_m']); zhi=zlo+float(spec['opening_height_m'])
        top=max(3.5,zhi+.55)
        levels=(0.,zlo,zhi,top)
        vert_start=len(verts); face_start=len(faces)
        for px,py in stations:
            for z in levels:verts.append((px+ox,py+oy,z))
        def vi(st,level):return vert_start+st*4+level
        # Keep the lower band, lintel, piers and (FILLED only) aperture cover.
        for station in range(len(stations)-1):
            span=hypot(stations[station+1][0]-stations[station][0],
                       stations[station+1][1]-stations[station][1])
            for band in (0,1,2):
                in_opening=band==1 and opening_start<=station<opening_end
                if in_opening and spec['kind']=='OPEN':continue
                dz=levels[band+1]-levels[band]
                # Check the actual useful physical side length, not raw pixel size.
                usable=(resolution-2*MARGIN_PIXELS[resolution])/PIXELS_PER_METRE
                if span>=usable or dz>=usable:
                    raise ValueError(f"{spec['id']}: quad cannot fit useful {resolution} tile")
                faces.append((vi(station,band),vi(station+1,band),
                              vi(station+1,band+1),vi(station,band+1)))
                u=low+span*PIXELS_PER_METRE/resolution
                v=low+dz*PIXELS_PER_METRE/resolution
                face_uvs.append(((low,low),(u,low),(u,v),(low,v)))
                face_case_ids.append(case_idx+1)
        case=dict(spec)
        case.update(case_index=case_idx+1, row=row, column=col,
                    face_start=face_start, face_count=len(faces)-face_start,
                    vertex_start=vert_start, vertex_count=len(stations)*4,
                    opening_start_station=opening_start, opening_end_station=opening_end,
                    wall_height_m=top, uv_resolution=resolution,
                    expected_boundary_loops=2 if spec['kind']=='OPEN' else 1,
                    description=('Отверстие без граней: выделяйте контур рёбер' if spec['kind']=='OPEN'
                                 else 'Проём закрыт полигонами: выделяйте грани'))
        case_records.append(case)
    result={'schema':SCHEMA,'version':'1.0.9','resolution':resolution,
            'vertices':verts,'faces':faces,'face_uvs':face_uvs,
            'face_case_ids':face_case_ids,'cases':case_records,
            'fixture_count':len(case_records),'mesh_objects':1}
    validate_wall_fixture_gallery(result)
    return result


def validate_wall_fixture_gallery(data):
    """Fail closed if any component is degenerate, shared, non-manifold, or mislabeled."""
    verts=data['vertices']; faces=data['faces']; fuv=data['face_uvs']; ids=data['face_case_ids']
    cases=data['cases']; resolution=data['resolution'];margin=MARGIN_PIXELS[resolution]/resolution
    high=1-margin
    if len(cases)!=30 or len(faces)!=len(ids) or len(faces)!=len(fuv):
        raise ValueError('Expected 30 cases and complete per-face UV+case mapping')
    if any(len(v)!=3 or not all(isfinite(float(k)) for k in v) for v in verts):
        raise ValueError('Non-finite vertex')
    output=[];seen_faces=set();cases_found=Counter((c['type'],c['kind']) for c in cases)
    if any(cases_found[shape,kind]!=5 for shape in TYPES for kind in KINDS):
        raise ValueError('Expected 5 of each type and state')
    for rec in cases:
        number=rec['case_index'];start=rec['face_start'];end=start+rec['face_count']
        patch=faces[start:end]
        if not patch or any(ids[i]!=number for i in range(start,end)):
            raise ValueError(f'{rec["id"]}: invalid mapping')
        lo=rec['vertex_start'];hi=lo+rec['vertex_count']
        used=set();edge_count=Counter();edge_direction=defaultdict(list);adj=defaultdict(set)
        covers=0
        for fi in range(start,end):
            face=faces[fi];uv=fuv[fi]
            if len(face)!=4 or len(set(face))!=4 or not all(lo<=i<hi for i in face):
                raise ValueError(f'{rec["id"]}: invalid quad or shared vertex')
            key=tuple(sorted(face))
            if key in seen_faces:raise ValueError(f'{rec["id"]}: duplicate quad')
            seen_faces.add(key)
            if len(uv)!=4 or any(not all(isfinite(float(x)) and margin-1e-9<=x<=high+1e-9 for x in pair) for pair in uv):
                raise ValueError(f'{rec["id"]}: invalid UV or outside useful tile')
            a,b,c=(verts[face[j]] for j in (0,1,3))
            ax,ay,az=(b[j]-a[j] for j in range(3)); bx,by,bz=(c[j]-a[j] for j in range(3))
            area2=(ay*bz-az*by)**2+(az*bx-ax*bz)**2+(ax*by-ay*bx)**2
            if area2<1e-10:raise ValueError(f'{rec["id"]}: degenerate quad')
            span=hypot(ax,ay);height=abs(bz)
            # a->b is along facade, a->last vertex is vertical; exact texel scale.
            height=abs(c[2]-a[2])
            umag=hypot(uv[1][0]-uv[0][0],uv[1][1]-uv[0][1])*resolution
            vmag=hypot(uv[3][0]-uv[0][0],uv[3][1]-uv[0][1])*resolution
            if abs(umag-span*PIXELS_PER_METRE)>0.05 or abs(vmag-height*PIXELS_PER_METRE)>0.05:
                raise ValueError(f'{rec["id"]}: incorrect texel density')
            if (min(verts[i][2] for i in face)>=rec['opening_bottom_m']-1e-5 and
                max(verts[i][2] for i in face)<=rec['opening_bottom_m']+rec['opening_height_m']+1e-5):
                # Only count opening mid-band faces, not piers, when x stations inside hole.
                rel=tuple(sorted((i-lo)//4 for i in face))
                if rel[0]>=rec['opening_start_station'] and rel[-1]<=rec['opening_end_station']:
                    covers+=1
            for aa,bb in zip(face,face[1:]+face[:1]):
                e=tuple(sorted((aa,bb)));edge_count[e]+=1;edge_direction[e].append((aa,bb))
                adj[aa].add(bb);adj[bb].add(aa);used.update((aa,bb))
        if any(n>2 for n in edge_count.values()):raise ValueError(f'{rec["id"]}: non-manifold')
        if any(len(dr)==2 and dr[0]==dr[1] for dr in edge_direction.values()):
            raise ValueError(f'{rec["id"]}: inconsistent normals')
        reached=set();queue=deque([min(used)])
        while queue:
            v=queue.popleft()
            if v in reached:continue
            reached.add(v);queue.extend(adj[v]-reached)
        if reached!=used:raise ValueError(f'{rec["id"]}: disconnected patch')
        border=[e for e,n in edge_count.items() if n==1]
        badj=defaultdict(list)
        for a,b in border:badj[a].append(b);badj[b].append(a)
        if not badj or any(len(neighbours)!=2 for neighbours in badj.values()):
            raise ValueError(f'{rec["id"]}: open/branching border')
        remaining=set(badj);loops=0
        while remaining:
            loops+=1;stack=[next(iter(remaining))]
            while stack:
                vert=stack.pop()
                if vert not in remaining:continue
                remaining.remove(vert);stack.extend(badj[vert])
        if loops!=rec['expected_boundary_loops']:
            raise ValueError(f'{rec["id"]}: expected {rec["expected_boundary_loops"]} loops, got {loops}')
        if (rec['kind']=='FILLED' and covers==0) or (rec['kind']=='OPEN' and covers!=0):
            raise ValueError(f'{rec["id"]}: opening coverage mismatch')
        output.append({'id':rec['id'],'type':rec['type'],'kind':rec['kind'],
                       'faces':len(patch),'boundary_loops':loops,'cover_faces':covers})
    return output
