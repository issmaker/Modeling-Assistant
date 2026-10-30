"""Non-destructive UV issue outlines for the Image Editor.

All mesh access happens on the main thread at explicit publish or a slow timer.
The GPU draw callback only reads immutable UV points, Blender UI state and prefs;
it NEVER mutates selection, geometry, active tools or saved .blend data.
"""

import bpy
import bmesh

from . import log, uv_feedback_math, selection_core, viewport_feedback

_HANDLER = None
_SNAPSHOT = None
_STATUS = 'EMPTY'
_FOCUSED_FACE = None
_FOCUSED_OUTLINE = None
_FACES_LIMIT = 400
_SEGMENTS_LIMIT = 4800


def _redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'IMAGE_EDITOR':
                    area.tag_redraw()
    except (ReferenceError, RuntimeError, AttributeError):
        pass


def _face_record(face, uv_layer):
    """Immutable UV + 3D state, including loop order, for stale-preview checks."""
    return tuple((int(loop.vert.index),
                  tuple(float(c) for c in loop.vert.co),
                  tuple(float(c) for c in loop[uv_layer].uv))
                 for loop in face.loops)


def _same_context(context, snapshot):
    try:
        obj = context.active_object
        if (context.mode != 'EDIT_MESH' or obj is None or obj != snapshot['object']
                or obj.type != 'MESH' or obj.data != snapshot['mesh']):
            return False
        return selection_core.uv_layer_matches(obj.data, snapshot['uv_key'])
    except (ReferenceError, AttributeError, RuntimeError):
        return False


def publish(context, obj, kind, face_indices):
    """Capture the *found* UV faces after analysis, without touching selection."""
    global _SNAPSHOT, _STATUS
    clear()
    if (kind not in {'FLIPPED', 'STRETCH'} or context.active_object != obj
            or context.mode != 'EDIT_MESH'):
        return False
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.index_update()
        bm.faces.ensure_lookup_table()
        layer = bm.loops.layers.uv.active
        active_uv = obj.data.uv_layers.active
        if layer is None or active_uv is None:
            return False
        unique = sorted(set(int(index) for index in face_indices))
        if not unique or any(index < 0 or index >= len(bm.faces) for index in unique):
            return False
        records = []
        segment_count = 0
        for index in unique:
            face = bm.faces[index]
            if (len(records) >= _FACES_LIMIT or
                    len(face.loops) + segment_count > _SEGMENTS_LIMIT):
                break
            record = _face_record(face, layer)
            records.append((index, record))
            segment_count += len(face.loops)
        outlines, total, omitted = uv_feedback_math.bounded_outlines(
            ((index, tuple(item[2] for item in record)) for index, record in records),
            _FACES_LIMIT, _SEGMENTS_LIMIT)
        _SNAPSHOT = {
            'object': obj, 'mesh': obj.data,
            'uv_layer': active_uv.name,
            'uv_key': selection_core.uv_layer_key(obj.data),
            'kind': kind, 'records': tuple(records),
            'outlines': outlines, 'total': len(unique),
            'omitted': len(unique) - len(outlines),
            'counts': (len(bm.verts), len(bm.edges), len(bm.faces)),
        }
        _STATUS = 'PARTIAL' if _SNAPSHOT['omitted'] else 'READY'
        log.write('UV feedback published: kind=%s, visible=%d, total=%d'
                  % (kind, len(outlines), len(unique)))
        viewport_feedback.publish(_SNAPSHOT)
        _redraw()
        return bool(outlines)
    except (ReferenceError, RuntimeError, ValueError, IndexError, AttributeError) as exc:
        clear('preview unavailable: %s' % exc)
        return False


def focus_face(face_index, uv_points=None, local_points=None):
    """Keep a single highlighted outline even beyond the 400-face preview cap."""
    global _FOCUSED_FACE, _FOCUSED_OUTLINE
    _FOCUSED_FACE = int(face_index) if face_index is not None else None
    _FOCUSED_OUTLINE = (tuple((float(x), float(y)) for x, y in uv_points)
                        if uv_points is not None else None)
    viewport_feedback.focus_face(face_index, local_points)
    _redraw()


