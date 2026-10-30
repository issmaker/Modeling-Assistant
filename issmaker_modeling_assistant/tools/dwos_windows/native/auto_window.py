"""W58: Selected UV Wall faces -> adaptive editable DWOS windows.
Reuses native tested Adaptive Opening Core and existing Window Composer.
No second HUD, no flattening of multi-planar facade, no hidden imposts.
"""
from __future__ import annotations
import bpy
import bmesh
import json
import math
import uuid
import hashlib
from collections import deque
from types import SimpleNamespace
from mathutils import Vector
from bpy.props import EnumProperty, StringProperty
from . import adaptive_opening_core, opening_detection, window_builder, source_lp_uv_core
from . import closed_wall_exterior_core
from .numeric import format_meter, parse_meter_text

OWNER = "DWOS_W58_auto_uid"
HOST = "DWOS_W58_auto_host"
ROLE = "DWOS_W58_auto_role"
LP_COLLECTION = "ISSMAKER_WINDOWS_LP"
FRAME = "FRAME"
GLASS = "GLASS"


def _groups(bm):
    """Connected FACE islands; shared module borders do NOT split a window."""
    selected={f for f in bm.faces if f.select}
    result=[]
    while selected:
        face=selected.pop()
        group={face}
        todo=[face]
        while todo:
            current=todo.pop()
            for edge in current.edges:
                for other in edge.link_faces:
                    if other in selected:
                        selected.remove(other)
                        group.add(other)
                        todo.append(other)
        result.append(group)
    return sorted(result,key=lambda g:min(f.index for f in g))


def _boundary(group):
    """One simple closed loop, exactly one selected face on each boundary edge."""
    edges={edge for face in group for edge in face.edges
           if sum(linked in group for linked in edge.link_faces)==1}
    if len(edges)<4:
        raise ValueError("NO_OPENING_PERIMETER: выделите замкнутую область фасада")
    graph={}
    for edge in edges:
        a,b=edge.verts
        graph.setdefault(a,[]).append(b)
        graph.setdefault(b,[]).append(a)
    if any(len(items)!=2 for items in graph.values()):
        raise ValueError("BRANCHED_OR_HOLES: разрывы или отверстия внутри выбранной области")
    start=min(graph,key=lambda v:v.index)
    chain=[start]
    seen={start}
    prev=None
    current=start
    while True:
        options=graph[current]
        nxt=options[0] if options[0] is not prev else options[1]
        if nxt is start:break
        if nxt in seen or len(chain)>len(edges):
            raise ValueError("SELF_CROSSING_OR_MULTIPLE_BOUNDARIES")
        chain.append(nxt);seen.add(nxt)
        prev,current=current,nxt
    if len(chain)!=len(edges):
        raise ValueError("MULTIPLE_BOUNDARY_LOOPS: выберите отдельные участки фасада")
    return chain


