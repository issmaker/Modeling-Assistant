"""W58.4 isolated planar UV Wall LP-cut builder, experimental.

Does not modify original host unless commit=True; always creates a separate LP
mesh and glass-zone previews. Caller must validate geometry and UV first.
"""
from __future__ import annotations
import bpy
import bmesh
import json
import math
from collections import defaultdict
from mathutils import Vector
from . import adaptive_edge_cut_core as core


def _bary(p,a,b,c):
    den=core.cross(a,b,c)
    if abs(den)<1e-12:return None
    u=core.cross(p,b,c)/den
    v=core.cross(a,p,c)/den
    w=1-u-v
    return (u,v,w)


def _interpolate(p, record):
    vertices=record["positions"];uv=record["uv"]
    options=[]
    for i in range(1,len(vertices)-1):
        q=_bary(p,vertices[0],vertices[i],vertices[i+1])
        if q is None:continue
        bad=max(0.,-min(q))
        a=q[0]*uv[0][0]+q[1]*uv[i][0]+q[2]*uv[i+1][0]
        b=q[0]*uv[0][1]+q[1]*uv[i][1]+q[2]*uv[i+1][1]
        options.append((bad,(a,b)))
    if not options:return None
    return min(options,key=lambda x:x[0])


def capture(host):
    if host.type!="MESH":raise ValueError("SOURCE_NOT_MESH")
    bm=bmesh.new();bm.from_mesh(host.data)
    try:
        uv=bm.loops.layers.uv.active
        if uv is None:raise ValueError("SOURCE_UV_MISSING")
        selected=set(f for f in bm.faces if f.select)
        if not selected:raise ValueError("NO_SOURCE_SELECTED_FACES")
        y_values=[v.co.y for f in selected for v in f.verts]
        if max(y_values)-min(y_values)>1e-5:
            raise ValueError("EXPERIMENTAL_PLANAR_ONLY")
        y=sum(y_values)/len(y_values)
        faces=[]
        for face in selected:
            faces.append({"id":int(face.index),
                          "positions":[(float(l.vert.co.x),float(l.vert.co.z)) for l in face.loops],
                          "uv":[tuple(map(float,l[uv].uv)) for l in face.loops],
                          "material":int(face.material_index)})
        boundary=[edge for edge in bm.edges if sum(f in selected for f in edge.link_faces)==1]
        adjacent=defaultdict(list)
        for edge in boundary:
            a,b=edge.verts;adjacent[a].append(b);adjacent[b].append(a)
        if any(len(v)!=2 for v in adjacent.values()):
            raise ValueError("BOUNDARY_BRANCH_OR_OPEN")
        loops=[];seen=set()
        for v in adjacent:
            if v in seen:continue
            cursor=v;prev=None;points=[]
            for _ in range(len(adjacent)+1):
                if cursor==v and points:break
                if cursor in seen:raise ValueError("SOURCE_BOUNDARY_REPEATED_VERTEX")
                seen.add(cursor)
                points.append((float(cursor.co.x),float(cursor.co.z)))
                n=adjacent[cursor]
                new=n[0] if n[0] is not prev else n[1]
                prev,cursor=cursor,new
            if cursor!=v:raise ValueError("SOURCE_BOUNDARY_NOT_CLOSED")
            loops.append(points)
        if len(loops)!=1:raise ValueError("EXPERIMENTAL_SINGLE_SELECTED_BOUNDARY_ONLY")
        shared_edges={}
        for edge in bm.edges:
            if len(edge.link_faces)==2 and all(face in selected for face in edge.link_faces):
                a,b=edge.verts
                key=tuple(sorted(((round(float(a.co.x),6),round(float(a.co.z),6)),
                                  (round(float(b.co.x),6),round(float(b.co.z),6)))))
                shared_edges[key]=(tuple(a.co),tuple(b.co))
        normal_y=sum(float(face.normal.y) for face in selected)/len(selected)
        return {"outer":loops[0],"source_faces":faces,
                "shared_edges":list(shared_edges.values()),"y":y,
                "source_normal_y":normal_y,
                "selected_face_ids":[face.index for face in selected],
                "host_name":host.name,"original_face_count":len(bm.faces)}
    finally:
        bm.free()


