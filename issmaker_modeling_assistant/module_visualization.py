"""Read-only UV overlay and explicit seam marking on any active Mesh.

Large scenes are audited in O(edges) auxiliary memory, and GPU drawing is
budgeted. Seam marking changes only mesh.edges[].use_seam with Undo.
"""
import bpy
from bpy.types import Operator
from .core.module_visual_core import classify_indexed_mesh_edges, hard_edge
from .core.uv_orientation_audit import audit_uv_islands, mirrored_uv_for_island
from . import log

_GPU_LAST_ERROR = ''
_GEOMETRY_UI_BUILD_ID = 'ISSMAKER_BH0470_LARGE_MESH_SEAMS_20261026'

_HANDLER = None
_SNAPSHOT = None
_MAX_EDGES = 12000
_GPU_ERROR_ONCE = False


def _require_unique_mesh(obj):
    if obj.data.users != 1 or obj.data.library is not None or obj.library is not None:
        raise ValueError('Mesh используется несколькими объектами или связан с библиотекой; операция отменена')


def _mesh_from_context(context):
    obj = getattr(context, 'active_object', None)
    if (context.mode != 'OBJECT' or obj is None or obj.type != 'MESH'
            or obj.get('uv_wall_mode') not in {'WALL', 'SURFACE', 'CORNICE'}):
        return None
    return obj


def _redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except (AttributeError, ReferenceError, RuntimeError):
        pass


def _matrix_signature(matrix):
    return tuple(round(float(n), 9) for row in matrix for n in row)


def _analyse(obj):
    mesh = obj.data
    layer = mesh.uv_layers.active
    if layer is None:
        raise ValueError('Нет активной UV-карты: нечего проверять')
    result = classify_indexed_mesh_edges(mesh.polygons, mesh.loops, layer.data, len(mesh.edges))
    result['marked'] = tuple(edge.index for edge in mesh.edges if edge.use_seam)
    return result


def status(context=None):
    snap = _SNAPSHOT
    if not snap:
        return {'active': False, 'state': 'OFF', 'gpu_error': _GPU_LAST_ERROR,
                'geometry_ui_build_id': _GEOMETRY_UI_BUILD_ID}
    try:
        obj = snap['object']
        valid = (obj is not None and obj.as_pointer() == snap['object_ptr']
                 and obj.data.as_pointer() == snap['mesh_ptr']
                 and _matrix_signature(obj.matrix_world) == snap['matrix'])
        if context is not None:
            valid = valid and getattr(context, 'active_object', None) == obj and context.mode == 'OBJECT'
        return {'active': valid, 'state': 'READY' if valid else 'STALE',
                'counts': snap['counts'], 'gpu_error': _GPU_LAST_ERROR,
                'geometry_ui_build_id': _GEOMETRY_UI_BUILD_ID,
                'displayed_edges': snap['displayed_edges'],
                'omitted_edges': snap['omitted_edges']}
    except (ReferenceError, AttributeError, RuntimeError):
        return {'active': False, 'state': 'STALE', 'gpu_error': _GPU_LAST_ERROR,
                'geometry_ui_build_id': _GEOMETRY_UI_BUILD_ID}


def clear(redraw=True):
    global _HANDLER, _SNAPSHOT
    _SNAPSHOT = None
    if _HANDLER is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_HANDLER, 'WINDOW')
        except (RuntimeError, ReferenceError, ValueError):
            pass
        _HANDLER = None
    if redraw:
        _redraw()


def _visible_edges(flags, mesh, budget=_MAX_EDGES):
    """Preserve UV errors first; mark/open lines never starve UV visualization."""
    used = bytearray(len(mesh.edges))
    selected = {'uv': [], 'marked': [], 'open': []}
    unique_count = 0
    displayed = 0
    for group, keys in (('uv',('uv_seams',)),
                        ('marked',('marked',)),
                        ('open',('open_edges','nonmanifold'))):
        for key in keys:
            for index in flags[key]:
                if used[index]:
                    continue
                used[index] = 1
                unique_count += 1
                if displayed < budget:
                    selected[group].append(index)
                    displayed += 1
    return selected, unique_count


