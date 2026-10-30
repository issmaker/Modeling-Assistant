"""W58.8 physical Smart Interior experiments.

MERGE uses Blender's EXACT 3D Boolean on temporary closed copies of upright
convex Interior volumes, then opens the original front mouths. No original
Interior, Window, Glass, UV or host mesh may ever be changed during analysis.

GAP reuses the production conservative SMART Interior clipping in world XYZ.
AUTO is a decision only; it never guesses that a failing merge was successful.
"""
from __future__ import annotations
import math
import bpy
import bmesh
from mathutils import Vector
from .interior_footprint_core import footprint,_on_segment
from .smart_interior_core import solve_group,contact_graph,UnresolvedInteriorContact
from .spatial_safety_core import mesh_intersections,self_intersections
from .adaptive_validation import validate_mesh

def interval_relation(a,b,*,epsilon=1.e-6):
    aa=sorted([float(a[0]),float(a[1])]);bb=sorted([float(b[0]),float(b[1])])
    if not all(math.isfinite(x) for x in aa+bb):
        raise ValueError("INTERIOR_Z_NOT_FINITE")
    lower=max(aa[0],bb[0]);upper=min(aa[1],bb[1])
    overlap=upper-lower
    if overlap>epsilon:return {"type":"OVERLAP_Z","m":overlap,"interval":[lower,upper]}
    if overlap>=-epsilon:return {"type":"TOUCH_Z","m":0.,"interval":[lower,lower]}
    return {"type":"SEPARATE_Z","m":-overlap,"interval":[lower,upper]}

# A measured 10 mm upper/lower discrepancy is treated as a construction
# tolerance ONLY for generated Interior envelopes. Never modify frame,
# glazing, source UV Wall or the actual authored window Z positions.
NEAR_LEVEL_TOLERANCE_M=0.010

def height_policy(fa,fb,tolerance=NEAR_LEVEL_TOLERANCE_M):
    loerr=abs(float(fa["zlo"])-float(fb["zlo"]))
    hierr=abs(float(fa["zhi"])-float(fb["zhi"]))
    near=(loerr<=tolerance+1.e-8 and hierr<=tolerance+1.e-8)
    return {"merge_allowed":bool(near),
      "relation":"NEAR_LEVEL" if near else "STAGGERED_Z_GAP_ONLY",
      "lower_discrepancy_m":loerr,"upper_discrepancy_m":hierr,
      "tolerance_m":float(tolerance),
      "aligned_envelope":[min(fa["zlo"],fb["zlo"]),max(fa["zhi"],fb["zhi"])] if near else None}

def classify_pair(a,b,clearance=.03):
    fa=footprint(a);fb=footprint(b)
    if not fa or not fb:
        return {"status":"UNSUPPORTED_GEOMETRY","mode_options":["GAP"],
            "reason":"MERGE needs exact upright single-shell convex footprint, no approximation"}
    z=interval_relation((fa['zlo'],fa['zhi']),(fb['zlo'],fb['zhi']))
    height=height_policy(fa,fb)
    # XY relation must be independent of the vertical interval: legacy
    # footprint_clearance_xy refuses Z-disjoint pairs, precisely the case
    # where staggered windows need an independent vertical-gap decision.
    from .interior_footprint_core import (
        _single_footprint_clearance_xy,rebuild_prism)
    def flat_copy(f):
        return rebuild_prism(
            {"poly":f["poly"],"front":f["front"],
             "zlo":0.0,"zhi":1.0},f["poly"])
    xyd=_single_footprint_clearance_xy(flat_copy(fa),flat_copy(fb))
    if xyd is None:
        return {"status":"UNSUPPORTED_XY","z":z,"mode_options":["GAP"]}
    geometric_intersection=bool(mesh_intersections(
        a["verts"],a["faces"],b["verts"],b["faces"],max_hits=1))
    separated=(z['type']=="SEPARATE_Z" and z['m']>=clearance-1.e-6)
    if z["type"]=="SEPARATE_Z" and z['m']>=clearance-1.e-6:
        status="NO_CONTACT_Z"
    elif (z["type"] in ("SEPARATE_Z","TOUCH_Z")
            and z["m"]<clearance-1.e-6 and xyd<clearance+1e-6):
        status="Z_CLEARANCE_CONFLICT"
    elif z["type"]=="OVERLAP_Z" and (geometric_intersection or xyd<clearance-1.e-6):
        status="OVERLAPPING_3D"
    else:status="NO_CONTACT_XY"
    return {"status":status,"z":z,"heights":height,
            "xy_clearance_m":float(xyd),
            "mesh_surface_intersection":geometric_intersection,
            "mode_options":(["MERGE","GAP","AUTO"]
                if status=="OVERLAPPING_3D" and height["merge_allowed"]
                else ["GAP","AUTO"])}