def clear(reason=None):
    global _SNAPSHOT, _STATUS, _FOCUSED_FACE, _FOCUSED_OUTLINE
    _FOCUSED_FACE = None
    _FOCUSED_OUTLINE = None
    _SNAPSHOT = None
    _STATUS = 'STALE' if reason else 'EMPTY'
    viewport_feedback.clear(reason)
    if reason:
        log.write('UV feedback invalidated: %s' % reason)
    _redraw()


def status():
    snapshot = _SNAPSHOT
    if snapshot is None:
        return {'state': _STATUS, 'total': 0, 'drawn': 0, 'omitted': 0, 'kind': None, 'focus': None}
    return {'state': _STATUS, 'total': snapshot['total'],
            'drawn': len(snapshot['outlines']), 'omitted': snapshot['omitted'],
            'kind': snapshot['kind'], 'focus': _FOCUSED_FACE}


def _watch():
    """Check affected polygons at a modest cadence, outside GPU callbacks."""
    snapshot = _SNAPSHOT
    if snapshot is None:
        return 0.85
    context = bpy.context
    if not _same_context(context, snapshot):
        current_obj = getattr(context, 'active_object', None)
        try:
            current_mesh = (getattr(current_obj, 'data', None)
                            if current_obj is not None and
                            getattr(current_obj, 'type', None) == 'MESH' else None)
            current_uv = (selection_core.uv_layer_key(current_mesh)
                          if current_mesh is not None else None)
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            current_uv = None
        clear('object, mode or UV map changed; expected_uv=%r; current_uv=%r' %
              (snapshot['uv_key'], current_uv))
        return 0.85
    try:
        if not viewport_feedback.transform_matches():
            clear('object transform changed')
            return 0.85
        bm = bmesh.from_edit_mesh(snapshot['mesh'])
        if (len(bm.verts), len(bm.edges), len(bm.faces)) != snapshot['counts']:
            clear('topology counts changed')
            return 0.85
        bm.verts.index_update()
        bm.faces.ensure_lookup_table()
        layer = bm.loops.layers.uv.active
        if layer is None:
            clear('UV layer no longer available')
            return 0.85
        for index, recorded in snapshot['records']:
            if not uv_feedback_math.face_points_equal(
                    recorded, _face_record(bm.faces[index], layer)):
                clear('UV geometry or topology changed')
                return 0.85
    except Exception as exc:
        clear('geometry validation failed: %s' % exc)
    return 0.85


