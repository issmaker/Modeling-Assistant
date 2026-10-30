"""Shared resource-selection model for the NEXTGEN 1.6.4 viewport workspace.

The picker deliberately owns no draw handler, timer, GPU object or Blender
datablock.  The active viewport controller owns one short-lived instance and
discards it at the same lifecycle boundary as the rest of the modal UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from ..core.material_intelligence import (
    inspect_material_asset,
    material_image_candidates,
    set_active_definition_asset,
)


KINDS = ("PATH", "PROFILE", "MATERIAL")
LABELS = {
    "PATH": "Путь",
    "PROFILE": "Профиль",
    "MATERIAL": "Материал",
}


def _safe_name(item):
    return str(getattr(item, "name", "") or "")


def _mesh_profile_summary(obj, tolerance=0.002):
    """Return (valid, reason, metadata) without logging or mutating the mesh."""
    if obj is None:
        return False, "Профиль не выбран", ""
    if getattr(obj, "type", None) != "MESH":
        return False, "Профиль должен быть Mesh-линией", getattr(obj, "type", "Object")
    mesh = getattr(obj, "data", None)
    if mesh is None or not getattr(mesh, "edges", None):
        return False, "Профиль должен содержать рёбра", "Mesh · без рёбер"
    if len(getattr(mesh, "polygons", ())) != 0:
        return False, "Профиль должен быть линией без полигонов", "Mesh · содержит грани"
    adjacency = {i: [] for i in range(len(mesh.vertices))}
    for edge in mesh.edges:
        a, b = map(int, edge.vertices)
        if a != b:
            adjacency[a].append(b)
            adjacency[b].append(a)
    used = {i for i, linked in adjacency.items() if linked}
    if not used:
        return False, "Профиль должен содержать хотя бы одно ребро", "Mesh · пустой"
    if set(adjacency) - used:
        return False, "В профиле есть изолированные вершины", "Mesh · invalid"
    degrees = [len(adjacency[i]) for i in used]
    if any(value > 2 for value in degrees):
        return False, "Профиль содержит ветвление", "Mesh · branched"
    endpoints = sum(value == 1 for value in degrees)
    if endpoints not in {0, 2}:
        return False, "Нужна одна открытая или замкнутая цепь", "Mesh · disconnected"
    seen = set()
    stack = [next(iter(used))]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency[current])
    if seen != used:
        return False, "Профиль содержит несколько цепей", "Mesh · disconnected"
    points = [mesh.vertices[i].co for i in used]
    y_span = max(float(p.y) for p in points) - min(float(p.y) for p in points)
    if y_span > tolerance:
        return False, "Профиль должен лежать в локальной плоскости XZ", f"Y span {y_span:.4f} m"
    closed = endpoints == 0
    minimum = 3 if closed else 2
    if len(used) < minimum:
        return False, f"Нужно минимум {minimum} вершины", "Mesh · too small"
    width = max(float(p.x) for p in points) - min(float(p.x) for p in points)
    height = max(float(p.z) for p in points) - min(float(p.z) for p in points)
    if min((float(p.length) for p in points), default=1.0e9) > tolerance:
        return False, "Origin должен касаться вершины профиля", f"{width:.2f} × {height:.2f} m"
    topology = "Closed" if closed else "Open"
    return True, "Готов", f"Mesh · {topology} · {len(used)} точек · {width:.2f} × {height:.2f} m"


def validate_resource(kind, item, *, path=None, definition=None):
    """Unified validation contract used by dropdown, selected and viewport pick."""
    kind = str(kind).upper()
    if kind not in KINDS:
        return False, "Неизвестный тип ресурса", ""
    if item is None:
        return False, f"{LABELS[kind]} не выбран", ""
    if kind == "PATH":
        if getattr(item, "type", None) != "MESH":
            return False, "Путь должен быть Mesh-линией", getattr(item, "type", "Object")
        mesh = getattr(item, "data", None)
        if mesh is None or len(getattr(mesh, "edges", ())) == 0:
            return False, "Путь должен содержать рёбра", "Mesh · без рёбер"
        if len(getattr(mesh, "polygons", ())) != 0:
            return False, "Путь должен быть линией без полигонов", "Mesh · содержит грани"
        if definition is not None and _safe_name(item) == str(getattr(definition, "last_result_name", "")):
            return False, "Result нельзя использовать как Source Path", "Generated Result"
        return True, "Готов", f"Mesh Path · {len(mesh.vertices)} точек · {len(mesh.edges)} рёбер"
    if kind == "PROFILE":
        if item is path:
            return False, "Путь и профиль должны быть разными объектами", "Path"
        return _mesh_profile_summary(item)
    if kind == "MATERIAL":
        from ..core.material_intelligence import material_module
        module, image, reason = material_module(item)
        return module is not None, ("Готов" if module else reason), reason
    return False, "Недопустимый ресурс", ""


def resources_for(kind, bpy_module, *, path=None, definition=None, include_invalid=False):
    """Return stable recommended-first rows; opening it never runs a build/BVH."""
    kind = str(kind).upper()
    if kind in {"PATH", "PROFILE"}:
        source = tuple(getattr(bpy_module.data, "objects", ()))
    elif kind == "MATERIAL":
        source = tuple(getattr(bpy_module.data, "materials", ()))
    else:
        source = ()
    rows = []
    for item in source:
        valid, reason, meta = validate_resource(kind, item, path=path, definition=definition)
        if valid or include_invalid:
            rows.append((item, valid, reason, meta))
    rows.sort(key=lambda row: (not row[1], _safe_name(row[0]).casefold()))
    return rows


def filter_rows(rows, query):
    needle = str(query or "").strip().casefold()
    if not needle:
        return list(rows)
    return [row for row in rows if needle in _safe_name(row[0]).casefold()
            or needle in str(row[3]).casefold()]


@dataclass
class SmartResourcePicker:
    """Ephemeral interaction state; authoritative values remain RNA pointers."""
    open_kind: str = ""
    query: str = ""
    active_index: int = 0
    viewport_kind: str = ""
    hovered_name: str = ""
    message: str = ""
    message_level: str = "INFO"
    include_invalid: bool = True
    hitboxes: list = field(default_factory=list)
    recent: dict = field(default_factory=lambda: {kind: [] for kind in KINDS})
    cache: dict = field(default_factory=dict)
    cache_ttl: float = 0.75
    pick_state: str = "IDLE"

    def reset(self):
        self.open_kind = ""
        self.query = ""
        self.active_index = 0
        self.viewport_kind = ""
        self.hovered_name = ""
        self.message = ""
        self.pick_state = "IDLE"
        self.hitboxes.clear()
        self.cache.clear()

    def close_dropdown(self):
        self.open_kind = ""
        self.query = ""
        self.active_index = 0

    def open(self, kind):
        kind = str(kind).upper()
        if kind not in KINDS:
            return False
        self.open_kind = kind
        self.viewport_kind = ""
        self.query = ""
        self.active_index = 0
        self.message = "Введите имя или выберите из списка"
        self.message_level = "INFO"
        return True

    def begin_viewport(self, kind):
        kind = str(kind).upper()
        if kind not in {"PATH", "PROFILE"}:
            return False
        self.close_dropdown()
        self.viewport_kind = kind
        self.pick_state = "PICK"
        self.hovered_name = ""
        self.message = f"{LABELS[kind]} · кликните объект · Esc отмена"
        self.message_level = "INFO"
        return True

    def cancel_viewport(self):
        self.viewport_kind = ""
        self.pick_state = "CANCELLED"
        self.hovered_name = ""
        self.message = "Выбор отменён"
        self.message_level = "INFO"

    def remember(self, kind, item):
        name = _safe_name(item)
        if not name:
            return
        try:
            pointer = int(item.as_pointer())
        except Exception:
            pointer = 0
        record = (pointer, name)
        bucket = self.recent.setdefault(str(kind).upper(), [])
        bucket[:] = [existing for existing in bucket if existing != record and existing[1] != name]
        bucket.insert(0, record)
        del bucket[5:]

    def invalidate(self, kind=None):
        if kind is None:
            self.cache.clear()
        else:
            self.cache.pop(str(kind).upper(), None)

    def rows(self, kind, bpy_module, *, path=None, definition=None):
        """Short-lived cache: no full datablock scan on every draw frame."""
        kind = str(kind).upper()
        now = monotonic()
        collection = {"PATH": "objects", "PROFILE": "objects",
                      "MATERIAL": "materials"}.get(kind)
        source = getattr(bpy_module.data, collection, ()) if collection else ()
        signature = (bool(self.include_invalid),
                     str(getattr(definition, "module_standard", "4")),
                     int(getattr(definition, "revision", 0) or 0),
                     _safe_name(path), len(source))
        cached = self.cache.get(kind)
        if cached and cached[1] == signature and now - cached[0] <= self.cache_ttl:
            rows = cached[2]
        else:
            rows = resources_for(
                kind, bpy_module, path=path, definition=definition,
                include_invalid=bool(self.include_invalid),
            )
            self.cache[kind] = (now, signature, rows)
        rows = list(rows)
        current = current_resource(kind, path, definition)
        recent = self.recent.get(kind, ())
        recent_order = {pointer: index for index, (pointer, _name) in enumerate(recent) if pointer}
        def priority(row):
            item = row[0]
            if item is current:
                return (0, 0)
            try:
                pointer = int(item.as_pointer())
            except Exception:
                pointer = 0
            return (1, recent_order[pointer]) if pointer in recent_order else (2, 0)
        rows.sort(key=lambda row: (priority(row), not row[1], _safe_name(row[0]).casefold()))
        return rows


def current_resource(kind, path, definition):
    kind = str(kind).upper()
    if kind == "PATH":
        return path
    if definition is None:
        return None
    if kind == "PROFILE":
        return getattr(definition, "cornice_profile", None)
    is_2k = str(getattr(definition, "module_standard", "4")) == "2"
    if kind == "MATERIAL":
        return getattr(definition, "virtual_material_2k" if is_2k else "virtual_material_4k", None)
    return None


def assign_resource(kind, item, definition):
    """Assign one Definition pointer. PATH is rebound by the controller."""
    kind = str(kind).upper()
    if definition is None or kind == "PATH":
        return False
    if kind == "PROFILE":
        definition.cornice_profile = item
        definition.revision += 1
        return True
    if kind == "MATERIAL":
        return set_active_definition_asset(definition, material=item)
    return False
