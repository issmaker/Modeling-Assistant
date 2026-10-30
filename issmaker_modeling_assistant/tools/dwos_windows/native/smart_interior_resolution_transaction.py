"""W58.9: irreversible geometry is NOT touched before full staging.

GAP publishes two independently valid shell meshes in ONE existing transactional
publisher. MERGE creates ONE common shell, keeps the two originals as hidden
recoverable sources, and blocks subsequent editing of their frames until the
merge is explicitly reverted. Blender operator owns the Undo step.
"""
from __future__ import annotations
import bpy
import json
import hashlib
import math
from . import smart_interior_scene as stage
from .smart_interior_3d_modes import classify_pair,stage_gap,stage_merge_exact
from .spatial_safety_core import self_intersections,mesh_intersections

def _signature(obj):
    payload=stage._mesh_data(obj)
    return hashlib.sha256(stage.encode(payload).encode("utf8")).hexdigest()

def _get_pair(scene,name_a,name_b):
    a=scene.objects.get(name_a);b=scene.objects.get(name_b)
    if not a or not b or a==b:
        raise ValueError("INTERIOR_RESOLVE_STALE_PAIR")
    if any(o.type!="MESH" or o.get("DWOS_role")!="INTERIOR"
           or not o.parent or
           o.parent.name not in scene.objects for o in (a,b)):
        raise ValueError("INTERIOR_RESOLVE_INVALID_SOURCE_OBJECTS")
    if any(o.get("DWOS_W58_9_active_group") for o in (a,b)):
        raise ValueError("INTERIOR_RESOLVE_ALREADY_MERGED")
    return a,b

def _source_world(a,b):
    return tuple(stage._world(stage._mesh_data(o),o.matrix_world)
                 for o in (a,b))

def gap_commit(scene,name_a,name_b,*,clearance,expected_sha=None):
    a,b=_get_pair(scene,name_a,name_b)
    originals=[_signature(a),_signature(b)]
    if expected_sha is not None and originals!=list(expected_sha):
        raise ValueError("INTERIOR_STALE_SINCE_USER_DECISION")
    wa,wb=_source_world(a,b)
    decisions=classify_pair(wa,wb,clearance=clearance)
    if decisions["status"].startswith("NO_CONTACT"):
        return {"status":"NO_CONTACT","objects":[a.name,b.name]}
    staged,metadata=stage_gap(wa,wb,clearance=clearance,mode="BALANCED")
    if mesh_intersections(staged["A"]["verts"],staged["A"]["faces"],
                          staged["B"]["verts"],staged["B"]["faces"],max_hits=1):
        raise ValueError("GAP_COMMIT_STILL_INTERSECTING")
    prepared=[]
    for obj,key,peer in ((a,"A",b),(b,"B",a)):
        local=stage._local(staged[key],obj.matrix_world)
        stage._validate_publish_mesh(local,obj.name)
        if self_intersections(local["verts"],local["faces"]):
            raise ValueError("GAP_COMMIT_SELF_INTERSECTIONS_"+obj.name)
        # Preserve originally-authored canonical source for full re-edit.
        canonical=stage.decode(obj)
        prepared.append({"object":obj,"mesh":local,
            "canonical":canonical,"links":[peer.parent.name],
            "replace_geometry":True})
    stage.publish_neighbors(prepared)
    # No further throwing operations after committing the transaction.
    return {"status":"COMMITTED_GAP","objects":[a.name,b.name],
            "policy":metadata.get("policy"),
            "new_signatures":[_signature(a),_signature(b)]}

