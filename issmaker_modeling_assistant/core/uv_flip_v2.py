"""Conservative, island-relative UV defect classification (no Blender mutations).

A clockwise UV polygon is NOT proof of a defect.  This module distinguishes
relative winding outliers from consistently mirrored islands, invalid polygons,
and actual interior self-overlap inside ONE continuous UV island.

Input faces: [{"vertices": (mesh vertex ids,...), "uv": ((u,v),...),
              "triangles": (((u,v),(u,v),(u,v)),...)}].
No UV geometry is ever modified by analysis. Incomplete scans are flagged, not
silently labeled PASS.  No hardcoded UDIM boundaries or artificial padding.
"""
from __future__ import annotations
from collections import defaultdict
from math import hypot, isfinite
from .uv_flip_detection import classify_uv_polygon


def _cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _area(points):
    return abs(sum(_cross((0.,0.),a,b) for a,b in
                   zip(points,points[1:]+points[:1])))*.5


def _polygon_crossing(points):
    """Only strict non-adjacent segment crossings; touching is ambiguous."""
    n=len(points)
    if n<4 or n>128:return False
    span=max(max(abs(c) for c in p) for p in points)
    eps=max(1.e-14,span*span*1.e-12)
    for i in range(n):
        a,b=points[i],points[(i+1)%n]
        for j in range(i+2,n):
            if i==0 and j==n-1:continue
            c,d=points[j],points[(j+1)%n]
            ab_c,ab_d=_cross(a,b,c),_cross(a,b,d)
            cd_a,cd_b=_cross(c,d,a),_cross(c,d,b)
            if ((ab_c>eps and ab_d < -eps or ab_c < -eps and ab_d>eps)
                    and (cd_a>eps and cd_b < -eps or cd_a < -eps and cd_b>eps)):
                return True
    return False


def _clip_triangle(subject, clip):
    """Sutherland–Hodgman positive-area intersection, shared edges excluded."""
    sign=1. if _cross(clip[0],clip[1],clip[2])>=0 else -1.
    output=list(subject)
    for a,b in zip(clip,clip[1:]+clip[:1]):
        inp=output
        output=[]
        if not inp:return []
        prev=inp[-1]
        prev_side=sign*_cross(a,b,prev)
        for current in inp:
            side=sign*_cross(a,b,current)
            if (side>=0)!=(prev_side>=0):
                den=prev_side-side
                if abs(den)>1.e-20:
                    t=prev_side/den
                    output.append((prev[0]+t*(current[0]-prev[0]),
                                   prev[1]+t*(current[1]-prev[1])))
            if side>=0:
                output.append(current)
            prev,prev_side=current,side
    return output


def _triangles_interior_overlap(a,b):
    overlap=_clip_triangle(a,b)
    if len(overlap)<3:return False
    scale=max(1.,*(abs(c) for p in a+b for c in p))
    return _area(overlap)>max(1.e-14,scale*scale*1.e-12)