def _new_mesh(name, rings, y):
    import mathutils
    from mathutils import geometry
    if not rings:raise ValueError("NO_RINGS")
    allpts=[p for ring in rings for p in ring]
    vectors=[[Vector((x,z,0.)) for x,z in ring] for ring in rings]
    triangles=geometry.tessellate_polygon(vectors)
    good=[]
    for i,j,k in triangles:
        if abs(core.cross(allpts[i],allpts[j],allpts[k]))*.5>1e-10:
            good.append((i,j,k))
    expected=abs(core.signed_area(rings[0]))-sum(abs(core.signed_area(r)) for r in rings[1:])
    real=sum(abs(core.cross(allpts[i],allpts[j],allpts[k]))*.5 for i,j,k in good)
    if expected<=0 or abs(real-expected)>max(1e-4,expected*1e-4):
        raise ValueError(f"TRIANGULATION_AREA_MISMATCH:{real}:{expected}")
    mesh=bpy.data.meshes.new(name)
    mesh.from_pydata([(x,y,z) for x,z in allpts],[],good)
    mesh.update()
    return mesh,{"triangles":len(good),"area_m2":real,"expected_area_m2":expected}


def _assign_uv(mesh,source_records):
    bm=bmesh.new();bm.from_mesh(mesh)
    uv=bm.loops.layers.uv.verify()
    errors=[];faces_done=0;max_outside=0.
    for f in bm.faces:
        center=f.calc_center_median();p=(float(center.x),float(center.z))
        candidates=[]
        for record in source_records:
            found=_interpolate(p,record)
            if found is not None:candidates.append((found[0],record))
        if not candidates:
            errors.append(f.index);continue
        outside,rec=min(candidates,key=lambda x:x[0])
        max_outside=max(max_outside,outside)
        if outside>0.002:
            errors.append((f.index,outside));continue
        for loop in f.loops:
            pt=(float(loop.vert.co.x),float(loop.vert.co.z))
            mapped=_interpolate(pt,rec)
            if mapped is None or mapped[0]>0.002:
                errors.append((f.index,"vertex_outside_source",mapped[0] if mapped else None))
                break
            loop[uv].uv=mapped[1]
        else:
            faces_done+=1
    if errors:
        bm.free()
        raise ValueError("SOURCE_UV_PROJECTION_FAIL:"+str(errors[:4]))
    bm.to_mesh(mesh);bm.free();mesh.update()
    return {"mapped_faces":faces_done,"max_barycentric_overrun":max_outside}


