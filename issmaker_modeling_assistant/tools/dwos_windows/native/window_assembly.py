"""DWOS 2.1.5: complete frame, glazing and Interior as one independent Mesh.

The Host wall is deliberately read-only.  Glass remains a separate shell within
one Blender Mesh object.  No simulated 'Weld' or fake shared-edge claims.
"""
from __future__ import annotations

import bpy
import json

from .numeric import parse_meter_text

from .normal_winding_core import compute_face_flips

from .weld_bake import (collect_auxiliary, _validate_auxiliary,
                        bake_auxiliary_mesh, remove_dwos_mesh_metadata)
from .dwos_log import event as log_event, exception as log_exception
from .batch_edit import resolve_window, target_windows
from .adaptive_opening_core import map_svd_local
from .adaptive_validation import validate_mesh as validate_adaptive_mesh
from .lod_publish import stage_planar_lp_mesh, get_or_create_collection, apply_lod_visibility, HP_COLLECTION, LP_COLLECTION


def _check_part_normals(obj, direction):
    # Existing planar meshes can be audited against fixed local ±Z anchors.
    # Adaptive meshes deliberately rotate their normals along S, so the legacy
    # Z-axis normal oracle is invalid there.  Use the adaptive topology validator
    # instead of falsely reporting every curved/corner face as inverted.
    key = 'DWOS_glass_roles' if obj.get('DWOS_role') == 'GLASS' else 'DWOS_face_roles'
    try:
        roles = json.loads(obj.get(key, '') or '[]')
        verts = [tuple(vert.co) for vert in obj.data.vertices]
        faces = [list(poly.vertices) for poly in obj.data.polygons]
        if obj.get('DWOS_adaptive_classification') in {'PLANAR','CURVED','CORNER'}:
            validate_adaptive_mesh({'verts':verts,'faces':faces,'roles':roles})
            return
        flips = compute_face_flips(verts, faces, roles, direction)
    except Exception as exc:
        raise RuntimeError(f'{obj.name}: проверка нормалей не выполнена: {exc}') from exc
    if flips:
        raise RuntimeError(f'{obj.name}: {len(flips)} неверно ориентированных граней. '
                           'Сначала нажмите «Исправить нормали DWOS».')


def _adaptive_topology(frame):
    try:
        payload=json.loads(frame.dwos_window.opening_payload_json or '{}')
        topo=payload.get('adaptive_topology')
        return topo if isinstance(topo,dict) and topo.get('coordinate_system')=='SVD_RIBBON' else None
    except Exception:
        return None


def contact_ring_stats(frame):
    """Audit the Host-contact perimeter without collapsing valid Host topology."""
    topology=_adaptive_topology(frame)
    if topology is not None:
        w=float(topology['width']);h=float(topology['height']);hx=w*.5;hy=h*.5
        d=float(frame.get('DWOS_frame_front_z',0.0))
        expected=[]
        for item in topology.get('top_contact',[]):expected.append(map_svd_local(topology,float(item['s']),hy,d))
        for item in topology.get('bottom_contact',[]):expected.append(map_svd_local(topology,float(item['s']),-hy,d))
        for item in topology.get('left_contact',[]):expected.append(map_svd_local(topology,-hx,float(item.get('v',0.0)),d))
        for item in topology.get('right_contact',[]):expected.append(map_svd_local(topology,hx,float(item.get('v',0.0)),d))
        # Corners occur in two contact chains; deduplicate geometrically.
        unique=[]
        for point in expected:
            if not any(sum((point[i]-q[i])**2 for i in range(3)) <= 1e-12 for q in unique):
                unique.append(point)
        verts=[tuple(float(x) for x in v.co) for v in frame.data.vertices]
        missing=[]
        for point in unique:
            if not any(sum((point[i]-q[i])**2 for i in range(3)) <= 1e-10 for q in verts):
                missing.append(point)
        if missing:
            raise RuntimeError(f'{frame.name}: контакт с Host потерял {len(missing)} исходных вершин')
        authored=int(frame.get('DWOS_contact_front_vertices',len(unique)))
        if authored!=len(unique):
            raise RuntimeError(f'{frame.name}: метаданные контактного контура не совпадают с OpeningTopology')
        return {'corners':4,'outer_vertices':len(unique),'extra_junctions':0,
                'adaptive':True,'classification':topology.get('classification',''),
                'host_contact_vertices':len(unique)}

    # Legacy PLANAR audit. Four corners are required and extra vertices really
    # are legacy Split pollution that can be rebuilt safely.
    settings = frame.dwos_window
    w, h = parse_meter_text(settings.width_text), parse_meter_text(settings.height_text)
    z = float(frame.get('DWOS_frame_front_z', 0.0))
    hx, hy = w/2.0, h/2.0
    epsilon = 1e-5
    boundary = set(); corner = set()
    for vertex in frame.data.vertices:
        x, y, vz = vertex.co
        if abs(vz-z) > epsilon:continue
        on_x = abs(abs(x)-hx) <= epsilon; on_y = abs(abs(y)-hy) <= epsilon
        if on_x or on_y:
            key=(round(float(x),6),round(float(y),6));boundary.add(key)
            if on_x and on_y:corner.add(key)
    if len(corner) != 4:
        raise RuntimeError(f'{frame.name}: наружный контур содержит {len(corner)} углов вместо 4')
    return {'corners':len(corner),'outer_vertices':len(boundary),
            'extra_junctions':len(boundary)-len(corner),'adaptive':False}


