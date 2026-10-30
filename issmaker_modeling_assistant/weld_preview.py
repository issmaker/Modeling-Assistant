"""Weld preflight: non-destructive numbered Blender Viewport markers.

The only operation that mutates a mesh is IMA_OT_WeldCommit. Preview, arrows,
HUD, callbacks and Exit never modify selection or geometry. The explicit Find
command filters selection to candidate vertices (one undoable selection step). The candidate map
is recomputed and compared just before committing. Blender native undo owns the
transaction; old BMesh element references are never cached between events.
"""
import bpy
from .core import viewport_skin as _hud_skin
import bmesh
from math import cos, sin, pi
import time
from bpy.types import Operator
from . import log, runtime

_SESSION = None
_DRAW_HANDLER = None
_DRAW_ERROR = ''
_MAX_PINS = 48
_MAX_SELECTED = 200000
_MAX_CANDIDATES = 25000
_DIST_M = 0.002


def _redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except (AttributeError, ReferenceError, RuntimeError):
        pass


def active():
    return _SESSION is not None


def context_matches(context):
    """Cheap read-only identity check; never query stale BMesh in draw/poll."""
    obj = _context_obj(context)
    try:
        return bool(_SESSION and obj is not None and
                    obj == _SESSION['obj'] and obj.data == _SESSION['mesh'] and
                    tuple(float(v) for row in obj.matrix_world for v in row) == _SESSION['matrix'])
    except (ReferenceError, RuntimeError, AttributeError):
        return False


def state():
    return {'active': active(), 'pairs': len(_SESSION['pairs']) if _SESSION else 0,
            'selected_candidates': len(_SESSION['signature'][3]) if _SESSION else 0,
            'cursor': _SESSION['cursor'] if _SESSION else 0,
            'draw_error': _DRAW_ERROR, 'handler': _DRAW_HANDLER is not None}


def clear(reason='exit'):
    global _SESSION, _DRAW_ERROR
    if _SESSION is not None:
        log.write('Weld preview closed: %s; pairs=%d' % (reason, len(_SESSION['pairs'])))
    _SESSION = None
    _DRAW_ERROR = ''
    _redraw()


def _context_obj(context):
    obj = getattr(context, 'active_object', None)
    if (getattr(context, 'mode', None) != 'EDIT_MESH' or obj is None or
            obj.type != 'MESH' or obj.library is not None or obj.data.library is not None):
        return None
    return obj


def _local_distance(context, obj):
    unit_scale = max(context.scene.unit_settings.scale_length, 1e-12)
    # matrix_world includes parent transforms; obj.scale alone can lie.
    actual_scale = (obj.matrix_world.to_scale()
                    if hasattr(obj.matrix_world, 'to_scale') else obj.scale)
    sx, sy, sz = (abs(float(v)) for v in actual_scale)
    if min(sx, sy, sz) < 1e-10:
        raise RuntimeError('Масштаб объекта содержит нулевую ось')
    if max(sx, sy, sz) / min(sx, sy, sz) > 1.000001:
        raise RuntimeError('Неравномерный Scale: Weld 0,002 м запрещён до Apply Scale, чтобы не сшить неверные точки')
    return _DIST_M / (unit_scale * sx)


def _selection_key(bm):
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    return (len(bm.verts), len(bm.edges), len(bm.faces),
            tuple((v.index, tuple(float(c) for c in v.co))
                  for v in bm.verts if v.select))


def _candidates(context, obj):
    bm = bmesh.from_edit_mesh(obj.data)
    signature = _selection_key(bm)
    selected = [vert for vert in bm.verts if vert.select]
    if len(selected) > _MAX_SELECTED:
        raise ValueError('Слишком много вершин для безопасного предпросмотра Weld')
    if len(selected) < 2:
        raise ValueError('Для Weld выделите минимум две вершины')
    distance = _local_distance(context, obj)
    found = bmesh.ops.find_doubles(bm, verts=selected, keep_verts=[], dist=distance)
    mapped = found.get('targetmap', {})
    pairs = sorted((a.index, b.index) for a,b in mapped.items()
                   if hasattr(a, 'index') and hasattr(b, 'index') and a != b)
    if len(pairs) > _MAX_CANDIDATES:
        raise ValueError('Превышен лимит 25 000 пар; уменьшите область выделения')
    involved = {i for pair in pairs for i in pair}
    coordinates = {v.index: tuple(float(x) for x in (obj.matrix_world @ v.co))
                   for v in selected if v.index in involved}
    return signature, pairs, coordinates