def create_lp_preview(host, source, solution, *, material=None):
    if solution["category"] not in ("ACCEPTED_SINGLE","ACCEPTED_SPLIT"):
        raise ValueError("NO_VALID_INNER_AREA")
    name="W58_4_LP_"+str(host.get("W58_4_CASE",host.name))
    # Solver operates in path-relative coordinates; source UV is stored in
    # actual host mesh coordinates. Translate all loops by the same proven
    # origin delta, and use solver's orientation-normalized CCW outer cycle.
    # The source may include microedges removed from the solver's analysis
    # copy. Never derive translation from vertex-count-dependent centroids.
    dx=min(p[0] for p in source["outer"])-min(p[0] for p in solution["outer"])
    dz=min(p[1] for p in source["outer"])-min(p[1] for p in solution["outer"])
    rings=[[(x+dx,z+dz) for x,z in solution["outer"]]]
    rings += [[(x+dx,z+dz) for x,z in reversed(loop)]
              for loop in solution["inner_loops"]]
    mesh,metrics=_new_mesh(name,rings,source["y"])
    obj=bpy.data.objects.new(name,mesh)
    # Preview geometry uses the wall's local mesh coordinate system. Preserve
    # full host transform so distant gallery positions do not affect precision.
    obj.matrix_world=host.matrix_world.copy()
    # Apply only internal selected-face cut lines; they carry source UV seams.
    bm=bmesh.new();bm.from_mesh(mesh)
    for a,b in source["shared_edges"]:
        e=Vector(b)-Vector(a)
        plane_no=Vector((e.z,0.,-e.x))
        if plane_no.length<1e-10:continue
        plane_no.normalize()
        bmesh.ops.bisect_plane(bm,geom=list(bm.verts)+list(bm.edges)+list(bm.faces),
                               dist=1e-7,plane_co=a,plane_no=plane_no)
    # BMesh cutting can introduce submicron slivers at multiple cut-plane
    # intersections. Weld only NEW LP vertices under 10 microns; this is
    # much smaller than the 1 mm geometry acceptance tolerance.
    bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=1e-5)
    bmesh.ops.dissolve_degenerate(bm,edges=list(bm.edges),dist=1e-5)
    deg=[f for f in bm.faces if f.calc_area()<1e-10]
    if deg:bmesh.ops.delete(bm,geom=deg,context="FACES_ONLY")
    bm.normal_update()
    expected_y=float(source.get("source_normal_y",-1.))
    actual_y=sum(float(f.normal.y) for f in bm.faces)/max(1,len(bm.faces))
    if abs(expected_y)>0.9 and actual_y*expected_y<0:
        bmesh.ops.reverse_faces(bm,faces=list(bm.faces))
    bm.to_mesh(mesh);bm.free();mesh.update()
    uvmetrics=_assign_uv(mesh,source["source_faces"])
    metrics.update(uvmetrics)
    metrics["faces_after_source_seam_cuts"]=len(mesh.polygons)
    if material is not None:mesh.materials.append(material)
    obj["W58_4_SOURCE_HOST"]=host.name
    obj["W58_4_TOPOLOGY"]=solution["category"]
    obj["W58_4_WIDTH_M"]=float(solution["width"])
    obj["W58_4_SOURCE_UV_TRANSFER"]="FACE_AFFINE_PIECEWISE"
    obj["W58_4_METRICS"]=json.dumps(metrics)
    obj["W58_4_SOURCE_SNAPSHOT"]=json.dumps(source,separators=(",",":"))
    bpy.context.scene.collection.objects.link(obj)
    glass=[]
    from . import material_system
    mat_glass=material_system.ensure_glass_material()
    for k,ring in enumerate(solution["inner_loops"]):
        translated=[(x+dx,z+dz) for x,z in ring]
        gm,area=_new_mesh("W58_4_GLASS_"+str(host.get("W58_4_CASE",host.name))+"_"+str(k),[translated],source["y"]-.012)
        gm.materials.append(mat_glass)
        go=bpy.data.objects.new(gm.name,gm)
        bpy.context.scene.collection.objects.link(go)
        go.parent=obj
        go.matrix_parent_inverse.identity()
        go["W58_4_GLASS_CANDIDATE"]=True
        glass.append(go)
    return obj,glass,metrics


def commit_preview(host):
    """Experimental QA commit: remove selected source faces after preview proof.

    Caller must first verify its LP preview. Keep original wall source snapshot
    in LP data. No UI operator; not a production atomic transaction yet.
    """
    if host.mode!="OBJECT":
        raise ValueError("HOST_MUST_BE_IN_OBJECT_MODE")
    bm=bmesh.new();bm.from_mesh(host.data)
    selected=[f for f in bm.faces if f.select]
    if not selected:bm.free();raise ValueError("NOTHING_TO_COMMIT")
    bmesh.ops.delete(bm,geom=selected,context="FACES")
    bm.to_mesh(host.data);bm.free();host.data.update()
    return len(selected)