def _stage_window(frame):
    if frame.type != 'MESH' or frame.dwos_window.finalized or frame.get('DWOS_archived_source'):
        raise RuntimeError(f'{frame.name}: выберите редактируемую раму DWOS')
    if (frame.library or frame.data.library or frame.data.users != 1
            or frame.data.shape_keys or frame.modifiers or frame.constraints):
        raise RuntimeError(f'{frame.name}: нужен локальный Single User Mesh без модификаторов')
    if not frame.users_collection:
        raise RuntimeError(f'{frame.name}: рама не принадлежит коллекции сцены')
    _validate_auxiliary(frame)
    ring = contact_ring_stats(frame)
    if ring['extra_junctions']:
        raise RuntimeError(f'{frame.name}: наружный контур содержит '
                           f'{ring["extra_junctions"]} лишних вершин. '
                           'Сначала перестройте контактный контур в боковом меню.')
    children = collect_auxiliary(frame)
    direction = 1 if frame.dwos_window.flip_interior else -1
    for obj in (frame, *children):
        _check_part_normals(obj, direction)
    result = frame.data.copy()
    try:
        pre_faces = len(result.polygons) + sum(len(o.data.polygons) for o in children)
        pre_verts = len(result.vertices) + sum(len(o.data.vertices) for o in children)
        bake_auxiliary_mesh(frame, result, children)
        if len(result.polygons) != pre_faces or len(result.vertices) != pre_verts:
            raise RuntimeError(f'{frame.name}: сборка неожиданно изменила число вершин/граней')
        if not result.materials:
            raise RuntimeError(f'{frame.name}: после сборки не осталось материалов')
        if any(poly.area <= 1e-12 for poly in result.polygons):
            raise RuntimeError(f'{frame.name}: нулевая площадь полигона в собранной модели')
        remove_dwos_mesh_metadata(result)
        # Four-corner external contact is mandatory. Other internal Split
        # junctions are retained within the frame and do not touch the Host.
        return result, children
    except Exception:
        if result.users == 0:
            bpy.data.meshes.remove(result)
        raise


def assemble_windows(frames, context):
    """Stage HP+planar LP as a pair; unsupported adaptive windows retain HP."""
    if not frames or context.mode != 'OBJECT':
        raise RuntimeError('Выберите редактируемые окна в Object Mode')
    if len({f.as_pointer() for f in frames}) != len(frames):
        raise RuntimeError('Повторение окна в группе сборки')
    staged=[]
    created=[]
    hp_outputs=[]
    sources=[]
    source_collections={}
    created_collections=[]
    try:
        for frame in frames:
            hp, children=_stage_window(frame)
            try:
                lp=stage_planar_lp_mesh(frame)
            except Exception:
                if hp.users==0:
                    bpy.data.meshes.remove(hp)
                raise
            staged.append((frame,hp,children,lp))
        # All LP and HP meshes are prebuilt and valid before any source unlink.
        for frame, hp_mesh, children, lp_mesh in staged:
            base='Window_'+frame.name.removeprefix('DWOS_Window_')
            uid=str(frame.dwos_window.window_id or frame.name)
            paired=lp_mesh is not None
            hp_obj=bpy.data.objects.new(base+'_HP' if paired else base,hp_mesh)
            created.append(hp_obj)
            hp_outputs.append(hp_obj)
            hp_obj.matrix_world=frame.matrix_world.copy()
            if paired:
                hp_group=get_or_create_collection(context.scene,HP_COLLECTION,created_collections)
                hp_group.objects.link(hp_obj)
                lp_obj=bpy.data.objects.new(base+'_LP',lp_mesh)
                created.append(lp_obj)
                lp_obj.matrix_world=frame.matrix_world.copy()
                lp_group=get_or_create_collection(context.scene,LP_COLLECTION,created_collections)
                lp_group.objects.link(lp_obj)
                hp_obj['DWOS_LOD_TYPE']='HP'
                lp_obj['DWOS_LOD_TYPE']='LP'
                hp_obj['DWOS_window_uuid']=uid
                lp_obj['DWOS_window_uuid']=uid
                hp_obj['DWOS_LOD_partner']=lp_obj.name
                lp_obj['DWOS_LOD_partner']=hp_obj.name
                hp_obj['DWOS_LOD_SCHEMA']='DWOS_PAIRED_PLANAR_V1'
                lp_obj['DWOS_LOD_SCHEMA']='DWOS_PAIRED_PLANAR_V1'
            else:
                # Existing CORNER/CURVED HP build is retained exactly; no
                # fabricated zero-depth planar LP is published.
                frame.users_collection[0].objects.link(hp_obj)
                hp_obj['DWOS_LOD_STATUS']='ADAPTIVE_HP_ONLY_NOT_YET_SUPPORTED'
            sources.extend((frame,*children))
        for source in sources:
            source_collections[source]=tuple(source.users_collection)
        for source in sources:
            for collection in tuple(source.users_collection):
                collection.objects.unlink(source)
        mode=str(getattr(context.scene.dwos,'lod_view_mode','HP'))
        apply_lod_visibility(context.scene,context.view_layer,mode)
        try:
            context.scene.dwos.show_overlay=False
            for obj in tuple(context.selected_objects):
                obj.select_set(False)
            visible_outputs=[obj for obj in created
                             if obj.get('DWOS_LOD_TYPE')==mode or
                             (mode in {'HP','BOTH'} and obj in hp_outputs)]
            for obj in visible_outputs:
                obj.select_set(True)
            if visible_outputs:
                context.view_layer.objects.active=visible_outputs[-1]
        except Exception as exc:
            log_event('ASSEMBLY_SELECTION_WARNING',error=repr(exc))
        for frame in frames:
            log_event('WINDOW_CONTACT_RING',window=frame.name,**contact_ring_stats(frame))
        log_event('WINDOW_MESH_ASSEMBLY_OK', windows=len(hp_outputs),
                  hp_lp_pairs=sum(1 for _f,_h,_c,lp in staged if lp is not None),
                  adaptive_hp_only=sum(1 for _f,_h,_c,lp in staged if lp is None),
                  host_untouched=True,objects=len(created),glass_as_separate_shell=True)
        return hp_outputs
    except Exception:
        for obj, collections in source_collections.items():
            for collection in collections:
                if obj.name not in collection.objects:
                    collection.objects.link(obj)
        for obj in reversed(created):
            bpy.data.objects.remove(obj,do_unlink=True)
        for collection in reversed(created_collections):
            if not collection.objects:
                context.scene.collection.children.unlink(collection)
                if collection.users==0:
                    bpy.data.collections.remove(collection)
        raise
    finally:
        for _frame,hp, _children,lp in staged:
            for mesh in (hp,lp):
                if mesh is not None and mesh.users==0:
                    bpy.data.meshes.remove(mesh)


