"""ISSMAKER DWOS W58.5: cut source SELECTED-module copies before LP mullions.

Actual BMesh bisect operations interpolate ORIGINAL UV per new loop.
The user's source Wall mesh is NEVER modified here. All cuts are applied
to an analysis copy, and its face/UV correspondence is independently verified
before it can become the LP projection source.
"""
from __future__ import annotations
import bmesh
import math
from mathutils import Vector
from . import adaptive_opening_core, zone_core, source_lp_uv_core

def _cuts(frame):
    import json
    topo=json.loads(frame.dwos_window.opening_payload_json)["adaptive_topology"]
    width=float(topo["width"]);height=float(topo["height"])
    from .numeric import parse_meter_text
    fw=parse_meter_text(frame.dwos_window.frame_width_text)
    tree=zone_core.decode(str(frame.dwos_window.zone_tree_json or ""))
    leaves,bands=zone_core.layout(tree,width,height,fw)
    sx=set();sy=set()
    for _,axis,center,thickness,_rect,band in bands:
        if axis=="V":
            sx.update((float(band[0]),float(band[1])))
        elif axis=="H":
            sy.update((float(band[2]),float(band[3])))
    for _,rect in leaves:
        sx.update((float(rect[0]),float(rect[1])))
        sy.update((float(rect[2]),float(rect[3])))
    return topo,sorted(x for x in sx if -width/2+fw<x<width/2-fw),sorted(y for y in sy if -height/2+fw<y<height/2-fw)

def cut_author_uv_source(frame, payload=None, *, min_edge=1.e-6):
    import json
    payload=payload or json.loads(str(frame.get("DWOS_W58_source_lp_uv_json","")))
    if payload.get("schema")!=source_lp_uv_core.SCHEMA:
        raise ValueError("SOURCE_MULLION_UV_SCHEMA_MISMATCH")
    source=payload["faces"]
    material_by_id={int(face["id"]):int(face["material"]) for face in source}
    topo,s_positions,y_positions=_cuts(frame)
    if not s_positions and not y_positions:
        return payload,{"cuts":0,"new_faces":len(source),"status":"NO_MULLION_CUTS"}
    bm=bmesh.new()
    try:
        uv=bm.loops.layers.uv.new("UVMap")
        src_id=bm.faces.layers.int.new("SourceFaceId")
        for item in source:
            vs=[bm.verts.new(Vector(pt)) for pt in item["position"]]
            face=bm.faces.new(vs);face[src_id]=int(item["id"])
            for loop,co in zip(face.loops,item["uv"]):
                loop[uv].uv=co
        bm.normal_update()
        plane_specs=[]
        width=float(topo["width"])
        for s in s_positions:
            ds=max(1.e-5,width*1.e-6)
            p=Vector(adaptive_opening_core.map_svd_local(topo,s,0.,0.))
            l=Vector(adaptive_opening_core.map_svd_local(topo,max(-width/2,s-ds),0.,0.))
            r=Vector(adaptive_opening_core.map_svd_local(topo,min(width/2,s+ds),0.,0.))
            n=r-l
            if n.length<1.e-7:raise ValueError("SOURCE_MULLION_TANGENT_ZERO")
            n.normalize()
            plane_specs.append((p,n,"S"))
        mid=Vector(adaptive_opening_core.map_svd_local(topo,0.,0.,0.))
        vertical=Vector(adaptive_opening_core.map_svd_local(topo,0.,.1,0.))-mid
        if vertical.length<1.e-8:raise ValueError("SOURCE_MULLION_VERTICAL_ZERO")
        vertical.normalize()
        for v in y_positions:
            plane_specs.append((Vector(adaptive_opening_core.map_svd_local(topo,0.,v,0.)),vertical,"V"))
        performed=[]
        for point,n,axis in plane_specs:
            geometry=list(bm.verts)+list(bm.edges)+list(bm.faces)
            result=bmesh.ops.bisect_plane(bm,geom=geometry,dist=min_edge,
                                         plane_co=point,plane_no=n,
                                         clear_inner=False,clear_outer=False)
            if result.get("geom_cut"):
                performed.append({"axis":axis,"cut_geometry":len(result["geom_cut"])})
        bm.faces.ensure_lookup_table()
        output_faces=[]
        for f in bm.faces:
            if f.calc_area()<1.e-10:
                raise ValueError("SOURCE_MULLION_DEGENERATE_CUT_FACE")
            uv_list=[[float(x) for x in loop[uv].uv] for loop in f.loops]
            output_faces.append({"id":int(f[src_id]),"position":[[float(x) for x in loop.vert.co] for loop in f.loops],
                                 "uv":uv_list,"material":material_by_id[int(f[src_id])]})
        new={"schema":source_lp_uv_core.SCHEMA,"faces":output_faces}
        # Verify every CUT CORNER against the authoritative original UV source.
        # The new pieces must not change phase, orientation or density.
        max_error=0.
        for f in output_faces:
            pt=[tuple(x) for x in f["position"]]
            uv_values,metrics=source_lp_uv_core.project_lp_uv(
                payload,pt,[list(range(len(pt)))])
            for expected,actual in zip(uv_values[0],f["uv"]):
                max_error=max(max_error,math.dist(expected,actual))
        if max_error>2.e-5:
            raise ValueError(f"SOURCE_MULLION_UV_TRANSFER_DRIFT:{max_error:.8g}")
        return new,{"cuts":len(performed),"new_faces":len(output_faces),
                    "old_faces":len(source),"mullion_cut_lines":len(plane_specs),
                    "max_uv_error":max_error,"status":"CUT_AND_UV_VERIFIED"}
    finally:
        bm.free()