def merge_commit(scene,name_a,name_b,*,expected_sha=None):
    a,b=_get_pair(scene,name_a,name_b)
    original=[_signature(a),_signature(b)]
    if expected_sha is not None and original!=list(expected_sha):
        raise ValueError("INTERIOR_STALE_SINCE_USER_DECISION")
    # A geometrically valid union is not an authorized material/UV rewrite.
    # Decline if we cannot preserve the exact material assignment safely.
    for obj in (a,b):
        if len(obj.data.uv_layers) or obj.data.shape_keys is not None:
            raise ValueError("MERGE_UV_OR_SHAPEKEY_AUTHORING_PROTECTED")
        if len(obj.vertex_groups):
            raise ValueError("MERGE_VERTEX_GROUP_AUTHORING_PROTECTED")
        blender_builtin={
            "position",".edge_verts","material_index","sharp_face",
            "sharp_edge","crease_edge",".corner_vert",".corner_edge",
            ".select_vert",".select_edge",".select_poly",
            ".hide_vert",".hide_edge",".hide_poly"}
        if any(attr.name not in blender_builtin for attr in obj.data.attributes):
            raise ValueError("MERGE_CUSTOM_ATTRIBUTE_AUTHORING_PROTECTED")
        if any(poly.material_index!=0 for poly in obj.data.polygons):
            raise ValueError("MERGE_PER_FACE_MATERIAL_AUTHORED_PROTECTED")
    mats=tuple(tuple(material.name if material else "" for material in
                     obj.data.materials) for obj in (a,b))
    if mats[0]!=mats[1]:
        raise ValueError("MERGE_DIFFERENT_MATERIALS_NEEDS_PROTECTED_REMAP")
    wa,wb=_source_world(a,b)
    c=classify_pair(wa,wb)
    if "MERGE" not in c.get("mode_options",()):
        raise ValueError("INTERIOR_MERGE_DIFFERENT_HEIGHTS_GAP_ONLY")
    shared,meta=stage_merge_exact(wa,wb,scene=scene)
    stage._validate_publish_mesh(shared,"MERGED_3D_SHARED")
    if self_intersections(shared["verts"],shared["faces"]):
        raise ValueError("MERGE_COMMIT_SELF_INTERSECTION")
    # Protect user materials/UV/transforms; a shared object replaces neither
    # source mesh and is always a separate Scene-owned reversible object.
    out_mesh=None;merged=None
    old_states=[(o,o.hide_get(),bool(o.hide_render),
                 "DWOS_W58_9_active_group" in o,
                 o.get("DWOS_W58_9_active_group")) for o in (a,b)]
    try:
        out_mesh=bpy.data.meshes.new("DWOS_W58_9_MERGED_INTERIOR_DATA")
        out_mesh.from_pydata(shared["verts"],[],shared["faces"])
        out_mesh.validate(clean_customdata=False);out_mesh.update()
        for mat in a.data.materials:out_mesh.materials.append(mat)
        if len(out_mesh.polygons)!=len(shared["faces"]):
            raise ValueError("MERGE_COMMIT_MESH_TOPOLOGY_CHANGED")
        merged=bpy.data.objects.new("DWOS_W58_9_SHARED_INTERIOR",out_mesh)
        scene.collection.objects.link(merged)
        merged["DWOS_role"]="INTERIOR_SHARED_GROUP"
        merged["DWOS_W58_9_group_sources"]=json.dumps([a.name,b.name])
        merged["DWOS_W58_9_input_hashes"]=json.dumps(original)
        merged["DWOS_W58_9_source_visibility"]=json.dumps({
            obj.name:{"hidden":bool(hidden),"hide_render":bool(render)}
            for obj,hidden,render,_,_ in old_states})
        merged["DWOS_W58_9_policy"]="MERGE_EXACT_NEAR_LEVEL"
        merged["DWOS_W58_9_meta"]=json.dumps(meta,default=str)[:16000]
        # Source shells remain in Blender for exact reversible unmerge.
        for o in (a,b):
            o["DWOS_W58_9_active_group"]=merged.name
            o.hide_render=True
            o.hide_set(True)
    except Exception:
        for o,visible,render,had,val in old_states:
            if had:o["DWOS_W58_9_active_group"]=val
            elif "DWOS_W58_9_active_group" in o:
                del o["DWOS_W58_9_active_group"]
            o.hide_render=render;o.hide_set(visible)
        if merged is not None:bpy.data.objects.remove(merged,do_unlink=True)
        if out_mesh is not None and out_mesh.users==0:
            bpy.data.meshes.remove(out_mesh)
        raise
    return {"status":"COMMITTED_MERGE","shared":merged.name,
            "sources":[a.name,b.name],
            "original_hashes":original,
            "common_mesh_faces":len(merged.data.polygons)}