def stage_gap(a,b,*,clearance=.03,mode="BALANCED"):
    if mode not in ("BALANCED","PRIORITY","CONTOUR_FOLLOW"):
        raise ValueError("INTERIOR_GAP_UNSUPPORTED_STRATEGY")
    cls={"A":"PLANAR","B":"PLANAR"}
    before=classify_pair(a,b,clearance=clearance)
    if before["status"]=="Z_CLEARANCE_CONFLICT":
        # A 20mm vertical separation with 30mm required clearance is NOT
        # unsolvable: keep both exact window Z intervals, and create sufficient
        # separation in XY, on isolated temporary PLANAR prism copies.
        fa=footprint(a);fb=footprint(b)
        if fa is None or fb is None:
            raise UnresolvedInteriorContact(
                "Smart Interior GAP: unsupported non-prismatic Z clearance contact",
                "Z_CLEARANCE_UNSUPPORTED_GEOMETRY")
        from .interior_footprint_core import (
            rebuild_prism,footprint as _footprint,
            _single_footprint_clearance_xy)
        def flat_copy(source):
            return rebuild_prism(
                {"poly":source["poly"],"front":source["front"],
                 "zlo":0.,"zhi":1.},source["poly"])
        flat,plan_meta=solve_group(
            {"A":flat_copy(fa),"B":flat_copy(fb)},
            clearance=clearance,mode=mode,priority_id="A",
            classes={"A":"PLANAR","B":"PLANAR"})
        qa,qb=_footprint(flat["A"]),_footprint(flat["B"])
        if qa is None or qb is None:
            raise UnresolvedInteriorContact(
                "Smart Interior GAP: planar clipping invalidates source mouth",
                "Z_CLEARANCE_PLANAR_MOUTH_LOST")
        output={"A":rebuild_prism(fa,qa["poly"]),
                "B":rebuild_prism(fb,qb["poly"])}
        for key,mesh in output.items():
            validate_mesh(mesh)
            if self_intersections(mesh["verts"],mesh["faces"]):
                raise ValueError("INTERIOR_GAP_Z_CONTACT_SELF_INTERSECTION:"+key)
        # XY >= clearance is deliberately conservative: physical 3D distance
        # is then >= clearance irrespective of the remaining original Z gap.
        xy_separation=_single_footprint_clearance_xy(
            flat["A"],flat["B"])
        if xy_separation is None or xy_separation<clearance-1.e-5:
            raise ValueError("INTERIOR_GAP_Z_CONTACT_XY_CLEARANCE_MISSING")
        if mesh_intersections(output["A"]["verts"],output["A"]["faces"],
                              output["B"]["verts"],output["B"]["faces"],max_hits=1):
            raise ValueError("INTERIOR_GAP_Z_CONTACT_STILL_INTERSECTING")
        return output,{"policy":"GAP_Z_STAGGERED_INDEPENDENT_XY_V1",
                       "before":before,"planar":plan_meta,
                       "xy_separation_m":xy_separation,
                       "original_heights_kept":True}
    output,meta=solve_group({"A":a,"B":b},clearance=clearance,mode=mode,
                            priority_id="A",classes=cls)
    for key,mesh in output.items():
        validate_mesh(mesh)
        if self_intersections(mesh["verts"],mesh["faces"]):
            raise ValueError("INTERIOR_GAP_CREATED_SELF_INTERSECTION:"+key)
    if mesh_intersections(output["A"]["verts"],output["A"]["faces"],
                          output["B"]["verts"],output["B"]["faces"],max_hits=1):
        raise ValueError("INTERIOR_GAP_STILL_INTERSECTING")
    return output,{"policy":"INDEPENDENT_GAP_3D_V1","before":before,
                   "clearance_m":clearance,"solver":meta}

def _closed_operand(foot):
    poly=foot["poly"];n=len(poly);zlo=foot["zlo"];zhi=foot["zhi"]
    verts=[(float(x),float(y),float(z)) for z in (zlo,zhi) for x,y in poly]
    faces=[tuple(reversed(range(n))),tuple(range(n,2*n))]
    faces += [(i,(i+1)%n,(i+1)%n+n,i+n) for i in range(n)]
    return verts,faces

