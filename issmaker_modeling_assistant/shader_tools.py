import bpy
import os
import time
from pathlib import Path
from bpy.types import Operator

from . import log, runtime, texture_chain, shader_radial


MARKER = "issmaker_modeling_assistant"


def _material(context):
    obj = context.active_object
    if not obj or not hasattr(obj, "active_material"):
        return None
    return obj.active_material


def _socket(collection, *names):
    for name in names:
        socket = collection.get(name)
        if socket:
            return socket
    return None


def _ensure_material_nodes(context):
    material = _material(context)
    if material is None:
        return None, None, None
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next((node for node in tree.nodes if node.type == 'BSDF_PRINCIPLED'), None)
    output = next((node for node in tree.nodes if node.type == 'OUTPUT_MATERIAL'), None)
    if bsdf is None:
        bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (200, 80)
    if output is None:
        output = tree.nodes.new('ShaderNodeOutputMaterial')
        output.location = (520, 80)
    return tree, bsdf, output


def _link(tree, output, input_socket):
    if output is None or input_socket is None:
        return
    for link in list(input_socket.links):
        tree.links.remove(link)
    tree.links.new(output, input_socket)


class IMA_OT_CleanMaterial(Operator):
    bl_idname = "ima.clean_material"
    bl_label = "Очистить материал"
    bl_description = "Очистить ноды материала, оставив Principled BSDF и Material Output. Изменяет материал; доступен Ctrl+Z."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _material(context) is not None

    @runtime.protected("clean_material")
    def execute(self, context):
        tree, bsdf, output = _ensure_material_nodes(context)
        if tree is None:
            return {'CANCELLED'}
        removed = 0
        for node in list(tree.nodes):
            if node not in {bsdf, output}:
                tree.nodes.remove(node)
                removed += 1
        _link(tree, bsdf.outputs.get('BSDF'), output.inputs.get('Surface'))
        bsdf.location = (0, 0)
        output.location = (340, 0)
        log.write(f"Material cleaned: {_material(context).name}; removed={removed}")
        self.report({'INFO'}, f"Удалено нод: {removed}")
        return {'FINISHED'}


def _find_or_create(tree, node_type, role):
    node = next((item for item in tree.nodes if item.get(MARKER) == role), None)
    if node is None:
        node = tree.nodes.new(node_type)
        node[MARKER] = role
    return node


class IMA_OT_BuildMaterial(Operator):
    bl_idname = "ima.build_material"
    bl_label = "Создать текстурные ноды"
    bl_description = "Создать ноды Base Color, ERM и Normal с подключениями к шейдеру материала."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _material(context) is not None

    @runtime.protected("build_material")
    def execute(self, context):
        tree, bsdf, output = _ensure_material_nodes(context)
        if tree is None:
            return {'CANCELLED'}

        base = _find_or_create(tree, 'ShaderNodeTexImage', 'BASE')
        erm = _find_or_create(tree, 'ShaderNodeTexImage', 'ERM')
        separate = _find_or_create(tree, 'ShaderNodeSeparateColor', 'ERM_SEPARATE')
        normal = _find_or_create(tree, 'ShaderNodeTexImage', 'NORMAL')
        normal_map = _find_or_create(tree, 'ShaderNodeNormalMap', 'NORMAL_MAP')

        base.label, base.name = "BASE COLOR", "BASE_COLOR"
        erm.label, erm.name = "ERM · Non-Color", "ERM"
        normal.label, normal.name = "NORMAL · Non-Color", "NORMAL"
        separate.label = "R: Emission · G: Roughness · B: Metallic"
        normal_map.label = "Normal Map"

        base.location = (-760, 260)
        erm.location = (-760, -60)
        separate.location = (-500, -60)
        normal.location = (-760, -390)
        normal_map.location = (-450, -390)
        bsdf.location = (0, 40)
        output.location = (360, 40)

        for node in (erm, normal):
            if node.image:
                try:
                    node.image.colorspace_settings.name = 'Non-Color'
                except TypeError:
                    pass

        _link(tree, base.outputs.get('Color'), _socket(bsdf.inputs, 'Base Color'))
        _link(tree, base.outputs.get('Alpha'), _socket(bsdf.inputs, 'Alpha'))
        _link(tree, base.outputs.get('Color'), _socket(bsdf.inputs, 'Emission Color', 'Emission'))
        _link(tree, erm.outputs.get('Color'), separate.inputs.get('Color'))
        _link(tree, _socket(separate.outputs, 'Red', 'R'), _socket(bsdf.inputs, 'Emission Strength'))
        _link(tree, _socket(separate.outputs, 'Green', 'G'), _socket(bsdf.inputs, 'Roughness'))
        _link(tree, _socket(separate.outputs, 'Blue', 'B'), _socket(bsdf.inputs, 'Metallic'))
        _link(tree, normal.outputs.get('Color'), normal_map.inputs.get('Color'))
        _link(tree, normal_map.outputs.get('Normal'), _socket(bsdf.inputs, 'Normal'))
        _link(tree, bsdf.outputs.get('BSDF'), output.inputs.get('Surface'))

        log.write(f"Material graph built: {_material(context).name}")
        self.report({'INFO'}, "BASE COLOR, ERM и NORMAL созданы")
        return {'FINISHED'}