def _topology(loop,faces,host):
    # All coordinates in original host-local space. Never apply transforms.
    samples=[{"point":tuple(float(x) for x in f.calc_center_median()),
              "normal":tuple(float(x) for x in f.normal)} for f in faces]
    matrix=host.matrix_world.to_3x3()
    up=matrix.inverted()@Vector((0.,0.,1.))
    up.normalize()
    # W58.21: Source UV Wall normal winding is not sufficient to determine
    # exterior for a CORNER. Its *authored closed path* is authoritative.
    # Independent of face selection order and isolated flipped host polygons.
    path_meta={"status":"NO_AUTHORITATIVE_CLOSED_PATH"}
    path_name=str(host.get("path_object",""))
    source_path=bpy.data.objects.get(path_name) if path_name else None
    if (host.get("uvwall_result_type")=="WALL"
            and str(host.get("wall_normal_mode",""))=="OUTWARD"
            and source_path and source_path.type=="MESH"
            and host.get("uvwall_owner_uid")
             == source_path.get("uvwall_definition_uid")
            and abs(up.x)<.001 and abs(up.y)<.001):
        try:
            to_host=host.matrix_world.inverted() @ source_path.matrix_world
            verts=[tuple(to_host@v.co) for v in source_path.data.vertices]
            edges=[tuple(edge.vertices) for edge in source_path.data.edges]
            ring,area=closed_wall_exterior_core.ordered_ring(verts,edges)
            samples,path_meta=closed_wall_exterior_core.orient_face_normals(
                samples,ring,area)
            path_meta["reference_path"]=source_path.name
            path_meta["host_path_uid_match"]=True
        except ValueError as exc:
            path_meta={"status":"UNCERTAIN_REFERENCE",
                       "reason":str(exc),
                       "reference_path":source_path.name}
    args=dict(vertex_ids=[v.index for v in loop],
              adjacent_normal_samples=samples,
              up_hint=tuple(up),
              source="W58_USER_SELECTED_FACES")
    points=[tuple(float(x) for x in v.co) for v in loop]
    topology=adaptive_opening_core.build_opening_topology(points,**args)
    # A snake with multiple moderate turns is one longitudinal window.
    # DO NOT forcibly flatten and DO NOT bypass miter/fold/height gates.
    if not topology.get("build_supported") and topology.get("reasons")==[
            "MULTI_HARD_JUNCTION_REQUIRES_MULTIPLE_WINDOWS"]:
        retry=adaptive_opening_core.build_opening_topology(
            points,hard_corner_deg=180.,**args)
        if (retry.get("build_supported")
                and retry.get("max_turn_deg",180.)<179.5
                and all(float(st.get("miter_scale",float("inf")))<=4.0
                        for st in retry.get("stations",[]))):
            topology=retry
            topology["W58_classification"]="SNAKE_MULTIBEND"
            topology["W58_preserved_original_kinks"]=True
    if (topology.get("build_supported")
            and topology.get("classification")=="CORNER"
            and len(topology.get("stations",[]))>3):
        # The legacy CORNER kernel guarantees EXACTLY two CAD branches.
        # A long, faceted, multi-module L/S curve with one sharp turn must
        # use its piecewise faceted CURVED generator, not the two-pane solver.
        retry=adaptive_opening_core.build_opening_topology(
            points,hard_corner_deg=180.,**args)
        if retry.get("build_supported") and retry.get("max_turn_deg",180.)<=95.:
            topology=retry
            # A semantic CAD kink is not always an isolated two-branch window.
            # Protect the strict legacy 2-pane CORNER kernel from 3+ branches.
            topology["classification"]="CURVED"
            topology["glass_policy"]="SEGMENTED_PLANAR"
            topology["hard_junction_station_indices"]=[]
            topology["corner_junctions"]=[]
            topology["W58_classification"]="MULTIMODULE_BEND"
    topology["W5821_closed_path_orientation"]=path_meta
    if not topology.get("build_supported"):
        raise ValueError("UNSUPPORTED_ADAPTIVE_PATCH: "
                         +topology.get("classification","UNKNOWN") + " "
                         +",".join(topology.get("reasons",[])))
    if topology["width"]<=.20 or topology["height"]<=.20:
        raise ValueError("OPENING_TOO_SMALL")
    if topology["width"]>150 or len(topology["stations"])>1024:
        raise ValueError("RESOURCE_LIMIT: opening too long for one window")
    return topology


def selected_patches(context):
    host=context.active_object
    if not host or host.type!="MESH" or host.mode!="EDIT":
        raise ValueError("Edit Mode: выберите полигоны стенового фасада")
    if getattr(host.dwos_window,"is_window",False):
        raise ValueError("Выбран готовый DWOS Window, а не исходная стена")
    if host.data.users!=1 or host.data.shape_keys:
        raise ValueError("SHARED_MESH_OR_SHAPE_KEYS: исходник защищён")
    # W57 has a single-host state machine whose saved face IDs would become
    # stale after any new cut. Never silently corrupt an existing W57 session.
    if host.get("DWOS_surface_session_v1"):
        raise ValueError("W57_SURFACE_SESSION_PRESENT: для нового W58-окна используйте "
                         "новую стену или восстановленную копию исходной стены")
    opening_detection._validate_host_source_contract(host)
    bm=bmesh.from_edit_mesh(host.data)
    bm.verts.ensure_lookup_table();bm.faces.ensure_lookup_table()
    bm.verts.index_update();bm.faces.index_update();bm.normal_update()
    groups=_groups(bm)
    if not groups:raise ValueError("NO_FACES_SELECTED: выделите полигоны будущих окон")
    if len(groups)>100:raise ValueError("RESOURCE_LIMIT: больше 100 окон за проход")
    uv_layer=bm.loops.layers.uv.active
    plans=[]
    for idx,group in enumerate(groups):
        loop=_boundary(group)
        topology=_topology(loop,group,host)
        plans.append({"component":idx,"face_ids":sorted(f.index for f in group),
                      "vertex_ids":[v.index for v in loop],"topology":topology,
                       "source_uv_faces":source_lp_uv_core.capture_face_uv(
                           group,uv_layer,
                           placeholder=(context.scene.get("W5829_window_policy","TEXEL")=="PLACEHOLDER"))})
    return host,plans