def _valid(context):
    session = _SESSION
    obj = _context_obj(context)
    if not session or not obj or obj != session['obj'] or obj.data != session['mesh']:
        return False, 'Изменился объект или Mesh'
    if tuple(float(v) for row in obj.matrix_world for v in row) != session['matrix']:
        return False, 'Изменилась трансформация объекта'
    try:
        signature, pairs, _ = _candidates(context, obj)
        if signature != session['signature'] or pairs != session['pairs']:
            return False, 'Изменилось выделение или геометрия; повторите предпросмотр'
    except (ValueError, RuntimeError, ReferenceError, AttributeError) as exc:
        return False, str(exc)
    return True, ''


def begin(context, operator):
    global _SESSION
    obj = _context_obj(context)
    if obj is None:
        operator.report({'WARNING'}, 'Weld доступен только для выбранной Edit Mesh')
        return {'CANCELLED'}
    if active():
        clear('new preview')
    old_selected = None
    old_mode = None
    selection_mutated = False
    try:
        signature, pairs, coords = _candidates(context, obj)
        if not pairs:
            operator.report({'INFO'}, 'Weld: совпадающих вершин на расстоянии 0,002 м нет')
            log.write('Weld preview empty: object=%s selected=%d' % (obj.name, len(signature[3])))
            return {'CANCELLED'}
        # Exactly one diagnostic session owns the center navigation dock.
        # Closing old UV results does not restore a different user's selection.
        from . import view_tools
        view_tools.clear_issue_session_for_tool(context, 'Weld preview')
        # The Find button is an explicit selection operation, unlike passive
        # preview/navigation. Keep ONLY candidate vertices, never nearby faces.
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        involved = {index for pair in pairs for index in pair}
        original_count = len(signature[3])
        original_mode = tuple(context.tool_settings.mesh_select_mode)
        old_mode = original_mode
        old_selected = {index for index, _xyz in ((v.index, v.co) for v in bm.verts if v.select)}
        selection_mutated = True
        for f in bm.faces:
            f.select_set(False)
        for e in bm.edges:
            e.select_set(False)
        for v in bm.verts:
            v.select_set(v.index in involved)
        context.tool_settings.mesh_select_mode = (True, False, False)
        bm.select_flush_mode()
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        # The reduced selection is the new authoritative preflight state.
        signature, pairs, coords = _candidates(context, obj)
        if not pairs:
            # The preflight must not publish a session with ambiguous pairs.
            raise RuntimeError('После выделения кандидатов пары Weld не подтвердились; Ctrl+Z вернёт выделение')
        matrix = tuple(float(v) for row in obj.matrix_world for v in row)
        _SESSION = dict(obj=obj, mesh=obj.data, signature=signature,
                        matrix=matrix, pairs=pairs, coords=coords, cursor=0,
                        source_selection_count=original_count, original_mode=original_mode)
        log.write('Weld preview READY: object=%s pairs=%d selected_before=%d selected_after=%d mode=VERT' %
                  (obj.name, len(pairs), original_count, len(signature[3])))
        operator.report({'INFO'}, 'Weld: %d пар; выделено %d вершин-кандидатов' %
                        (len(pairs), len(signature[3])))
        _redraw()
        return {'FINISHED'}
    except Exception as exc:
        # A preflight failure must not leave partially changed selection.
        if selection_mutated and old_selected is not None:
            try:
                live = bmesh.from_edit_mesh(obj.data)
                live.verts.ensure_lookup_table()
                for f in live.faces: f.select_set(False)
                for e in live.edges: e.select_set(False)
                for v in live.verts: v.select_set(v.index in old_selected)
                context.tool_settings.mesh_select_mode = old_mode
                live.select_flush_mode()
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
                log.write('Weld preflight selection rollback: restored=%d' % len(old_selected))
            except Exception as rollback_exc:
                log.write_exception('Weld preflight selection rollback FAILURE', rollback_exc)
                operator.report({'ERROR'}, 'Weld: сбой возврата выделения; Ctrl+Z на копии файла')
        log.write('Weld preview blocked: %s' % exc)
        operator.report({'WARNING'}, str(exc))
        return {'CANCELLED'}


def _focus(context):
    if not _SESSION or context.area is None or context.area.type != 'VIEW_3D':
        return
    try:
        from mathutils import Vector
        a,b = _SESSION['pairs'][_SESSION['cursor']]
        ca,cb = _SESSION['coords'][a], _SESSION['coords'][b]
        center = Vector(tuple((x+y)*0.5 for x,y in zip(ca,cb)))
        rv = context.space_data.region_3d
        rv.view_location = center
        rv.view_distance = min(float(rv.view_distance), 0.75)
        context.area.tag_redraw()
    except (AttributeError, ReferenceError, RuntimeError, ValueError, KeyError):
        pass