def capture(context):
    global _SNAPSHOT, _HANDLER, _GPU_ERROR_ONCE, _GPU_LAST_ERROR
    _GPU_ERROR_ONCE = False
    _GPU_LAST_ERROR = ''
    obj = _any_active_mesh(context)
    if obj is None:
        raise ValueError('Выберите Mesh в Object Mode')
    flags = _analyse(obj)
    mesh = obj.data
    categories, total_unique = _visible_edges(flags, mesh)
    coords = {}
    displayed = 0
    for group, indices in categories.items():
        pts = []
        for idx in indices:
            edge = mesh.edges[idx]
            for vid in edge.vertices:
                pts.append(tuple(float(v) for v in mesh.vertices[vid].co))
        coords[group] = tuple(pts)
        displayed += len(indices)
    counts = {key: len(value) if isinstance(value, tuple) else value
              for key,value in flags.items()}
    counts['displayed_edges'] = displayed
    counts['omitted_edges'] = max(0, total_unique-displayed)
    snapshot = {
        'object': obj, 'object_ptr': obj.as_pointer(), 'mesh_ptr': mesh.as_pointer(),
        'matrix': _matrix_signature(obj.matrix_world),
        'coords': coords, 'counts': counts,
        'displayed_edges': displayed, 'omitted_edges': counts['omitted_edges'],
    }
    clear()
    _SNAPSHOT = snapshot
    try:
        if not bpy.app.background:
            _HANDLER = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
    except Exception:
        clear()
        raise
    _redraw()
    return counts


def _draw():
    global _GPU_ERROR_ONCE, _GPU_LAST_ERROR
    snap = _SNAPSHOT
    if snap is None or status(bpy.context)['state'] != 'READY':
        return
    context = bpy.context
    if not getattr(context, 'area', None) or context.area.type != 'VIEW_3D':
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        tint = tuple(float(v) for v in context.scene.ima_uv_module_tint)
        colors = {'uv': (*tint, .95), 'marked': (1., .16, .24, .98),
                  'open': (1., .2, .25, .95)}
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.line_width_set(2.0)
        gpu.matrix.push()
        try:
            gpu.matrix.multiply_matrix(snap['object'].matrix_world)
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            for kind in ('uv', 'marked', 'open'):
                coords = snap['coords'][kind]
                if not coords:
                    continue
                shader.bind()
                shader.uniform_float('color', colors[kind])
                batch_for_shader(shader, 'LINES', {'pos': coords}).draw(shader)
        finally:
            gpu.matrix.pop()
    except Exception as exc:
        if not _GPU_ERROR_ONCE:
            _GPU_ERROR_ONCE = True
            _GPU_LAST_ERROR = repr(exc)[:250]
            log.write('Module visualization GPU unavailable: '+_GPU_LAST_ERROR)
    finally:
        try:
            gpu.state.line_width_set(1.0)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
        except (NameError, RuntimeError, AttributeError):
            pass


class IMA_OT_ModuleOverlay(Operator):
    bl_idname = 'ima.module_overlay'
    bl_label = 'Показать UV-границы на Mesh'
    bl_description = 'Read-only: UV-разрывы, ручные швы и открытые рёбра на любом Mesh с UV; с лимитом GPU-линий'
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        if status(context)['active']:
            clear()
            return {'FINISHED'}
        try:
            counts = capture(context)
        except (ReferenceError, ValueError, RuntimeError, AttributeError, IndexError) as exc:
            self.report({'WARNING'}, str(exc)[:220])
            return {'CANCELLED'}
        self.report({'INFO'}, f"UV-разрывов {counts['uv_seams']}, открытых {counts['open_edges']}, отмеченных {counts['marked']}; показано {counts['displayed_edges']}, скрыто по GPU-бюджету {counts['omitted_edges']}")
        return {'FINISHED'}


