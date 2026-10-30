"""Read-only 3D Viewport visualization of a verified UV analysis snapshot.

Never stores BMesh/RNA face references. Blender data is read only during a
publish/focus action; draw handlers use bounded immutable world-space tuples.
An error disables rendering but DOES NOT discard the authoritative issue list.
"""

import bpy

from . import log, selection_core

_HANDLERS = []
_CACHE = None
_FOCUS = None
_FOCUS_TRIANGLES = ()
_STATE = 'EMPTY'
_DRAW_ERROR = ''
_MAX_FACES = 400
_MAX_EDGES = 4800


def _redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except (ReferenceError, RuntimeError, AttributeError):
        pass


def _matrix_key(obj):
    return tuple(round(float(value), 9) for row in obj.matrix_world for value in row)


def _world_ring(obj, local_ring):
    return tuple(tuple(float(c) for c in obj.matrix_world @ point) for point in local_ring)


def _lines(ring):
    return tuple(point for i, first in enumerate(ring)
                 for point in (first, ring[(i + 1) % len(ring)])) if len(ring) >= 2 else ()


def _triangles(obj, points):
    """Tessellate concave n-gons correctly, not with a naive triangle fan."""
    if len(points) < 3:
        return ()
    try:
        from mathutils.geometry import tessellate_polygon
        triangles = tessellate_polygon([points])
        return tuple(tuple(float(c) for c in (obj.matrix_world @ points[index]))
                     for triangle in triangles for index in triangle)
    except (ValueError, RuntimeError, TypeError):
        # Safe outline-only fallback for degenerate faces.
        return ()


def publish(uv_snapshot):
    """Derive bounded 3D geometry from the same immutable UV result registry."""
    global _CACHE, _FOCUS, _FOCUS_TRIANGLES, _STATE, _DRAW_ERROR
    clear()
    if not uv_snapshot:
        return False
    try:
        obj = uv_snapshot['object']
        contours, triangles = [], []
        rings = {}
        edge_budget = 0
        for index, record in uv_snapshot['records']:
            if len(rings) >= _MAX_FACES or len(record) + edge_budget > _MAX_EDGES:
                break
            from mathutils import Vector
            points = tuple(Vector(item[1]) for item in record)
            if len(points) < 3:
                continue
            ring = _world_ring(obj, points)
            rings[int(index)] = ring
            contours.extend(_lines(ring))
            triangles.extend(_triangles(obj, points))
            edge_budget += len(points)
        if not rings:
            _STATE = 'UNAVAILABLE'
            return False
        _CACHE = {
            'object': obj, 'mesh': uv_snapshot['mesh'],
            'uv_key': uv_snapshot['uv_key'], 'matrix': _matrix_key(obj),
            'kind': uv_snapshot['kind'], 'total': int(uv_snapshot['total']),
            'omitted': max(0, int(uv_snapshot['total']) - len(rings)),
            'rings': rings, 'lines': tuple(contours), 'tris': tuple(triangles),
            'center': {i: tuple(sum(p[d] for p in ring) / len(ring) for d in range(3))
                       for i, ring in rings.items()},
        }
        # Immediately highlight one problem, even before the first arrow click.
        first = next(iter(rings))
        _FOCUS = (first, rings[first])
        _FOCUS_TRIANGLES = _focus_triangles(rings[first])
        _STATE = 'PARTIAL' if _CACHE['omitted'] else 'READY'
        _DRAW_ERROR = ''
        log.write('3D UV overlay published: kind=%s faces=%d/%d tris=%d' % (
            _CACHE['kind'], len(rings), _CACHE['total'], len(triangles) // 3))
        _redraw()
        return True
    except Exception as exc:
        _CACHE = None
        _STATE = 'ERROR'
        _DRAW_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:240])
        log.write('3D UV overlay publish failed: ' + _DRAW_ERROR)
        return False


def focus_face(index, local_points=None):
    """Focus a face outside the 400-result budget using coordinates from navigation."""
    global _FOCUS, _FOCUS_TRIANGLES
    if _CACHE is None:
        return
    index = int(index)
    ring = _CACHE['rings'].get(index)
    if ring is None and local_points is not None:
        try:
            from mathutils import Vector
            ring = _world_ring(_CACHE['object'], tuple(Vector(p) for p in local_points))
        except (ValueError, ReferenceError, RuntimeError, TypeError):
            ring = None
    _FOCUS = (index, ring) if ring and len(ring) >= 3 else None
    _FOCUS_TRIANGLES = _focus_triangles(ring) if _FOCUS else ()
    _redraw()