def unmerge(scene,shared_name):
    merged=scene.objects.get(shared_name)
    if merged is None or merged.get("DWOS_role")!="INTERIOR_SHARED_GROUP":
        raise ValueError("MERGED_GROUP_NOT_FOUND")
    names=json.loads(merged.get("DWOS_W58_9_group_sources","[]"))
    if len(names)!=2:raise ValueError("MERGED_GROUP_CORRUPTED")
    originals=[]
    for name in names:
        obj=scene.objects.get(name)
        if obj is None or obj.get("DWOS_W58_9_active_group")!=merged.name:
            raise ValueError("MERGED_GROUP_ORIGINAL_MISSING_OR_MODIFIED")
        originals.append(obj)
    verified=json.loads(merged["DWOS_W58_9_input_hashes"])
    if [_signature(o) for o in originals]!=verified:
        raise ValueError("MERGED_GROUP_SOURCE_GEOMETRY_CHANGED")
    visibility=json.loads(merged.get(
        "DWOS_W58_9_source_visibility","{}"))
    for obj in originals:
        values=visibility.get(obj.name,{"hidden":False,"hide_render":False})
        obj.hide_set(bool(values["hidden"]))
        obj.hide_render=bool(values["hide_render"])
        del obj["DWOS_W58_9_active_group"]
    me=merged.data
    bpy.data.objects.remove(merged,do_unlink=True)
    if me.users==0:bpy.data.meshes.remove(me)
    return {"status":"RESTORED_ORIGINALS","objects":names}

def guard_edit_on_merged_group(frame_obj):
    # Call BEFORE ANY staged Mesh mutation in the editor.
    for child in frame_obj.children:
        if child.get("DWOS_role")=="INTERIOR":
            owner=child.get("DWOS_W58_9_active_group")
            if owner:
                raise ValueError(
                    "DWOS_MERGED_GROUP_LOCKED: first revert shared Interior "
                    +str(owner)+" before editing this window")