class IMA_OT_ModuleMarkSeams(Operator):
    bl_idname = 'ima.module_mark_uv_seams'
    bl_label = 'Пометить UV-границы как Seam'
    bl_description = 'С Undo: помечает UV-разрывы, материальные границы и свободный контур активного Mesh как Blender Seam, без изменения геометрии и UV'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _any_active_mesh(context)
        return obj is not None and obj.data.uv_layers.active is not None

    def execute(self, context):
        obj = _any_active_mesh(context)
        if obj is None:
            self.report({'WARNING'}, 'Выберите Mesh в Object Mode')
            return {'CANCELLED'}
        try:
            flags = _analyse(obj)
        except (ValueError, ReferenceError, RuntimeError, IndexError) as exc:
            self.report({'ERROR'}, str(exc)[:220]); return {'CANCELLED'}
        try:
            _require_unique_mesh(obj)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc)); return {'CANCELLED'}
        changed = []
        try:
            # Byte mask instead of a potentially huge Python set on production Meshes.
            target = bytearray(len(obj.data.edges))
            for group in ('uv_seams', 'material_edges', 'open_edges'):
                for idx in flags[group]:
                    target[idx] = 1
            for idx, requested in enumerate(target):
                if requested:
                    edge = obj.data.edges[idx]
                    if not edge.use_seam:
                        edge.use_seam = True
                        changed.append(idx)
            obj.data.update()
        except Exception as exc:
            for idx in changed:
                try: obj.data.edges[idx].use_seam = False
                except Exception: pass
            self.report({'ERROR'}, 'Seam не применён: '+str(exc)[:170])
            return {'CANCELLED'}
        clear()  # Snapshot invalid after any mesh annotation change.
        self.report({'INFO'}, f'Отмечено UV-швов: {len(changed)}. Undo доступен.')
        return {'FINISHED'}


class IMA_OT_ModuleShading(Operator):
    bl_idname = 'ima.wall_safe_shading'
    bl_label = 'Сглаживание UV Wall'
    bl_description = 'С Undo: меняет только shading/Sharp Flags; UV и вершины не изменяются'
    bl_options = {'REGISTER', 'UNDO'}
    mode: bpy.props.EnumProperty(items=(('AUTO','По углу','Сохранять конструктивные жёсткие рёбра'),
                                       ('FLAT','Плоское','Не сглаживать плоскости')),default='AUTO')

    @classmethod
    def poll(cls, context):
        return _mesh_from_context(context) is not None

    def execute(self, context):
        obj = _mesh_from_context(context)
        if obj is None: return {'CANCELLED'}
        try:
            _require_unique_mesh(obj)
        except ValueError as exc:
            self.report({'WARNING'}, str(exc)); return {'CANCELLED'}
        mesh = obj.data
        original_smooth = [p.use_smooth for p in mesh.polygons]
        attr = mesh.attributes.get('sharp_edge')
        had_attr = attr is not None
        old_sharp = [x.value for x in attr.data] if had_attr else None
        try:
            if self.mode == 'AUTO':
                threshold = float(context.scene.ima_uv_smooth_angle)
                if not 1.0 <= threshold <= 179.0:
                    raise ValueError('Неверный порог сглаживания')
                face_links = {edge.index: [] for edge in mesh.edges}
                for face in mesh.polygons:
                    normal = tuple(float(x) for x in face.normal)
                    for i in range(face.loop_start, face.loop_start+face.loop_total):
                        face_links[mesh.loops[i].edge_index].append(normal)
                to_mark = tuple(idx for idx, normals in face_links.items()
                                if len(normals) != 2 or hard_edge(normals[0], normals[1], threshold))
                if to_mark and attr is None:
                    attr = mesh.attributes.new(name='sharp_edge', type='BOOLEAN', domain='EDGE')
                for idx in to_mark:
                    attr.data[idx].value = True
                for face in mesh.polygons: face.use_smooth = True
            else:
                for face in mesh.polygons: face.use_smooth = False
            mesh.update()
        except Exception as exc:
            for i, old in enumerate(original_smooth):
                try: mesh.polygons[i].use_smooth = old
                except Exception: pass
            if not had_attr:
                try:
                    current = mesh.attributes.get('sharp_edge')
                    if current is not None: mesh.attributes.remove(current)
                except Exception: pass
            elif attr is not None:
                for i, old in enumerate(old_sharp):
                    try: attr.data[i].value = old
                    except Exception: pass
            self.report({'ERROR'}, 'Сглаживание отменено: '+str(exc)[:180])
            return {'CANCELLED'}
        clear()
        self.report({'INFO'}, 'Режим сглаживания применён (с Undo); геометрия и UV не менялись')
        return {'FINISHED'}