def _draw():
    """Readonly UI callback: no edit BMesh, operators, model writes, or selections."""
    snapshot = _SNAPSHOT
    if snapshot is None or _HANDLER is None:
        return
    context = bpy.context
    try:
        if (context.area is None or context.area.type != 'IMAGE_EDITOR'
                or context.region is None or context.region.type != 'WINDOW'
                or not _same_context(context, snapshot)):
            return
        space = context.space_data
        if getattr(space, 'ui_mode', 'UV') != 'UV':
            return
        item = context.preferences.addons.get(__package__)
        prefs = item.preferences if item else None
        if prefs is not None and not prefs.show_uv_issue_overlay:
            return
        import gpu
        import blf
        from gpu_extras.batch import batch_for_shader
        opacity = float(getattr(prefs, 'overlay_opacity', 0.90))
        strong = ((1.0, 0.36, 0.12, opacity) if snapshot['kind'] == 'FLIPPED'
                  else (0.78, 0.35, 1.0, opacity))
        pale = (*strong[:3], opacity * 0.23)
        to_region = context.region.view2d.view_to_region
        w, h = context.region.width, context.region.height
        segments = []
        for first, second in uv_feedback_math.outline_segments(snapshot['outlines']):
            a = to_region(*first, clip=False)
            b = to_region(*second, clip=False)
            if (max(a[0], b[0]) < -16 or min(a[0], b[0]) > w + 16
                    or max(a[1], b[1]) < -16 or min(a[1], b[1]) > h + 16):
                continue
            if max(abs(a[0]), abs(a[1]), abs(b[0]), abs(b[1])) > 10000000:
                continue
            segments.extend((a, b))
        if not segments:
            return
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'LINES', {'pos': segments})
        gpu.state.blend_set('ALPHA')
        try:
            for width, color in ((4.0, pale), (1.8, strong)):
                gpu.state.line_width_set(width)
                shader.bind()
                shader.uniform_float('color', color)
                batch.draw(shader)
        finally:
            gpu.state.line_width_set(1.0)
            gpu.state.blend_set('NONE')
        # The active navigation issue is outlined independently from UV select.
        if _FOCUSED_FACE is not None:
            target = (_FOCUSED_OUTLINE or next(
                (points for idx, points in snapshot['outlines']
                 if idx == _FOCUSED_FACE), None))
            if target:
                focus_segments = []
                for i, point in enumerate(target):
                    a = to_region(*point, clip=False)
                    b = to_region(*target[(i + 1) % len(target)], clip=False)
                    focus_segments.extend((a, b))
                focus_shader = gpu.shader.from_builtin('UNIFORM_COLOR')
                focus_batch = batch_for_shader(focus_shader, 'LINES', {'pos': focus_segments})
                gpu.state.blend_set('ALPHA')
                try:
                    gpu.state.line_width_set(3.0)
                    focus_shader.bind()
                    focus_shader.uniform_float('color', (0.20, 0.90, 0.84, opacity))
                    focus_batch.draw(focus_shader)
                finally:
                    gpu.state.line_width_set(1.0)
                    gpu.state.blend_set('NONE')
        # Read-only badge, no modal event capture.
        kind_label = 'FLIP UV' if snapshot['kind'] == 'FLIPPED' else 'UV STRETCH'
        label = '%s  ·  %d' % (kind_label, snapshot['total'])
        if snapshot['omitted']:
            label += '  (shown %d)' % len(snapshot['outlines'])
        # A non-interactive caption, measured with BLF and contained within
        # the actual UV Editor WINDOW region (no selection/tool changes).
        from .core import diagnostic_caption, theme_engine, ui_layout_scaling, viewport_skin
        from .appearance_gpu import Painter
        profile = ui_layout_scaling.from_preferences(prefs, context=context)
        font_px = max(10, round(viewport_skin.text_px('control') * profile.font))
        blf.size(0, font_px)
        rect = diagnostic_caption.layout_caption(
            label, (w,h), (22,h-42), lambda value: blf.dimensions(0,value)[0],
            scale=profile.font, location='top_left', max_width_ratio=.55)
        if rect is not None:
            theme = theme_engine.from_preferences(prefs)
            painter = Painter()
            try:
                diagnostic_caption.draw_caption(painter, rect[0], rect[1], theme,
                    size=font_px, semantic=strong)
            finally:
                painter.close()
    except Exception as exc:
        # Fail closed on unsupported GPU or missing context, never edit geometry.
        clear('overlay draw failed: %s' % exc)
        return


def register():
    global _HANDLER
    try:
        _HANDLER = bpy.types.SpaceImageEditor.draw_handler_add(
            _draw, (), 'WINDOW', 'POST_PIXEL')
    except (RuntimeError, TypeError, AttributeError) as exc:
        _HANDLER = None
        log.write('UV preview GPU handler unavailable: %s' % exc)
    viewport_feedback.register()
    if not bpy.app.timers.is_registered(_watch):
        bpy.app.timers.register(_watch, first_interval=0.85, persistent=True)


def unregister():
    global _HANDLER
    viewport_feedback.unregister()
    clear()
    if bpy.app.timers.is_registered(_watch):
        bpy.app.timers.unregister(_watch)
    if _HANDLER is not None:
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(_HANDLER, 'WINDOW')
        except (RuntimeError, ValueError, ReferenceError):
            pass
        _HANDLER = None