def global_staggered_gap_commit(scene,names,*,clearance,expected_sha=None):
    """W58.10 conservative staggered-Z group mode: 3D-certified pair iteration.

    Every pair is evaluated with its true Z range. Only after *all* final
    physical contacts, heights and mouth positions pass do we atomically
    publish the entire connected group. A failed case leaves all originals
    unchanged; no partial pair publication or 2D projection is allowed.
    """
    from .smart_interior_3d_modes import classify_pair,stage_gap
    from .interior_footprint_core import footprint
    from .spatial_safety_core import mesh_intersections
    from . import smart_interior_conflict_ui as nav
    import math
    names=tuple(sorted(set(names)))
    if not 3<=len(names)<=8:
        raise ValueError("GLOBAL_MIXED_Z_REQUIRES_3_TO_8_INTERIORS")
    objects=[]
    for name in names:
        obj=scene.objects.get(name)
        if (obj is None or obj.type!="MESH" or obj.get("DWOS_role")!="INTERIOR"
                or obj.parent is None or obj.get("DWOS_W58_9_active_group")):
            raise ValueError("GLOBAL_MIXED_Z_INVALID_SOURCE:"+name)
        objects.append(obj)
    sha=[_signature(o) for o in objects]
    if expected_sha is not None and sha!=list(expected_sha):
        raise ValueError("GLOBAL_MIXED_Z_STALE_SOURCE")
    raw={o.name:stage._world(stage._mesh_data(o),o.matrix_world)
         for o in objects}
    def foot(mesh):
        result=footprint(mesh)
        if result is None:
            raise ValueError("GLOBAL_MIXED_Z_NONPRISMATIC_UNSUPPORTED")
        return result
    original={k:foot(mesh) for k,mesh in raw.items()}
    source=set(names)
    issues=[]
    for a in names:
        for b in names:
            if a>=b:continue
            outcome=classify_pair(raw[a],raw[b],clearance=clearance)
            if outcome["status"].startswith("UNSUPPORTED"):
                raise ValueError("GLOBAL_MIXED_Z_UNCERTAIN_CONTACT_CLASSIFICATION")
            if not outcome["status"].startswith("NO_CONTACT"):
                issues.append((a,b))
    if not issues:
        return {"status":"NO_CONTACT","objects":list(names)}
    edges={k:set() for k in names}
    for a,b in issues:edges[a].add(b);edges[b].add(a)
    visited=set();stack=[names[0]]
    while stack:
        k=stack.pop()
        if k in visited:continue
        visited.add(k)
        stack.extend(edges[k]-visited)
    if len(visited)!=len(names):
        raise ValueError("GLOBAL_MIXED_Z_DISCONNECTED_COMPONENT")
    for event in nav.scan(scene):
        if ((event["a"] in source or event["b"] in source) and
            (event["a"] not in source or event["b"] not in source)):
            raise ValueError("GLOBAL_MIXED_Z_UNSELECTED_NEIGHBOR")
    # Additional clearance guards float32 Blender Mesh world-coordinate
    # quantization. Refuse if ULP is too large to preserve millimetre quality.
    coordinate_scale=max(abs(float(v)) for mesh in raw.values()
        for vertex in mesh["verts"] for v in vertex)
    ulp=math.ldexp(1.,math.frexp(max(1.,coordinate_scale))[1]-24)
    if ulp>.002:
        raise ValueError("GLOBAL_MIXED_Z_WORLD_COORDINATE_PRECISION_INSUFFICIENT")
    quantization_guard_m=max(.0001,4.*ulp)
    solving_clearance=clearance+quantization_guard_m
    staged={k:v for k,v in raw.items()}
    operations=0
    for _ in range(8):
        active=[]
        for a in names:
            for b in names:
                if a>=b:continue
                decision=classify_pair(staged[a],staged[b],clearance=solving_clearance)
                if decision["status"].startswith("UNSUPPORTED"):
                    raise ValueError("GLOBAL_MIXED_Z_POST_CLIP_CLASSIFICATION_UNCERTAIN")
                if not decision["status"].startswith("NO_CONTACT"):
                    active.append((a,b))
        if not active:break
        for a,b in active:
            # Reevaluate after every clip: a previous operation can resolve
            # another pair or make a later candidate impossible.
            decision=classify_pair(staged[a],staged[b],clearance=solving_clearance)
            if decision["status"].startswith("NO_CONTACT"):continue
            trial,info=stage_gap(staged[a],staged[b],clearance=solving_clearance,mode="BALANCED")
            staged[a],staged[b]=trial["A"],trial["B"]
            operations+=1
    # INDEPENDENT FINAL ORACLE: 3D Z as well as exact surface geometry.
    remain=[]
    for a in names:
        for b in names:
            if a>=b:continue
            pair=classify_pair(staged[a],staged[b],clearance=clearance)
            if not pair["status"].startswith("NO_CONTACT"):
                remain.append((a,b,pair["status"]))
            if mesh_intersections(staged[a]["verts"],staged[a]["faces"],
                                  staged[b]["verts"],staged[b]["faces"],max_hits=1):
                remain.append((a,b,"EXACT_TRIANGLES_INTERSECT"))
    if remain:
        raise ValueError("GLOBAL_MIXED_Z_UNRESOLVED:"+str(remain[:8]))
    # Do not change actual Z interval. Front mouth source endpoints are
    # retained within a strictly bounded 2mm clipping tolerance.
    max_mouth_change=0.
    for name in names:
        prev=original[name];after=foot(staged[name])
        if (abs(float(prev["zlo"])-float(after["zlo"]))>1e-5 or
            abs(float(prev["zhi"])-float(after["zhi"]))>1e-5):
            raise ValueError("GLOBAL_MIXED_Z_AUTHORED_HEIGHT_CHANGED")
        u,v=prev["front"];x,y=after["front"]
        def d(p,q):return math.hypot(float(p[0]-q[0]),float(p[1]-q[1]))
        drift=min(max(d(u,x),d(v,y)),max(d(u,y),d(v,x)))
        max_mouth_change=max(max_mouth_change,drift)
        if drift>.002+1e-7:
            raise ValueError(f"GLOBAL_MIXED_Z_MOUTH_MOVED:{name}:{drift:.6f}m")
    updates=[]
    for obj in objects:
        local=stage._local(staged[obj.name],obj.matrix_world)
        stage._validate_publish_mesh(local,obj.name)
        if self_intersections(local["verts"],local["faces"]):
            raise ValueError("GLOBAL_MIXED_Z_SELF_INTERSECTION:"+obj.name)
        updates.append({"object":obj,"mesh":local,
            "canonical":stage.decode(obj),
            "links":sorted(edges[obj.name]),"replace_geometry":True})
    # Simulate the exact f32 world/local/world publication path before any
    # destructive mesh write, including the source's real matrix_world.
    published={entry["object"].name:
        stage._world(entry["mesh"],entry["object"].matrix_world)
        for entry in updates}
    precision_failures=[]
    for a in names:
        for b in names:
            if a>=b:continue
            test=classify_pair(published[a],published[b],clearance=clearance)
            if not test["status"].startswith("NO_CONTACT"):
                precision_failures.append((a,b,test["status"],
                    test.get("xy_clearance_m")))
            if mesh_intersections(published[a]["verts"],published[a]["faces"],
                                  published[b]["verts"],published[b]["faces"],
                                  max_hits=1):
                precision_failures.append((a,b,"FLOAT32_TRIANGLE_CONTACT"))
    if precision_failures:
        raise ValueError("GLOBAL_MIXED_Z_FLOAT32_PREFLIGHT_FAILED:"+
                         str(precision_failures[:8]))
    stage.publish_neighbors(updates)
    return {"status":"COMMITTED_GLOBAL_3D_Z_GAP",
            "objects":list(names),"updated":len(updates),
            "initial_contact_pairs":len(issues),
             "solver_steps":operations,
            "quantization_guard_m":quantization_guard_m,
            "max_front_mouth_trim_m":max_mouth_change,
            "authored_z_preserved":True}