def _any_active_mesh(context):
    obj = getattr(context, 'active_object', None)
    return obj if context.mode == 'OBJECT' and obj is not None and obj.type == 'MESH' else None


def uv_quality_summary(context, *, detailed=False):
    """Read-only: available for ANY active Mesh, including DWOS and Brick Hole."""
    obj = _any_active_mesh(context)
    if obj is None:
        raise ValueError('Проверка UV доступна для выбранного Mesh в Object Mode')
    mesh = obj.data
    if len(mesh.polygons) > 100000 or len(mesh.loops) > 500000:
        raise ValueError('Большой Mesh: используйте Support ZIP / отдельную проверку')
    layer = mesh.uv_layers.active
    if layer is None:
        raise ValueError('У объекта отсутствует активная UV-карта')
    data = layer.data
    mesh.calc_loop_triangles()
    render_uv={}
    for tri in mesh.loop_triangles:
        render_uv.setdefault(tri.polygon_index,[]).append(tuple(tuple(data[i].uv) for i in tri.loops))
    faces = []
    for poly in mesh.polygons:
        loops = range(poly.loop_start, poly.loop_start+poly.loop_total)
        faces.append({'vertices':tuple(mesh.loops[i].vertex_index for i in loops),
                      'uv':tuple(tuple(data[i].uv) for i in loops),
                      'triangles':render_uv.get(poly.index,())})
    summary = audit_uv_islands(faces)
    # One common read-only oracle for UV Wall, DWOS, Brick Hole and any Mesh.
    # UV winding is not a face-normal verdict or a material defect verdict.
    from .core.surface_orientation import face_orientation, mirrored_transform
    matrix3 = obj.matrix_world.to_3x3()
    mirror = mirrored_transform(tuple(tuple(matrix3[row][col] for col in range(3))
                                      for row in range(3)))
    counts = {'virtual_twisted_faces': 0, 'virtual_uv_fold_faces': 0,
              'virtual_uv_negative_faces': 0, 'surface_degenerate_faces': 0,
              'surface_unverified_ngons': 0}
    # UI requests this summary on every redraw: no expensive world-space
    # triangle evaluation until explicit report/support diagnostics.
    if detailed:
        for poly, uv_face in zip(mesh.polygons, faces):
            points = tuple(tuple(obj.matrix_world @ mesh.vertices[i].co)
                           for i in uv_face['vertices'])
            diag = face_orientation(points, uv_face['uv'])
            if diag['normal'] in {'NONPLANAR_OR_FOLDED', 'DEGENERATE_OR_FOLDED'}:
                counts['virtual_twisted_faces'] += 1
            if diag['uv'] == 'FOLDED_OR_DEGENERATE':
                counts['virtual_uv_fold_faces'] += 1
            if diag['uv'] == 'NEGATIVE' and diag['confidence'] == 'VIRTUAL_ONLY':
                counts['virtual_uv_negative_faces'] += 1
            if diag['normal'] == 'DEGENERATE_OR_FOLDED':
                counts['surface_degenerate_faces'] += 1
            if diag['confidence'] == 'NOT_TRIANGULATED' and len(points) > 4:
                counts['surface_unverified_ngons'] += 1
    summary.update(counts)
    summary['object_transform_orientation'] = mirror
    summary['surface_audit_status'] = 'ADVISORY_VIRTUAL' if detailed else 'NOT_RUN'
    summary['surface_orientation_verified'] = False  # no role-aware Blender oracle
    return obj, layer, summary


