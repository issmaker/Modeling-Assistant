"""Collision Inspector — non-destructive Viewport diagnostics (3.5.2 Persistent Controls Hotfix).

Purely observational GPU draw handlers. No mesh/edit state is modified during
rendering, highlighting, picking, or framing. Blender GPU must run on UI thread.
"""
import math
import json
import time
from collections import Counter, OrderedDict

import bpy
from bpy.app.handlers import persistent
from mathutils import Vector
from . import viewport_visual as _visual
from ...core.ui_layout_scaling import from_preferences as _ui_profile
from ...core import design_tokens as _iss_style
from ...core import window_visibility
from ...core import theme_engine as _theme_engine
from ...core.workspace_components import component_visual as _workspace_visual
from ...core import viewport_skin as _viewport_skin
from ...core import window_shell as _shell
from ...core import lifecycle_lease as _lease

_ACTIVE = None
_HANDLERS = []
# GPU geometry and signature verification must be isolated by scene/object.
# Single global cache led to cross-window thrashing and unnecessary full-mesh scans.
_CACHE = {}  # Most recent payload; retained for older diagnostic integrations.
_CACHES = OrderedDict()
_SIGNATURE_CHECKED = OrderedDict()
_MAX_SCENE_CACHES = 8
_MAX_SIGNATURE_TIMESTAMPS = 24
_PERF = {'cache_hits': 0, 'cache_misses': 0, 'signature_checks': 0,
         'signature_last_ms': 0.0, 'signature_max_ms': 0.0}
_STALE_KEYS = set()
_VERIFY_PENDING_KEYS = set()
_LAST_SIGNATURE = 0.0  # Backwards-compatible diagnostics only.
_LAST_SIGNATURE_KEY = None
_DRAW_ERROR = ""
_MAX_DRAW_FACES = 1200
_MAX_DRAW_EDGES = 3000
_MAX_DRAW_VERTICES = 2500
_MAX_PINS = 80
_TIMER_RUNNING = False
_LIFECYCLE_TOKEN = None
_LIFECYCLE_ERRORS = []  # text-only, bounded
_LEASE_OWNER = "collision.viewport"
_FOOTER_HOVER = (None, None)
_MARKER_MENU_AREA = None
_MARKER_MENU_HOVER = (None, None)
_GROUP_MENU_AREA = None
_VERIFY_PENDING = False

# Colors also have text identifiers. A and B are differentiated in shape + text.
COLORS = {
    "UNCLOSED": (1.0, .67, .31), "NONMANIFOLD": (1.0, .43, .42),
    "NONCONVEX": (1.0, .76, .31), "DEGENERATE": (1.0, .38, .46),
    "SELF_INTERSECTION": (1.0, .32, .48), "INTERSECTION": (1.0, .32, .48),
    "NESTED": (.65, .50, 1.0), "GAP": (.99, .72, .30),
    "CONTACT": (.90, .69, .35), "COINCIDENT": (.77, .48, 1.0),
    "SURFACE_OVERLAP": (1.0, .43, .53),
}
SIDE_COLORS = {"A": (1.0, .47, .33), "B": (.35, .91, .78)}


def _appearance_theme():
    try:
        addons=bpy.context.preferences.addons
        item=addons.get('issmaker_modeling_assistant')
        return _theme_engine.from_preferences(item.preferences if item else None)
    except (AttributeError, ReferenceError, RuntimeError, TypeError):
        return _theme_engine.from_values({})


def _appearance_panel_bg():
    panel=_appearance_theme().panel
    return (*panel, _iss_style.PANEL_BG[3])


def invalidate():
    """Discard derived geometry for all scenes without retaining Blender Mesh refs."""
    global _CACHE, _LAST_SIGNATURE, _LAST_SIGNATURE_KEY, _DRAW_ERROR
    global _FOOTER_HOVER, _MARKER_MENU_HOVER, _GROUP_MENU_AREA, _MARKER_MENU_AREA
    _FOOTER_HOVER=(None,None)
    _MARKER_MENU_HOVER=(None,None)
    _GROUP_MENU_AREA=None
    _MARKER_MENU_AREA=None
    _CACHE = {}
    _CACHES.clear()
    _SIGNATURE_CHECKED.clear()
    _STALE_KEYS.clear()
    _VERIFY_PENDING_KEYS.clear()
    _LAST_SIGNATURE = 0.0
    _LAST_SIGNATURE_KEY = None
    _DRAW_ERROR = ""
    redraw()


def cache_metrics():
    """Read-only bounded diagnostics; does not retain Mesh/Window references."""
    return dict(_PERF, cache_entries=len(_CACHES),
                signature_entries=len(_SIGNATURE_CHECKED),
                pending_entries=len(_VERIFY_PENDING_KEYS),
                stale_entries=len(_STALE_KEYS))