def _candidate(host,topology):
    data=opening_detection._adaptive_candidate(topology)
    mw=host.matrix_world
    matrix=mw.to_3x3()
    return SimpleNamespace(
        candidate_id=uuid.uuid4().hex[:12],
        center=mw@Vector(data["center_local"]),
        axis_x=(matrix@Vector(data["axis_x_local"])).normalized(),
        axis_y=(matrix@Vector(data["axis_y_local"])).normalized(),
        axis_z=(matrix@Vector(data["axis_z_local"])).normalized(),
        payload_json=json.dumps(data,separators=(",",":")),
        width_text=format_meter(data["width"]),
        height_text=format_meter(data["height"]),
        wall_depth_text="0")


def _material(name,rgba):
    material=bpy.data.materials.get(name)
    if material is None:
        material=bpy.data.materials.new(name)
        material.diffuse_color=rgba
    return material


def rebuild_lp(frame):
    """Physical simplified LP: frame strips + glazing, in the authored SVD ribbon.
    Use source stations, not a planar bounding rectangle or duplicated HP mesh.
    """
    if not frame.get(OWNER) or not frame.dwos_window.is_window:return None
    data=json.loads(str(frame.dwos_window.opening_payload_json))
    topo=data["adaptive_topology"]
    width=float(topo["width"]);height=float(topo["height"])
    inset=parse_meter_text(frame.dwos_window.frame_width_text)
    if not 0<inset<min(width,height)*.40:
        raise ValueError("LP_INSET_EXCEEDS_APERTURE")
    from . import zone_core
    stations=topo["stations"]
    fw=.5*width;fh=.5*height
    tree=zone_core.decode(str(frame.dwos_window.zone_tree_json or ""))
    leaves,bands=zone_core.layout(tree,width,height,inset)
    # W58.5: slice copies of SOURCE UV Wall faces along the actual
    # intended mullion boundaries BEFORE building LP/HP profiles.
    # Do not touch the original wall mesh or source authored UV payload.
    source_uv_prepared=None
    if frame.get("DWOS_W58_source_lp_uv_json") and bands:
        from . import source_mullion_edge_cuts
        source_uv_prepared,source_cut_metrics=(
            source_mullion_edge_cuts.cut_author_uv_source(frame))
        frame["DWOS_W58_5_mullion_source_cut_metrics"]=json.dumps(
            source_cut_metrics,separators=(",",":"))
        frame["DWOS_W58_5_mullion_source_cut_snapshot"]=json.dumps(
            source_uv_prepared,separators=(",",":"))
    # Every grid boundary includes native authored surface stations. This
    # creates an actual adaptive facade, not a planar bounding box.
    sx={-fw,fw,-fw+inset,fw-inset}
    sx.update(float(st["s"])-fw for st in stations)
    sx.update(float(i)*4.-fw for i in range(1,math.ceil(width/4.)))
    sy={-fh,fh,-fh+inset,fh-inset}
    for _id,axis,center,thickness,rect,band in bands:
        sx.update((float(band[0]),float(band[1])))
        sy.update((float(band[2]),float(band[3])))
    for _id,rect in leaves:
        sx.update((float(rect[0]),float(rect[1])))
        sy.update((float(rect[2]),float(rect[3])))
    def merge_micro_steps(values,lo,hi,eps=1e-5):
        # Generated LP-only station/module cuts can differ by 0.1 microns.
        # Never generate zero-area quads from machine precision duplicates.
        result=[]
        for value in sorted(v for v in values if lo-1e-6<=v<=hi+1e-6):
            if not result or value-result[-1]>eps:
                result.append(value)
        return result
    steps=merge_micro_steps(sx,-fw,fw)
    levels=merge_micro_steps(sy,-fh,fh)
    # W58.2-001: LP UV follows actual 3D distance, not arbitrary per-cell S
    # (a cell's S span is not necessarily its physical width at a bend).
    # One cumulative field preserves seam continuity across the whole window.
    physical_u_by_s = {}
    metric_u = 0.0
    previous_point = None
    for station_s in steps:
        point = Vector(adaptive_opening_core.map_svd_local(topo, station_s, 0., 0.))
        if previous_point is not None:
            segment_m = (point - previous_point).length
            if not math.isfinite(segment_m) or segment_m < 1.e-9:
                raise ValueError("LP_PHYSICAL_UV_COLLAPSED_STATION")
            metric_u += segment_m * 1000. / 4096.
        physical_u_by_s[round(float(station_s), 7)] = metric_u
        previous_point = point
    verts=[];faces=[];roles=[];uv_values=[];indices={}
    def add(s,v):
        key=(round(s,7),round(v,7))
        if key not in indices:
            local=adaptive_opening_core.map_svd_local(topo,s,v,0.)
            indices[key]=len(verts)
            verts.append(local)
        return indices[key]
    def quad(coords,role):
        ids=[add(s,v) for s,v in coords]
        if len(set(ids))<4:raise ValueError("LP_DEGENERATE_FRAME_CELL")
        pp=[Vector(verts[i]) for i in ids]
        area=(pp[1]-pp[0]).cross(pp[3]-pp[0]).length
        if area<=1e-9:
            raise ValueError(f"LP_DEGENERATE_FACE_GEOMETRY: role={role} coords={coords} area={area:.12g}")
        faces.append(ids);roles.append(role)
        # W58.2-001: same U at every shared station; every face including GLASS
        # has nondegenerate UV. Vertical V is physically metric along elevation.
        uv_values.append([(physical_u_by_s[round(float(s), 7)],
                           (v+fh)*1000./4096.) for s,v in coords])
    for left,right in zip(steps,steps[1:]):
        if right-left<1e-7:continue
        for bottom,top in zip(levels,levels[1:]):
            if top-bottom<1e-7:continue
            mid=((left+right)*.5,(bottom+top)*.5)
            if (mid[0] < -fw+inset-1e-6 or mid[0]>fw-inset+1e-6
                    or mid[1]<-fh+inset-1e-6 or mid[1]>fh-inset+1e-6):
                role="FRAME"
            elif any(band[0]-1e-7<=mid[0]<=band[1]+1e-7
                     and band[2]-1e-7<=mid[1]<=band[3]+1e-7
                     for _id,_axis,_center,_thickness,_rect,band in bands):
                role="MULLION"
            else:
                leaf=next((zone_core.find(tree,zid) for zid,rect in leaves
                           if rect[0]-1e-7<=mid[0]<=rect[1]+1e-7
                           and rect[2]-1e-7<=mid[1]<=rect[3]+1e-7),None)
                role=str((leaf or {}).get("fill","GLASS"))
            if role=="EMPTY":continue
            if role not in ("FRAME","MULLION","GLASS","PANEL"):
                raise ValueError("LP_UNKNOWN_ZONE_ROLE:"+role)
            quad([(left,bottom),(right,bottom),(right,top),(left,top)],role)
    source_blob=str(frame.get("DWOS_W58_source_lp_uv_json",""))
    if source_blob:
        # A generated LP cell may span several SOURCE-CUT polygons. Projecting
        # the whole cell against only one subdivided polygon would be wrong.
        # Keep original source UV as the authoritative continuous mapping;
        # cut snapshot retains exact source topology for mullion attachment.
        source_payload=json.loads(source_blob)
        uv_values,projection_stats=source_lp_uv_core.project_lp_uv(
            source_payload,verts,faces)
        frame["DWOS_W58_lp_uv_transfer_error_m"]=float(
            projection_stats["max_position_error_m"])
        frame["DWOS_W58_lp_uv_source_face_count"]=int(
            projection_stats["source_face_count"])
    lp_obj=bpy.data.objects.get(str(frame.get("DWOS_W58_lp_name","")))
    new=bpy.data.meshes.new("DWOS_W58_LP_"+frame.name)
    new.from_pydata(verts,[],faces);new.update()
    # Material is chosen from Window Settings BEFORE the HP object exists.
    from . import material_system
    frame_material=material_system.ensure_frame_material(frame.dwos_window)
    new.materials.append(frame_material)
    new.materials.append(material_system.ensure_glass_material())
    new.materials.append(frame_material)
    new.materials.append(_material("DWOS LP | PANEL",(.20,.22,.23,1.)))
    layer=new.uv_layers.new(name="UVMap")
    for fi,poly in enumerate(new.polygons):
        poly.material_index={"FRAME":0,"GLASS":1,"MULLION":2,"PANEL":3}[roles[fi]]
        if uv_values[fi] is not None:
            for li,uv in zip(poly.loop_indices,uv_values[fi]):
                layer.data[li].uv=uv
    if lp_obj is None:
        lp_obj=bpy.data.objects.new("DWOS_LP_"+frame.name,new)
        collection=bpy.data.collections.get(LP_COLLECTION)
        if collection is None:
            collection=bpy.data.collections.new(LP_COLLECTION)
            bpy.context.scene.collection.children.link(collection)
        collection.objects.link(lp_obj)
        lp_obj.parent=frame
        lp_obj.matrix_parent_inverse=frame.matrix_world.inverted()
        lp_obj.matrix_world=frame.matrix_world.copy()
        lp_obj[OWNER]=str(frame[OWNER])
        lp_obj[ROLE]="LP"
        lp_obj[HOST]=frame.dwos_window.host_name
        frame["DWOS_W58_lp_name"]=lp_obj.name
    else:
        old=lp_obj.data
        lp_obj.data=new
        if old.users==0:bpy.data.meshes.remove(old)
    lp_obj.hide_set(True)
    frame["DWOS_W58_lp_faces"]=len(faces)
    frame["DWOS_W58_lp_mullion_faces"]=sum(r=="MULLION" for r in roles)
    frame["DWOS_W58_lp_zone_count"]=len(leaves)
    return lp_obj



