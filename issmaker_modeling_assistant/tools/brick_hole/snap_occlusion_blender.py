"""BH-03.2F: occlusion proof by Blender BVH of visible faces only.

No GPU depth-buffer assumptions; BMesh and source authored UV are read-only.
The BVH is built once per frozen mesh state, never per mouse-move.
"""
import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from bpy_extras.view3d_utils import region_2d_to_origin_3d


def build_visible_bvh(obj):
    """Build readonly triangle BVH from visible mesh faces, ignoring hidden."""
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    coords = [tuple(v.co) for v in bm.verts]
    polygons = [tuple(v.index for v in face.verts)
                for face in bm.faces if not face.hide and len(face.verts) >= 3]
    return BVHTree.FromPolygons(coords, polygons, all_triangles=False, epsilon=0.) if polygons else None


def ray_to_target_is_clear(bvh, start, target):
    """True if no other visible face lies appreciably before a target vertex."""
    if bvh is None:
        return True
    delta = Vector(target) - Vector(start)
    length = delta.length
    if length < 1e-9:
        return True
    _hit, _normal, face_index, distance = bvh.ray_cast(Vector(start), delta / length, length)
    eps = max(1e-5, min(1e-3, length * 2e-5))
    return face_index is None or distance >= length - eps


class VisibleFaceOcclusion:
    def __init__(self, context, *, obj=None):
        obj = obj or context.edit_object
        if obj is None or obj.type != 'MESH':
            raise ValueError('Для Snap нужен активный Mesh')
        self._bvh = build_visible_bvh(obj)
        self._inverse = obj.matrix_world.inverted_safe()
        self._region = context.region if getattr(getattr(context,'region',None),'type',None) == 'WINDOW' else next(
            (region for region in context.area.regions if region.type == 'WINDOW'), None)
        if self._region is None:
            raise ValueError('Окно 3D Viewport недоступно')
        self._rv3d = context.area.spaces.active.region_3d
        self.checks = 0
        self.occluded = 0
        self._result_cache = {}

    def is_visible(self, screen, world):
        if self._bvh is None:
            return True  # No visible faces to hide this vertex.
        # For frozen view and frozen Mesh the visibility of an exactly
        # projected vertex is invariant across mouse movements.
        key = tuple(float(x) for x in world)
        cached = self._result_cache.get(key)
        if cached is not None:
            return cached
        ray_world = region_2d_to_origin_3d(self._region, self._rv3d, screen)
        near = self._inverse @ Vector(ray_world)
        target = self._inverse @ Vector(world)
        self.checks += 1
        visible = ray_to_target_is_clear(self._bvh, near, target)
        if not visible:
            self.occluded += 1
        if len(self._result_cache)>=4096:
            self._result_cache.clear()
        self._result_cache[key]=visible
        return visible
