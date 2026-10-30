"""W58.4 experimental LP preview operator. Not a production DWOS replacement."""
import bpy
from bpy.props import FloatProperty
from . import adaptive_edge_cut_core, adaptive_edge_cut_blender


class MESH_OT_issmaker_edge_cut_preview(bpy.types.Operator):
    bl_idname="mesh.issmaker_adaptive_edge_cut_preview"
    bl_label="Adaptive Edge Cut · LP Preview"
    bl_description="Experimental planar source-UV LP frame preview. Does NOT cut the wall."
    bl_options={"REGISTER", "UNDO"}

    frame_width: FloatProperty(name="Frame width (m)", default=.08, min=.001, max=2.,
                               precision=4, description="Exact nominal width; never silently reduced")

    @classmethod
    def poll(cls, context):
        return bool(context.mode=="EDIT_MESH" and context.edit_object
                    and context.edit_object.type=="MESH")

    def execute(self, context):
        host=context.edit_object
        if host is None:
            self.report({"ERROR"}, "Select mesh faces in Edit Mode")
            return {"CANCELLED"}
        try:
            # End Edit Mode to synchronize BMesh selection / UV, without
            # changing the user's source mesh geometry.
            bpy.ops.object.mode_set(mode="OBJECT")
            capture=adaptive_edge_cut_blender.capture(host)
            outcome=adaptive_edge_cut_core.solve(capture["outer"],
                                                  float(self.frame_width))
            if outcome["category"] not in ("ACCEPTED_SINGLE","ACCEPTED_SPLIT"):
                self.report({"WARNING"},"No admissible internal glazing region at this width")
                bpy.ops.object.mode_set(mode="EDIT")
                return {"CANCELLED"}
            aluminum=bpy.data.materials.get("Material.002")
            lp,glass,stats=adaptive_edge_cut_blender.create_lp_preview(
                host,capture,outcome,material=aluminum)
            lp["W58_4_EXPERIMENTAL_PREVIEW_ONLY"]=True
            lp["W58_4_NOMINAL_WIDTH_M"]=float(self.frame_width)
            host.select_set(False)
            bpy.ops.object.select_all(action="DESELECT")
            lp.select_set(True)
            context.view_layer.objects.active=lp
            self.report({"INFO"},f"LP preview built: {len(glass)} glass zone(s), UV source copied")
            return {"FINISHED"}
        except (ValueError,RuntimeError) as exc:
            if host and host.name in bpy.data.objects:
                bpy.ops.object.select_all(action="DESELECT")
                host.select_set(True)
                context.view_layer.objects.active=host
                if host.mode=="OBJECT":
                    bpy.ops.object.mode_set(mode="EDIT")
            self.report({"ERROR"},str(exc)[:240])
            return {"CANCELLED"}



