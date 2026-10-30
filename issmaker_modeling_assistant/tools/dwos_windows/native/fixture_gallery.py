"""Blender operators for the one-object DWOS window-opening fixture gallery.

Pure construction and contract tests live in fixture_wall_core.py.  This module
is the only bridge to bpy; no existing DWOS windows, hosts or geometry are edited.
"""
from __future__ import annotations
import json
from collections import Counter
import bpy
from mathutils import Vector
from .fixture_wall_core import build_fixture_gallery, validate_fixture_gallery, SCHEMA, SCHEMA_LONG
from .adaptive_detection_core import discover_surface_openings
from .dwos_log import event as log_event, exception as log_exception

MAT_COLORS = {
    'PLANAR': (0.20, 0.42, 0.56, 1.0),
    'CURVED': (0.17, 0.47, 0.42, 1.0),
    'CORNER': (0.65, 0.36, 0.21, 1.0),
}


def _check_openings(data, *, actual_vertices=None, actual_faces=None, actual_normals=None):
    vertices = actual_vertices if actual_vertices is not None else data['vertices']
    faces = actual_faces if actual_faces is not None else data['faces']
    results = discover_surface_openings(vertices, faces, face_normals=actual_normals,
                                        up_hint=(0, 0, 1), source='DWOS_FIXTURE_GALLERY')
    records=[]
    candidates=results['openings']
    for spec in data['cases']:
        start=spec['vertex_start']; end=start+spec['vertex_count']
        linked=[r for r in candidates if r.get('loop_vertex_ids') and
                all(start<=vid<end for vid in r['loop_vertex_ids'])]
        if len(linked)!=1:
            raise ValueError(f"{spec['id']}: expected one opening, detected {len(linked)}")
        topo=linked[0]
        if not topo.get('build_supported') or topo['classification']!=spec['type']:
            raise ValueError(f"{spec['id']}: expected {spec['type']}, got {topo['classification']} ({topo.get('reasons')})")
        records.append({'id':spec['id'],'expected':spec['type'],
                        'detected':topo['classification'],'width_m':round(float(topo['width']),5),
                        'height_m':round(float(topo['height']),5),
                        'stations':len(topo['stations']),
                        'reasons':list(topo.get('reasons',[]))})
    counts=Counter(r['detected'] for r in records)
    if len(candidates)!=15 or any(counts.get(typ)!=5 for typ in MAT_COLORS):
        raise ValueError('Gallery must have exactly 5 PLANAR, 5 CURVED, 5 CORNER openings')
    return {'schema':data['schema'], 'passed':15, 'expected':15, 'counts':dict(counts),
            'cases':records, 'boundary_edges':results['boundary_edges'],
            'face_components':results['face_components']}


def _material(typ):
    name='DWOS · Fixture '+typ
    material=bpy.data.materials.get(name)
    if material is None:
        material=bpy.data.materials.new(name)
        material.diffuse_color=MAT_COLORS[typ]
    return material


def _is_gallery(obj):
    return obj is not None and obj.type=='MESH' and obj.get('DWOS_fixture_schema') in {SCHEMA,SCHEMA_LONG,'DWOS_fixture_interior_contact_v1'}


