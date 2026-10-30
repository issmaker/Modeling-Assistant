"""Read-only UV-orientation inspection and conservative per-island U reflection.

Pure Python, independent of Blender. Coordinates belong to UV faces and are NOT
mesh geometry. A negative signed area is only a winding diagnosis; intentional
backface or mirrored materials can legitimately require it. Never auto-repair.
"""
from math import floor, isfinite
from .uv_flip_detection import classify_uv_polygon


def signed_area(coords):
    points = tuple((float(p[0]), float(p[1])) for p in coords)
    if len(points) < 3 or not all(isfinite(v) for p in points for v in p):
        raise ValueError('Некорректная UV-грань (меньше 3 точек или NaN/Inf)')
    x,y=points[0]
    return .5 * sum((a[0]-x)*(b[1]-y)-(b[0]-x)*(a[1]-y)
                    for a,b in zip(points, points[1:]+points[:1]))


def _same_uv(a,b,tol):
    return abs(a[0]-b[0]) <= tol and abs(a[1]-b[1]) <= tol


def audit_uv_islands(faces, tolerance=1e-7):
    """faces: iterable of {vertices:[vertex-id,...], uv:[(u,v),...]}

    Return serializable per-island summaries and permitted conservative repairs.
    Island connectivity requires both shared mesh edge and matching UV endpoints.
    Blocks multi-tile, mixed, degenerate and overlapping island AABB corrections.
    """
    tol=float(tolerance)
    if not 0 < tol < .001: raise ValueError('Некорректный UV-допуск')
    faces=list(faces)
    if len(faces)>250000: raise ValueError('Слишком много UV-граней для проверки')
    parent=list(range(len(faces)))
    def root(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]]; x=parent[x]
        return x
    def union(a,b): parent[root(a)] = root(b)
    edge_index={}
    signs=[]
    norm_faces=[]
    for fi,face in enumerate(faces):
        verts=tuple(int(v) for v in face['vertices'])
        uv=tuple((float(p[0]),float(p[1])) for p in face['uv'])
        if not all(isfinite(value) for point in uv for value in point):
            raise ValueError('UV содержит NaN/Inf')
        if len(verts)!=len(uv) or len(verts)<3 or len(set(verts))<3:
            raise ValueError('Неоднозначная/вырожденная топология UV-граней')
        # A polygonal signed area is NOT proof of the orientation of its
        # Blender render triangles. Triangles are unambiguous; n-gons must be
        # checked on an independent triangulation copy before approval.
        state=(classify_uv_polygon(uv,triangles=face.get('triangles'))
               if len(verts)==3 or face.get('triangles') is not None else 'UNVERIFIED')
        signs.append({'NORMAL':1,'FLIPPED':-1,'DEGENERATE':0}.get(state))
        norm_faces.append((verts,uv))
        for i,a in enumerate(verts):
            j=(i+1)%len(verts); b=verts[j]
            if a==b: raise ValueError('Нулевая геометрическая сторона')
            lo,hi=sorted((a,b))
            uva,uvb=(uv[i],uv[j]) if a==lo else (uv[j],uv[i])
            edge_index.setdefault((lo,hi),[]).append((fi,uva,uvb))
    for edge,incident in edge_index.items():
        if len(incident)!=2: continue
        a,b=incident
        if _same_uv(a[1],b[1],tol) and _same_uv(a[2],b[2],tol):
            union(a[0],b[0])
    groups={}
    for fi in range(len(faces)):
        groups.setdefault(root(fi),[]).append(fi)
    islands=[]
    for indices in groups.values():
        signset={signs[i] for i in indices}
        coords=[p for fi in indices for p in norm_faces[fi][1]]
        umin=min(p[0] for p in coords); umax=max(p[0] for p in coords)
        vmin=min(p[1] for p in coords); vmax=max(p[1] for p in coords)
        tile_u_min=floor(umin+tol); tile_u_max=floor(umax-tol) if umax-umin>tol else floor(umax)
        tile_v_min=floor(vmin+tol); tile_v_max=floor(vmax-tol) if vmax-vmin>tol else floor(vmax)
        one_tile=(tile_u_min==tile_u_max and tile_v_min==tile_v_max)
        classification=('UNVERIFIED' if None in signset else
                        'DEGENERATE' if 0 in signset else 'MIXED' if len(signset)>1
                        else 'MIRRORED' if -1 in signset else 'NORMAL')
        islands.append({'faces':tuple(indices), 'state':classification,
                        'bounds':(umin,vmin,umax,vmax), 'one_tile':one_tile,
                        'tile':(tile_u_min,tile_v_min) if one_tile else None,
                        'repairable':classification=='MIRRORED' and one_tile,
                        'overlap_risk':False})
    # Conservative bounding-box overlap check. False positives only prevent repairs.
    by_tile={}
    for i,island in enumerate(islands):
        if island['tile'] is not None:
            by_tile.setdefault(island['tile'],[]).append(i)
    for indices in by_tile.values():
        for ix,i in enumerate(indices):
            ai=islands[i]['bounds']
            for j in indices[ix+1:]:
                aj=islands[j]['bounds']
                if (min(ai[2],aj[2])-max(ai[0],aj[0])>tol and
                    min(ai[3],aj[3])-max(ai[1],aj[1])>tol):
                    islands[i]['overlap_risk']=True
                    islands[j]['overlap_risk']=True
                    islands[i]['repairable']=False
                    islands[j]['repairable']=False
    stats={'faces':len(faces), 'islands':len(islands),
           'mirrored_faces':signs.count(-1), 'positive_faces':signs.count(1),
           'unverified_faces':signs.count(None),
           'unverified_islands':sum(i['state']=='UNVERIFIED' for i in islands),
           'degenerate_faces':signs.count(0),
           'mirrored_islands':sum(i['state']=='MIRRORED' for i in islands),
           'mixed_islands':sum(i['state']=='MIXED' for i in islands),
           'repairable_islands':sum(i['repairable'] for i in islands),
           'overlap_risk_islands':sum(i['overlap_risk'] for i in islands),
           'islands_detail':tuple(islands)}
    return stats


def mirrored_uv_for_island(uv, umin, umax):
    """Reflect U in original island bounds, preserving UV area magnitude."""
    return tuple((umin+umax-float(p[0]),float(p[1])) for p in uv)