def global_gap_commit(scene,names,*,clearance,expected_sha=None):
    """One atomic joint solve, never sequential pair clipping.

    Limited to genuinely connected 3+ upright simple-shell PLANAR Interiors
    with near-consistent Z bounds. More complex height mixes must use
    independently validated 3D GAP decisions, not approximate one global plane.
    """
    from .smart_interior_core import contact_graph,solve_group
    from .interior_footprint_core import footprint
    from . import smart_interior_conflict_ui as nav
    names=tuple(sorted(set(names)))
    if not 3<=len(names)<=12:
        raise ValueError("GLOBAL_GAP_REQUIRES_3_TO_12_INTERIORS")
    objects=[]
    for name in names:
        obj=scene.objects.get(name)
        if (obj is None or obj.type!="MESH" or obj.get("DWOS_role")!="INTERIOR"
                or obj.parent is None or obj.get("DWOS_W58_9_active_group")):
            raise ValueError("GLOBAL_GAP_INVALID_OR_MERGED_SOURCE:"+name)
        objects.append(obj)
    originals=[_signature(o) for o in objects]
    if expected_sha is not None and originals!=list(expected_sha):
        raise ValueError("GLOBAL_GAP_STALE_SELECTION")
    meshes={o.name:stage._world(stage._mesh_data(o),o.matrix_world) for o in objects}
    feet=[footprint(m) for m in meshes.values()]
    if any(f is None for f in feet):
        raise ValueError("GLOBAL_GAP_NONPRISMATIC_GEOMETRY_UNSUPPORTED")
    # The solver is deliberately restricted to aligned height classes, to
    # prevent the 2D branch from hallucinating a Z-compatible gap.
    if (max(f['zlo'] for f in feet)-min(f['zlo'] for f in feet)>.01000001
       or max(f['zhi'] for f in feet)-min(f['zhi'] for f in feet)>.01000001):
        return global_staggered_gap_commit(
            scene,names,clearance=clearance,expected_sha=expected_sha)
    classes={name:"PLANAR" for name in names}
    pairs=contact_graph(meshes,clearance=clearance,classes=classes)
    if not pairs:
        return {"status":"NO_CONTACT","objects":names}
    links={name:set() for name in names}
    for a,b in pairs:links[a].add(b);links[b].add(a)
    visited=set();stack=[names[0]]
    while stack:
        a=stack.pop()
        if a in visited:continue
        visited.add(a);stack.extend(links[a]-visited)
    if len(visited)!=len(names):
        raise ValueError("GLOBAL_GAP_DISCONNECTED_SELECTION_USE_COMPONENTS")
    # The full issue graph matters. A user must never commit only part of a
    # connected contact component and silently collide with an unselected peer.
    source=set(names)
    for event in nav.scan(scene):
        if ((event["a"] in source or event["b"] in source)
                and (event["a"] not in source or event["b"] not in source)):
            raise ValueError("GLOBAL_GAP_UNSELECTED_CONNECTED_NEIGHBOR")
    staged,meta=solve_group(meshes,clearance=clearance,mode="BALANCED",classes=classes)
    remain=contact_graph(staged,clearance=clearance,classes=classes)
    if remain:raise ValueError("GLOBAL_GAP_SOLVER_CONTACTS_REMAIN:"+str(remain))
    updates=[]
    for obj in objects:
        local=stage._local(staged[obj.name],obj.matrix_world)
        stage._validate_publish_mesh(local,obj.name)
        if self_intersections(local["verts"],local["faces"]):
            raise ValueError("GLOBAL_GAP_SELF_INTERSECTION:"+obj.name)
        updates.append({"object":obj,"mesh":local,
            "canonical":stage.decode(obj),
            "links":sorted(links[obj.name]),"replace_geometry":True})
    stage.publish_neighbors(updates)
    return {"status":"COMMITTED_GLOBAL_GAP","objects":names,
            "initial_contact_pairs":len(pairs),
            "updated":len(updates),"policy":meta.get("policy")}