class DWOS_OT_create_fixture_gallery(bpy.types.Operator):
    bl_idname='dwos.create_fixture_gallery'
    bl_label='Создать 15 тестовых стен с проёмами'
    bl_description='Один Mesh с 15 настоящими проёмами: 5 прямых, 5 скруглённых, 5 острых углов. Не меняет существующие объекты'
    bl_options={'REGISTER','UNDO'}
    variant: bpy.props.EnumProperty(name='Длина проёмов', items=[('STANDARD','Обычные','Обычные тесты'),('LONG','Очень длинные','Проёмы 24–200 м')],default='STANDARD')

    @classmethod
    def poll(cls, context):
        return context.scene is not None and context.mode=='OBJECT'

    def execute(self, context):
        new_obj=None
        new_mesh=None
        old_active=context.view_layer.objects.active
        old_selected=tuple(context.selected_objects)
        try:
            data=build_fixture_gallery(variant=self.variant)
            # Preflight the exact topology that will be passed to Blender.
            validate_fixture_gallery(data)
            pure_report=_check_openings(data)
            new_mesh=bpy.data.meshes.new('DWOS_15_'+self.variant+'_Fixture_Walls_Mesh')
            new_mesh.from_pydata(data['vertices'], [], data['faces'])
            new_mesh.update(calc_edges=True)
            if len(new_mesh.vertices)!=len(data['vertices']) or len(new_mesh.polygons)!=len(data['faces']):
                raise RuntimeError('Blender Mesh creation unexpectedly changed topology')
            actual_verts=[tuple(float(x) for x in v.co) for v in new_mesh.vertices]
            actual_faces=[tuple(int(i) for i in p.vertices) for p in new_mesh.polygons]
            actual_normals=[tuple(float(x) for x in p.normal) for p in new_mesh.polygons]
            verified=_check_openings(data,actual_vertices=actual_verts,
                                     actual_faces=actual_faces,actual_normals=actual_normals)
            if [row['detected'] for row in pure_report['cases']] != [row['detected'] for row in verified['cases']]:
                raise ValueError('Actual mesh classification differs from fixture source')
            # A single object and exactly one mesh datablock; separate islands
            # preserve boundary loops and the current DWOS SURFACE detection.
            new_obj=bpy.data.objects.new('DWOS_15_'+self.variant+'_Window_Opening_Fixtures',new_mesh)
            context.collection.objects.link(new_obj)
            for mat_name in MAT_COLORS:
                new_mesh.materials.append(_material(mat_name))
            material_index={name:i for i,name in enumerate(MAT_COLORS)}
            face_attr=new_mesh.attributes.new('DWOS_fixture_case','INT','FACE')
            for fi,case_index in enumerate(data['face_case_ids']):
                face_attr.data[fi].value=int(case_index+1)
                new_mesh.polygons[fi].material_index=material_index[data['cases'][case_index]['type']]
            # Material slots and face attributes may not alter surface topology.
            new_obj['DWOS_fixture_schema']=data['schema']
            new_obj['DWOS_fixture_variant']=self.variant
            new_obj['DWOS_fixture_version']='3.7.15-rc16'
            new_obj['DWOS_fixture_manifest_json']=json.dumps(data['cases'],ensure_ascii=False,separators=(',',':'))
            new_obj['DWOS_fixture_validation_json']=json.dumps(verified,ensure_ascii=False,separators=(',',':'))
            new_obj['DWOS_fixture_expected_count']=15
            new_obj['DWOS_fixture_wall_mode']='SURFACE'
            new_obj['DWOS_fixture_hint']='N → DWOS → НАЙТИ ПРОЁМЫ; 5 PLANAR / 5 CURVED / 5 CORNER'
            new_obj.location=context.scene.cursor.location.copy()
            new_obj.display_type='TEXTURED'
            for obj in old_selected:
                try:obj.select_set(False)
                except (ReferenceError,RuntimeError):pass
            new_obj.select_set(True)
            context.view_layer.objects.active=new_obj
            log_event('FIXTURE_GALLERY_CREATE',object=new_obj.name,expected=15,detected=15,
                      planar=5,curved=5,corner=5,wall_mode='SURFACE',
                      vertices=len(new_mesh.vertices),faces=len(new_mesh.polygons))
        except Exception as exc:
            if new_obj is not None:
                bpy.data.objects.remove(new_obj,do_unlink=True)
            if new_mesh is not None and new_mesh.users==0:
                bpy.data.meshes.remove(new_mesh)
            for obj in old_selected:
                try:obj.select_set(True)
                except (ReferenceError,RuntimeError):pass
            try:context.view_layer.objects.active=old_active
            except (ReferenceError,RuntimeError):pass
            log_exception('FIXTURE_GALLERY_CREATE_FAILED',exc)
            self.report({'ERROR'},f'DWOS: не удалось создать галерею: {exc}')
            return {'CANCELLED'}
        self.report({'INFO'},'Создан один Mesh: 15 проёмов (PLANAR 5 / CURVED 5 / CORNER 5). Далее: НАЙТИ ПРОЁМЫ')
        return {'FINISHED'}