def uv_quality_diagnostics(context):
    try:
        # Support ZIP is a frequent user workflow and must not freeze a large
        # scene on the quadratic legacy UV-island bounding-box overlap pass.
        # Explicit UV Quality Report retains the untruncated complete audit.
        obj = _any_active_mesh(context)
        if obj is not None and len(obj.data.polygons) > 2000:
            return {'available': False, 'status': 'BLOCKED_LARGE_MESH',
                    'faces': len(obj.data.polygons),
                    'reason': 'Тяжёлый UV-аудит пропущен в Support ZIP; запустите полный аудит UV вручную'}
        obj, layer, summary = uv_quality_summary(context, detailed=True)
        return {'object':obj.name,'uv_layer':layer.name,
                **{k:v for k,v in summary.items() if k!='islands_detail'}}
    except (ValueError, RuntimeError, ReferenceError, AttributeError, IndexError) as exc:
        return {'available':False,'reason':str(exc)[:240]}


def role_surface_diagnostics(context, *, mode='FAST'):
    """Bounded support evidence; never claim FULL from a partial sample."""
    try:
        from .core.role_surface_blender import inspect_object
        obj=_any_active_mesh(context)
        if obj is None:
            raise ValueError('Нужен Mesh в Object Mode')
        return inspect_object(obj,mode=mode)
    except (ValueError, RuntimeError, ReferenceError, AttributeError, IndexError) as exc:
        return {'available':False,'status':'BLOCKED','reason':str(exc)[:240]}


class IMA_OT_RoleSurfaceReport(Operator):
    bl_idname = 'ima.role_surface_report'
    bl_label = 'Нормали по ролям: полный аудит'
    bl_description = ('Реальные Blender loop-треугольники, UV и роли стен/рам/стёкол; '
                      'без изменений Mesh, только по команде')
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return _any_active_mesh(context) is not None

    def execute(self, context):
        from .core.role_surface_blender import inspect_object
        try:
            result=inspect_object(_any_active_mesh(context),mode='FULL')
        except (ValueError,RuntimeError,ReferenceError,AttributeError,IndexError) as exc:
            self.report({'WARNING'},'Аудит ролей не проведён: '+str(exc)[:180])
            return {'CANCELLED'}
        log.write('ISSMAKER_ROLE_SURFACE_AUDIT '+str(result))
        msg=(f"Треугольников {result['scanned_triangles']}/{result['total_triangles']}; "
             f"роль-конфликтов {result['role_conflicts']}; "
             f"геометрия/UV degenerate {result['geometry_degenerate']}/{result['uv_degenerate']}; "
             f"отражённые UV {result['uv_negative']}; "
             f"непроверенные роли {result['unanchored_triangles']}; "
             f"статус {result['status']} (исходный Mesh, не render)")
        self.report({'WARNING'} if result['status'] in {'ISSUES','PARTIAL_ROLES'}
                    else {'INFO'},msg[:255])
        return {'FINISHED'}


class IMA_OT_UVQualityReport(Operator):
    bl_idname = 'ima.uv_quality_report'
    bl_label = 'Проверить ориентацию UV'
    bl_description = 'Без изменения Mesh: отчёт о зеркальных, смешанных UV-островах и вырожденных UV'
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        try:
            obj, layer, summary = uv_quality_summary(context, detailed=True)
            result=(f"{obj.name} [{layer.name}]: островов {summary['islands']}, "
                    f"зеркальных {summary['mirrored_islands']}, смешанных {summary['mixed_islands']}, "
                    f"вырожденных граней {summary['degenerate_faces']}, "
                    f"неоднозначных UV-граней {summary['unverified_faces']}, "
                    f"quad UV folds {summary['virtual_uv_fold_faces']}, "
                    f"twisted faces {summary['virtual_twisted_faces']}, "
                    f"object mirror {summary['object_transform_orientation']}, "
                    f"проверенных кандидатов исправления {summary['repairable_islands']}; "
                    f"роль-зависимые нормали NOT_VERIFIED")
            log.write('UV_QUALITY_AUDIT '+result)
            self.report({'WARNING'} if summary['unverified_faces'] or
                        summary['virtual_twisted_faces'] or summary['virtual_uv_fold_faces']
                        else {'INFO'}, result[:255]); return {'FINISHED'}
        except (ValueError, RuntimeError, ReferenceError, AttributeError, IndexError) as exc:
            self.report({'WARNING'}, str(exc)[:240]); return {'CANCELLED'}