def assembly_action_ready(context, frames):
    """Cheap, non-mutating visibility gate for the centered Viewport CTA.

    This is NOT geometric certification. execute() repeats the complete
    expensive `_stage_window` preflight before publishing any mesh.
    """
    if getattr(context,'mode',None)!='OBJECT' or not frames:
        return False
    if len({f.as_pointer() for f in frames}) != len(frames):
        return False
    for frame in frames:
        if (frame is None or getattr(frame,'type',None)!='MESH' or
            not getattr(frame,'dwos_window',None) or
            not frame.dwos_window.is_window or frame.dwos_window.finalized or
            frame.get('DWOS_archived_source') or
            frame.get('DWOS_last_validation_error') or
            frame.get('DWOS_smart_interior_pending') or
            not getattr(frame,'data',None) or not len(frame.data.polygons) or
            frame.library or frame.data.library or frame.data.users!=1 or
            frame.data.shape_keys or len(frame.modifiers) or len(frame.constraints) or
            not len(frame.users_collection)):
            return False
    return True


class DWOS_OT_assemble_window_mesh(bpy.types.Operator):
    bl_idname = 'dwos.assemble_window_mesh'
    bl_label = 'Собрать HP + LP (PLANAR)'
    bl_description = ('PLANAR: 1 HP Mesh + 1 плоский LP Mesh в отдельных коллекциях; '
                      'CORNER/CURVED: сохранён прежний HP-only путь; Host без изменений')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        frame = resolve_window(context.active_object)
        return (context.mode == 'OBJECT' and frame is not None
                and not frame.dwos_window.finalized and not frame.get('DWOS_archived_source'))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        frames = target_windows(context)
        try:
            output = assemble_windows(frames, context)
        except Exception as exc:
            log_exception('WINDOW_MESH_ASSEMBLY_FAILED', exc, targets=len(frames))
            self.report({'ERROR'}, f'Сборка отменена: {exc}')
            return {'CANCELLED'}
        self.report({'INFO'}, f'Собрано {len(output)} окон. LP доступен для PLANAR; Host не изменялся.')
        return {'FINISHED'}


class DWOS_OT_set_lod_view(bpy.types.Operator):
    bl_idname='dwos.set_lod_view'
    bl_label='Показать DWOS HP / LP'
    bl_options={'REGISTER','UNDO'}

    mode: bpy.props.EnumProperty(items=[('HP','HP','Полная геометрия'),
                                       ('LP','LP','Плоская геометрия'),
                                       ('BOTH','BOTH','Обе модели'),
                                       ('NONE','HIDE','Скрыть обе модели')],default='HP')

    def execute(self,context):
        try:
            apply_lod_visibility(context.scene,context.view_layer,self.mode)
            context.scene.dwos.lod_view_mode=self.mode
        except Exception as exc:
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


CLASSES = (DWOS_OT_assemble_window_mesh,DWOS_OT_set_lod_view)
