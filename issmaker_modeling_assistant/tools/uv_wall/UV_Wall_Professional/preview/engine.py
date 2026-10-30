from dataclasses import dataclass, field
from array import array
import hashlib
from enum import IntFlag, auto
from time import perf_counter
from mathutils import Vector
from mathutils.geometry import tessellate_polygon
from ..core.definitions import analyze_path, total_wall_height
from ..core.preview_budget import policy as preview_budget, fast_path_lines
from ..core.performance import PERFORMANCE_REGISTRY


class Dirty(IntFlag):
    NONE = 0
    SOURCE = auto()
    PATH = auto()
    HEIGHT = auto()
    THICKNESS = auto()
    SIDE = auto()
    OFFSET = auto()
    SURFACE = auto()
    CORNICE = auto()
    SURFACE_HEIGHT = auto()
    ALL = SOURCE | PATH | HEIGHT | THICKNESS | SIDE | OFFSET | SURFACE | CORNICE | SURFACE_HEIGHT


@dataclass
class PreviewGeometry:
    triangles: list = field(default_factory=list)
    cap_triangles: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    points: list = field(default_factory=list)
    seam_points: list = field(default_factory=list)  # authored UV origin per WALL chain
    status: str = ""
    quality: str = "INTERACTIVE"
    build_ms: float = 0.0


def _v3(p):
    return Vector((float(p.x), float(p.y), float(p.z)))


def _offset_chain(points, distance, side="LEFT", closed=False, *, secondary=False):
    """Same geometric engine as final Result, including secondary Offset Edges."""
    from ..production.path import offset_polyline, offset_edges_reference_polyline
    chain = [_v3(p) for p in points]
    if len(chain)<2:return chain
    if closed and (chain[0]-chain[-1]).length>1.e-8:
        chain.append(chain[0].copy())
    operator=offset_edges_reference_polyline if secondary else offset_polyline
    return operator(chain,abs(float(distance)),side)


def _wall_preview(chain, definition, path_obj=None):
    points = [_v3(p) for p in chain["points"]]
    closed = bool(chain["closed"])
    from ..production.wall_alignment import left_signed_shift, automatic_inset_side, resolve_placement, preview_normal_side
    preview_path=points+[points[0].copy()] if closed else points
    mode=resolve_placement(getattr(definition,"wall_alignment","LEGACY"),preview_path,definition.inset_side)
    side = automatic_inset_side(mode, definition.inset_side)
    # Production WALL.build_wall explicitly ignores inset_distance when SINGLE.
    # The old preview did not, shifting an otherwise zero-thickness wall by
    # half its disabled double-wall thickness (sometimes several metres).
    shift = float(definition.wall_offset) + (
        left_signed_shift(mode, definition.inset_distance, side, True)
        if definition.create_inner else 0.0)
    if abs(shift)>1.0e-12:
        points = _offset_chain(points,abs(shift),"LEFT" if shift>0 else "RIGHT",closed)
    height = total_wall_height(definition)
    stories_down = definition.story_direction == 'DOWN'
    global_bottom = None
    if stories_down and definition.align_bottom and points:
        global_bottom=min(p.z for p in points)-max(0.0,float(definition.bottom_extend))
    triangles, lines, cap_triangles = [], [], []
    facing = preview_normal_side(points, closed=closed,
                                 normal_mode=definition.wall_normal_mode)
    # Along the path, downwards extrusion initially faces LEFT; upwards
    # initially faces RIGHT. Production applies the authored normal policy.
    basic_facing = 'LEFT' if stories_down else 'RIGHT'

    def other_point(p,h=None):
        if global_bottom is not None:
            return Vector((p.x,p.y,global_bottom))
        delta=(height if h is None else h)
        return p+Vector((0.,0.,(-1. if stories_down else 1.)*delta))

    def top_point(p,h=None):
        return p.copy() if stories_down else other_point(p,h)

    def ribbon(path,heights=None,*,inside_shell=False):
        for i,(a,b) in enumerate(zip(path,path[1:])):
            ah=None if heights is None else heights[i]
            bh=None if heights is None else heights[i+1]
            a2,b2=other_point(a,ah),other_point(b,bh)
            reverse = (facing != basic_facing) != bool(inside_shell)
            if reverse:
                triangles.extend(((a,b2,b),(a,a2,b2)))
            else:
                triangles.extend(((a,b,b2),(a,b2,a2)))
            lines.extend(((a,b),(a2,b2),(a,a2),(b,b2)))

    ribbon(points)
    if definition.create_inner and definition.inset_distance > 0.0:
        # A preview is transient and read-only. The builder owns final UV and
        # topology; this mirrors point height and end-cap decisions visually.
        inner = _offset_chain(points,definition.inset_distance,side,closed,secondary=True)
        inner_heights=None
        from ..production.secondary_points import selected_endcaps
        # No secondary markers, height fields or XY edits. The production
        # result and preview use one authoritative generated Offset path.
        ribbon(inner,inner_heights,inside_shell=True)
        if definition.create_top_cap:
            for i,(a,b,ia,ib) in enumerate(zip(points,points[1:],inner,inner[1:])):
                iha=None if inner_heights is None else inner_heights[i]
                ihb=None if inner_heights is None else inner_heights[i+1]
                at,bt,iat,ibt=top_point(a),top_point(b),top_point(ia,iha),top_point(ib,ihb)
                cap_piece=((at,bt,ibt),(at,ibt,iat))
                cap_triangles.extend(cap_piece);triangles.extend(cap_piece)
                lines.extend(((at,iat),(bt,ibt)))
        start,finish=selected_endcaps(True,closed,getattr(definition,'close_ends',True),
                                    getattr(definition,'close_start_end',True),
                                    getattr(definition,'close_finish_end',True))
        for i,enabled in ((0,start),(-1,finish)):
            if not enabled:continue
            outer,innerp=points[i],inner[i]
            ih=None if inner_heights is None else inner_heights[i]
            outer2,inner2=other_point(outer),other_point(innerp,ih)
            triangles.extend(((outer,innerp,inner2),(outer,inner2,outer2)))
    return triangles, lines, cap_triangles