def analyze_uv_faces(faces, *, tolerance=1.e-7,
                     max_overlap_pairs=30000, max_faces=250000):
    """Return severity-tagged face problems; advisory mirror != UV error.

    Findings: LOCAL_WINDING_OUTLIER, SELF_CROSSING_FACE, DEGENERATE_FACE,
    FOLDED_OR_AMBIGUOUS, ISLAND_INTERIOR_OVERLAP and MIXED_UNRESOLVED.
    A consistent all-clockwise island is ADVISORY_MIRRORED, never a defect.
    Distinct UV islands may intentionally overlap: not scanned against each other.
    """
    faces=list(faces)
    if len(faces)>max_faces:
        raise ValueError("UV analysis exceeds configured face budget")
    if tolerance<=0 or tolerance>.001:
        raise ValueError("Invalid UV equality tolerance")
    parent=list(range(len(faces)))
    def root(i):
        while parent[i]!=i:
            parent[i]=parent[parent[i]]
            i=parent[i]
        return i
    def union(i,j):
        ri,rj=root(i),root(j)
        if ri!=rj:parent[rj]=ri
    edges=defaultdict(list)
    states=[]
    normalized=[]
    face_codes=[set() for _ in faces]
    area=[]
    for fi,face in enumerate(faces):
        vert=tuple(int(v) for v in face['vertices'])
        points=tuple((float(p[0]),float(p[1])) for p in face['uv'])
        triangles=tuple(tuple((float(p[0]),float(p[1])) for p in tri)
                        for tri in face.get('triangles',()))
        if len(vert)!=len(points) or len(vert)<3 or len(set(vert))<3:
            raise ValueError("Invalid mesh-face topology")
        if any(not isfinite(c) for p in points for c in p):
            face_codes[fi].add("FOLDED_OR_AMBIGUOUS")
            state="UNVERIFIED"
        else:
            state=classify_uv_polygon(points,triangles=triangles if triangles else None)
            if _polygon_crossing(points):
                face_codes[fi].add("SELF_CROSSING_FACE")
            elif state=="DEGENERATE":
                face_codes[fi].add("DEGENERATE_FACE")
            elif state=="UNVERIFIED":
                face_codes[fi].add("FOLDED_OR_AMBIGUOUS")
        states.append(state)
        normalized.append((vert,points,triangles))
        area.append(_area(points) if all(isfinite(c) for p in points for c in p) else 0.)
        for j,v in enumerate(vert):
            k=(j+1)%len(vert)
            other=vert[k]
            lo,hi=sorted((v,other))
            if lo==hi:continue
            uva,uvb=(points[j],points[k]) if v==lo else (points[k],points[j])
            edges[(lo,hi)].append((fi,uva,uvb))
    for incident in edges.values():
        if len(incident)!=2:continue
        a,b=incident
        if all(abs(x-y)<=tolerance for pa,pb in zip(a[1:],b[1:])
               for x,y in zip(pa,pb)):
            union(a[0],b[0])
    groups=defaultdict(list)
    for fi in range(len(faces)):
        groups[root(fi)].append(fi)
    islands=[]
    comparisons=0
    complete=True
    overlap_pairs=[]
    for faceids in groups.values():
        positive=[i for i in faceids if states[i]=="NORMAL"]
        negative=[i for i in faceids if states[i]=="FLIPPED"]
        advisory=None
        if positive and negative:
            # Majority is required; total UV area must also dominate.
            if len(positive)>len(negative) and len(positive)>=2 and (
                sum(area[i] for i in positive)>2.*sum(area[i] for i in negative)):
                for i in negative:face_codes[i].add("LOCAL_WINDING_OUTLIER")
            elif len(negative)>len(positive) and len(negative)>=2 and (
                sum(area[i] for i in negative)>2.*sum(area[i] for i in positive)):
                for i in positive:face_codes[i].add("LOCAL_WINDING_OUTLIER")
            else:
                advisory="MIXED_UNRESOLVED"
        elif negative and not positive and len(negative)==len(faceids):
            advisory="ADVISORY_MIRRORED"
        tris=[]
        for i in faceids:
            for tri in normalized[i][2]:
                if len(tri)!=3 or any(not isfinite(x) for uv in tri for x in uv):
                    continue
                if _area(tri)<1.e-14:continue
                low_u=min(p[0] for p in tri)
                high_u=max(p[0] for p in tri)
                low_v=min(p[1] for p in tri)
                high_v=max(p[1] for p in tri)
                tris.append((low_u,high_u,low_v,high_v,i,tri))
        # Sweep by U: remove candidate pairs that cannot share positive area.
        tris.sort(key=lambda item:item[0])
        active=[]
        for item in tris:
            active=[other for other in active if other[1]>item[0]+tolerance]
            for other in active:
                if other[4]==item[4] or (other[3] <= item[2]+tolerance or
                    item[3] <= other[2]+tolerance):
                    continue
                if comparisons>=max_overlap_pairs:
                    complete=False
                    break
                comparisons+=1
                if _triangles_interior_overlap(other[5],item[5]):
                    first,second=sorted((other[4],item[4]))
                    overlap_pairs.append((first,second))
                    face_codes[first].add("ISLAND_INTERIOR_OVERLAP")
                    face_codes[second].add("ISLAND_INTERIOR_OVERLAP")
            if not complete:break
            active.append(item)
        islands.append({
            "faces":tuple(faceids),
            "status":("MIXED" if positive and negative else
                      "NORMAL" if positive else "MIRRORED" if negative else
                      "UNVERIFIED"),
            "advisory":advisory,
            "outliers":tuple(i for i in faceids
                             if "LOCAL_WINDING_OUTLIER" in face_codes[i]),
        })
        if not complete:break
    issues={i:tuple(sorted(codes)) for i,codes in enumerate(face_codes) if codes}
    local=tuple(i for i,codes in issues.items() if "LOCAL_WINDING_OUTLIER" in codes)
    return {
        "issues":issues,
        "local_flip_faces":local,
        "overlap_pairs":tuple(sorted(set(overlap_pairs))),
        "islands":tuple(islands),
        "face_states":tuple(states),
        "faces_scanned":len(faces),
        "overlap_comparisons":comparisons,
        "complete":complete,
        "mirrored_islands_advisory":sum(i["advisory"]=="ADVISORY_MIRRORED"
                                        for i in islands),
        "unresolved_islands":sum(i["advisory"]=="MIXED_UNRESOLVED"
                                  for i in islands),
    }


def mesh_uv_faces(mesh):
    """Return indexed faces and render triangles for pure preflight planning."""
    layer=mesh.uv_layers.active
    if layer is None:raise ValueError("Mesh has no active UV map")
    mesh.calc_loop_triangles()
    tris=defaultdict(list)
    for tri in mesh.loop_triangles:
        tris[tri.polygon_index].append(
            tuple(tuple(float(c) for c in layer.data[i].uv) for i in tri.loops))
    faces=[]
    for poly in mesh.polygons:
        ids=tuple(poly.loop_indices)
        faces.append({
            "vertices":tuple(mesh.loops[i].vertex_index for i in ids),
            "uv":tuple(tuple(float(c) for c in layer.data[i].uv) for i in ids),
            "triangles":tuple(tris.get(poly.index,()))
        })
    return faces


def analyze_mesh_uv(mesh):
    """Blender read-only island-relative UV audit using real loop triangles."""
    return analyze_uv_faces(mesh_uv_faces(mesh))


def analyze_bmesh_uv(bm, layer):
    """Live Edit Mesh adapter, no topology changes, no persistent BMesh refs.

    Tessellate polygon in temporary local vectors without changing source Mesh.
    Used once per navigation verification rather than once per inspected face.
    """
    from mathutils.geometry import tessellate_polygon
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    faces=[]
    for face in bm.faces:
        loops=tuple(face.loops)
        geo=[loop.vert.co.copy() for loop in loops]
        uv=[tuple(float(c) for c in loop[layer].uv) for loop in loops]
        tris=[]
        for triangle in tessellate_polygon([geo]):
            indexes=[]
            for v in triangle:
                if isinstance(v,int):
                    indexes.append(v)
                else:
                    # Coordinate mapping is unambiguous for nonduplicate corners.
                    indexes.append(geo.index(v))
            if len(indexes)==3:
                tris.append(tuple(uv[i] for i in indexes))
        faces.append({
            "vertices":tuple(loop.vert.index for loop in loops),
            "uv":tuple(uv),
            "triangles":tuple(tris),
        })
    return analyze_uv_faces(faces)
