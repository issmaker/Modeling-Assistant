"""W58.11 — partial-window topology cutter for planar UV Wall quads.

The production Blender operator creates inset window source faces (not a
decorative mesh), preserves all boundary vertices and bilinearly maps source
UVs. DWOS auto_windows consumes the selected new central faces.
"""
from __future__ import annotations
import bpy
import bmesh
from bpy.types import Operator
from bpy.props import FloatProperty
from mathutils import Vector
from . import opening_detection

def cut_selected_quads(context, width_fraction=.54, height_fraction=.56):
    wall=context.active_object
    if wall is None or wall.type!="MESH" or wall.mode!="EDIT":
        raise ValueError("DWOS_REGION_REQUIRES_EDIT_MODE_HOST")
    opening_detection._validate_host_source_contract(wall)
    bm=bmesh.from_edit_mesh(wall.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    originals=sorted([face for face in bm.faces if face.select], key=lambda f:f.index)
    if not 1<=len(originals)<=64:
        raise ValueError("DWOS_REGION_REQUIRES_1_TO_64_SELECTED_FACES")
    if not (.2<=width_fraction<=.8 and .2<=height_fraction<=.8):
        raise ValueError("DWOS_REGION_INVALID_FRACTIONS")
    ul=bm.loops.layers.uv.active
    if ul is None:raise ValueError("DWOS_REGION_MISSING_SOURCE_UV")
    prepared=[]
    for face in originals:
        if len(face.verts)!=4 or abs(face.normal.z)>.15:
            raise ValueError("DWOS_REGION_ONLY_VERTICAL_PLANAR_QUADS:"+str(face.index))
        verts=[loop.vert for loop in face.loops]
        p=[v.co.copy() for v in verts]
        if any((p[(j+1)%4]-p[j]).length<1.8 for j in range(4)):
            raise ValueError("DWOS_REGION_MODULE_TOO_SMALL:"+str(face.index))
        if face.calc_area()<3.0:
            raise ValueError("DWOS_REGION_MODULE_AREA_TOO_SMALL")
        if not isinstance(face.normal,Vector):raise ValueError("INVALID_NORMAL")
        uvs=[loop[ul].uv.copy() for loop in face.loops]
        layers={key:face[key] for key in bm.faces.layers.int}
        prepared.append((face,verts,p,uvs,face.material_index,face.smooth,layers))
    # Every precondition above was pure. A failure below rolls edit mesh back.
    backup=bm.copy()
    centers=[]
    def bi(values,u,v):
        return (values[0]*(1-u)*(1-v)+values[1]*u*(1-v)+
                values[2]*u*v+values[3]*(1-u)*v)
    try:
        for face,vs,p,uvs,material,smooth,layers in prepared:
            u0=(1.-width_fraction)/2.;u1=1.-u0
            v0=(1.-height_fraction)/2.;v1=1.-v0
            uv_quad=[(u0,v0),(u1,v0),(u1,v1),(u0,v1)]
            inside=[bm.verts.new(bi(p,u,v)) for u,v in uv_quad]
            inner_uv=[bi(uvs,u,v) for u,v in uv_quad]
            bm.faces.remove(face)
            face_specs=[]
            for i in range(4):
                j=(i+1)%4
                face_specs.append(([vs[i],vs[j],inside[j],inside[i]],
                                   [uvs[i],uvs[j],inner_uv[j],inner_uv[i]],False))
            face_specs.append((inside,inner_uv,True))
            for vertices,uvpoints,is_window in face_specs:
                new_face=bm.faces.new(vertices)
                new_face.material_index=material
                new_face.smooth=smooth
                for attr,value in layers.items():new_face[attr]=value
                for loop,coord in zip(new_face.loops,uvpoints):loop[ul].uv=coord
                if is_window:centers.append(new_face)
        for face in bm.faces:face.select_set(False)
        for vert in bm.verts:vert.select_set(False)
        for face in centers:face.select_set(True)
        bm.normal_update()
        bmesh.update_edit_mesh(wall.data,loop_triangles=True,destructive=True)
        return {"selected_window_patches":len(centers),
                "old_quads":len(originals),
                "new_total_faces":len(bm.faces),
                "policy":"ISSMAKER_W5811_PARTIAL_QUAD_1000pxm"}
    except Exception:
        # Undo-ready Blender edit-state restore with exactly the original mesh.
        bpy.ops.object.mode_set(mode="OBJECT")
        backup.to_mesh(wall.data)
        wall.data.update()
        bpy.ops.object.mode_set(mode="EDIT")
        raise
    finally:
        backup.free()

class DWOS_OT_prepare_partial_window_regions(Operator):
    bl_idname="dwos.prepare_partial_window_regions"
    bl_label="DWOS · Разметить частичные окна"
    bl_description="Создаёт внутренние оконные полигоны в существующих модулях UV Wall с точной метрической UV"
    bl_options={"REGISTER","UNDO"}
    width_fraction:FloatProperty(name="Доля ширины модуля",default=.54,min=.20,max=.80)
    height_fraction:FloatProperty(name="Доля высоты модуля",default=.56,min=.20,max=.80)
    @classmethod
    def poll(cls,context):
        return bool(context.active_object and context.active_object.type=="MESH"
                and context.active_object.mode=="EDIT")
    def execute(self,context):
        try:
            report=cut_selected_quads(context,self.width_fraction,self.height_fraction)
            self.report({"INFO"},"DWOS: размечено %d частичных окон"%report["selected_window_patches"])
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"},str(exc)[:230])
            return {"CANCELLED"}

class DWOS_PT_W5811_architectural_qa(bpy.types.Panel):
    bl_idname="DWOS_PT_W5811_architectural_qa"
    bl_label="DWOS  |  Architectural Atelier"
    bl_category="ISSMAKER"
    bl_space_type="VIEW_3D"
    bl_region_type="UI"

    @classmethod
    def poll(cls,context):
        return context.scene is not None and hasattr(context.scene,"dwos")

    def draw(self,context):
        ly=self.layout
        header=ly.box()
        header.label(text="W58.11  /  WINDOWS",icon="MOD_BUILD")
        header.label(text="UV Wall > Partial Region > DWOS",icon="INFO")
        row=header.column(align=True)
        row.enabled=context.mode=="EDIT_MESH"
        row.operator("dwos.prepare_partial_window_regions",
                     text="01 / Cut Partial Window Zones",icon="MESH_GRID")
        row.operator("dwos.auto_windows",text="02 / Build LP / HP / Glass",icon="WINDOW")
        controls=ly.box()
        controls.label(text="DWOS / COMPOSER",icon="PREFERENCES")
        controls.operator("dwos.viewport_composer",
                          text="Open Window Composer",icon="WINDOW")
        contacts=ly.box()
        contacts.label(text="SMART INTERIOR / ASK FIRST",icon="ERROR")
        contacts.prop(context.scene.dwos,"smart_interior_conflict_behavior",
                      text="Policy")
        contacts.operator("dwos.scan_interior_conflicts",
                          text="Find Interior Contacts",icon="VIEWZOOM")
        count=int(context.scene.get("W58_8_smart_interior_detected_count",0))
        contacts.label(text=f"Confirmed contacts: {count}")
        ly.label(text="Strict UV 1000 px/m  /  Glass & Interior exempt")

CLASSES=(DWOS_OT_prepare_partial_window_regions,
         DWOS_PT_W5811_architectural_qa)