class IMA_OT_UVFixMirroredIslands(Operator):
    bl_idname = 'ima.uv_fix_mirrored_islands'
    bl_label = 'Исправить однозначные зеркальные UV-острова'
    bl_description = ('Только по нажатию: отражает U отрицательно ориентированных '
                      'островов в пределах одного UDIM. Не исправляет смешанные/перекрывающиеся острова')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            obj, layer, summary = uv_quality_summary(context)
            _require_unique_mesh(obj)
        except (ValueError, RuntimeError, ReferenceError, AttributeError, IndexError) as exc:
            self.report({'WARNING'}, str(exc)[:240]); return {'CANCELLED'}
        if summary['repairable_islands'] == 0:
            self.report({'WARNING'}, 'Нет однозначных островов для исправления; проверьте отчёт')
            return {'CANCELLED'}
        # Complete transaction: plan and validate UV before writing a single loop.
        mesh=obj.data
        original=[tuple(item.uv) for item in layer.data]
        proposed=list(original)
        modified=set()
        for island in summary['islands_detail']:
            if not island['repairable']: continue
            umin,_,umax,_=island['bounds']
            for fi in island['faces']:
                poly=mesh.polygons[fi]
                loop_ids=range(poly.loop_start,poly.loop_start+poly.loop_total)
                changed=mirrored_uv_for_island([original[i] for i in loop_ids],umin,umax)
                for idx,point in zip(loop_ids,changed):
                    if idx in modified: raise ValueError('UV-loop попал в два острова; отменено')
                    proposed[idx]=point; modified.add(idx)
        # Do not publish a partial fix, a negative winding, or a UV that escaped a tile.
        from .core.uv_flip_detection import classify_uv_polygon
        for island in summary['islands_detail']:
            if not island['repairable']: continue
            for fi in island['faces']:
                face=mesh.polygons[fi]
                loop_ids=tuple(face.loop_indices)
                triangles=[[proposed[i] for i in tri.loops] for tri in mesh.loop_triangles
                           if tri.polygon_index==fi]
                if classify_uv_polygon([proposed[i] for i in loop_ids],triangles=triangles)!='NORMAL':
                    self.report({'ERROR'},'UV-проверка после отражения не пройдена')
                    return {'CANCELLED'}
        try:
            for idx in sorted(modified): layer.data[idx].uv=proposed[idx]
            mesh.update()
        except Exception as exc:
            for idx in modified:
                try: layer.data[idx].uv=original[idx]
                except Exception: pass
            mesh.update()
            self.report({'ERROR'},'Изменения UV отменены: '+str(exc)[:170]); return {'CANCELLED'}
        clear()
        changed=summary['repairable_islands']
        log.write(f'UV_QUALITY_REPAIR object={obj.name!r} islands={changed} loops={len(modified)}')
        self.report({'INFO'}, f'Исправлено UV-островов: {changed}. Undo доступен.')
        return {'FINISHED'}


