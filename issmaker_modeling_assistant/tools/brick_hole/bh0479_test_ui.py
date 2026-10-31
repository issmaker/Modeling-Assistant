"""Opt-in TEST 1 viewport copy (no change to production extrusion)."""
import bpy
import bmesh
from mathutils import Vector


class OBJECT_OT_brick_hole_corner_test1_copy(bpy.types.Operator):
    bl_idname = 'object.brick_hole_corner_test1_copy'
    bl_label = 'TEST 1 · Экспериментальный угол'
    bl_description = ('Создать отдельную копию стены с экспериментальным проёмом '
                      'на 3 выбранных гранях. Исходную стену не изменять')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode == 'EDIT_MESH':
            return (context.edit_object is not None and
                    context.edit_object.type == 'MESH' and
                    tuple(context.tool_settings.mesh_select_mode) == (False,False,True)
                    and len(context.objects_in_mode) == 1)
        return (context.mode == 'OBJECT' and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        from .bh0478_corner_poc import run_corner_test_one
        source = context.edit_object if context.mode == 'EDIT_MESH' else context.active_object
        scratch = None
        output = None
        preview = None
        try:
            if source.data.shape_keys or source.data.library:
                raise ValueError('Требуется локальный Mesh без Shape Keys')
            if context.mode == 'EDIT_MESH':
                edit = bmesh.from_edit_mesh(source.data)
                edit.faces.ensure_lookup_table()
                edit.faces.index_update()
                ids = tuple(f.index for f in edit.faces if f.select and not f.hide)
                scratch = bpy.data.meshes.new('BH_TEST1_SOURCE_TEMP')
                bm_copy = edit.copy()
                try:
                    bm_copy.to_mesh(scratch)
                finally:
                    bm_copy.free()
                test_source = scratch
            else:
                ids = tuple(f.index for f in source.data.polygons
                            if f.select and not f.hide)
                test_source = source.data
            if len(ids) != 3:
                raise ValueError('Выделите ровно 3 связанные грани')
            if (str(context.scene.brick_hole_uv_map or 'UVMap') != 'UVMap'
                    or int(context.scene.brick_hole_resolution) != 4096):
                raise ValueError('TEST 1 требует UVMap и тайл 4096')
            depth = -abs(float(context.scene.brick_hole_depth))
            if not abs(depth) > 1e-6:
                raise ValueError('Глубина должна быть больше нуля')
            proxy = type('SourceProxy', (), {
                'type':'MESH', 'data':test_source,
                'matrix_world':source.matrix_world.copy()})()
            output = bpy.data.meshes.new('BH_TEST1_RESULT_MESH')
            report = run_corner_test_one(proxy, ids, depth, output_mesh=output)
            if report['status'] != 'PASS':
                raise RuntimeError('Проверка геометрии / UV не пройдена')
            for material in source.data.materials:
                output.materials.append(material)
            preview = bpy.data.objects.new('BH TEST 1 · '+source.name, output)
            preview.matrix_world = source.matrix_world.copy()
            xs = [p[0] for p in source.bound_box]
            preview.matrix_world.translation += source.matrix_world.to_3x3() @ (
                Vector((max(xs)-min(xs)+3.0,0,0)))
            preview['BH_TEST1_EXPERIMENT'] = True
            preview['BH_TEST1_SOURCE'] = source.name
            preview['BH_TEST1_SELECTED_FACES'] = ','.join(map(str,ids))
            preview['BH_TEST1_DEPTH_M'] = depth
            preview['BH_TEST1_UV_DENSITY'] = 1000
            preview['BH_TEST1_RESULT'] = 'PASS'
            preview.hide_render = True
            preview.show_name = True
            context.scene.collection.objects.link(preview)
            self.report({'INFO'},'TEST 1: '+preview.name+
                        ' · геометрия + UV PASS · источник не изменён')
            return {'FINISHED'}
        except (RuntimeError,ValueError,TypeError,ReferenceError) as ex:
            if preview is not None and preview.name in bpy.data.objects:
                bpy.data.objects.remove(preview,do_unlink=True)
            self.report({'ERROR'},'TEST 1: '+str(ex)[:210])
            return {'CANCELLED'}
        finally:
            if scratch is not None and scratch.users == 0:
                bpy.data.meshes.remove(scratch)
            if output is not None and output.users == 0:
                bpy.data.meshes.remove(output)