def _surface_preview(chain):
    points = [_v3(p) for p in chain["points"]]
    if len(points) > 1 and (points[0] - points[-1]).length < 1.0e-8:
        points = points[:-1]
    if len(points) < 3:
        return [], []
    triangles = []
    try:
        for tri in tessellate_polygon([points]):
            triangles.append(tuple(_v3(p) for p in tri))
    except Exception:
        triangles = []
    lines = [(a, b) for a, b in zip(points, points[1:] + points[:1])]
    return triangles, lines


class LivePreviewEngine:
    """Incremental, object-free preview state.

    It deliberately never creates Blender Objects.  Geometry lives as transient
    coordinates and is drawn by the viewport controller.  Production builders
    are called only by commit.
    """
    def __init__(self):
        self.path = None
        self.definition = None
        self.analysis = None
        self.geometry = PreviewGeometry()
        self.dirty = Dirty.ALL
        self.last_revision = -1
        self.last_definition_signature = None
        self.last_path_signature = None
        self.surface_overrides = {}  # index -> tentative absolute world Z; never edits a Mesh
        self.last_build_at = 0.0
        self.max_preview_hz = 60.0
        self.coalesced_updates = 0
        self.settled_delay = 0.12
        self.last_invalidate_at = 0.0
        self.needs_settled = False
        self.path_signature_hz = 20.0
        self.last_signature_check_at = 0.0
        self.fast_preview_frames = 0
        self.last_point_count = 0

    def set_surface_override(self, vertex_index, world_z):
        self.surface_overrides = {int(vertex_index): float(world_z)}
        self.invalidate(Dirty.SURFACE_HEIGHT)

    def clear_surface_override(self):
        if self.surface_overrides:
            self.surface_overrides.clear()
            self.invalidate(Dirty.SURFACE_HEIGHT)

    def bind(self, path_obj, definition):
        if self.path is not path_obj or self.definition is not definition:
            self.path = path_obj
            self.definition = definition
            self.dirty = Dirty.ALL
            self.last_revision = -1
            self.last_definition_signature = None
            self.last_path_signature = None
            self.surface_overrides.clear()
            self.last_build_at = 0.0
            self.coalesced_updates = 0
            self.last_invalidate_at = perf_counter()
            self.needs_settled = True
            self.last_signature_check_at = 0.0

    def invalidate(self, flags=Dirty.ALL):
        self.dirty |= flags
        if flags != Dirty.NONE:
            self.last_invalidate_at = perf_counter()
            self.needs_settled = True

    def settled_due(self, now=None):
        if not self.needs_settled:
            return False
        now = perf_counter() if now is None else float(now)
        return (now - self.last_invalidate_at) >= self.settled_delay

    def _definition_signature(self):
        d=self.definition
        if d is None:return None
        names=(
            "generator_type","module_standard","stories","custom_first_story","first_story_height",
            "story_direction","align_bottom","bottom_extend","create_inner","inset_distance","wall_offset","wall_alignment",
            "inset_side","close_ends","close_start_end","close_finish_end",
            "secondary_points_json","inner_wall_height_delta","inner_wall_height_overrides_json",
            "create_top_cap","create_bottom_cap","wall_normal_mode",
            "roof_normal","surface_method","cap_rotation","cornice_flip_side","cornice_uv_mode",
            "cornice_corner_mode","cornice_linear_step",
        )
        values={}
        for name in names:
            try:values[name]=getattr(d,name)
            except Exception:values[name]=None
        try:values["cornice_profile"]=d.cornice_profile.name if d.cornice_profile else None
        except Exception:values["cornice_profile"]=None
        return values

    @staticmethod
    def _dirty_from_definition_change(previous, current):
        if previous is None or current is None:
            return Dirty.ALL
        changed={name for name,value in current.items() if previous.get(name)!=value}
        if not changed:
            return Dirty.NONE
        structural={"generator_type","module_standard"}
        height={"stories","custom_first_story","first_story_height","story_direction","align_bottom","bottom_extend"}
        thickness={"create_inner","inset_distance","close_ends","close_start_end","close_finish_end",
                   "secondary_points_json","inner_wall_height_delta","inner_wall_height_overrides_json",
                   "create_top_cap","create_bottom_cap"}
        side={"inset_side","wall_normal_mode"}
        offset={"wall_offset","wall_alignment"}
        surface={"roof_normal","surface_method","cap_rotation"}
        cornice={"cornice_flip_side","cornice_uv_mode","cornice_corner_mode","cornice_linear_step","cornice_profile"}
        flags=Dirty.NONE
        if changed & structural: flags |= Dirty.ALL
        if changed & height: flags |= Dirty.HEIGHT
        if changed & thickness: flags |= Dirty.THICKNESS
        if changed & side: flags |= Dirty.SIDE
        if changed & offset: flags |= Dirty.OFFSET
        if changed & surface: flags |= Dirty.SURFACE
        if changed & cornice: flags |= Dirty.CORNICE
        return flags or Dirty.ALL

    def _path_signature(self):
        """Fingerprint the entire source, not only the first 128 vertices/edges.

        Blender's foreach_get transfers dense coordinates in native code;
        blake2b detects silent edits to distant vertices without constructing
        millions of Python tuples on every viewport redraw.
        """
        if self.path is None or self.path.type != "MESH":
            return None
        mesh = self.path.data
        verts = array("f", [0.0]) * (3 * len(mesh.vertices))
        edges = array("i", [0]) * (2 * len(mesh.edges))
        mesh.vertices.foreach_get("co", verts)
        mesh.edges.foreach_get("vertices", edges)
        digest = hashlib.blake2b(digest_size=16)
        digest.update(verts.tobytes())
        digest.update(edges.tobytes())
        return (
            len(mesh.vertices), len(mesh.edges), digest.digest(),
            tuple(round(v, 9) for row in self.path.matrix_world for v in row),
        )

    def update(self, force=False, quality="INTERACTIVE"):
        started = perf_counter()
        if self.path is not None and getattr(self.path,"type",None)=="MESH":
            self.last_point_count=len(self.path.data.vertices)
        budget=preview_budget(self.last_point_count)
        self.max_preview_hz=budget["preview_hz"]
        self.path_signature_hz=budget["signature_hz"]
        if self.path is None or self.definition is None:
            self.geometry = PreviewGeometry(status="NO SOURCE")
            return self.geometry
        # Full source hashing is intentionally throttled. foreach_get is fast,
        # but hashing a very large authored Path on every draw callback wastes
        # CPU. A forced/settled update always rechecks immediately.
        check_signature = force or self.last_path_signature is None
        if not check_signature:
            interval = 1.0 / max(1.0, self.path_signature_hz)
            check_signature = (started - self.last_signature_check_at) >= interval
        if check_signature:
            sig = self._path_signature()
            self.last_signature_check_at = started
            if sig != self.last_path_signature:
                self.dirty |= Dirty.PATH
                self.last_path_signature = sig
                self.needs_settled = True
        dsig = self._definition_signature()
        if dsig != self.last_definition_signature:
            self.dirty |= self._dirty_from_definition_change(self.last_definition_signature, dsig)
            self.last_definition_signature = dsig
        # Revision is persistence/ownership metadata, not a reason by itself for
        # a full preview rebuild. Field signatures above determine real dirtiness.
        self.last_revision = self.definition.revision
        requested_quality = str(quality or "INTERACTIVE").upper()
        if self.dirty == Dirty.NONE:
            if requested_quality == "SETTLED" and self.settled_due(started):
                # Geometry is already current. Promote the preview state without
                # rebuilding identical triangles; production commit remains the
                # only authoritative mesh mutation.
                self.geometry.quality = "SETTLED"
                self.needs_settled = False
            return self.geometry

        # Coalesce high-frequency mouse events. The Definition always contains
        # the latest value; skipped intermediate preview frames are discarded
        # rather than queued, preventing input-lag tails on heavy paths.
        if not force and self.last_build_at > 0.0 and self.max_preview_hz > 0.0:
            min_interval = 1.0 / self.max_preview_hz
            if started - self.last_build_at < min_interval:
                self.coalesced_updates += 1
                return self.geometry

        if self.analysis is None or (self.dirty & Dirty.PATH):
            self.analysis = analyze_path(self.path)
        triangles, lines, points, cap_triangles = [], [], [], []
        mode = self.definition.generator_type
        simple = self.analysis.get("simple_chains", []) if self.analysis else []
        # Seam is anchored to the first station, independent of preview LOD.
        # No new scene object, handler or clickable marker is created.
        from ..core.seam_preview import uv_seam_origins
        seam_points = uv_seam_origins(simple, mode)
        fast = bool(budget["fast"])
        if fast:
            # No tessellation/production Offset calls in the draw callback on
            # high-density contours. All original edges remain in the visual.
            lines, points = fast_path_lines(simple,include_points=(mode=="SURFACE"))
            status = "БЫСТРЫЙ КОНТУР · точная геометрия при построении"
            self.fast_preview_frames += 1
        elif mode == "WALL":
            for chain in simple:
                if self.surface_overrides:
                    chain = dict(chain)
                    chain["points"] = [
                        Vector((p.x, p.y, self.surface_overrides[index]))
                        if index in self.surface_overrides else p
                        for index, p in zip(chain["indices"], chain["points"])
                    ]
                t, l, caps = _wall_preview(chain, self.definition, self.path)
                triangles.extend(t); lines.extend(l); cap_triangles.extend(caps)
            status = "WALL · VARIABLE Z" if self.surface_overrides else ("WALL LIVE" if simple else "PATH INVALID")
        elif mode == "SURFACE":
            closed = [c for c in simple if c["closed"]]
            for chain in closed:
                if self.surface_overrides:
                    # Patch only the cached world positions, never the original
                    # analysis or the source mesh; cheap during height drag.
                    chain = dict(chain)
                    chain["points"] = [
                        Vector((p.x, p.y, self.surface_overrides[index]))
                        if index in self.surface_overrides else p
                        for index, p in zip(chain["indices"], chain["points"])
                    ]
                t, l = _surface_preview(chain); triangles.extend(t); lines.extend(l)
                points.extend(chain["points"][:-1] if chain["closed"] else chain["points"])
            status = "SURFACE LIVE" if closed else "CLOSED PATH REQUIRED"
        elif mode == "CORNICE":
            for chain in simple:
                p = chain["points"]
                lines.extend((a, b) for a, b in zip(p, p[1:]))
                points.extend(p[:-1] if chain["closed"] else p)
            status = "CORNICE PATH" if simple else "PATH INVALID"
        else:
            for chain in simple:
                p = chain["points"]
                lines.extend((a, b) for a, b in zip(p, p[1:]))
                points.extend(p[:-1] if chain["closed"] else p)
            status = "PATH READY" if simple else "PATH INVALID"
        finished = perf_counter()
        self.geometry = PreviewGeometry(
            triangles=triangles, lines=lines, points=points, seam_points=seam_points,
            cap_triangles=cap_triangles, status=status, quality=requested_quality, build_ms=(finished-started)*1000.0,
        )
        self.last_build_at = finished
        PERFORMANCE_REGISTRY.add("PREVIEW_UPDATE",finished-started)
        self.dirty = Dirty.NONE
        if requested_quality == "SETTLED":
            self.needs_settled = False
        return self.geometry