class DWOS_OT_check_fixture_gallery(bpy.types.Operator):
    bl_idname='dwos.check_fixture_gallery'
    bl_label='Проверить 15 тестовых проёмов'
    bl_description='Повторно классифицировать настоящие отверстия текущего Mesh и сохранить диагностический отчёт'
    bl_options={'REGISTER'}

    @classmethod
    def poll(cls,context):
        return _is_gallery(context.active_object) and context.mode=='OBJECT'

    def execute(self,context):
        obj=context.active_object
        try:
            reference=build_fixture_gallery(variant=obj.get('DWOS_fixture_variant','LONG' if obj.get('DWOS_fixture_schema')==SCHEMA_LONG else 'STANDARD'))
            mesh=obj.data
            actual_vertices=[tuple(float(v) for v in vertex.co) for vertex in mesh.vertices]
            actual_faces=[tuple(int(i) for i in face.vertices) for face in mesh.polygons]
            actual_normals=[tuple(float(i) for i in face.normal) for face in mesh.polygons]
            report=_check_openings(reference,actual_vertices=actual_vertices,
                                   actual_faces=actual_faces,actual_normals=actual_normals)
            report.update(object_name=obj.name,mesh_vertices=len(mesh.vertices),
                          mesh_faces=len(mesh.polygons),status='PASS')
            obj['DWOS_fixture_validation_json']=json.dumps(report,ensure_ascii=False,separators=(',',':'))
            log_event('FIXTURE_GALLERY_CHECK_OK',object=obj.name,passed=15)
            self.report({'INFO'},'15/15: P=5, R=5, A=5. Отчёт включается в диагностический ZIP')
            return {'FINISHED'}
        except Exception as exc:
            report={'schema':obj.get('DWOS_fixture_schema',SCHEMA),'object_name':obj.name,'status':'FAIL',
                    'error':f'{type(exc).__name__}: {exc}'}
            obj['DWOS_fixture_validation_json']=json.dumps(report,ensure_ascii=False,separators=(',',':'))
            log_exception('FIXTURE_GALLERY_CHECK_FAILED',exc,object=obj.name)
            self.report({'ERROR'},f'Тестовая стена: {exc}')
            return {'CANCELLED'}


class DWOS_OT_focus_fixture_case(bpy.types.Operator):
    bl_idname='dwos.focus_fixture_case'
    bl_label='Показать тестовый проём'
    bl_description='Навести 3D-вид на один из 15 фрагментов, не меняя Mesh и выделение'
    bl_options={'REGISTER'}

    case_id: bpy.props.StringProperty(name='ID примера',default='P01')

    @classmethod
    def poll(cls,context):
        return _is_gallery(context.active_object) and context.area and context.area.type=='VIEW_3D'

    def execute(self,context):
        obj=context.active_object
        try:
            cases=json.loads(obj['DWOS_fixture_manifest_json'])
            case=next((x for x in cases if x['id']==self.case_id),None)
            if case is None:raise ValueError('Пример не найден')
            verts=obj.data.vertices
            ids=range(case['vertex_start'],case['vertex_start']+case['vertex_count'])
            points=[obj.matrix_world @ verts[i].co for i in ids]
            bounds_min=Vector(tuple(min(p[k] for p in points) for k in range(3)))
            bounds_max=Vector(tuple(max(p[k] for p in points) for k in range(3)))
            space=context.space_data
            rv3d=space.region_3d
            if rv3d is None:raise ValueError('3D RegionView не найден')
            rv3d.view_location=(bounds_min+bounds_max)*0.5
            rv3d.view_distance=max(4.5,(bounds_max-bounds_min).length*1.5)
            context.area.tag_redraw()
            log_event('FIXTURE_GALLERY_FOCUS',object=obj.name,case=self.case_id)
        except (KeyError,IndexError,ValueError,TypeError) as exc:
            self.report({'WARNING'},f'Не удалось перейти к {self.case_id}: {exc}')
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES=(DWOS_OT_create_fixture_gallery,DWOS_OT_check_fixture_gallery,DWOS_OT_focus_fixture_case)