def ensure_interior(frame):
    """W5833: create simple no-UV Interior after LP/HP/Glass validation."""
    if bpy.context.scene.get("W5833_simple_interior",False):
        from . import simple_interior_shell_core
        target,meta=simple_interior_shell_core.build_and_publish(frame)
        return target
    existing=next((obj for obj in frame.children
                   if obj.get("DWOS_role")=="INTERIOR"),None)
    if existing and len(existing.data.polygons)>0:
        return existing
    payload=json.loads(str(frame.dwos_window.opening_payload_json))
    topo=payload["adaptive_topology"]
    width=float(topo["width"]);height=float(topo["height"])
    fw=parse_meter_text(frame.dwos_window.frame_width_text)
    depth=parse_meter_text(frame.dwos_window.interior_depth_text)
    d0=-parse_meter_text(frame.dwos_window.frame_depth_text)
    d1=-depth
    if not (depth>=.8 and depth<=1.5 and d1 < d0-1e-3):
        raise ValueError("INTERIOR_DEPTH_MUST_BE_0.8_TO_1.5_AND_BEHIND_FRAME")
    low=-height*.5+fw
    high=height*.5-fw
    start=-width*.5+fw
    stop=width*.5-fw
    if not high>low+.01 or not stop>start+.01:
        raise ValueError("INTERIOR_INSET_COLLAPSE")
    s_values=sorted({start,stop}|
                    {float(item["s"])-width*.5 for item in topo["stations"]
                     if start<float(item["s"])-width*.5<stop})
    # W58.30: Contact CAD often carries 0.3 micron duplicate stations.
    # At the full Interior depth they must not create a zero-thickness quad.
    # Collapse ONLY when both the front and rear mapped 3D positions
    # are demonstrably within 10um; no authored source geometry is changed.
    _filtered=[]
    for _s in s_values:
        if _filtered and abs(_s-_filtered[-1])<=1e-5:
            old=_filtered[-1]
            deltas=[
                (Vector(adaptive_opening_core.map_svd_local(topo,_s,high,d))
                -Vector(adaptive_opening_core.map_svd_local(topo,old,high,d))).length
                for d in (d0,d1)]
            if max(deltas)<=1e-5:
                continue
        _filtered.append(_s)
    s_values=_filtered
    ids={};verts=[];faces=[]
    def vertex(s,v,d):
        key=tuple(round(float(x),7) for x in (s,v,d))
        if key not in ids:
            ids[key]=len(verts)
            verts.append(adaptive_opening_core.map_svd_local(topo,*key))
        return ids[key]
    def quad(points):
        face=[vertex(*p) for p in points]
        if len(set(face))<4:raise ValueError("INTERIOR_DEGENERATE_QUAD_INDICES")
        ps=[Vector(verts[i]) for i in face]
        if (ps[1]-ps[0]).cross(ps[3]-ps[0]).length<1e-7:
            raise ValueError(f"INTERIOR_DEGENERATE_QUAD_GEOMETRY: coords={points} 3d={[tuple(q) for q in ps]} area2={(ps[1]-ps[0]).cross(ps[3]-ps[0]).length:.9g}")
        faces.append(face)
    for left,right in zip(s_values,s_values[1:]):
        quad(((left,high,d0),(right,high,d0),(right,high,d1),(left,high,d1)))
        quad(((right,low,d0),(left,low,d0),(left,low,d1),(right,low,d1)))
        quad(((left,low,d1),(right,low,d1),(right,high,d1),(left,high,d1)))
    quad(((start,low,d0),(start,high,d0),(start,high,d1),(start,low,d1)))
    quad(((stop,high,d0),(stop,low,d0),(stop,low,d1),(stop,high,d1)))
    # Require one connected manifold shell and an intentionally open front.
    edge_uses={}
    for face in faces:
        for a,b in zip(face,face[1:]+face[:1]):
            key=tuple(sorted((a,b)))
            edge_uses[key]=edge_uses.get(key,0)+1
    if not edge_uses or max(edge_uses.values())>2 or 0 in edge_uses.values():
        raise ValueError("INTERIOR_NON_MANIFOLD_GRAPH")
    expected_open=2*(len(s_values)-1)+2
    if sum(n==1 for n in edge_uses.values())!=expected_open:
        raise ValueError("INTERIOR_OPEN_FRONT_TOPOLOGY_ERROR")
    new=bpy.data.meshes.new("DWOS_W58_INTERIOR_"+frame.name)
    new.from_pydata(verts,[],faces);new.update()
    if len(new.uv_layers)>0:
        bpy.data.meshes.remove(new)
        raise RuntimeError("INTERIOR_UV_PROHIBITED")
    if existing:
        old=existing.data
        existing.data=new
        if old and old.users==0:bpy.data.meshes.remove(old)
        target=existing
    else:
        target=bpy.data.objects.new("DWOS_W58_Interior_"+frame.name,new)
        bpy.context.collection.objects.link(target)
        target.parent=frame
        target.matrix_parent_inverse=frame.matrix_world.inverted()
        target.matrix_world=frame.matrix_world.copy()
        target["DWOS_role"]="INTERIOR"
    target.hide_set(False)
    frame["DWOS_W58_interior_fallback"]="BOX_OPEN_FRONT_SEALED_BACK"
    frame["DWOS_W58_interior_faces"]=len(faces)
    return target