def clear(reason=None):
    global _CACHE, _FOCUS, _FOCUS_TRIANGLES, _STATE, _DRAW_ERROR
    _CACHE = None
    _FOCUS = None
    _FOCUS_TRIANGLES = ()
    _STATE = 'STALE' if reason else 'EMPTY'
    _DRAW_ERROR = str(reason)[:240] if reason else ''
    _redraw()


def transform_matches():
    cache = _CACHE
    if cache is None:
        return True
    try:
        return _matrix_key(cache['object']) == cache['matrix']
    except (AttributeError, ReferenceError, RuntimeError, ValueError, TypeError):
        return False


def status():
    cache = _CACHE
    render_state = _STATE
    if cache and _STATE != 'ERROR':
        try:
            item = bpy.context.preferences.addons.get(__package__)
            prefs = item.preferences if item else None
            if not getattr(prefs, 'show_3d_issue_overlay', True):
                render_state = 'HIDDEN'
            elif not _HANDLERS:
                render_state = 'NO_HANDLER'
            elif not transform_matches():
                render_state = 'STALE_TRANSFORM'
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            render_state = 'CONTEXT_UNAVAILABLE'
    return {
        'state': _STATE, 'render_state': render_state,
        'total': cache['total'] if cache else 0,
        'drawn': len(cache['rings']) if cache else 0,
        'omitted': cache['omitted'] if cache else 0,
        'focus': _FOCUS[0] if _FOCUS else None,
        'handler_count': len(_HANDLERS), 'error': _DRAW_ERROR,
        'kind': cache['kind'] if cache else None,
    }


def _visible(context, cache):
    if cache is None or not _HANDLERS or _STATE == 'ERROR':
        return False
    try:
        obj = context.active_object
        if (context.area is None or context.area.type != 'VIEW_3D'
                or context.region is None or context.region.type != 'WINDOW'
                or context.mode != 'EDIT_MESH' or obj is None
                or obj != cache['object'] or obj.type != 'MESH'
                or obj.data != cache['mesh']
                or not selection_core.uv_layer_matches(obj.data, cache['uv_key'])
                or _matrix_key(obj) != cache['matrix']):
            return False
        item = context.preferences.addons.get(__package__)
        prefs = item.preferences if item else None
        return getattr(prefs, 'show_3d_issue_overlay', True)
    except (ReferenceError, RuntimeError, ValueError, AttributeError, TypeError):
        return False


def _fault(exc):
    global _STATE, _DRAW_ERROR
    if _STATE != 'ERROR':
        _DRAW_ERROR = '%s: %s' % (type(exc).__name__, str(exc)[:240])
        _STATE = 'ERROR'
        log.write('3D UV overlay GPU failed: ' + _DRAW_ERROR)
        _redraw()


def _viewer_offset(context, vertices):
    """Draw-only eye-facing bias to prevent coplanar depth fighting.

    The original snapshot remains unchanged. A tiny view-dependent offset is
    calculated from the current camera for the rendering batch only. In case
    Blender does not expose the view rotation, safely use the original tuple.
    """
    try:
        from mathutils import Vector
        rv3d = getattr(context, 'region_data', None)
        if rv3d is None:
            rv3d = getattr(context.space_data, 'region_3d', None)
        back = rv3d.view_rotation @ Vector((0., 0., 1.))
        view_distance = abs(float(getattr(rv3d, 'view_distance', 10.)))
        bias = min(0.012, max(0.00002, view_distance * 0.00012))
        direction = tuple(float(v) * bias for v in back)
        return tuple(tuple(float(point[d]) + direction[d] for d in range(3))
                     for point in vertices)
    except (AttributeError, TypeError, ReferenceError, RuntimeError, ValueError):
        return vertices


def _focus_triangles(ring):
    if not ring or len(ring) < 3:
        return ()
    try:
        from mathutils import Vector
        from mathutils.geometry import tessellate_polygon
        triangles = tessellate_polygon([[Vector(p) for p in ring]])
        return tuple(tuple(float(c) for c in ring[index]) for tri in triangles for index in tri)
    except (ReferenceError, RuntimeError, ValueError, TypeError):
        return ()