def expected_exact_union_volume(fa,fb):
    """Independent convex-poly prism union volume Oracle, XYZ not projected."""
    from .interior_footprint_core import clip_polygon,_area
    overlap_poly=list(fa["poly"])
    clip=list(fb["poly"])
    for a,b in zip(clip,clip[1:]+clip[:1]):
        dx=b[0]-a[0];dy=b[1]-a[1]
        normal=(dy,-dx)
        bound=normal[0]*a[0]+normal[1]*a[1]
        overlap_poly=clip_polygon(overlap_poly,normal,bound,keep_low=True)
        if overlap_poly is None:break
    area_ab=abs(_area(overlap_poly)) if overlap_poly else 0.
    za=max(float(fa["zlo"]),float(fb["zlo"]))
    zb=min(float(fa["zhi"]),float(fb["zhi"]))
    overlap_z=max(0.,zb-za)
    v_a=abs(_area(fa["poly"]))*(fa["zhi"]-fa["zlo"])
    v_b=abs(_area(fb["poly"]))*(fb["zhi"]-fb["zlo"])
    return float(v_a+v_b-area_ab*overlap_z),float(area_ab),float(overlap_z)

def _make_obj(label,foot,collection):
    points,faces=_closed_operand(foot)
    me=bpy.data.meshes.new(label+"_DATA")
    me.from_pydata(points,[],faces);me.validate(clean_customdata=False);me.update()
    obj=bpy.data.objects.new(label,me);collection.objects.link(obj)
    return obj

def stage_merge_exact(a,b,*,scene=None):
    fa=footprint(a);fb=footprint(b)
    if fa is None or fb is None:
        raise ValueError("INTERIOR_MERGE_EXACT_UNSUPPORTED_FOOTPRINT")
    relation=classify_pair(a,b,clearance=0.)
    height=height_policy(fa,fb)
    if not height["merge_allowed"]:
        raise ValueError("INTERIOR_MERGE_STAGGERED_Z_FORBIDDEN_USE_GAP")
    if relation["status"]!="OVERLAPPING_3D":
        raise ValueError("INTERIOR_MERGE_EXACT_NOT_OVERLAPPING")
    # Physically align derived Interior shells within 10mm, WITHOUT touching
    # original window or host meshes. Envelope containment prevents truncation.
    fa={**fa,"zlo":height["aligned_envelope"][0],"zhi":height["aligned_envelope"][1]}
    fb={**fb,"zlo":height["aligned_envelope"][0],"zhi":height["aligned_envelope"][1]}
    if interval_relation((fa["zlo"],fa["zhi"]),(fb["zlo"],fb["zhi"]))["type"]!="OVERLAP_Z":
        raise ValueError("INTERIOR_MERGE_EXACT_REQUIRES_Z_VOLUME_OVERLAP")
    scene=scene or bpy.context.scene
    coll=bpy.data.collections.new("W58_8_TEMP_BOOLEAN_MERGE")
    scene.collection.children.link(coll)
    objects=[]
    try:
        one=_make_obj("W58_8_BOOLEAN_A",fa,coll)
        two=_make_obj("W58_8_BOOLEAN_B",fb,coll)
        objects.extend([one,two])
        if bpy.context.object is not None and bpy.context.object.mode!='OBJECT':
            raise ValueError("INTERIOR_MERGE_REQUIRES_OBJECT_MODE")
        # Evaluate the Boolean modifier via the dependency graph without
        # applying it to either user's actual Interior object.
        mod=one.modifiers.new("W58_8_TEST_UNION","BOOLEAN")
        mod.operation="UNION";mod.solver="EXACT";mod.object=two
        depsgraph=bpy.context.evaluated_depsgraph_get()
        eval_obj=one.evaluated_get(depsgraph)
        result=bpy.data.meshes.new_from_object(
            eval_obj,preserve_all_data_layers=False,depsgraph=depsgraph)
        try:
            bm=bmesh.new()
            bm.from_mesh(result)
            bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=1.e-7)
            bmesh.ops.triangulate(
                bm,faces=list(bm.faces),quad_method="BEAUTY",ngon_method="BEAUTY")
            closed_volume=abs(float(bm.calc_volume(signed=True)))
            expected_volume,xy_overlap_area,z_overlap=(
                expected_exact_union_volume(fa,fb))
            err=abs(closed_volume-expected_volume)
            if err>max(1.e-5,expected_volume*2.e-5):
                raise ValueError(
                    f"INTERIOR_MERGE_VOLUME_MISMATCH:"
                    f"actual={closed_volume:.6f} expected={expected_volume:.6f}")
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            fullverts=[tuple(float(x) for x in v.co) for v in bm.verts]
            fullfaces=[tuple(int(v.index) for v in face.verts) for face in bm.faces]
            bm.free()
        finally:
            bpy.data.meshes.remove(result)
        if not fullfaces or len(fullverts)<8:
            raise ValueError("INTERIOR_MERGE_EMPTY_BOOLEAN_RESULT")
        # Closing the aperture is mandatory for Boolean; only remove those
        # *exactly located* on the original front edge of either input.
        def is_front_face(face,origin):
            x,y=origin["front"][0];q,r=origin["front"][1]
            zlo,zhi=origin["zlo"],origin["zhi"]
            return all(_on_segment((fullverts[v][0],fullverts[v][1]),(x,y),(q,r))
                and zlo-1e-5<=fullverts[v][2]<=zhi+1e-5 for v in face)
        faces=[];front_removed=0
        for poly in fullfaces:
            if is_front_face(poly,fa) or is_front_face(poly,fb):
                front_removed+=1
            else:faces.append(poly)
        if front_removed<2:
            raise ValueError("INTERIOR_MERGE_LOST_FRONT_APERTURES")
        # Dense Boolean mesh is allowed but all output coordinates/faces
        # must pass independent topological and self-intersection gates.
        mesh={"verts":fullverts,"faces":faces,
              "roles":["INTERIOR_MERGED_3D"]*len(faces)}
        verified=validate_mesh(mesh)
        hits=self_intersections(mesh["verts"],mesh["faces"])
        if hits:
            pairs=[(u,v,[[tuple(round(float(c),4) for c in mesh["verts"][k]) for k in mesh["faces"][fi]] for fi in (u,v)]) for u,v in hits[:3]]
            raise ValueError(f"INTERIOR_MERGE_NEW_SELF_INTERSECTIONS:{pairs}")
        return mesh,{"policy":"EXACT_BOOLEAN_MERGE_STAGGERED_Z_V1",
                     "front_faces_removed":front_removed,
                     "source_z_ranges": [[min(v[2] for v in a["verts"]), max(v[2] for v in a["verts"])],
                                         [min(v[2] for v in b["verts"]), max(v[2] for v in b["verts"])]],
                     "near_level_envelope_z":height["aligned_envelope"],
                     "height_policy":height,
                     "triangles_or_faces":len(faces),"z_contact":relation["z"],
                     "verified":verified,
                     "closed_union_volume_m3":closed_volume,
                     "independent_expected_volume_m3":expected_volume,
                     "volume_error_m3":err,
                     "xy_overlap_area_m2":xy_overlap_area,
                     "overlap_height_m":z_overlap}
    finally:
        for obj in objects:
            mesh=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        bpy.data.collections.remove(coll)


