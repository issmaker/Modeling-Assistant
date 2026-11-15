"""UMS WALL Material Builder: validated D / ERM / N nodes, no automatic scene edits."""
from pathlib import Path
import re, struct
import bpy

def preset_files(folder, key):
    key=str(key).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+",key):
        raise ValueError("Имя набора: только буквы, цифры, _ и -")
    root=Path(bpy.path.abspath(folder)).resolve()
    return root/("D_"+key+".png"),root/("ERM_"+key+".png"),root/("N_"+key+".png")

def validate_triplet(files):
    paths=tuple(Path(bpy.path.abspath(str(f))).resolve() for f in files)
    dims=[]
    for path in paths:
        if not path.is_file() or path.suffix.lower()!=".png":
            raise ValueError("Отсутствует PNG: "+str(path))
        with path.open("rb") as f:header=f.read(24)
        if len(header)!=24 or header[:8]!=b"\x89PNG\r\n\x1a\n":
            raise ValueError("Неверный формат PNG: "+str(path))
        dims.append(struct.unpack(">II",header[16:24]))
    if len(set(dims))!=1 or dims[0] not in ((2048,2048),(4096,4096)):
        raise ValueError("D/ERM/N должны быть одного размера: только 2048²/4096²")
    return paths,dims[0]

def create_pbr(name,files,*,pack=False):
    name=str(name).strip()
    if not name or len(name)>128:raise ValueError("Название материала: 1–128 символов")
    if bpy.data.materials.get(name):raise ValueError("Материал уже существует: "+name)
    paths,size=validate_triplet(files)
    imgs=[];material=None
    try:
        for path,colorspace in zip(paths,("sRGB","Non-Color","Non-Color")):
            existing={x.as_pointer() for x in bpy.data.images}
            img=bpy.data.images.load(str(path),check_existing=True)
            if img.as_pointer() in existing and img.colorspace_settings.name!=colorspace:
                img=img.copy()
                img.filepath=str(path)
            img.colorspace_settings.name=colorspace
            if tuple(img.size)!=size:raise ValueError("Несовпадение размера PNG "+path.name)
            imgs.append(img)
        material=bpy.data.materials.new(name)
        material.use_nodes=True
        nt=material.node_tree;nt.nodes.clear();nodes=nt.nodes;links=nt.links
        out=nodes.new("ShaderNodeOutputMaterial");out.location=(720,100)
        bsdf=nodes.new("ShaderNodeBsdfPrincipled");bsdf.location=(410,100)
        d=nodes.new("ShaderNodeTexImage");d.name="ISS_Diffuse";d.image=imgs[0];d.location=(-620,220)
        e=nodes.new("ShaderNodeTexImage");e.name="ISS_ERM";e.image=imgs[1];e.location=(-620,-30)
        n=nodes.new("ShaderNodeTexImage");n.name="ISS_Normal";n.image=imgs[2];n.location=(-620,-330)
        split=nodes.new("ShaderNodeSeparateColor");split.name="ISS_ERM_Channels";split.mode="RGB";split.location=(-295,-25)
        nm=nodes.new("ShaderNodeNormalMap");nm.name="ISS_NormalMap";nm.location=(-285,-320)
        links.new(d.outputs["Color"],bsdf.inputs["Base Color"])
        links.new(e.outputs["Color"],split.inputs["Color"])
        links.new(split.outputs["Green"],bsdf.inputs["Roughness"])
        links.new(split.outputs["Blue"],bsdf.inputs["Metallic"])
        links.new(n.outputs["Color"],nm.inputs["Color"])
        links.new(nm.outputs["Normal"],bsdf.inputs["Normal"])
        links.new(bsdf.outputs["BSDF"],out.inputs["Surface"])
        material["issmaker_pbr_source"]="D_ERM_N"
        material["issmaker_pbr_resolution"]=int(size[0])
        if pack:
            for img in imgs:img.pack()
        return material
    except Exception:
        if material is not None:bpy.data.materials.remove(material,do_unlink=True)
        for img in imgs:
            if img is not None and img.users==0:bpy.data.images.remove(img)
        raise