def redraw():
    wm = getattr(bpy.context, 'window_manager', None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _valid_world_point(obj, vertex_index):
    mesh = obj.data
    if vertex_index < 0 or vertex_index >= len(mesh.vertices):
        return None
    local = mesh.vertices[vertex_index].co
    if not all(math.isfinite(float(x)) for x in local):
        return None
    world = obj.matrix_world @ local
    if not all(math.isfinite(float(x)) for x in world):
        return None
    return tuple(float(x) for x in world)


def _event_indices(event, side='BOTH'):
    from . import _string_to_indices
    a = _string_to_indices(event.indices_a)
    b = _string_to_indices(event.indices_b)
    if side == 'A':
        return {'A': a}
    if side == 'B':
        return {'B': b}
    return {'A': a, 'B': b}


def _collect_vertex_indices(obj, event, side='BOTH', limit=200000):
    """Use original mesh indices, not Edit Mode indices or evaluated data."""
    indices = set()
    mesh = obj.data
    mode = event.select_mode
    for component in _event_indices(event, side).values():
        for index in sorted(component):
            if mode == 'VERT':
                if 0 <= index < len(mesh.vertices):
                    indices.add(index)
            elif mode == 'EDGE' and 0 <= index < len(mesh.edges):
                indices.update(mesh.edges[index].vertices)
            elif mode == 'FACE' and 0 <= index < len(mesh.polygons):
                indices.update(mesh.polygons[index].vertices)
            if len(indices) > limit:
                return sorted(indices)[:limit], True
    return sorted(indices), False


def _event_center(obj, event, side='BOTH'):
    vertex_ids, _ = _collect_vertex_indices(obj, event, side, limit=6000)
    if not vertex_ids:
        return None
    # For very large face groups use deterministic evenly spread samples.
    stride = max(1, len(vertex_ids) // 200)
    pts = [_valid_world_point(obj, i) for i in vertex_ids[::stride]]
    pts = [p for p in pts if p is not None]
    if not pts:
        return None
    mean = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
    # A centroid may lie inside a cavity or between disconnected hot spots.
    # Pin the label to a REAL inspected vertex nearest the representative mean.
    return min(pts, key=lambda p: sum((p[i]-mean[i])**2 for i in range(3)))


def _primitives(obj, event, side):
    """Return intentionally capped, read-only highlight geometry for one side."""
    mode = event.select_mode
    items = sorted(_event_indices(event, side).get(side, ()))
    mesh = obj.data
    result = {'triangles': [], 'lines': [], 'points': [], 'truncated': False}
    if mode == 'VERT':
        for index in items[:_MAX_DRAW_VERTICES]:
            point = _valid_world_point(obj, index)
            if point is not None:
                result['points'].append(point)
        result['truncated'] = len(items) > _MAX_DRAW_VERTICES
    elif mode == 'EDGE':
        for index in items[:_MAX_DRAW_EDGES]:
            if 0 <= index < len(mesh.edges):
                edge = mesh.edges[index]
                first, second = (_valid_world_point(obj, i) for i in edge.vertices)
                if first is not None and second is not None:
                    result['lines'].extend((first, second))
        result['truncated'] = len(items) > _MAX_DRAW_EDGES
    elif mode == 'FACE':
        edge_occurrences = Counter()
        for index in items[:_MAX_DRAW_FACES]:
            if not (0 <= index < len(mesh.polygons)):
                continue
            poly = mesh.polygons[index]
            vertex_ids = list(poly.vertices)
            points = [_valid_world_point(obj, i) for i in vertex_ids]
            if any(p is None for p in points) or len(points) < 3:
                continue
            # Correct triangulation for concave n-gons (no fan overreach).
            from mathutils.geometry import tessellate_polygon
            try:
                tris = tessellate_polygon([[Vector(v) for v in points]])
            except (RuntimeError, ValueError):
                continue
            # tessellate_polygon returns local integer indices in Blender 4.2–5.x,
            # not the vertices themselves. Re-map before sending GPU coordinates.
            for tri in tris:
                if len(tri) != 3 or any(
                    not isinstance(idx, int) or not 0 <= idx < len(points) for idx in tri
                ):
                    continue
                result['triangles'].extend(points[idx] for idx in tri)
            for i, u in enumerate(vertex_ids):
                v = vertex_ids[(i + 1) % len(vertex_ids)]
                edge_occurrences[tuple(sorted((u, v)))] += 1
        # Draw the actual outer boundary, not every triangulation diagonal.
        for (u, v), count in edge_occurrences.items():
            if count == 1 and len(result['lines']) // 2 < _MAX_DRAW_EDGES:
                first, second = _valid_world_point(obj, u), _valid_world_point(obj, v)
                if first is not None and second is not None:
                    result['lines'].extend((first, second))
        result['truncated'] = len(items) > _MAX_DRAW_FACES or len(edge_occurrences) > _MAX_DRAW_EDGES
    return result


def _read_trace(event):
    """Read bounded coordinate witnesses only, never inferred polygon diagonals."""
    raw = getattr(event, 'trace_segments', '')
    if not raw or len(raw) > 30000:
        return []
    try:
        segments = json.loads(raw)
    except (ValueError, TypeError):
        return []
    valid = []
    if not isinstance(segments, list):
        return []
    for segment in segments[:48]:
        if (isinstance(segment, list) and len(segment) == 2 and
                all(isinstance(point, list) and len(point) == 3 and
                    all(isinstance(x, (int, float)) and math.isfinite(x) for x in point)
                    for point in segment)):
            valid.extend(tuple(float(x) for x in point) for point in segment)
    return valid


def _read_diagonal_preview(event):
    """Only validated finite 3D line witnesses are allowed into GPU overlay."""
    if not getattr(event, 'repair_available', False):
        return []
    raw = getattr(event, 'repair_preview_lines', '')
    if not raw or len(raw) > 16000:
        return []
    try:
        segments = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(segments, list):
        return []
    points = []
    for segment in segments[:48]:
        if (not isinstance(segment, list) or len(segment) != 2
            or not all(isinstance(pt,list) and len(pt) == 3 and
                       all(type(c) in (int,float) and math.isfinite(c) for c in pt)
                       for pt in segment)):
            continue
        points.extend(tuple(map(float,pt)) for pt in segment)
    return points


def _read_overlap(event):
    """Parse bounded positive-area witness polygons; never trust stored JSON."""
    raw = getattr(event, 'overlap_polygons', '')
    if not raw or len(raw)>40000:
        return []
    try:
        polygons = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(polygons, list):
        return []
    result=[]
    for polygon in polygons[:48]:
        if (not isinstance(polygon, list) or not 3 <= len(polygon) <= 8 or
            not all(isinstance(p,list) and len(p)==3 and
                    all(type(v) in (int,float) and math.isfinite(v) for v in p)
                    for p in polygon)):
            continue
        points=[tuple(float(x) for x in pt) for pt in polygon]
        # Reject collapsed regions; the minimum area is relative to extents.
        origin=points[0]
        crosssum=[0.0,0.0,0.0]
        for i in range(1,len(points)-1):
            a=[points[i][k]-origin[k] for k in range(3)]
            b=[points[i+1][k]-origin[k] for k in range(3)]
            c=(a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
            crosssum=[crosssum[k]+c[k] for k in range(3)]
        ext=max((math.dist(a,b) for a in points for b in points),default=0.0)
        if math.sqrt(sum(c*c for c in crosssum))<=max(1e-18,ext*ext*1e-13):
            continue
        result.append(points)
    return result


def _make_cache(context, obj, settings):
    """Geometry cache invalidated on analysis/mesh change, not on every redraw."""
    index = min(settings.event_index, len(settings.events) - 1)
    # Never scan unbounded numbers of events merely to draw passive labels.
    from . import _visible_issue
    marker_ids = set()
    for i, item in enumerate(settings.events):
        if _visible_issue(item.kind, settings.issue_filter):
            marker_ids.add(i)
            if len(marker_ids) >= _MAX_PINS:
                break
    marker_ids.add(index)
    pins = {}
    for i in sorted(marker_ids):
        event = settings.events[i]
        center_a = _event_center(obj, event, 'A')
        center_b = _event_center(obj, event, 'B') if event.indices_b else None
        if event.has_witness_pair:
            # The closest points for a gap are mathematically measured,
            # unlike face-centroid approximations used for other event kinds.
            center_a = tuple(event.witness_a)
            center_b = tuple(event.witness_b)
        # Never place a marker at an artificial midpoint in empty space.
        position = center_a or center_b
        pins[i] = {'index': i, 'position': position, 'A': center_a, 'B': center_b}
    active = settings.events[index]
    data = {
        'key': (context.scene.as_pointer(), obj.as_pointer(),
                settings.analysis_signature, len(settings.events), index,
                settings.overlay_side, settings.issue_filter),
        'pins': pins,
        'active': index,
        'kind': active.kind,
        'trace': _read_trace(active),
        'repair_preview': _read_diagonal_preview(active),
        'coplanar': _read_overlap(active),
        'witness': (tuple(active.witness_a), tuple(active.witness_b),
                    float(active.metric_value)) if active.has_witness_pair else None,
        'A': _primitives(obj, active, 'A') if settings.overlay_side != 'B' else None,
        'B': _primitives(obj, active, 'B') if settings.overlay_side != 'A' else None,
    }
    data['truncated'] = any(x is not None and x['truncated'] for x in (data['A'], data['B']))
    return data


def _read_context(context):
    """Fail closed on stale data, with independent bounded caches per scene/object.

    Drawing two Blender windows must not force two full geometry hashes on every
    frame. Cache entries contain only derived Python coordinates, never Mesh refs.
    """
    global _LAST_SIGNATURE, _LAST_SIGNATURE_KEY, _CACHE
    scene = getattr(context, 'scene', None)
    area = getattr(context, 'area', None)
    region = getattr(context, 'region', None)
    if not scene or not area or area.type != 'VIEW_3D' or not region or region.type != 'WINDOW':
        return None
    settings = getattr(scene, 'collision_inspector', None)
    if not settings or not settings.overlay_enabled or not settings.analysis_signature:
        return None
    obj = bpy.data.objects.get(settings.object_name)
    if obj is None or obj.type != 'MESH' or obj != context.active_object or obj.mode != 'OBJECT':
        return None
    object_key = (scene.as_pointer(), obj.as_pointer())
    if object_key in _STALE_KEYS:
        return (settings, obj, None, 'Изменена геометрия — перепроверить')
    now = time.monotonic()
    signature_key = (scene.as_pointer(), obj.as_pointer(), settings.analysis_signature)
    size = len(obj.data.vertices)
    interval = 5.0 if size > 250000 else 2.0 if size > 100000 else .65
    last_check = _SIGNATURE_CHECKED.get(signature_key)
    if object_key in _VERIFY_PENDING_KEYS or last_check is None or now - last_check > interval:
        from . import _object_signature
        started = time.monotonic()
        try:
            signature = _object_signature(obj)
        except Exception:
            signature = ''
        spent_ms = max(0.0, (time.monotonic() - started) * 1000.0)
        _PERF['signature_checks'] += 1
        _PERF['signature_last_ms'] = spent_ms
        _PERF['signature_max_ms'] = max(_PERF['signature_max_ms'], spent_ms)
        _LAST_SIGNATURE = now
        _LAST_SIGNATURE_KEY = signature_key
        _VERIFY_PENDING_KEYS.discard(object_key)
        if not signature or signature != settings.analysis_signature:
            _STALE_KEYS.add(object_key)
            for old_key in list(_CACHES):
                if old_key[:2] == object_key:
                    _CACHES.pop(old_key, None)
            _CACHE = {}
            return (settings, obj, None, 'Результаты устарели — перепроверить')
        _SIGNATURE_CHECKED[signature_key] = now
        _SIGNATURE_CHECKED.move_to_end(signature_key)
        while len(_SIGNATURE_CHECKED) > _MAX_SIGNATURE_TIMESTAMPS:
            _SIGNATURE_CHECKED.popitem(last=False)
    if not settings.events:
        return (settings, obj, None, settings.status or 'Ошибок нет')
    from . import _visible_issue
    if not any(_visible_issue(event.kind, settings.issue_filter) for event in settings.events):
        return (settings, obj, None, 'В выбранной группе нет проблем')
    index = min(settings.event_index, len(settings.events) - 1)
    key = (scene.as_pointer(), obj.as_pointer(), settings.analysis_signature,
           len(settings.events), index, settings.overlay_side, settings.issue_filter)
    if key not in _CACHES:
        _PERF['cache_misses'] += 1
        _CACHES[key] = _make_cache(context, obj, settings)
        # Drop older revisions of this object's highlight first. If the scene
        # is still being used by another window, its different key is retained.
        for old_key in list(_CACHES):
            if old_key != key and old_key[:2] == object_key:
                _CACHES.pop(old_key, None)
        while len(_CACHES) > _MAX_SCENE_CACHES:
            _CACHES.popitem(last=False)
    else:
        _PERF['cache_hits'] += 1
    _CACHES.move_to_end(key)
    _CACHE = _CACHES[key]
    return (settings, obj, _CACHE, '')


def _draw_mesh_primitive(gpu, batch_for_shader, shader, mode, coords, rgba, width=1.0):
    """Blender 4.0–5.x line/point shader bridge with safe 1px fallback.

    Since 4.5 wide lines need POLYLINE and points should use POINT shaders.
    Try these on their actual runtime; unavailable variants fall back to
    portable thin primitives rather than disabling all geometry highlights.
    """
    selected = shader
    try:
        if mode == 'LINES':
            selected = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            selected.bind()
            viewport = gpu.state.viewport_get()
            selected.uniform_float('viewportSize', (float(viewport[2]), float(viewport[3])))
            selected.uniform_float('lineWidth', float(width))
        elif mode == 'POINTS':
            selected = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
            selected.bind()
            selected.uniform_float('size', float(width))
    except (AttributeError, RuntimeError, TypeError, ValueError, KeyError):
        selected = shader
        selected.bind()
    selected.uniform_float('color', rgba)
    batch_for_shader(selected, mode, {'pos': coords}).draw(selected)


def _draw_geometry():
    try:
        context = bpy.context
        payload = _read_context(context)
        if payload is None:
            return
        settings, obj, data, message = payload
        if data is None:
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE' if settings.overlay_xray else 'LESS_EQUAL')
        gpu.state.depth_mask_set(False)
        try:
            shimmer = _visual.pulse_value(time.monotonic(), data['active'], settings.overlay_pulse)
            visual_alpha = .76 + .24*shimmer
            for side in ('A', 'B'):
                part = data.get(side)
                if part is None:
                    continue
                rgb = SIDE_COLORS[side]
                for mode, coords, opacity in (
                    ('TRIS', part['triangles'], settings.overlay_opacity * visual_alpha),
                    ('LINES', part['lines'], .78+.19*shimmer),
                    ('POINTS', part['points'], .80+.18*shimmer),
                ):
                    if not coords:
                        continue
                    _draw_mesh_primitive(gpu, batch_for_shader, shader, mode, coords,
                                         (*rgb, opacity),
                                         2.5 if mode == 'LINES' else 7.0)
            if data.get('coplanar'):
                # Exact clipped triangle-overlap regions, not the union outline.
                # Drawing on the reference surface may require X-Ray at some angles.
                shade=(.64, .47, 1.0, min(.38, settings.overlay_opacity+.14) * visual_alpha)
                contour=(.86, .70, 1.0, .80+.18*shimmer)
                tris=[]; edges=[]
                for poly in data['coplanar']:
                    for i in range(1,len(poly)-1):
                        tris.extend((poly[0],poly[i],poly[i+1]))
                    for i,pt in enumerate(poly):
                        edges.extend((pt,poly[(i+1)%len(poly)]))
                shader.bind()
                shader.uniform_float('color',shade)
                batch_for_shader(shader,'TRIS',{'pos':tris}).draw(shader)
                _draw_mesh_primitive(gpu, batch_for_shader, shader, 'LINES', edges, contour, 3.0)
            if data.get('repair_preview'):
                # Read-only proposed NEW diagonals, never physically present in Mesh yet.
                _draw_mesh_primitive(gpu, batch_for_shader, shader, 'LINES',
                                     data['repair_preview'], (.43, .83, 1.0, .75+.25*shimmer), 4.0)
            if data.get('trace'):
                # True triangle-triangle intersection, NOT face-centroid approximation.
                _draw_mesh_primitive(gpu, batch_for_shader, shader, 'LINES', data['trace'],
                                     (1.0, .96, .27, .80+.20*shimmer), 4.0)
                _draw_mesh_primitive(gpu, batch_for_shader, shader, 'POINTS', data['trace'],
                                     (1.0, .96, .27, .80+.20*shimmer), 7.0)
        finally:
            gpu.state.point_size_set(1.0)
            gpu.state.line_width_set(1.0)
            gpu.state.depth_mask_set(True)
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
    except Exception as exc:
        # Draw handlers must never crash user interaction or mutate mesh.
        global _DRAW_ERROR
        _DRAW_ERROR = f'{type(exc).__name__}: {exc}'


def _display_profile():
    try:
        addon=bpy.context.preferences.addons.get('issmaker_modeling_assistant')
        return _ui_profile(addon.preferences if addon else None)
    except (AttributeError,RuntimeError,ReferenceError):
        return _ui_profile(None)


def _hud_text(x,y,value,size,color,max_width=None):
    """BLF-measured fitting instead of estimating Cyrillic glyph widths."""
    import blf
    style=_display_profile()
    font_size=max(8,round(size*style.font))
    blf.size(0,font_size)
    value=str(value)
    if max_width is not None:
        value,_clipped=_iss_style.fit_label(value,max_width,
                                             lambda content:blf.dimensions(0,content)[0])
    from ...core.hud_readability import snap as _snap_text
    blf.position(0,_snap_text(x),_snap_text(y),0)
    blf.color(0,*color)
    blf.draw(0,value)
    return blf.dimensions(0,value)[0]


def _rect(x, y, w, h, rgba, shader, batch_for_shader):
    vertices = [(x,y),(x+w,y),(x+w,y+h),(x,y),(x+w,y+h),(x,y+h)]
    shader.bind()
    shader.uniform_float('color',rgba)
    batch_for_shader(shader,'TRIS',{'pos':vertices}).draw(shader)


def _ui_surface(x, y, w, h, rgba, shader, batch_for_shader, radius=7.):
    """Use the DWOS/UV Wall GPU primitive with a strict legacy fallback."""
    from ...core.studio_gpu_components import fill_rect
    if not fill_rect(x,y,x+w,y+h,rgba,min(radius,w*.25,h*.25)):
        _rect(x,y,w,h,rgba,shader,batch_for_shader)


def _ui_outline(x, y, w, h, color):
    from ...core.studio_gpu_components import outline_rect
    return outline_rect(x,y,x+w,y+h,color,7.,1.)


def _text(x, y, text, size, color):
    import blf
    blf.size(0, size)
    from ...core.hud_readability import snap as _snap_text
    blf.position(0, _snap_text(x), _snap_text(y), 0)
    blf.color(0, *color)
    blf.draw(0, str(text))


def _screen_pins(context, cache, show_all):
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    region = context.region
    region_3d = context.space_data.region_3d
    wanted = [cache['pins'][cache['active']]]
    if show_all:
        wanted += [p for i, p in cache['pins'].items() if i != cache['active']]
    for pin in wanted:
        if pin['position'] is None:
            continue
        pt = location_3d_to_region_2d(region, region_3d, Vector(pin['position']))
        if pt is None:
            continue
        x, y = float(pt.x), float(pt.y)
        if 10 < x < region.width-10 and 10 < y < region.height-10:
            yield pin, x, y




def _pixel_line(points, rgba, shader, batch_for_shader):
    shader.bind()
    shader.uniform_float('color',rgba)
    batch_for_shader(shader,'LINES',{'pos':points}).draw(shader)

def marker_menu_is_open(area):
    return area is not None and _MARKER_MENU_AREA == area.as_pointer()


def toggle_marker_menu(area):
    """Transient Viewport UI state; never stored in Mesh or .blend."""
    global _MARKER_MENU_AREA, _MARKER_MENU_HOVER
    if area is None:
        return False
    pointer = area.as_pointer()
    _MARKER_MENU_AREA = None if _MARKER_MENU_AREA == pointer else pointer
    if _MARKER_MENU_AREA is not None:
        close_footer_group_menu()
    _MARKER_MENU_HOVER = (None, None)
    redraw()
    return _MARKER_MENU_AREA is not None


def group_menu_is_open(area):
    return area is not None and _GROUP_MENU_AREA == area.as_pointer()


def close_footer_group_menu():
    global _GROUP_MENU_AREA
    if _GROUP_MENU_AREA is not None:
        _GROUP_MENU_AREA = None
        redraw()


def toggle_footer_group_menu(area):
    global _GROUP_MENU_AREA
    if area is None:
        return False
    pointer = area.as_pointer()
    was_open = (_GROUP_MENU_AREA == pointer)
    _GROUP_MENU_AREA = None if was_open else pointer
    if not was_open:
        close_marker_menu()
    redraw()
    return not was_open


def close_marker_menu():
    global _MARKER_MENU_AREA, _MARKER_MENU_HOVER
    if _MARKER_MENU_AREA is not None or _MARKER_MENU_HOVER != (None, None):
        _MARKER_MENU_AREA = None
        _MARKER_MENU_HOVER = (None, None)
        redraw()


def update_marker_menu_hover(context, window_x, window_y):
    global _MARKER_MENU_HOVER
    area = getattr(context, 'area', None)
    region = next((r for r in area.regions if r.type == 'WINDOW'), None) if area and area.type == 'VIEW_3D' else None
    result = (None, None)
    if region and region.x <= window_x < region.x + region.width and region.y <= window_y < region.y + region.height:
        action = _visual.hit_control(region.width, region.height,
                                     window_x-region.x, window_y-region.y,
                                     expanded=marker_menu_is_open(area),scale=_display_profile().safe_window)
        if action:
            result = (area.as_pointer(), action)
    if result != _MARKER_MENU_HOVER:
        _MARKER_MENU_HOVER = result
        redraw()


def _draw_toolbar(settings, region, shader, batch_for_shader):
    """One dropdown; options are only rendered while the popup is open."""
    area = getattr(bpy.context, 'area', None)
    buttons = _visual.controls(region.width, region.height, settings.overlay_marker_style,
                               scale=_display_profile().safe_window)
    if not buttons:
        window_visibility.reserve_fixed(window_visibility.scope_from_context(bpy.context,region=region),
                                        'COLLISION_MARKER_BUTTON',None)
        window_visibility.reserve_fixed(window_visibility.scope_from_context(bpy.context,region=region),
                                        'COLLISION_MARKER_MENU',None)
        return
    scope=window_visibility.scope_from_context(bpy.context,region=region)
    x0=min(rect[0] for _,_,rect in buttons)
    y0=min(rect[1] for _,_,rect in buttons)
    x1=max(rect[0]+rect[2] for _,_,rect in buttons)
    y1=max(rect[1]+rect[3] for _,_,rect in buttons)
    window_visibility.reserve_fixed(scope,'COLLISION_MARKER_BUTTON',(x0,y0,x1,y1))
    expanded = marker_menu_is_open(area)
    menu_bounds=_visual.menu_rect(region.width,region.height,scale=_display_profile().safe_window) if expanded else None
    window_visibility.reserve_fixed(scope,'COLLISION_MARKER_MENU',
        (menu_bounds[0],menu_bounds[1],menu_bounds[0]+menu_bounds[2],menu_bounds[1]+menu_bounds[3])
        if menu_bounds is not None else None)
    k=_display_profile().safe_window
    for action, label, (x, y, w, h) in buttons:
        hovered = area is not None and _MARKER_MENU_HOVER == (area.as_pointer(), action)
        _rect(x+1, y-2, w, h, (0,0,0,.28), shader, batch_for_shader)
        from ...core.studio_gpu_components import draw_component
        state='active' if expanded else 'hover' if hovered else 'normal'
        if not draw_component((x,y,x+w,y+h),state=state,role='primary',
                              theme=_appearance_theme(),scale=k):
            _rect(x,y,w,h,_iss_style.component_style(state,theme=_appearance_theme())['bg'],
                  shader,batch_for_shader)
        _rect(x+8*k, y+h-3*k, min(32*k,w-16*k), 2*k, (*_appearance_theme().accent,.86), shader, batch_for_shader)
        _hud_text(x+12*k, y+9*k, label, _viewport_skin.text_px('control'),
                  _viewport_skin.control_ink(_appearance_theme(),part='button',state=state,role='primary'),w-22*k)
        # Small pulse status dot, without claiming pulse is a marker style.
        if settings.overlay_pulse:
            _rect(x+w-17, y+12, 5, 5, (*_appearance_theme().accent,.96), shader, batch_for_shader)
    if not expanded:
        return
    bx, by, bw, bh = _visual.menu_rect(region.width, region.height, scale=_display_profile().safe_window)
    _rect(bx+2, by-3, bw, bh, (0,0,0,.33), shader, batch_for_shader)
    _ui_surface(bx, by, bw, bh, _viewport_skin.shell(_appearance_theme())['panel'], shader, batch_for_shader,7.*k)
    _rect(bx, by+bh-3, bw, 3, (*_appearance_theme().accent,.85), shader, batch_for_shader)
    for action, label, (x,y,w,h) in _visual.menu_controls(region.width, region.height, True, scale=_display_profile().safe_window):
        checked = (settings.overlay_marker_style == action if action in _visual.STYLES
                   else settings.overlay_pulse if action == 'PULSE'
                   else settings.overlay_xray if action == 'XRAY'
                   else settings.overlay_all_markers)
        hovered = area is not None and _MARKER_MENU_HOVER == (area.as_pointer(), action)
        visual=_workspace_visual('button',
            'active' if checked else 'hover' if hovered else 'normal',theme=_appearance_theme())
        if not draw_component((x,y,x+w,y+h),state=(
                'active' if checked else 'hover' if hovered else 'normal'),
                theme=_appearance_theme(),scale=k):
            _rect(x,y,w,h,visual.background,shader,batch_for_shader)
        menu_state='active' if checked else 'hover' if hovered else 'normal'
        _hud_text(x+11*k,y+8*k,('✓  ' if checked else '     ')+label,
                  _viewport_skin.text_px('control'),
                  _viewport_skin.control_ink(_appearance_theme(),part='button',state=menu_state),w-17*k)


def _navigation_state(settings):
    """Exactly the filtered index used by _move_issue and the sidebar UIList."""
    from . import _visible_issue
    matches = [i for i, event in enumerate(settings.events)
               if _visible_issue(event.kind, settings.issue_filter)]
    if not matches:
        return 0, 0
    try:
        current = matches.index(settings.event_index) + 1
    except ValueError:
        current = 1
    return len(matches), current


def _can_repair_active(settings, stale=False):
    if stale or not settings.events or settings.event_index >= len(settings.events):
        return False
    event = settings.events[settings.event_index]
    from . import _visible_issue
    return bool(_visible_issue(event.kind, settings.issue_filter)
                and event.kind == 'NONCONVEX' and event.repair_available
                and event.repair_plan and settings.analysis_signature
                and event.signature == settings.analysis_signature)


def _draw_footer(settings, region, shader, batch_for_shader, has_data, stale):
    total, current = _navigation_state(settings) if has_data else (0, 0)
    buttons = _visual.footer_controls(region.width, region.height, total, current, settings.issue_filter,
                                      _can_repair_active(settings, stale),scale=_display_profile().safe_window)
    scope=window_visibility.scope_from_context(bpy.context,region=region)
    if not buttons:
        window_visibility.reserve_fixed(scope,'COLLISION_FOOTER',None)
        return
    # A restrained dark float at bottom center, never a full-width overlay.
    k=_display_profile().safe_window
    left=min(rect[0] for _,_,rect,_ in buttons)-7*k
    bottom=min(rect[1] for _,_,rect,_ in buttons)-6*k
    right=max(rect[0]+rect[2] for _,_,rect,_ in buttons)+7*k
    top=max(rect[1]+rect[3] for _,_,rect,_ in buttons)+6*k
    width,height=right-left,top-bottom
    window_visibility.reserve_fixed(scope,'COLLISION_FOOTER',(left,bottom,right,top))
    _rect(left+2*k,bottom-3*k,width,height,(0,0,0,.28),shader,batch_for_shader)
    _ui_surface(left,bottom,width,height,_shell.surface(_appearance_theme())['panel'],shader,batch_for_shader,8.*k)
    from issmaker_modeling_assistant.appearance_gpu import draw_shared_background
    draw_shared_background(_appearance_theme(),(left,bottom,width,height),strength_factor=.3)
    for action, label, (x,y,w,h), enabled in buttons:
        selected = action == 'COUNT'
        area = getattr(bpy.context, 'area', None)
        hovered = area is not None and _FOOTER_HOVER == (area.as_pointer(), action)
        role=('warning' if action=='REPAIR' or (selected and stale) else
              'primary' if action in {'CHECK','GROUP'} else 'neutral')
        visual=_workspace_visual('button',
            'disabled' if not enabled else
            'active' if selected or (action=='GROUP' and group_menu_is_open(area)) else
            'hover' if hovered else 'normal',role,_appearance_theme())
        background=visual.background
        foreground=_viewport_skin.control_ink(_appearance_theme(),part='button',
            state=('disabled' if not enabled else 'active' if selected or (
                action=='GROUP' and group_menu_is_open(area)) else 'hover' if hovered else 'normal'),
            role=role)
        from ...core.studio_gpu_components import draw_component
        if not draw_component((x,y,x+w,y+h),state=(
                'disabled' if not enabled else 'active' if selected or (
                  action=='GROUP' and group_menu_is_open(area)) else 'hover' if hovered else 'normal'),
                role=role,theme=_appearance_theme(),scale=k):
            _ui_surface(x,y,w,h,background,shader,batch_for_shader,5.*k)
        if action == 'CHECK':
            _rect(x+7*k,y+h-2.5*k,min(w-14*k,30*k),2*k,(*_appearance_theme().accent,.90),shader,batch_for_shader)
        text_size = 16 if action in ('PREV','NEXT') else _viewport_skin.text_px('body') if selected else _viewport_skin.text_px('control')
        import blf
        blf.size(0,max(8,round(text_size*_display_profile().font)))
        estimate=blf.dimensions(0,label)[0]
        _hud_text(x+max(7*k,(w-estimate)/2),y+11*k,label,text_size,foreground,w-14*k)


def _draw_group_menu(settings, region, shader, batch_for_shader, stale=False):
    if not group_menu_is_open(getattr(bpy.context, 'area', None)):
        return
    scope=window_visibility.scope_from_context(bpy.context,region=region)
    bounds = _visual.group_menu_rect(region.width, region.height,
                                     _can_repair_active(settings, stale),scale=_display_profile().safe_window,scope=scope)
    if not bounds:
        return
    x,y,w,h = bounds
    k=_display_profile().safe_window
    _rect(x+2,y-3,w,h,(0,0,0,.35),shader,batch_for_shader)
    _ui_surface(x,y,w,h,_viewport_skin.shell(_appearance_theme())['panel'],shader,batch_for_shader,7.*k)
    _rect(x,y+h-3,w,3,(*_appearance_theme().accent,.90),shader,batch_for_shader)
    for action, label, (bx,by,bw,bh) in _visual.group_menu_controls(region.width,region.height,True,
                                                                _can_repair_active(settings, stale),scale=_display_profile().safe_window,scope=scope):
        selected = action == 'GROUP_' + settings.issue_filter
        # Same DWOS state and actual hit rectangle as the footer buttons.
        from ...core.studio_gpu_components import draw_component
        visual=_workspace_visual('button','active' if selected else 'normal',
                                 theme=_appearance_theme())
        if not draw_component((bx,by,bx+bw,by+bh),
                              state='active' if selected else 'normal',
                              theme=_appearance_theme(),scale=k):
            _rect(bx,by,bw,bh,visual.background,shader,batch_for_shader)
        menu_state='active' if selected else 'normal'
        _hud_text(bx+10*k,by+8*k,('✓  ' if selected else '     ')+label,
              _viewport_skin.text_px('control'),
              _viewport_skin.control_ink(_appearance_theme(),part='button',state=menu_state),bw-16*k)


def update_footer_hover(context, window_x, window_y):
    """Passive hover styling only. Never swallow mouse motion or query mesh data."""
    global _FOOTER_HOVER
    area = getattr(context, 'area', None)
    region = next((r for r in area.regions if r.type == 'WINDOW'),None) if area and area.type == 'VIEW_3D' else None
    result = (None, None)
    if region and region.x <= window_x < region.x + region.width and region.y <= window_y < region.y + region.height:
        settings = getattr(getattr(context,'scene',None),'collision_inspector',None)
        if settings and settings.overlay_pick_mode and settings.overlay_enabled:
            # Hover is deliberately read-only and must not scan Mesh.  In RC12
            # `message` existed only in the drawing/click paths, so every mouse
            # move here raised NameError and killed Blender's modal controller.
            obj = getattr(context, 'active_object', None)
            scene = getattr(context, 'scene', None)
            stale = (not settings.analysis_signature or obj is None or
                     obj.name != settings.object_name or obj.mode != 'OBJECT')
            if not stale:
                try:
                    stale = (scene.as_pointer(), obj.as_pointer()) in _STALE_KEYS
                except (AttributeError, ReferenceError, RuntimeError):
                    stale = True
            total,current = _navigation_state(settings)
            action = _visual.hit_footer(region.width,region.height,
                                        window_x-region.x,window_y-region.y,total,current,
                                        settings.issue_filter,group_menu_is_open(area),
                                        _can_repair_active(settings, stale),scale=_display_profile().safe_window,
                                        scope=window_visibility.scope_from_context(context,region=region))
            if action is not None:
                result = (area.as_pointer(), action)
    if result != _FOOTER_HOVER:
        _FOOTER_HOVER = result
        redraw()


def clear_footer_hover():
    global _FOOTER_HOVER
    if _FOOTER_HOVER != (None, None):
        _FOOTER_HOVER = (None, None)
        redraw()


def hit_footer(context, window_x, window_y):
    """Clicks on footer buttons only, using the exact drawing region rectangle."""
    area = getattr(context,'area',None)
    if area is None or area.type != 'VIEW_3D':
        return None
    region = next((r for r in area.regions if r.type == 'WINDOW'),None)
    if region is None or not (region.x <= window_x < region.x + region.width and
                               region.y <= window_y < region.y + region.height):
        return None
    with context.temp_override(area=area,region=region):
        payload = _read_context(bpy.context)
        if payload is None:
            return None
        settings, obj, data, message = payload
        total,current = _navigation_state(settings) if data else (0,0)
        return _visual.hit_footer(region.width,region.height,
                                  window_x-region.x,window_y-region.y,total,current,
                                  settings.issue_filter,group_menu_is_open(area),
                                  _can_repair_active(settings, bool(message)),scale=_display_profile().safe_window,
                                  scope=window_visibility.scope_from_context(context,region=region))


def _draw_marker(shader, batch_for_shader, x, y, number, style, active, rgb, pulse):
    """Accessible ring, always screen-sized; geometry is never altered."""
    radius = _visual.marker_radius(style, active)
    if style in ('HALO', 'BEACON'):
        # Soft glow via at most three translucent line batches, not an image shader.
        passes = ((10, .09*pulse), (5, .18*pulse), (0, .82*pulse)) if active else ((0, .50+.23*pulse),)
        for extension, alpha in passes:
            outline = _visual.ring_segments(x,y,radius+extension,32 if active else 20)
            _pixel_line(outline,(*rgb,alpha),shader,batch_for_shader)
    if style == 'DOT':
        _rect(x-radius,y-radius,2*radius,2*radius,(.02,.04,.055,.97),shader,batch_for_shader)
        _rect(x-radius+2,y-radius+2,2*radius-4,2*radius-4,(*rgb,.70+.3*pulse),shader,batch_for_shader)
        _text(x-3.3*len(str(number)),y-4,str(number),11,(.025,.04,.055,1))
    else:
        # Center badge has strong contrast even when glow is disabled.
        _rect(x-9,y-9,18,18,(.018,.04,.055,.97),shader,batch_for_shader)
        _text(x-3.1*len(str(number)),y-4,str(number),10,(*rgb,1))
        if style == 'BEACON':
            _pixel_line(((x+radius,y),(x+radius+13,y+12)),(*rgb,.80),shader,batch_for_shader)
            _rect(x+radius+11,y+6,24,16,(.035,.065,.08,.94),shader,batch_for_shader)
            _text(x+radius+18,y+10,'!',11,(*rgb,1))


def hit_toolbar(context, window_x, window_y):
    area = getattr(context, 'area', None)
    if area is None or area.type != 'VIEW_3D':
        return None
    region = next((r for r in area.regions if r.type == 'WINDOW'),None)
    if region is None or not(region.x <= window_x < region.x+region.width and
                             region.y <= window_y < region.y+region.height):
        return None
    return _visual.hit_control(region.width,region.height,
                               window_x-region.x,window_y-region.y,
                               expanded=marker_menu_is_open(area),scale=_display_profile().safe_window)


def _draw_pixel():
    try:
        context = bpy.context
        payload = _read_context(context)
        if payload is None:
            return
        settings, obj, data, message = payload
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        try:
            region = context.region
            if data and data.get('witness'):
                from bpy_extras.view3d_utils import location_3d_to_region_2d
                first, second, gap_mm = data['witness']
                a2 = location_3d_to_region_2d(region,context.space_data.region_3d,Vector(first))
                b2 = location_3d_to_region_2d(region,context.space_data.region_3d,Vector(second))
                if a2 is not None and b2 is not None:
                    pa = (float(a2.x),float(a2.y))
                    pb = (float(b2.x),float(b2.y))
                    _pixel_line([pa,pb],(.99,.94,.47,1),shader,batch_for_shader)
                    for pt,side in ((pa,'A'),(pb,'B')):
                        _rect(pt[0]-4,pt[1]-4,8,8,(*SIDE_COLORS[side],1),shader,batch_for_shader)
                    label_x,label_y=pa[0]+25,pa[1]+21
                    if label_x + 122 > region.width: label_x = max(12,pa[0]-145)
                    if label_y + 26 > region.height: label_y = pa[1]-38
                    _pixel_line([pa,(label_x,label_y)],(.99,.94,.47,.7),shader,batch_for_shader)
                    _rect(label_x,label_y,118,22,(.045,.065,.078,.93),shader,batch_for_shader)
                    _text(label_x+8,label_y+5,f'{gap_mm:.5g} мм  A ↔ B',11,(1,.94,.65,1))
            # A repair confirmation or Undo may temporarily stop the modal
            # controller. Drawing and input are separate lifecycles: the footer
            # MUST remain together with the information card until Esc hides
            # overlay_enabled. The UI timer restores input after the action.
            _draw_footer(settings, region, shader, batch_for_shader, bool(data), bool(message))
            if data and settings.overlay_markers:
                for pin, x, y in _screen_pins(context, data, settings.overlay_all_markers):
                    idx = pin['index']
                    active = idx == data['active']
                    event = settings.events[idx]
                    rgb = COLORS.get(event.kind, (1.0,.60,.35))
                    phase = _visual.pulse_value(time.monotonic(),idx,settings.overlay_pulse)
                    _draw_marker(shader,batch_for_shader,x,y,idx+1,
                                 settings.overlay_marker_style,active,rgb,phase)
                # Independent A/B locators on the active pair, not an invented intersection point.
                pin = data['pins'][data['active']]
                from bpy_extras.view3d_utils import location_3d_to_region_2d
                for side in ('A','B'):
                    p = pin.get(side)
                    if p is None or settings.overlay_side not in ('BOTH',side):
                        continue
                    pt = location_3d_to_region_2d(region,context.space_data.region_3d,Vector(p))
                    if pt and 0 <= pt.x <= region.width and 0 <= pt.y <= region.height:
                        _rect(float(pt.x)+12,float(pt.y)+7,18,16,(*SIDE_COLORS[side],.9),shader,batch_for_shader)
                        _text(float(pt.x)+17,float(pt.y)+10,side,11,(.03,.05,.06,1))
            card_rect = (_visual.hud_rect(region.width, region.height, has_data=bool(data),scale=_display_profile().safe_window)
                         if settings.overlay_hud and not marker_menu_is_open(context.area) else None)
            scope=window_visibility.scope_from_context(context,region=region)
            if card_rect is None:
                window_visibility.observe(scope,"COLLISION_INFO_CARD",None)
            if card_rect is not None:
                x0,y0,w0,h0=card_rect
                from ...core.window_manager import place_unobstructed
                approved=place_unobstructed((region.width,region.height),
                      'COLLISION_INFO_CARD',(w0,h0),(x0,y0),margin=8,gap=6,scope=scope)
                card_rect=(approved[0],approved[1],approved[2]-approved[0],approved[3]-approved[1]) if approved else None
            if card_rect is not None:
                x, y, width, h = card_rect
                k=_display_profile().safe_window
                _rect(x+3,y-3,width,h,(0,0,0,.28),shader,batch_for_shader)
                _ui_surface(x,y,width,h,_appearance_panel_bg(),shader,batch_for_shader,8.*k)
                if data:
                    from . import KIND_LABELS
                    active = settings.events[data['active']]
                    rgb = COLORS.get(active.kind, (1.0,.65,.35))
                    _rect(x,y+h-4,width,4,(*rgb,.95),shader,batch_for_shader)
                    _hud_text(x+13*k,y+h-25*k,f'{data["active"]+1:02d} / {len(settings.events):02d}  ·  {KIND_LABELS.get(active.kind, active.kind)}',13,(1,1,1,1),width-26*k)
                    line = active.text
                    
                    _hud_text(x+13*k,y+h-48*k,line,12,(.91,.95,.96,1),width-26*k)
                    detail = active.detail or 'Подробнее — панель Collision'
                    
                    _hud_text(x+13*k,y+h-69*k,detail,10,(.82,.86,.88,1),width-26*k)
                    trace_text = (f'ЖЁЛТАЯ ЛИНИЯ · пересечение ({len(data["trace"])//2})'
                                  if data['trace'] else
                                  f'ФИОЛЕТОВЫЙ КОНТУР · наложение ({len(data["coplanar"])})'
                                  if data['coplanar'] else
                                  'A — оранжевая       B — мятная')
                    if active.kind == 'NONCONVEX' and active.repair_available:
                        trace_text = 'ГОЛУБЫЕ ЛИНИИ: новые диагонали · ИСПРАВИТЬ внизу'
                    elif active.kind == 'NONCONVEX':
                        trace_text = (active.repair_reason or 'Подбор диагоналей не помог')[:53]
                    _hud_text(x+13*k,y+h-91*k,trace_text,10,(.75,.87,.84,1),width-26*k)
                    hint = ('Режимы — снизу слева · ЛКМ маркер · ← → · Esc скрывает' if settings.overlay_pick_mode else
                            'Управление восстанавливается · Ctrl+Shift+M')
                    _hud_text(x+13*k,y+11*k,hint,10,(.65,.77,.79,1),width-26*k)
                    if data['truncated'] or len(settings.events) > _MAX_PINS or len(data['trace']) >= 96 or len(data['coplanar']) >= 48:
                        notice = ('Показано не более 48 областей' if len(data['coplanar']) >= 48 else
                                  'Показано не более 48 отрезков' if len(data['trace']) >= 96 else
                                  'Подсветка участка ограничена' if data['truncated'] else
                                  f'Показаны маркеры 1–{_MAX_PINS} + активный')
                        _hud_text(x+13*k,y+27*k,notice,10,(1,.72,.39,1),width-26*k)
                else:
                    zero = (not settings.events or message == 'В выбранной группе нет проблем') and not message.startswith(('Изменена', 'Результаты устарели'))
                    _rect(x,y+h-4,width,4,((.37,.88,.69,1) if zero else (1,.62,.30,1)),shader,batch_for_shader)
                    caption = ('В ГРУППЕ НЕТ ПРОБЛЕМ' if message == 'В выбранной группе нет проблем' else
                               'ПРОВЕРКА ПРОЙДЕНА · 0 ПРОБЛЕМ' if zero else 'ТРЕБУЕТСЯ ПОВТОРНАЯ ПРОВЕРКА')
                    _hud_text(x+13*k,y+h-26*k,caption,12,((.56,1,.82,1) if zero else (1,.75,.45,1)))
                    _hud_text(x+13*k,y+h-51*k,message[:56],11,(.91,.95,.96,1))
            # Menu is above all model markers, HUD cards and other floating labels.
            _draw_toolbar(settings, region, shader, batch_for_shader)
            _draw_group_menu(settings, region, shader, batch_for_shader, bool(message))
            if _display_profile().debug:
                try:
                    from ...core.ui_layout_scaling import record_layout
                    k=_display_profile().safe_window
                    visible=[]
                    if card_rect is not None:
                        x,y,w,h=card_rect
                        visible.append(('HUD_CARD',(x,y,x+w,y+h)))
                    if not marker_menu_is_open(context.area):
                        for key,_label,(x,y,w,h) in _visual.controls(region.width,region.height,scale=k):
                            visible.append(('MARKER_'+key,(x,y,x+w,y+h)))
                    total,current=_navigation_state(settings) if data else (0,0)
                    rects=_visual.footer_controls(region.width,region.height,total,current,
                        settings.issue_filter,_can_repair_active(settings,bool(message)),scale=k)
                    if rects:
                        left=min(row[2][0] for row in rects)-7*k
                        bottom=min(row[2][1] for row in rects)-6*k
                        right=max(row[2][0]+row[2][2] for row in rects)+7*k
                        top=max(row[2][1]+row[2][3] for row in rects)+6*k
                        visible.append(('FOOTER',(left,bottom,right,top)))
                    record_layout('Collision',region.width,region.height,visible)
                except (AttributeError,RuntimeError,ValueError,TypeError):
                    pass
        finally:
            gpu.state.depth_test_set('NONE')
            gpu.state.blend_set('NONE')
    except Exception as exc:
        global _DRAW_ERROR
        _DRAW_ERROR = f'{type(exc).__name__}: {exc}'


def pick_event(context, window_x, window_y):
    """Window-absolute coordinates work even if modal launched from N-panel UI."""
    area = context.area
    if not area or area.type != 'VIEW_3D':
        return None
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region is None or not (region.x <= window_x < region.x + region.width and
                              region.y <= window_y < region.y + region.height):
        return None
    with context.temp_override(area=area, region=region):
        payload = _read_context(bpy.context)
        if not payload or payload[2] is None:
            return None
        settings, obj, data, message = payload
        if not settings.overlay_markers:
            return None
        x, y = window_x-region.x, window_y-region.y
        radius = _visual.marker_radius(settings.overlay_marker_style, False)
        best = ((max(22.0, radius+10.0)) ** 2, None)
        for pin, sx, sy in _screen_pins(bpy.context, data, settings.overlay_all_markers):
            distance = (x-sx)**2+(y-sy)**2
            if distance < best[0]:
                best = (distance, pin['index'])
        return best[1]


def frame_event(context, event, side='BOTH'):
    """Read-only viewport framing: does NOT select, unhide, or change modes."""
    from . import _object_signature
    obj = bpy.data.objects.get(event.obj_name)
    if obj is None or obj.type != 'MESH' or context.active_object != obj:
        return False, 'Активируйте объект, использованный для проверки'
    if obj.mode != 'OBJECT' or _object_signature(obj) != event.signature:
        return False, 'Геометрия/режим изменились: сначала перепроверьте объект'
    # Prefer the true defect witnesses to entire participating faces.
    # Otherwise a 0.025 mm gap could frame two 2-metre cubes, hiding the error.
    points = []
    if getattr(event,'has_witness_pair',False):
        points = [Vector(tuple(event.witness_a)),Vector(tuple(event.witness_b))]
    else:
        trace=_read_trace(event)
        if trace:
            points=[Vector(point) for point in trace]
        else:
            overlap=_read_overlap(event)
            if overlap:
                points=[Vector(point) for poly in overlap for point in poly]
    if not points:
        ids, capped = _collect_vertex_indices(obj, event, side)
        if not ids or capped:
            return False, 'Точный охват не определён; автоматический зум отключён'
        points = [_valid_world_point(obj, i) for i in ids]
        points = [Vector(p) for p in points if p is not None]
    if not points or any(not all(math.isfinite(float(v)) for v in p) for p in points):
        return False, 'Не найдены действительные координаты проблемы'
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (low + high) / 2
    radius = (high - low).length / 2
    # A point has no extent: keep contextual scale, never zoom into near-plane.
    model_extent = max(float(v) for v in obj.dimensions)
    min_distance = max(.12, min(1.25, model_extent * .12)) if math.isfinite(model_extent) else .5
    area = context.area if context.area and context.area.type == 'VIEW_3D' else None
    if area is None:
        for candidate in context.screen.areas:
            if candidate.type == 'VIEW_3D':
                area = candidate
                break
    if area is None:
        return False, 'Не найден 3D Viewport'
    try:
        view = area.spaces.active.region_3d
        view.view_location = center
        view.view_distance = max(min_distance, radius * 3.1)
        area.tag_redraw()
    except (AttributeError, RuntimeError, TypeError) as exc:
        return False, f'Не удалось приблизить: {exc}'
    return True, 'Камера наведена на проблемные элементы'


def note_controller_error(exc):
    """Expose interaction failures in diagnostics without killing Blender modal UI."""
    global _DRAW_ERROR
    _DRAW_ERROR = f'Viewport controller: {type(exc).__name__}: {exc}'[:400]


def draw_error():
    return _DRAW_ERROR


@persistent
def _depsgraph_dirty(scene, depsgraph):
    """Immediate fail-closed invalidation; numeric hash still verifies periodically."""
    if not _CACHES:
        return
    settings = getattr(scene, 'collision_inspector', None)
    if not settings or not settings.analysis_signature:
        return
    obj = bpy.data.objects.get(settings.object_name)
    if obj is None:
        return
    object_key = (scene.as_pointer(), obj.as_pointer())
    if object_key in _STALE_KEYS or not any(
        key[:2] == object_key for key in _CACHES
    ):
        return
    for update in depsgraph.updates:
        source = getattr(update.id, 'original', None) or update.id
        if source == obj or source == obj.data:
            if update.is_updated_geometry or update.is_updated_transform:
                # Blender emits depsgraph notifications on evaluated state changes
                # that may not alter stored Mesh/transform coordinates. Defer to
                # the complete signature check instead of hiding valid highlights.
                _VERIFY_PENDING_KEYS.add(object_key)
                redraw()
                return


@persistent
def _load_post(_unused):
    # Blender may clear driver_namespace when a new .blend is loaded. Reclaim
    # our EXISTING draw handles, not allocate duplicates after file load.
    global _LIFECYCLE_TOKEN
    if (_HANDLERS and _LIFECYCLE_TOKEN is not None
            and not _lease.alive(_LEASE_OWNER, _LIFECYCLE_TOKEN)
            and not _lease.snapshot(_LEASE_OWNER)['leased']):
        _LIFECYCLE_TOKEN, _ = _lease.claim(_LEASE_OWNER)
        row = _lease.owned(_LEASE_OWNER, _LIFECYCLE_TOKEN)
        row['handles'].extend(_HANDLERS)
        row['timer'] = _pulse_timer
        try:
            if not _timer_registered_safe():
                bpy.app.timers.register(_pulse_timer, first_interval=.15, persistent=True)
        except (RuntimeError, ValueError, ReferenceError, AttributeError) as exc:
            _lifecycle_error('rebind_after_load_timer', exc)
    invalidate()
    for scene in bpy.data.scenes:
        settings = getattr(scene, 'collision_inspector', None)
        if settings is not None:
            settings.overlay_pick_mode = False


@persistent
def _undo_redo_post(_unused):
    """Undo/Redo can replace Mesh datablocks and invalidate stored face indices.

    Do not modify user Mesh or selection. Force a new signature comparison on
    the next draw, and close modal picker controls so they cannot use stale data.
    """
    invalidate()
    for scene in bpy.data.scenes:
        try:
            settings = getattr(scene, 'collision_inspector', None)
            if settings is not None:
                settings.overlay_pick_mode = False
        except (AttributeError, ReferenceError, RuntimeError):
            pass


def _pulse_timer():
    """Update animation and recover buttons without manipulating any Mesh.

    Repair, Undo/Redo and temporary Blender confirmations can finish a modal
    picker. The draw handlers do not disappear, so a timer reinstates INPUT
    only when the inspector is still intentionally visible. Esc sets
    overlay_enabled=False, which strictly forbids automatic resurrection.
    """
    if not _HANDLERS or not _lease.alive(_LEASE_OWNER, _LIFECYCLE_TOKEN):
        return None
    needs_rearm = False
    animated = False
    try:
        wm = getattr(bpy.context, 'window_manager', None)
        windows = getattr(wm, 'windows', ()) if wm else ()
        for window in windows:
            scene = getattr(window, 'scene', None)
            settings = getattr(scene, 'collision_inspector', None) if scene else None
            if not settings or not settings.overlay_enabled:
                continue
            areas = [area for area in window.screen.areas if area.type == 'VIEW_3D']
            if not areas:
                continue
            if getattr(settings, 'analysis_signature', '') and not getattr(settings, 'overlay_pick_mode', False):
                needs_rearm = True
                try:
                    from . import _resume_visible_picker
                    # Only one picker per scene, even with several 3D editors.
                    for area in areas:
                        if _resume_visible_picker(window, area):
                            break
                except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError, ImportError) as exc:
                    note_controller_error(exc)
            if settings.overlay_pulse and settings.events and settings.overlay_markers:
                animated = True
        if animated:
            redraw()
            return .10  # Max 10 FPS, all Blender windows.
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        note_controller_error(exc)
    return .35 if needs_rearm else .8


def install():
    global _LIFECYCLE_TOKEN
    if bpy.app.background or _HANDLERS:
        return
    _LIFECYCLE_TOKEN, previous = _lease.claim(_LEASE_OWNER)
    _release_old_lease(previous)
    lease_row = _lease.owned(_LEASE_OWNER, _LIFECYCLE_TOKEN)
    try:
        for callback, stage in ((_draw_geometry, 'POST_VIEW'), (_draw_pixel, 'POST_PIXEL')):
            handle = bpy.types.SpaceView3D.draw_handler_add(callback, (), 'WINDOW', stage)
            _HANDLERS.append(handle)
            lease_row['handles'].append(handle)
        for hook, callback in ((bpy.app.handlers.depsgraph_update_post, _depsgraph_dirty),
                               (bpy.app.handlers.load_post, _load_post),
                               (bpy.app.handlers.undo_post, _undo_redo_post),
                               (bpy.app.handlers.redo_post, _undo_redo_post)):
            _remove_callback_stale(hook, callback)
            hook.append(callback)
        invalidate()
        if not _timer_registered_safe():
            bpy.app.timers.register(_pulse_timer, first_interval=.15, persistent=True)
        lease_row['timer'] = _pulse_timer
    except Exception:
        # An incomplete install must not leave a single GPU handler or timer.
        uninstall()
        raise

def uninstall():
    global _LIFECYCLE_TOKEN
    for name, fn in (('clear_footer', clear_footer_hover),
                     ('close_marker', close_marker_menu),
                     ('close_footer', close_footer_group_menu)):
        try:
            fn()
        except Exception as exc:
            _lifecycle_error(name, exc)
    try:
        if _timer_registered_safe():
            bpy.app.timers.unregister(_pulse_timer)
    except (RuntimeError, ValueError, ReferenceError, AttributeError) as exc:
        _lifecycle_error('remove_timer', exc)
    row = _lease.owned(_LEASE_OWNER, _LIFECYCLE_TOKEN)
    # Both lists can be stale after a failed partial registration.
    handles = list(_HANDLERS)
    if row:
        handles.extend(h for h in row.get('handles', ()) if h not in handles)
    _release_draw_tokens(handles)
    _HANDLERS.clear()
    # A stale incarnation may shut down after the new module has registered.
    # It may release its own GPU resources but MUST NOT delete new callbacks.
    if _lease.alive(_LEASE_OWNER, _LIFECYCLE_TOKEN):
        for hook, callback in ((bpy.app.handlers.depsgraph_update_post, _depsgraph_dirty),
                               (bpy.app.handlers.load_post, _load_post),
                               (bpy.app.handlers.undo_post, _undo_redo_post),
                               (bpy.app.handlers.redo_post, _undo_redo_post)):
            try:
                _remove_callback_stale(hook, callback)
            except Exception as exc:
                _lifecycle_error('remove_hook', exc)
    _lease.retire(_LEASE_OWNER, _LIFECYCLE_TOKEN)
    _LIFECYCLE_TOKEN = None
    try:
        invalidate()
    except Exception as exc:
        _lifecycle_error('invalidate', exc)

def _lifecycle_error(stage, exc):
    row = f"{stage}: {type(exc).__name__}: {exc}"[:300]
    _LIFECYCLE_ERRORS.append(row)
    del _LIFECYCLE_ERRORS[:-32]


def lifecycle_snapshot():
    """Text-only diagnostics; no retained Window, Mesh, GPU or callback refs."""
    return {"active_handles": len(_HANDLERS), "timer_registered": _timer_registered_safe(),
            "lease": _lease.snapshot(_LEASE_OWNER),
            "errors": list(_LIFECYCLE_ERRORS[-10:])}


def _timer_registered_safe(callback=None):
    try:
        return bool(bpy.app.timers.is_registered(callback or _pulse_timer))
    except (AttributeError, RuntimeError, ReferenceError, ValueError):
        return False


def _remove_callback_stale(hook, callback):
    for prior in tuple(hook):
        if (getattr(prior, "__module__", None) == getattr(callback, "__module__", None)
                and getattr(prior, "__name__", None) == getattr(callback, "__name__", None)):
            try:
                hook.remove(prior)
            except (RuntimeError, ValueError, ReferenceError) as exc:
                _lifecycle_error("remove_callback", exc)


def _release_draw_tokens(handles):
    for handler in tuple(handles):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handler, 'WINDOW')
        except (RuntimeError, ValueError, ReferenceError) as exc:
            _lifecycle_error('remove_draw_handler', exc)


def _release_old_lease(previous):
    if not previous:
        return
    _release_draw_tokens(previous.get('handles', ()))
    timer = previous.get('timer')
    if timer is not None:
        try:
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
        except (RuntimeError, ValueError, ReferenceError, AttributeError) as exc:
            _lifecycle_error('remove_old_timer', exc)