from bpy.props import EnumProperty

def preview_for_interior_objects(context,first,second,mode="AUTO"):
    """Stage and publish *preview copies only*; user window meshes untouched."""
    from . import smart_interior_scene as smart
    original=[first,second]
    if len(set(o.as_pointer() for o in original))!=2:
        raise ValueError("INTERIOR_PREVIEW_REQUIRES_TWO_DISTINCT_OBJECTS")
    a,b=(smart._world(smart._mesh_data(o),o.matrix_world) for o in original)
    gap=float(context.scene.dwos.smart_interior_gap)
    decision=classify_pair(a,b,clearance=gap)
    chosen=mode
    if chosen=="AUTO":
        if decision["status"].startswith("NO_CONTACT"):
            return {"policy":"NO_CONTACT","objects":[],"decision":decision}
        chosen="GAP"
    if chosen=="MERGE":
        if "MERGE" not in decision.get("mode_options",()):
            raise ValueError("INTERIOR_MERGE_DIFFERENT_HEIGHTS_GAP_ONLY")
        mesh,meta=stage_merge_exact(a,b,scene=context.scene)
        generated=[("MERGE",mesh)]
    elif chosen=="GAP":
        pair,meta=stage_gap(a,b,clearance=gap)
        generated=[("GAP_A",pair["A"]),("GAP_B",pair["B"])]
    else:raise ValueError("INTERIOR_PREVIEW_UNKNOWN_MODE")
    for label,raw in generated:
        validate_mesh(raw)
        if self_intersections(raw["verts"],raw["faces"]):
            raise ValueError("INTERIOR_PREVIEW_SELF_INTERSECTION:"+label)
    staged=[];objs=[];created_col=False
    coll=bpy.data.collections.get("W58_8_INTERIOR_3D_PREVIEWS")
    if coll is None:
        coll=bpy.data.collections.new("W58_8_INTERIOR_3D_PREVIEWS")
        context.scene.collection.children.link(coll);created_col=True
    try:
        for label,raw in generated:
            me=bpy.data.meshes.new("W58_8_PREVIEW_"+label+"_MESH")
            staged.append(me)
            me.from_pydata(raw["verts"],[],raw["faces"])
            me.validate(clean_customdata=False);me.update()
            obj=bpy.data.objects.new("W58_8_PREVIEW_"+label,me)
            coll.objects.link(obj);objs.append(obj)
            obj["ISSMAKER_W58_8_PREVIEW_ONLY"]=True
            obj["W58_8_MODE"]=chosen
            obj["W58_8_SOURCES"]=' | '.join(o.name for o in original)
            obj["W58_8_Z_CONTACT"]=str(decision.get("z",{}))
            obj["W58_8_META"]=str(meta)[:4000]
            obj.display_type="WIRE"
        old=[obj for obj in coll.objects if
             obj not in objs and obj.get("ISSMAKER_W58_8_PREVIEW_ONLY")]
        for obj in old:
            mesh=obj.data;bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
    except Exception:
        for obj in objs:
            mesh=obj.data
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        for me in staged:
            if me.users==0 and me.name in bpy.data.meshes:
                bpy.data.meshes.remove(me)
        if created_col and not coll.objects:
            bpy.data.collections.remove(coll)
        raise
    return {"policy":chosen,"objects":[o.name for o in objs],
            "decision":decision,"metadata":meta}