class WM_OT_issmaker_edge_cut_diagnostics_zip(bpy.types.Operator):
    bl_idname="wm.issmaker_edge_cut_diagnostics_zip"
    bl_label="Collect W58.4 Diagnostics ZIP"
    bl_options={"REGISTER"}

    def execute(self,context):
        import pathlib,json,hashlib,datetime,zipfile
        import issmaker_modeling_assistant as addon
        root=pathlib.Path(addon.__file__).parent
        lab=pathlib.Path.home()/"Documents"/"ISSMAKER_DWOS_NEXT_Release_Lab"
        lab.mkdir(parents=True,exist_ok=True)
        stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest=lab/("ISSMAKER_W58_4_DIAGNOSTICS_"+stamp+".zip")
        source_files=("adaptive_edge_cut_core.py","adaptive_edge_cut_blender.py",
                      "adaptive_edge_cut_ui.py")
        source_paths=[root/"tools"/"dwos_windows"/"native"/name for name in source_files]
        environment={"blender":bpy.app.version_string,
                     "addon_version":list(addon.bl_info.get("version",())),
                     "file":bpy.data.filepath,
                     "mode":context.mode,
                     "active":context.active_object.name if context.active_object else None,
                     "scene_objects":len(context.scene.objects),
                     "selected":[o.name for o in context.selected_objects],
                     "materials":[m.name for m in bpy.data.materials],
                     "textures":[{"name":i.name,"size":list(i.size)}
                                 for i in bpy.data.images if i.name.startswith("D_")],
                     "source_hashes":{x.name:hashlib.sha256(x.read_bytes()).hexdigest()
                                      for x in source_paths}}
        meshes=[]
        for o in bpy.data.objects:
            if o.type!="MESH" or not (o.name.startswith("W58_4_") or o.get("W58_4_SOURCE_HOST")):
                continue
            data=o.data
            pack={"verts":[[round(float(x),6) for x in v.co] for v in data.vertices],
                  "faces":[[int(data.loops[i].vertex_index) for i in f.loop_indices]
                           for f in data.polygons],
                  "uv":[[round(float(v),7) for v in x.uv]
                        for x in data.uv_layers.active.data] if data.uv_layers.active else []}
            meshes.append({"name":o.name,"faces":len(data.polygons),
                           "verts":len(data.vertices),"materials":[m.name for m in data.materials],
                           "visible":not o.hide_get(),
                           "xyz_uv_sha256":hashlib.sha256(
                                json.dumps(pack,separators=(",",":")).encode()).hexdigest()})
        with zipfile.ZipFile(dest,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            z.writestr("environment.json",json.dumps(environment,ensure_ascii=False,indent=2))
            z.writestr("mesh_snapshots.json",json.dumps(meshes,ensure_ascii=False,indent=2))
            z.writestr("README.txt","W58.4 experimental LP geometry QA. Tests are not a production release.")
            for x in source_paths:z.write(x,"source/"+x.name)
            for x in sorted(lab.glob("W58_4_*.json")):
                if x.stat().st_size<3000000:z.write(x,"qa/"+x.name)
            for x in sorted(lab.glob("W58_4_REAL_BLENDER*.png")):
                z.write(x,"screenshots/"+x.name)
            for x in sorted(lab.glob("CHG_W58_4_*.patch")):
                z.write(x,"patches/"+x.name)
            for x in sorted(lab.glob("W58_4_*.md")):
                z.write(x,"reports/"+x.name)
            candidate=lab/"ISSMAKER_W58_4_ADAPTIVE_EDGE_CUT_DEV_CANDIDATE.zip"
            if candidate.is_file():z.write(candidate,"addon/"+candidate.name)
            if bpy.data.filepath and pathlib.Path(bpy.data.filepath).is_file():
                z.write(bpy.data.filepath,"saved_scene/"+pathlib.Path(bpy.data.filepath).name)
        with zipfile.ZipFile(dest) as z:
            if z.testzip() is not None:
                self.report({"ERROR"},"ZIP CRC failed")
                return {"CANCELLED"}
        bpy.context.window_manager.clipboard=str(dest)
        self.report({"INFO"},"Diagnostics ZIP ready; path copied")
        print("W58_4_DIAGNOSTICS_ZIP",str(dest),dest.stat().st_size)
        return {"FINISHED"}


class VIEW3D_PT_issmaker_edge_cut_qa(bpy.types.Panel):
    bl_label="W58.4 | Adaptive Edge Cut (QA)"
    bl_idname="VIEW3D_PT_issmaker_edge_cut_qa"
    bl_space_type="VIEW_3D"
    bl_region_type="UI"
    bl_category="ISSMAKER"

    def draw(self, context):
        col=self.layout.column(align=True)
        col.label(text="EXPERIMENTAL - PLANAR ONLY",icon="INFO")
        col.label(text="Select wall faces in Edit Mode")
        col.operator("mesh.issmaker_adaptive_edge_cut_preview",text="Preview LP from UV Wall",icon="MOD_BEVEL")
        col.label(text="Preview does not delete source faces")
        col.separator()
        col.operator("wm.issmaker_edge_cut_diagnostics_zip",
                     text="Collect Diagnostics ZIP",icon="PACKAGE")


CLASSES=(MESH_OT_issmaker_edge_cut_preview,
         WM_OT_issmaker_edge_cut_diagnostics_zip,
         VIEW3D_PT_issmaker_edge_cut_qa)