class IMA_OT_BuildMaterialFromFolder(Operator):
    """Validate all roles/tiles before touching material nodes."""
    bl_idname = 'ima.build_material_from_folder'
    bl_label = 'Подобрать текстуры (Diffuse / ERM / Normal)'
    bl_description = 'Выберите папку либо пример одной текстуры — вторая и третья найдутся автоматически'
    bl_options = {'REGISTER','UNDO'}
    filepath: bpy.props.StringProperty(name='Одна текстура (необязательно)',subtype='FILE_PATH',default='')
    directory: bpy.props.StringProperty(name='Папка с текстурами',subtype='DIR_PATH',default='')
    filter_glob: bpy.props.StringProperty(default='*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.exr;*.tga;*.webp;*.bmp',options={'HIDDEN'})
    object_name: bpy.props.StringProperty(default='',options={'HIDDEN'})
    material_name: bpy.props.StringProperty(default='',options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return _material(context) is not None

    def invoke(self, context, event):
        self.object_name=context.active_object.name
        self.material_name=_material(context).name
        if not self.directory:
            self.directory=bpy.path.abspath('//') if bpy.data.filepath else os.path.expanduser('~')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        # Blender file picker is asynchronous; never apply to a different object.
        if (not context.active_object or not _material(context) or
                (self.object_name and self.object_name!=context.active_object.name) or
                (self.material_name and self.material_name!=_material(context).name)):
            self.report({'ERROR'},'Объект или материал изменился — операция отменена')
            return {'CANCELLED'}
        sample=bpy.path.abspath(self.filepath) if self.filepath else ''
        folder=bpy.path.abspath(self.directory) if self.directory else (os.path.dirname(sample) if sample else '')
        if sample and os.path.isdir(sample):
            folder=sample;sample=''
        if sample and not os.path.isfile(sample):
            # The Blender browser may supply an unselected filename; choose only
            # a real sample file, never guess from stale file browser text.
            sample=''
        try:
            sets=texture_chain.scan(folder,sample or None)
        except (texture_chain.TextureChainError,OSError,ValueError) as exc:
            self.report({'ERROR'},'Текстуры: '+str(exc)[:215])
            log.write('UDIM preflight blocked: '+str(exc))
            return {'CANCELLED'}
        # Stage all images BEFORE creating any shader nodes.
        staged={}
        created=[]
        try:
            for role,tile_set in sets.items():
                first=tile_set.files[0][1]
                img=bpy.data.images.load(first,check_existing=False)
                created.append(img)
                if tile_set.tiles!=(0,):
                    img.source='TILED'
                    for num in tile_set.tiles:
                        if num not in {t.number for t in img.tiles}:
                            img.tiles.new(tile_number=num)
                    img.filepath=texture_chain.udim_filepath(tile_set)
                if role!='DIFFUSE':
                    img.colorspace_settings.name='Non-Color'
                staged[role]=img
        except Exception as exc:
            for img in created:
                try:bpy.data.images.remove(img)
                except (RuntimeError,ReferenceError):pass
            self.report({'ERROR'},'Загрузка текстур отменена: '+str(exc)[:190])
            log.write_exception('UDIM staging failed',exc)
            return {'CANCELLED'}
        # Existing stable construction path, performed once after complete preflight.
        try:
            result=bpy.ops.ima.build_material('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                raise RuntimeError('Не удалось создать схему материала')
            material=_material(context)
            nodes=material.node_tree.nodes
            for role,marker in (('DIFFUSE','BASE'),('ERM','ERM'),('NORMAL','NORMAL')):
                node=next((n for n in nodes if n.get(MARKER)==marker),None)
                if node is None:raise RuntimeError('Отсутствует нода '+marker)
                node.image=staged[role]
            log.write('UDIM material assigned: asset=%s, tiles=%s, folder=%s' %
                      (sets['DIFFUSE'].asset,sets['DIFFUSE'].tiles,folder))
            self.report({'INFO'},'Текстуры назначены: %d плиток UDIM' % len(sets['DIFFUSE'].tiles)
                        if sets['DIFFUSE'].tiles!=(0,) else 'Diffuse / ERM / Normal назначены')
            return {'FINISHED'}
        except Exception as exc:
            # Let Blender UNDO recover material edits; do not pretend this was an
            # atomic transaction. Images without users may be disposed safely.
            for img in created:
                try:
                    if img.users==0:bpy.data.images.remove(img)
                except (RuntimeError,ReferenceError):pass
            log.write_exception('Material build failed after preflight',exc)
            self.report({'ERROR'},'Ошибка нод: '+str(exc)[:180]+'. При необходимости Ctrl+Z')
            return {'CANCELLED'}


_ACTIVE_CHOICES=[]

class IMA_OT_MaterialChoice(Operator):
    bl_idname='ima.material_choice'
    bl_label='Создание текстурных нод'
    bl_description='Открыть выбор создания нод: пустая схема или автоматическая загрузка Diffuse, ERM, Normal и UDIM-плиток.'
    bl_options={'REGISTER','INTERNAL'}

    @classmethod
    def poll(cls,context):
        return bool(context.area and context.area.type=='NODE_EDITOR' and
                    getattr(context.space_data,'tree_type','')=='ShaderNodeTree' and
                    _material(context) is not None)

    def invoke(self,context,event):
        if _ACTIVE_CHOICES:return {'CANCELLED'}
        if context.region.width<340 or context.region.height<310:
            self.report({'WARNING'},'Shader Editor слишком мал для выбора; увеличьте область')
            return {'CANCELLED'}
        self._area=context.area
        self._window=context.window
        self._object=context.active_object
        self._material=_material(context)
        self._center=(context.region.width/2,context.region.height/2)
        self._scale=min(1.1,max(.8,min(context.region.width/560,context.region.height/440)))
        self._handler=None
        self._timer=None
        self._closed=False
        self._hover=-1
        self._start=time.monotonic()
        try:
            self._handler=bpy.types.SpaceNodeEditor.draw_handler_add(self._draw,(),'WINDOW','POST_PIXEL')
            self._timer=context.window_manager.event_timer_add(.04,window=context.window)
            context.window_manager.modal_handler_add(self)
            _ACTIVE_CHOICES.append(self)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}
        except Exception as exc:
            self._cleanup(context)
            log.write_exception('Shader choice modal error',exc)
            return {'CANCELLED'}

    def _valid(self,context):
        try:
            return (context.window==self._window and self._area.type=='NODE_EDITOR' and
                    context.area==self._area and context.active_object==self._object and
                    _material(context)==self._material)
        except (ReferenceError,RuntimeError,AttributeError):return False

    def _draw(self):
        try:
            ctx=bpy.context
            if not self._closed and self._valid(ctx):
                shader_radial.render(self._center,self._scale,self._start,self._hover)
        except Exception as exc:
            log.write_exception('Shader radial GPU failed',exc)
            self._gpu_failed=True

    def _cleanup(self,context):
        if getattr(self,'_closed',False):return
        self._closed=True
        if self in _ACTIVE_CHOICES:_ACTIVE_CHOICES.remove(self)
        if self._handler is not None:
            try:bpy.types.SpaceNodeEditor.draw_handler_remove(self._handler,'WINDOW')
            except (ReferenceError,RuntimeError,ValueError):pass
            self._handler=None
        if self._timer is not None:
            try:context.window_manager.event_timer_remove(self._timer)
            except (ReferenceError,RuntimeError,ValueError):pass
            self._timer=None
        try:self._area.tag_redraw()
        except (ReferenceError,RuntimeError):pass

    def modal(self,context,event):
        if getattr(self,'_gpu_failed',False) or not self._valid(context):
            self._cleanup(context)
            return {'CANCELLED'}
        if (event.type=='WINDOW_DEACTIVATE' or
                (event.type in {'ESC','RIGHTMOUSE'} and event.value=='PRESS')):
            self._cleanup(context);return {'CANCELLED'}
        if event.type=='TIMER':
            self._area.tag_redraw();return {'RUNNING_MODAL'}
        if event.type in {'MOUSEMOVE','INBETWEEN_MOUSEMOVE'}:
            self._hover=shader_radial.hit(event.mouse_region_x-self._center[0],
                                          event.mouse_region_y-self._center[1],self._scale)
            self._area.tag_redraw();return {'RUNNING_MODAL'}
        if event.type=='LEFTMOUSE' and event.value=='PRESS':
            hit=shader_radial.hit(event.mouse_region_x-self._center[0],
                                  event.mouse_region_y-self._center[1],self._scale)
            self._cleanup(context)
            if hit==0:return bpy.ops.ima.build_material('EXEC_DEFAULT')
            if hit==1:return bpy.ops.ima.build_material_from_folder('INVOKE_DEFAULT')
            return {'CANCELLED'}
        if event.value=='PRESS' and event.type not in {'LEFTMOUSE','WHEELUPMOUSE','WHEELDOWNMOUSE'}:
            self._cleanup(context)
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


def release_choice_modals():
    for modal in tuple(_ACTIVE_CHOICES):
        try:modal._cleanup(bpy.context)
        except (ReferenceError,RuntimeError,AttributeError):pass

CLASSES = (IMA_OT_CleanMaterial, IMA_OT_BuildMaterial, IMA_OT_BuildMaterialFromFolder, IMA_OT_MaterialChoice)


def register():
    runtime.register_classes_atomic(CLASSES)


def unregister():
    release_choice_modals()
    runtime.unregister_classes_safe(CLASSES)