class DWOS_OT_preview_interior_3d_modes(bpy.types.Operator):
    """Safe 3D Smart Interior MERGE / GAP visualization on two selected windows"""
    bl_idname="dwos.preview_interior_3d_modes"
    bl_label="Smart Interior 3D — Preview"
    bl_description="Preview merged or separated interiors without changing either window"
    bl_options={"REGISTER","UNDO"}
    mode: EnumProperty(items=[
        ("AUTO","AUTO","Choose GAP for a solvable pair; never silently merge"),
        ("GAP","GAP","Build independent cavities with the configured clearance"),
        ("MERGE","MERGE","Exact Boolean union with both open front mouths")],
        default="AUTO")

    @classmethod
    def poll(cls,context):
        return context.scene is not None and context.mode=="OBJECT"

    def execute(self,context):
        from . import smart_interior_scene as smart
        selected=list(context.selected_objects)
        original=[]
        for obj in selected:
            if obj.type!="MESH":continue
            if obj.get("DWOS_role")=="INTERIOR":target=obj
            else:
                target=next((child for child in obj.children
                    if child.type=="MESH" and child.get("DWOS_role")=="INTERIOR"),None)
            if target is not None and target not in original:original.append(target)
        if len(original)!=2:
            self.report({"ERROR"},"Select exactly two DWOS windows or their Interior meshes")
            return {"CANCELLED"}
        try:
            answer=preview_for_interior_objects(context,original[0],
                                                 original[1],self.mode)
        except (ValueError,RuntimeError,UnresolvedInteriorContact) as exc:
            self.report({"WARNING"},str(exc)[:230])
            return {"CANCELLED"}
        mode=answer["policy"]
        new_objects=answer["objects"]
        self.report({"INFO"},f"W58.8 {mode}: {len(new_objects)} preview mesh(es); original windows unchanged")
        return {"FINISHED"}

class DWOS_PT_smart_interior_3d_lab(bpy.types.Panel):
    bl_label="Smart Interior 3D Lab | W58.8"
    bl_idname="DWOS_PT_smart_interior_3d_lab"
    bl_space_type="VIEW_3D"
    bl_region_type="UI"
    bl_category="ISSMAKER"
    bl_options={"DEFAULT_CLOSED"}

    def draw(self,context):
        col=self.layout.column(align=True)
        col.label(text="Choose two DWOS windows",icon="MOD_BOOLEAN")
        col.label(text="Experiments: non-destructive preview")
        row=col.row(align=True)
        row.operator("dwos.preview_interior_3d_modes",text="AUTO").mode="AUTO"
        row.operator("dwos.preview_interior_3d_modes",text="GAP").mode="GAP"
        row.operator("dwos.preview_interior_3d_modes",text="MERGE").mode="MERGE"
        col.label(text="Different Z heights supported")
        col.label(text="MERGE supports exact convex upright prisms only")
        col.label(text="No automatic publication to Windows")

CLASSES=(DWOS_OT_preview_interior_3d_modes,DWOS_PT_smart_interior_3d_lab)