class IMA_OT_WeldStep(Operator):
    bl_idname='ima.weld_step'
    bl_label='Перейти к месту сшивки'
    bl_description='Перейти к найденной группе вершин для сшивки без изменения выделения'

    @classmethod
    def description(cls, context, properties):
        from . import tooltip_text
        return tooltip_text.weld_navigation(getattr(properties, 'direction', 1))
    direction: bpy.props.IntProperty(default=1)
    def execute(self, context):
        if not active():
            return {'CANCELLED'}
        valid, reason = _valid(context)
        if not valid:
            clear('stale: '+reason)
            self.report({'WARNING'}, reason)
            return {'CANCELLED'}
        _SESSION['cursor'] = (_SESSION['cursor'] + (1 if self.direction >= 0 else -1)) % len(_SESSION['pairs'])
        _focus(context)
        log.write('Weld navigation: %d/%d' % (_SESSION['cursor']+1, len(_SESSION['pairs'])))
        _redraw()
        return {'FINISHED'}


class IMA_OT_WeldExit(Operator):
    bl_idname='ima.weld_exit'
    bl_label='Выйти из Weld'
    bl_description='Закрыть просмотр Weld без сшивки. Найденные проблемные вершины останутся выделенными.'
    def execute(self, context):
        clear('user exit')
        return {'FINISHED'}


class IMA_OT_WeldCommit(Operator):
    bl_idname='ima.weld_commit'
    bl_label='Сшить показанные'
    bl_description='Сшить проверенные пары вершин (Weld). Изменяет геометрию; доступен Ctrl+Z.'
    bl_options={'REGISTER','UNDO'}
    @classmethod
    def poll(cls, context):
        return active() and _context_obj(context) is not None
    def execute(self, context):
        if not active():
            return {'CANCELLED'}
        ok, reason = _valid(context)
        if not ok:
            clear('stale: '+reason)
            self.report({'WARNING'}, reason)
            return {'CANCELLED'}
        key='weld_commit'
        if not runtime.acquire(key):
            return {'CANCELLED'}
        transaction=None
        mutation_started=False
        try:
            obj=_SESSION['obj']
            transaction=runtime.EditMeshTransaction(context, obj, 'Weld Preview Commit')
            bm=bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.verts.index_update()
            selected=[v for v in bm.verts if v.select]
            targets=bmesh.ops.find_doubles(bm, verts=selected, keep_verts=[],
                                           dist=_local_distance(context,obj)).get('targetmap',{})
            if sorted((a.index,b.index) for a,b in targets.items() if a!=b) != _SESSION['pairs']:
                # No mesh modification has begun. Release the safety copy,
                # never rebuild the mesh for an ordinary preflight rejection.
                transaction.commit()
                transaction=None
                clear('stale before commit')
                self.report({'WARNING'},'Список пар Weld изменился; повторите предпросмотр')
                return {'CANCELLED'}
            count=len(targets)
            mutation_started=True
            bmesh.ops.weld_verts(bm,targetmap=targets)
            bmesh.update_edit_mesh(obj.data,loop_triangles=True,destructive=True)
            transaction.commit()
            clear('committed')
            log.write('Weld COMMIT: object=%s merged=%d' % (obj.name,count))
            self.report({'INFO'},'Сшито вершин: %d' % count)
            return {'FINISHED'}
        except Exception as exc:
            log.write_exception('Weld COMMIT failed', exc)
            if transaction is not None:
                if mutation_started:
                    transaction.rollback()
                else:
                    transaction.commit()  # no destructive operation has started
                if transaction.rollback_error is not None:
                    log.write_exception('Weld rollback FAILURE',transaction.rollback_error)
                    self.report({'ERROR'}, 'АВАРИЯ ОТКАТА. Сохраните файл под новым именем')
            clear('commit failed')
            self.report({'ERROR'},'Weld остановлен: %s' % exc)
            return {'CANCELLED'}
        finally:
            runtime.release(key)