class IMA_OT_WallNativeAngleSmooth(Operator):
    bl_idname = 'ima.wall_native_angle_smooth'
    bl_label = 'Smooth by Angle Modifier (Blender)'
    bl_description = 'Добавить штатный модификатор Smooth by Angle с сохранением Sharp; виден в Object/Edit Mode'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _mesh_from_context(context) is not None

    def execute(self, context):
        obj = _mesh_from_context(context)
        if obj is None: return {'CANCELLED'}
        try:
            _require_unique_mesh(obj)
            if len(context.selected_objects) != 1 or context.selected_objects[0] != obj:
                raise ValueError('Для модификатора выберите только одну готовую стену')
            angle=float(context.scene.ima_uv_smooth_angle)
            if not 1. <= angle <= 179.: raise ValueError('Некорректный угол')
            from math import radians
            old_mods=tuple(mod.as_pointer() for mod in obj.modifiers)
            existing=[m for m in obj.modifiers if m.type=='NODES' and m.node_group
                      and 'smooth by angle' in m.node_group.name.lower()]
            if existing:
                # Do not pile modifiers on top of one another; preserve its
                # existing angle and sharpness policy, expose it in Edit Mode.
                for mod in existing: mod.show_in_editmode=True
                self.report({'INFO'},'Smooth by Angle уже есть; включено отображение в Edit Mode')
                return {'FINISHED'}
            op=getattr(bpy.ops.object,'shade_auto_smooth',None)
            if op is None or not op.poll():
                raise RuntimeError('Штатный оператор Smooth by Angle недоступен в Blender')
            old_smooth=[p.use_smooth for p in obj.data.polygons]
            old_sharp_attr=obj.data.attributes.get('sharp_edge')
            old_sharp=([bool(x.value) for x in old_sharp_attr.data]
                       if old_sharp_attr is not None else None)
            result=op(use_auto_smooth=True,angle=radians(angle))
            if 'FINISHED' not in result: raise RuntimeError('Blender не создал Smooth by Angle')
            added=[mod for mod in obj.modifiers if mod.as_pointer() not in old_mods]
            if not added:
                raise RuntimeError('Модификатор не появился; проверьте контекст Blender')
            for mod in added:
                mod.show_viewport=True
                mod.show_in_editmode=True
            clear()
            log.write(f'ISSMAKER_SMOOTH_MODIFIER object={obj.name!r} angle={angle} edit_visible=1')
            self.report({'INFO'},'Smooth by Angle добавлен; Sharp сохраняются, в Edit Mode виден')
            return {'FINISHED'}
        except (ValueError, AttributeError, RuntimeError, TypeError) as exc:
            # Native operators can partially create a modifier before failing.
            # Remove ONLY new modifiers; existing ones are never removed.
            if 'old_mods' in locals():
                for mod in tuple(obj.modifiers):
                    try:
                        if mod.as_pointer() not in old_mods: obj.modifiers.remove(mod)
                    except (RuntimeError, ReferenceError, AttributeError): pass
            if 'old_smooth' in locals():
                try:
                    for face,value in zip(obj.data.polygons,old_smooth): face.use_smooth=value
                    attr=obj.data.attributes.get('sharp_edge')
                    if old_sharp is None and attr is not None: obj.data.attributes.remove(attr)
                    elif attr is not None:
                        for entry,value in zip(attr.data,old_sharp): entry.value=value
                    obj.data.update()
                except (RuntimeError,ReferenceError,AttributeError):
                    log.write('ISSMAKER_SMOOTH_MODIFIER rollback incomplete: restore via Undo')
            self.report({'WARNING'}, 'Smooth by Angle не применён: '+str(exc)[:180])
            return {'CANCELLED'}