def _draw_geometry():
    cache = _CACHE
    context = bpy.context
    if not _visible(context, cache):
        return
    try:
        import gpu
        from gpu_extras.batch import batch_for_shader
        item = context.preferences.addons.get(__package__)
        prefs = item.preferences if item else None
        alpha = float(getattr(prefs, 'overlay_opacity', .90))
        fill_alpha = float(getattr(prefs, 'overlay_3d_fill', .67)) * alpha
        xray = bool(getattr(prefs, 'overlay_3d_xray', False))
        rgb = ((1., .46, .15) if cache['kind'] == 'FLIPPED' else (.70, .34, 1.))
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE' if xray else 'LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        try:
            if cache['tris']:
                shader.bind()
                shader.uniform_float('color', (*rgb, fill_alpha))
                triangles = cache['tris'] if xray else _viewer_offset(context, cache['tris'])
                batch_for_shader(shader, 'TRIS', {'pos': triangles}).draw(shader)
            if cache['lines']:
                gpu.state.line_width_set(1.0)
                shader.bind()
                shader.uniform_float('color', (*rgb, .38 * alpha))
                segments = cache['lines'] if xray else _viewer_offset(context, cache['lines'])
                batch_for_shader(shader, 'LINES', {'pos': segments}).draw(shader)
            if _FOCUS is not None and _FOCUS[1]:
                ring = _FOCUS[1]
                area_triangles = _FOCUS_TRIANGLES
                if area_triangles:
                    shader.bind()
                    # Active area visibly brighter than the surrounding errors.
                    shader.uniform_float('color', (.12, 1., .84, .76 * alpha))
                    pts = area_triangles if xray else _viewer_offset(context, area_triangles)
                    batch_for_shader(shader, 'TRIS', {'pos': pts}).draw(shader)
                gpu.state.line_width_set(2.0)
                shader.bind()
                shader.uniform_float('color', (.12, 1., .84, alpha))
                segments = _lines(ring) if xray else _viewer_offset(context, _lines(ring))
                batch_for_shader(shader, 'LINES', {'pos': segments}).draw(shader)
        finally:
            gpu.state.line_width_set(1.0)
            gpu.state.depth_mask_set(True)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
    except Exception as exc:
        _fault(exc)


def _draw_label():
    cache = _CACHE
    context = bpy.context
    if not _visible(context, cache) or _FOCUS is None or not _FOCUS[1]:
        return
    try:
        import blf
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        from mathutils import Vector
        center = Vector(tuple(sum(p[d] for p in _FOCUS[1]) / len(_FOCUS[1])
                              for d in range(3)))
        point = location_3d_to_region_2d(context.region, context.space_data.region_3d, center)
        if point is None:
            return
        x, y = float(point[0]), float(point[1])
        if not (0 < x < context.region.width and 0 < y < context.region.height):
            return
        label = ('FLIP UV' if cache['kind'] == 'FLIPPED' else 'UV STRETCH')
        from .core import diagnostic_caption, theme_engine, ui_layout_scaling, viewport_skin
        from .core import window_visibility
        from .appearance_gpu import Painter
        addon = context.preferences.addons.get(__package__)
        prefs = addon.preferences if addon else None
        profile = ui_layout_scaling.from_preferences(prefs, context=context)
        font_px = max(10, round(viewport_skin.text_px('control') * profile.font))
        blf.size(0, font_px)
        value = '%s  ·  Face %d  ·  %d найдено' % (label, _FOCUS[0], cache['total'])
        scope = window_visibility.scope_from_context(context)
        peers = tuple(rect for _key, rect in window_visibility.peer_rectangles(
            scope, 'UV_ISSUE_READONLY')) if scope else ()
        result = diagnostic_caption.layout_caption(
            value, (context.region.width, context.region.height), (x,y),
            lambda part: blf.dimensions(0,part)[0],
            scale=profile.font, location='near', obstacles=peers, max_width_ratio=.57)
        if result is not None:
            theme = theme_engine.from_preferences(prefs)
            painter = Painter()
            try:
                diagnostic_caption.draw_caption(painter, result[0], result[1], theme,
                    size=font_px, semantic=(.12,1.,.84,1.))
            finally:
                painter.close()
    except Exception as exc:
        _fault(exc)


def _on_load_pre(_unused):
    clear('project load')


def register():
    if bpy.app.background:
        return
    if _HANDLERS:
        return
    try:
        for cb, stage in ((_draw_geometry, 'POST_VIEW'), (_draw_label, 'POST_PIXEL')):
            handle = bpy.types.SpaceView3D.draw_handler_add(cb, (), 'WINDOW', stage)
            _HANDLERS.append(handle)
        from bpy.app.handlers import persistent
        if _on_load_pre not in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.append(persistent(_on_load_pre))
    except Exception as exc:
        log.write('3D UV overlay register failed: %s' % exc)
        unregister()


def unregister():
    for handle in list(_HANDLERS):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
        except (RuntimeError, ValueError, ReferenceError):
            pass
    _HANDLERS.clear()
    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    clear()