def _draw():
    global _DRAW_ERROR
    sess=_SESSION
    ctx=bpy.context
    if not sess or not ctx.area or ctx.area.type!='VIEW_3D' or not ctx.region or ctx.region.type!='WINDOW':
        return
    if not context_matches(ctx):
        return
    try:
        import gpu, blf
        from gpu_extras.batch import batch_for_shader
        from mathutils import Vector
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        shader=gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        try:
            badge_surface = _hud_skin.component('card').background
            indices=set(range(min(_MAX_PINS,len(sess['pairs']))))
            indices.add(sess['cursor'])
            for idx in sorted(indices):
                a,b=sess['pairs'][idx]
                c1,c2=(Vector(sess['coords'][v]) for v in (a,b))
                mid=(c1+c2)*0.5
                px=location_3d_to_region_2d(ctx.region,ctx.space_data.region_3d,mid)
                if px is None: continue
                x,y=px.x,px.y
                if x<-60 or y<-60 or x>ctx.region.width+60 or y>ctx.region.height+60:continue
                current=idx==sess['cursor']
                pulse=.5+.5*sin(time.monotonic()*3.0+idx*.7)
                size=18 if current else 12
                rgba=(.14,1.,.73,1.) if current else (1.,.65,.19,.77)
                rings=((size+12,.08+.08*pulse),(size+6,.18+.08*pulse),(size,.95)) if current else ((size,.45+.20*pulse),)
                for rad,alpha in rings:
                    count=32
                    seg=[(x+rad*cos(2*pi*j/count),y+rad*sin(2*pi*j/count))
                         for j in range(count+1)]
                    shader.bind();shader.uniform_float('color',(*rgba[:3],alpha))
                    batch_for_shader(shader,'LINE_STRIP',{'pos':seg}).draw(shader)
                # High-contrast dark badge rather than a number on busy cyan wireframes.
                badge=[(x-10,y-10),(x+10,y-10),(x+10,y+10),(x-10,y+10)]
                shader.bind();shader.uniform_float('color',(*badge_surface[:3],.94))
                batch_for_shader(shader,'TRIS',{'pos':[badge[0],badge[1],badge[2],badge[0],badge[2],badge[3]]}).draw(shader)
                if current:
                    # Pair witnesses: only show distinct A/B endpoints when
                    # their screen separation is meaningful, never fake spacing.
                    p1 = location_3d_to_region_2d(ctx.region,ctx.space_data.region_3d,c1)
                    p2 = location_3d_to_region_2d(ctx.region,ctx.space_data.region_3d,c2)
                    if p1 is not None and p2 is not None and (p1-p2).length>7:
                        shader.bind();shader.uniform_float('color',(*rgba[:3],.85))
                        batch_for_shader(shader,'LINES',{'pos':[tuple(p1),tuple(p2)]}).draw(shader)
                    shader.bind();shader.uniform_float('color',(*rgba[:3],.84))
                    batch_for_shader(shader,'LINES',{'pos':[(x+size,y),(x+size+15,y+17)]}).draw(shader)
                blf.size(0,_hud_skin.text_px('body' if current else 'caption'))
                blf.color(0,*rgba[:3],1)
                blf.position(0,x-(7 if idx+1>=10 else 4),y-4,0)
                blf.draw(0,str(idx+1))
        finally:
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
    except Exception as exc:
        _DRAW_ERROR='%s: %s' % (type(exc).__name__,str(exc)[:160])
        log.write('Weld marker GPU fault: '+_DRAW_ERROR)
        # Fail closed on repeated draw failures; no Blender mesh mutation.
        clear('draw error')


def _undo_pre(_unused):
    clear('undo')


def _redo_pre(_unused):
    clear('redo')


def _load_pre(_unused):
    clear('load')


CLASSES=(IMA_OT_WeldStep,IMA_OT_WeldExit,IMA_OT_WeldCommit)

def register():
    global _DRAW_HANDLER
    runtime.register_classes_atomic(CLASSES)
    if not bpy.app.background:
        try:
            _DRAW_HANDLER=bpy.types.SpaceView3D.draw_handler_add(_draw,(), 'WINDOW','POST_PIXEL')
        except Exception as exc:
            log.write('Weld marker handler unavailable: %s' % exc)
    from bpy.app.handlers import persistent
    for coll,fun in ((bpy.app.handlers.undo_pre,_undo_pre),
                     (bpy.app.handlers.redo_pre,_redo_pre),
                     (bpy.app.handlers.load_pre,_load_pre)):
        if fun not in coll: coll.append(persistent(fun))


def unregister():
    global _DRAW_HANDLER
    clear('unregister')
    if _DRAW_HANDLER is not None:
        try:bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLER,'WINDOW')
        except (ReferenceError,RuntimeError,ValueError):pass
        _DRAW_HANDLER=None
    for coll,fun in ((bpy.app.handlers.undo_pre,_undo_pre),
                     (bpy.app.handlers.redo_pre,_redo_pre),
                     (bpy.app.handlers.load_pre,_load_pre)):
        if fun in coll:coll.remove(fun)
    for cls in reversed(CLASSES):
        try:bpy.utils.unregister_class(cls)
        except (RuntimeError,ValueError):pass