class IMA_OT_ModuleCoverageReport(Operator):
    """Explicit Object Mode / read-only tileability assessment for any Mesh."""
    bl_idname = 'ima.uv_module_coverage_report'
    bl_label = 'Проверить вместимость UV-модулей'
    bl_description = 'Показать грани, которым нужна UV-разбивка 4м/2м; Mesh не изменяется'
    bl_options = {'REGISTER', 'INTERNAL'}
    resolution: bpy.props.EnumProperty(
        name='Модуль',items=(('4096','4×4 м / 4K',''),('2048','2×2 м / 2K','')),
        default='4096')

    @classmethod
    def poll(cls, context):
        ob=getattr(context,'active_object',None)
        return context.mode=='OBJECT' and ob is not None and ob.type=='MESH'

    def execute(self, context):
        obj=getattr(context,'active_object',None)
        if obj is None or obj.type!='MESH' or context.mode!='OBJECT':
            return {'CANCELLED'}
        from .core.uv_module_splitting import module_coverage_report
        try:
            mesh=obj.data
            verts=[tuple(float(v) for v in vertex.co) for vertex in mesh.vertices]
            faces=[tuple(int(i) for i in poly.vertices) for poly in mesh.polygons]
            result=module_coverage_report(verts,faces,resolution=int(self.resolution),max_faces=10000)
            scale=tuple(float(n) for n in obj.scale)
        except (ValueError, RuntimeError, ReferenceError, IndexError, TypeError, AttributeError) as exc:
            self.report({'ERROR'},'UV-аудит не выполнен: '+str(exc)[:170])
            return {'CANCELLED'}
        msg=(f"UV module audit: {obj.name!r} {self.resolution} "
             f"faces={result['faces']} eligible={result['eligible']} "
             f"oversized={result['oversized']} unsafe={result['unsafe']} "
             f"status={result['state']} examples={result['examples']} scale={scale}")
        log.write(msg)
        res_k = int(self.resolution)//1024
        def draw_menu(menu, _context):
            col=menu.layout.column(align=True)
            col.label(text=f"{res_k}K · 1000 px/м · {result['state']}")
            col.label(text=f"Граней: {result['faces']} · влезают: {result['eligible']}")
            col.label(text=f"Нуждаются в разбиении: {result['oversized']}")
            col.label(text=f"Небезопасные/неплоские: {result['unsafe']}")
            if result['examples']:
                col.label(text='Примеры (индекс грани: причина):')
                for fi, reason in result['examples'][:6]:
                    col.label(text=f"{fi}: {reason}")
            if any(abs(s-1)>1e-7 for s in scale):
                col.label(text='Внимание: масштаб объекта не равен 1!',icon='ERROR')
            col.label(text='Только диагностика · геометрия и UV не изменены')
        context.window_manager.popup_menu(draw_menu,title='ISSMAKER · UV-модули',icon='INFO')
        self.report({'INFO'},f"UV 4K/2K: крупных {result['oversized']}, небезопасных {result['unsafe']}")
        return {'FINISHED'}


CLASSES = (IMA_OT_ModuleOverlay, IMA_OT_ModuleMarkSeams, IMA_OT_ModuleShading,
           IMA_OT_UVQualityReport, IMA_OT_RoleSurfaceReport, IMA_OT_UVFixMirroredIslands,
           IMA_OT_WallNativeAngleSmooth, IMA_OT_ModuleCoverageReport)


def _invalidate_before_undo(_dummy=None):
    # Must release cached Mesh/Python references before Blender restores the data.
    clear(redraw=False)


def _install_handlers():
    for name in ('undo_pre', 'redo_pre', 'load_pre'):
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None and _invalidate_before_undo not in handlers:
            handlers.append(_invalidate_before_undo)


def _remove_handlers():
    for name in ('undo_pre', 'redo_pre', 'load_pre'):
        handlers = getattr(bpy.app.handlers, name, None)
        if handlers is not None:
            try: handlers.remove(_invalidate_before_undo)
            except ValueError: pass



def register():
    from . import runtime
    runtime.register_classes_atomic(CLASSES)
    try:
        bpy.types.Scene.ima_uv_module_tint = bpy.props.FloatVectorProperty(
            name='Цвет границ', description='Цвет линий UV-разрывов в 3D Preview',
            subtype='COLOR', size=3, default=(.42,.9,.73), min=0., max=1., update=lambda s,c: _redraw())
        bpy.types.Scene.ima_uv_smooth_angle = bpy.props.FloatProperty(
            name='Sharp порог', default=35.0, min=1.0, max=179.0, 
            description='Порог угла в градусах для сохранения жёстких рёбер')
        _install_handlers()
    except Exception:
        unregister()
        raise


def unregister():
    _remove_handlers()
    clear()
    for prop in ('ima_uv_smooth_angle','ima_uv_module_tint'):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    from . import runtime
    runtime.unregister_classes_safe(CLASSES)