class OBJECT_OT_uv_wall_create_material(bpy.types.Operator):
    bl_idname="object.uv_wall_create_material"
    bl_label="Создать материал D / ERM / Normal"
    bl_description="Собрать PBR материал из PNG с возможностью назначения текущей стене"
    bl_options={"REGISTER","UNDO"}

    material_name:bpy.props.StringProperty(name="Материал",default="M_Brick_05",maxlen=128)
    use_preset:bpy.props.BoolProperty(name="Набор текстур",default=True)
    directory:bpy.props.StringProperty(name="Папка",subtype="DIR_PATH",default=r"D:\AGR\T")
    asset_key:bpy.props.StringProperty(name="Набор",default="Brick_05")
    diffuse_path:bpy.props.StringProperty(name="Diffuse",subtype="FILE_PATH")
    erm_path:bpy.props.StringProperty(name="ERM",subtype="FILE_PATH")
    normal_path:bpy.props.StringProperty(name="Normal",subtype="FILE_PATH")
    pack_images:bpy.props.BoolProperty(name="Упаковать текстуры в .blend",default=False)
    assign_to_wall:bpy.props.BoolProperty(name="Назначить текущей стене",default=True)

    @classmethod
    def poll(cls,context):
        return bool(context.area and context.area.type=="VIEW_3D" and context.mode=="OBJECT")

    def invoke(self,context,event):
        return context.window_manager.invoke_props_dialog(self,width=450)

    def draw(self,context):
        ui=self.layout
        ui.prop(self,"material_name")
        ui.prop(self,"use_preset")
        if self.use_preset:
            ui.prop(self,"directory")
            ui.prop(self,"asset_key")
            ui.label(text="D_ / ERM_ / N_ + набор.png",icon="FILE_IMAGE")
        else:
            ui.prop(self,"diffuse_path")
            ui.prop(self,"erm_path")
            ui.prop(self,"normal_path")
        ui.separator()
        ui.prop(self,"assign_to_wall")
        ui.prop(self,"pack_images")
        ui.label(text="ERM: G → Roughness · B → Metallic",icon="NODE_MATERIAL")

    def execute(self,context):
        material=None;definition=None;settings=None;path=None;old=None
        try:
            files=(preset_files(self.directory,self.asset_key) if self.use_preset
                   else (self.diffuse_path,self.erm_path,self.normal_path))
            # Validation and image loading precede WALL property mutation.
            material=create_pbr(self.material_name,files,pack=self.pack_images)
            assigned=False
            if self.assign_to_wall:
                settings=getattr(context.scene,"uv_wall_settings",None)
                path=getattr(settings,"path_mesh",None) if settings else None
                if path is not None:
                    from ..core.definitions import definition_for,sync_scene_from_definition
                    from ..core.material_intelligence import set_active_definition_asset
                    definition=definition_for(path)
                    if definition is not None:
                        old=(definition.module_standard,
                             definition.virtual_material_2k,
                             definition.virtual_material_4k,
                             definition.revision)
                        if not set_active_definition_asset(definition,material=material):
                            raise RuntimeError("UV Wall не принял материал по разрешению Diffuse")
                        sync_scene_from_definition(path,settings)
                        assigned=True
            text="Создан материал "+material.name+" · D/ERM/N"
            if assigned:text+=" · назначен WALL"
            elif self.assign_to_wall:text+=" · выберите Path для назначения"
            self.report({"INFO"},text)
            return {"FINISHED"}
        except Exception as exc:
            if definition is not None and old is not None:
                try:
                    (definition.module_standard,definition.virtual_material_2k,
                     definition.virtual_material_4k,definition.revision)=old
                except Exception:pass
            if material is not None:
                bpy.data.materials.remove(material,do_unlink=True)
            self.report({"WARNING"},str(exc)[:240])
            return {"CANCELLED"}