def _remove_new_window(frame):
    for child in list(frame.children):
        if child.type=="MESH":
            mesh=child.data
            bpy.data.objects.remove(child,do_unlink=True)
            if mesh and mesh.users==0:bpy.data.meshes.remove(mesh)
    mesh=frame.data if frame.type=="MESH" else None
    bpy.data.objects.remove(frame,do_unlink=True)
    if mesh is not None and mesh.users==0:bpy.data.meshes.remove(mesh)


def _restore_host_after_failed_cut(context, host, mesh_snapshot):
    """W58.1: recover the original edit-BMesh after ANY failure post host cut.

    BMesh.to_mesh is not legal on an edit-mode Mesh, so leave Edit Mode,
    restore the authoritative snapshot and re-enter Edit Mode. Keep the
    original host Mesh datablock (and its user-facing object) intact.
    """
    if context.view_layer.objects.active is not host:
        host.select_set(True)
        context.view_layer.objects.active = host
    if host.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    mesh_snapshot.to_mesh(host.data)
    host.data.update()
    host.select_set(True)
    context.view_layer.objects.active = host
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bm = bmesh.from_edit_mesh(host.data)
    if len(edit_bm.faces) != len(mesh_snapshot.faces):
        raise RuntimeError("HOST_ROLLBACK_FACE_COUNT_MISMATCH")


def build_from_selection(context):
    """W58.1: build all windows, then atomically cut the host or restore it."""
    host, plans = selected_patches(context)
    bm = bmesh.from_edit_mesh(host.data)
    before = len(bm.faces)
    frames = []
    snapshot = None
    host_cut_started = False
    try:
        for plan in plans:
            c = _candidate(host, plan["topology"])
            frame = window_builder.create_window_from_candidate(
                context, c, host, defer_rebuild=True)
            frames.append(frame)
            frame[OWNER] = uuid.uuid4().hex
            frame[HOST] = host.name
            frame[ROLE] = "HP"
            frame["DWOS_W58_source_face_ids"] = json.dumps(plan["face_ids"])
            frame["DWOS_W58_topology_class"] = plan["topology"].get(
                "W58_classification", plan["topology"]["classification"])
            frame.dwos_window.interior_depth_text = format_meter(
                context.scene.dwos_surface_lab.interior_depth)
            # W58.32 isolated experiments only: parameter sweeps evaluate
            # whether a true geometric collision can be avoided without
            # moving the authored wall or disabling the independent Oracle.
            if "W5832_trial_frame_width_m" in context.scene:
                frame.dwos_window.frame_width_text=format_meter(
                    float(context.scene["W5832_trial_frame_width_m"]))
            if "W5832_trial_frame_depth_m" in context.scene:
                frame.dwos_window.frame_depth_text=format_meter(
                    float(context.scene["W5832_trial_frame_depth_m"]))
            if "W5832_trial_interior_depth_m" in context.scene:
                frame.dwos_window.interior_depth_text=format_meter(
                    float(context.scene["W5832_trial_interior_depth_m"]))
            if plan["topology"]["classification"] == "CORNER":
                frame.dwos_window.corner_post_mode = "OFF"
            # Author-selected mesh positions and UV precede ANY window geometry.
            # W58.30: Placeholder keeps material and geometry but skips
            # all authored-source UV mapping and Brick Hole UV dependencies.
            window_uv_mode=str(context.scene.get("W5829_window_policy","TEXEL"))
            frame["DWOS_W5830_window_uv_mode"]=window_uv_mode
            if window_uv_mode!="PLACEHOLDER":
                source_lp=source_lp_uv_core.prepare_local_payload(
                    plan["source_uv_faces"],frame.matrix_world,host.matrix_world)
                frame["DWOS_W58_source_lp_uv_json"]=json.dumps(
                    source_lp,separators=(",",":"))
                frame["DWOS_W58_source_lp_uv_hash"]=source_lp_uv_core.checksum(source_lp)
            requested_material=bpy.data.materials.get(
                str(context.scene.get("DWOS_W58_frame_material_name","")))
            if requested_material is not None:
                frame.dwos_window.frame_material=requested_material
            # W58.30 guarded experimental simple interior: a continuous
            # open-front ribbon constructed AFTER HP/GLASS. It explicitly
            # avoids the experimental self-union while retaining an Interior.
            if (context.scene.get("W5830_trial_simple_interior",False)
                    and plan["face_ids"]==json.loads(
                        context.scene.get("W5830_simple_interior_face_ids","[]"))):
                frame.dwos_window.interior_mode="OFF"
                frame["W5830_interior_lab"]="SIMPLE_RIBBON_SKIP_NATIVE_UNION"
            # LP-FIRST: Width and zone layout before HP depth/glazing/interior.
            lp=rebuild_lp(frame)
            if lp is None or not len(lp.data.polygons):
                raise RuntimeError("LP_FIRST_NO_GEOMETRY")
            frame["DWOS_W58_pipeline_stage"]="LP_PREPARED"
            window_builder.rebuild_window(frame)
            interior = ensure_interior(frame)
            if interior is None or not len(interior.data.polygons):
                raise RuntimeError("INTERIOR_MISSING_AFTER_NATIVE_OR_FALLBACK")
            frame["DWOS_W58_pipeline_stage"]="HP_GLASS_INTERIOR_READY"
            # W58.28 experimental opt-in; native frame remains the authority.
            # NEVER block an already valid WINDOW for an optional post.
            if (context.scene.get("W5828_enable_user_post_solver",False)
                    and frame.get("DWOS_W58_topology_class")=="CORNER"):
                from . import user_reference_corner_solver_w5828
                current_hp=frame.data
                current_glass=next((obj for obj in frame.children
                    if obj.get("DWOS_role")=="GLASS"),None)
                old_glass=current_glass.data if current_glass else None
                try:
                    geometry=user_reference_corner_solver_w5828.solve_native(frame)
                    frame["W5828_corner_auto_result"]=json.dumps(geometry)
                except Exception as optional_error:
                    # Recover source HP/Glass; the main window remains valid.
                    frame.data=current_hp
                    if current_glass and old_glass:current_glass.data=old_glass
                    frame["W5828_user_post_warning"]="OPTIONAL_POST_FAILED_BUT_WINDOW_CREATED"
                    frame["W5828_optional_post_error"]=repr(optional_error)
            frame["DWOS_W58_ready"] = True

        # Capture ALL edit-mode mesh data (including UV and face selection)
        # immediately before any destructive operation on the source.
        snapshot = bm.copy()
        union = {i for plan in plans for i in plan["face_ids"]}
        bm.faces.ensure_lookup_table()
        if len(union) > len(bm.faces) or any(i < 0 or i >= len(bm.faces) for i in union):
            raise RuntimeError("HOST_CUT_SOURCE_FACE_IDS_INVALID")
        faces = [bm.faces[i] for i in sorted(union)]
        if len(faces) != len(union):
            raise RuntimeError("HOST_CUT_PRECONDITION_MISMATCH")

        host_cut_started = True
        bmesh.ops.delete(bm, geom=faces, context="FACES")
        bmesh.update_edit_mesh(host.data, loop_triangles=True, destructive=True)
        after = len(bm.faces)
        if before-after != len(union):
            raise RuntimeError("HOST_CUT_FACE_COUNT_MISMATCH")
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        frames[0].select_set(True)
        context.view_layer.objects.active = frames[0]
        context.scene["DWOS_W58_last_source"] = host.name
        context.scene["DWOS_W58_last_count"] = len(frames)
        return frames

    except Exception:
        restore_error = None
        if host_cut_started and snapshot is not None:
            try:
                _restore_host_after_failed_cut(context, host, snapshot)
            except Exception as error:
                restore_error = error
        cleanup_errors = []
        for frame in frames:
            try:
                _remove_new_window(frame)
            except Exception as error:
                cleanup_errors.append(str(error))
        if restore_error is not None or cleanup_errors:
            raise RuntimeError(
                "W58_HOST_TRANSACTION_ROLLBACK_FAILED: "
                + (str(restore_error) if restore_error else "")
                + ("; WINDOW_CLEANUP_FAILED: "+str(cleanup_errors[:3]) if cleanup_errors else "")
            ) from restore_error
        raise
    finally:
        if snapshot is not None:
            snapshot.free()


class DWOS_OT_auto_windows(bpy.types.Operator):
    bl_idname="dwos.auto_windows"
    bl_label="DWOS · Автоматически создать окна"
    bl_description="Выделите фасадные полигоны: все базовые окна, LP/HP, стекло и Interior за одно действие"
    bl_options={"REGISTER","UNDO"}
    def execute(self,context):
        try:
            frames=build_from_selection(context)
            self.report({"INFO"},f"DWOS W58: создано {len(frames)} окон; открывайте Composer и редактируйте импосты")
            return {"FINISHED"}
        except Exception as e:
            self.report({"ERROR"},str(e)[:230])
            return {"CANCELLED"}
    def invoke(self,context,event):
        return bpy.ops.dwos.w5829_choice_hud('INVOKE_DEFAULT')


class DWOS_OT_auto_view(bpy.types.Operator):
    bl_idname="dwos.auto_view"
    bl_label="DWOS W58 · переключить LP/HP"
    bl_options={"REGISTER"}
    mode:EnumProperty(items=[("HP","HP",""),("LP","LP","")],default="HP")
    window_name:StringProperty(name="Window",default="")
    def execute(self,context):
        ob=bpy.data.objects.get(self.window_name) if self.window_name else context.active_object
        if ob and ob.get(ROLE)=="LP" and ob.parent:ob=ob.parent
        if ob is None or not ob.get(OWNER) or ob.get(ROLE)!="HP":
            self.report({"WARNING"},"Выберите автоматически созданное окно DWOS")
            return {"CANCELLED"}
        lp=bpy.data.objects.get(str(ob.get("DWOS_W58_lp_name","")))
        if lp is None:
            self.report({"ERROR"},"LP отсутствует")
            return {"CANCELLED"}
        lp.hide_set(self.mode!="LP")
        ob.hide_set(self.mode=="LP")
        for child in ob.children:
            if child is lp:continue
            if child.get("DWOS_role") in ("GLASS","INTERIOR"):
                child.hide_set(self.mode=="LP")
        return {"FINISHED"}


CLASSES=(DWOS_OT_auto_windows,DWOS_OT_auto_view)
