"""Deduplicated non-destructive Problem Registry for UV Wall NEXTGEN 1.6.4."""
from __future__ import annotations
import hashlib
import json
import math
from datetime import datetime, timezone


def _definition_uid(settings):
    try:
        definition = settings.path_mesh.uvwall_definition
        return str(definition.source_uid or "")
    except Exception:
        return ""


def _stable_problem_id(settings, subsystem, code, object_name, element_kind, element_indices):
    payload = "|".join((
        _definition_uid(settings), str(subsystem), str(code or "GENERIC"),
        str(object_name), str(element_kind), str(element_indices),
    ))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def active_problem_count(settings):
    """Number of unique active records, never the number of repeated log lines."""
    return sum(1 for item in getattr(settings, "problems", ())
               if str(getattr(item, "state", "ACTIVE")) == "ACTIVE")


def clear_problems(settings, subsystem=None):
    collection = getattr(settings, "problems", None)
    if collection is None:
        return 0
    removed = 0
    for index in range(len(collection) - 1, -1, -1):
        item = collection[index]
        if subsystem is None or str(getattr(item, "subsystem", "")) == str(subsystem):
            collection.remove(index)
            removed += 1
    try:
        settings.active_problem_index = min(max(0, settings.active_problem_index), max(0, len(collection)-1))
    except Exception:
        pass
    return removed


def add_problem(settings, *, subsystem, severity, title, description="", object_name="",
                world_position=None, radius=0.5, element_kind="", element_indices="",
                suggested_action="", category="RECOVERABLE_WARNING", code="",
                occurrence_count=1, definition_uid=""):
    collection = getattr(settings, "problems", None)
    if collection is None:
        return None
    problem_id = _stable_problem_id(
        settings, subsystem, code or title, object_name, element_kind, element_indices
    )
    item = next((candidate for candidate in collection
                 if str(getattr(candidate, "problem_id", "")) == problem_id), None)
    created = item is None
    if item is None:
        item = collection.add()
    item.problem_id = problem_id
    item.problem_code = str(code or title).upper().replace(" ", "_")[:64]
    item.definition_uid = str(definition_uid or _definition_uid(settings))
    item.subsystem = str(subsystem)
    item.category = str(category)
    item.severity = str(severity)
    item.state = "ACTIVE"
    item.title = str(title)
    item.description = str(description)
    item.object_name = str(object_name)
    item.radius = max(0.05, float(radius))
    item.element_kind = str(element_kind)
    item.element_indices = str(element_indices)
    item.suggested_action = str(suggested_action)
    item.occurrence_count = max(1, int(occurrence_count)) if created else max(
        1, int(getattr(item, "occurrence_count", 1)) + int(occurrence_count)
    )
    item.updated_utc = datetime.now(timezone.utc).isoformat()
    if world_position is not None:
        try:
            item.world_position = tuple(float(v) for v in world_position[:3])
        except Exception:
            pass
    return item


def active_problem(settings):
    collection = getattr(settings, "problems", None)
    if not collection:
        return None
    index = int(getattr(settings, "active_problem_index", 0))
    index = max(0, min(index, len(collection)-1))
    try:
        settings.active_problem_index = index
    except Exception:
        pass
    return collection[index]


def navigate_problem(settings, delta):
    collection = getattr(settings, "problems", None)
    if not collection:
        return None
    current = int(getattr(settings, "active_problem_index", 0))
    settings.active_problem_index = (current + int(delta)) % len(collection)
    return collection[settings.active_problem_index]


def serialize_problems(settings):
    result = []
    for item in getattr(settings, "problems", ()):
        result.append({
            "id": item.problem_id,
            "subsystem": item.subsystem,
            "category": getattr(item, "category", "RECOVERABLE_WARNING"),
            "code": getattr(item, "problem_code", ""),
            "state": getattr(item, "state", "ACTIVE"),
            "definition_uid": getattr(item, "definition_uid", ""),
            "occurrence_count": int(getattr(item, "occurrence_count", 1)),
            "severity": item.severity,
            "title": item.title,
            "description": item.description,
            "object_name": item.object_name,
            "world_position": list(item.world_position),
            "radius": float(item.radius),
            "element_kind": item.element_kind,
            "element_indices": item.element_indices,
            "suggested_action": item.suggested_action,
        })
    return result


def _face_center_world(obj, polygon):
    matrix = obj.matrix_world
    if not polygon.vertices:
        return (0.0, 0.0, 0.0)
    x = y = z = 0.0
    for vi in polygon.vertices:
        p = matrix @ obj.data.vertices[vi].co
        x += p.x; y += p.y; z += p.z
    n = float(len(polygon.vertices))
    return (x/n, y/n, z/n)


def scan_result_mesh(settings, obj, *, max_pairs=128, max_faces=50000):
    """Find actionable topology/intersection candidates after a successful build.

    This is warning-level diagnostics. Production generators remain authoritative;
    no geometry is modified here.
    """
    clear_problems(settings, "GEOMETRY")
    if obj is None or getattr(obj, "type", None) != "MESH":
        return 0
    mesh = obj.data
    count = 0

    # Cheap deterministic topology checks first.
    for edge in mesh.edges:
        linked = 0
        try:
            linked = len(edge.link_faces)  # offline/future adapters
        except Exception:
            linked = 0
        # Blender MeshEdge has no link_faces; manifold details are covered below
        # by polygon edge usage counts.

    edge_use = {}
    for poly in mesh.polygons:
        verts = list(poly.vertices)
        if len(verts) < 3:
            add_problem(settings, subsystem="GEOMETRY", severity="ERROR",
                        title="Вырожденная грань", object_name=obj.name,
                        world_position=_face_center_world(obj, poly),
                        element_kind="FACE", element_indices=str(poly.index))
            count += 1
            continue
        for a, b in zip(verts, verts[1:] + verts[:1]):
            key = (min(a,b), max(a,b))
            edge_use.setdefault(key, []).append(poly.index)
    for key, faces in edge_use.items():
        if len(faces) > 2:
            a = obj.matrix_world @ mesh.vertices[key[0]].co
            b = obj.matrix_world @ mesh.vertices[key[1]].co
            center = (a+b)*0.5
            add_problem(settings, subsystem="GEOMETRY", severity="ERROR",
                        title="Non-manifold ребро", description=f"Граней на ребре: {len(faces)}",
                        object_name=obj.name, world_position=center, radius=max(0.15,(b-a).length*0.5),
                        element_kind="EDGE", element_indices=f"{key[0]},{key[1]}",
                        suggested_action="Проверьте локальную топологию и пересечение")
            count += 1
            if count >= max_pairs:
                return count

    if len(mesh.polygons) > max_faces:
        add_problem(settings, subsystem="GEOMETRY", severity="WARNING",
                    title="Глубокая проверка пересечений пропущена",
                    description=f"Слишком много граней для интерактивного сканирования: {len(mesh.polygons)}",
                    object_name=obj.name, world_position=obj.matrix_world.translation,
                    suggested_action="Запустите Deep diagnostics")
        return count + 1

    # BVH overlap for non-neighbouring polygons. False positives are possible on
    # coincident coplanar surfaces, so entries are called candidates and remain
    # warnings until the user inspects them in the navigator.
    try:
        from mathutils.bvhtree import BVHTree
        verts = [tuple(obj.matrix_world @ v.co) for v in mesh.vertices]
        polys = [tuple(p.vertices) for p in mesh.polygons]
        bvh = BVHTree.FromPolygons(verts, polys, all_triangles=False)
        overlaps = bvh.overlap(bvh)
        seen = set()
        for a, b in overlaps:
            if a == b:
                continue
            pair = (min(int(a), int(b)), max(int(a), int(b)))
            if pair in seen:
                continue
            seen.add(pair)
            pa, pb = mesh.polygons[pair[0]], mesh.polygons[pair[1]]
            if set(pa.vertices) & set(pb.vertices):
                continue
            ca = _face_center_world(obj, pa); cb = _face_center_world(obj, pb)
            center = tuple((ca[i]+cb[i])*0.5 for i in range(3))
            add_problem(settings, subsystem="GEOMETRY", severity="WARNING",
                        title="Возможное пересечение геометрии",
                        description=f"Грани {pair[0]} и {pair[1]}", object_name=obj.name,
                        world_position=center, radius=0.6, element_kind="FACE_PAIR",
                        element_indices=f"{pair[0]},{pair[1]}",
                        suggested_action="Перейдите к проблеме и осмотрите пересечение")
            count += 1
            if count >= max_pairs:
                break
    except Exception:
        pass
    return count



def scan_result_uv_quality(settings, obj, *, stretch_ratio=4.0, max_items=32):
    """Interactive UV QA: missing/degenerate/flipped UV and relative stretch.

    This does not replace production validators; it turns their guarantees into
    user-visible Problem Inspector feedback, especially for SURFACE results.
    """
    clear_problems(settings, "UV")
    if obj is None or getattr(obj, "type", None) != "MESH":
        return 0
    mesh=obj.data; uv_layer=getattr(mesh.uv_layers,"active",None)
    if uv_layer is None:
        add_problem(settings,subsystem="UV",severity="ERROR",title="UV отсутствует",
                    description="У результата нет активной UV-карты",object_name=obj.name,
                    world_position=obj.matrix_world.translation,suggested_action="Перестройте результат или проверьте генератор UV")
        return 1
    records=[]; count=0
    for poly in mesh.polygons:
        uvs=[uv_layer.data[i].uv for i in poly.loop_indices]
        area=.5*sum(a.x*b.y-b.x*a.y for a,b in zip(uvs,uvs[1:]+uvs[:1]))
        if abs(area)<=1e-12:
            add_problem(settings,subsystem="UV",severity="ERROR",title="Нулевая UV-площадь",
                        description=f"Polygon {poly.index}",object_name=obj.name,
                        world_position=_face_center_world(obj,poly),element_kind="FACE",element_indices=str(poly.index),
                        suggested_action="Перестройте UV этого участка")
            count+=1
        elif area<0:
            add_problem(settings,subsystem="UV",severity="ERROR",title="Flipped UV",
                        description=f"Polygon {poly.index} имеет зеркальную UV-ориентацию",object_name=obj.name,
                        world_position=_face_center_world(obj,poly),element_kind="FACE",element_indices=str(poly.index),
                        suggested_action="Исправьте генератор UV; production результат не должен содержать flipped islands")
            count+=1
        else:
            world_area=max(1e-12,float(getattr(poly,'area',0.0)))
            records.append((poly,world_area/max(abs(area),1e-12)))
        if count>=max_items:return count
    if len(records)>=4:
        ratios=sorted(value for _poly,value in records if value>0)
        median=ratios[len(ratios)//2] if ratios else 0.0
        if median>0:
            bad=[(poly,value) for poly,value in records if value/median>stretch_ratio or value/median<1.0/stretch_ratio]
            for poly,value in bad[:max(0,max_items-count)]:
                factor=max(value/median,median/value)
                add_problem(settings,subsystem="UV",severity="WARNING",title="UV stretch",
                            description=f"Polygon {poly.index} отличается от медианы примерно в {factor:.1f}×",
                            object_name=obj.name,world_position=_face_center_world(obj,poly),
                            element_kind="FACE",element_indices=str(poly.index),
                            suggested_action="Покажите участок и проверьте плотность/растяжение UV")
                count+=1
    return count

def scan_cornice_result_metadata(settings, obj):
    """Publish actionable Cornice corner/UV metadata into Problem Navigator."""
    clear_problems(settings, "CORNICE_UV")
    clear_problems(settings, "CORNICE_CORNER")
    if obj is None or getattr(obj, "type", None) != "MESH":
        return 0
    count = 0
    try:
        corner_records = json.loads(obj.get("cornice_corner_problem_records", "[]") or "[]")
    except Exception:
        corner_records = []
    if corner_records:
        worst = max(corner_records, key=lambda record: float(record.get("miter_ratio", 1.0)))
        indices = sorted({int(record.get("source_vertex_index", record.get("vertex", -1)))
                          for record in corner_records})
        strategy = str(worst.get("strategy", "AUTO"))
        add_problem(
            settings, subsystem="CORNICE_CORNER", severity="WARNING",
            title="Углы Cornice требуют внимания",
            description=(
                f"Участков: {len(corner_records)} · худший поворот "
                f"{float(worst.get('turn_deg', 0.0)):.1f}° · "
                f"miter {float(worst.get('miter_ratio', 1.0)):.2f}× · {strategy}"
            ),
            object_name=obj.name, world_position=worst.get("position", (0.0, 0.0, 0.0)),
            radius=0.65, element_kind="CORNICE_VERTEX_GROUP",
            element_indices=",".join(map(str, indices)),
            suggested_action="Проверьте угол или сделайте его authored Sharp",
            category="RECOVERABLE_WARNING", code="CORNICE_CORNER_SAFEGUARD",
            occurrence_count=len(corner_records),
        )
        count += 1
    try:
        distortion_records = json.loads(obj.get("cornice_uv_distortion_records", "[]") or "[]")
    except Exception:
        distortion_records = []
    # Two module intervals can touch the same corner. Present one actionable
    # item per authored vertex using the worst predicted distortion.
    distortion_by_vertex = {}
    for record in distortion_records:
        source_index = int(record.get("source_vertex_index", -1))
        previous = distortion_by_vertex.get(source_index)
        if previous is None or float(record.get("relative_error", 0.0)) > float(previous.get("relative_error", 0.0)):
            distortion_by_vertex[source_index] = record
    if distortion_by_vertex:
        source_index, record = max(
            distortion_by_vertex.items(), key=lambda pair: float(pair[1].get("relative_error", 0.0))
        )
        error = float(record.get("relative_error", 0.0))
        indices = sorted(distortion_by_vertex)
        add_problem(
            settings, subsystem="CORNICE_UV", severity="WARNING",
            title="UV distortion Cornice",
            description=(
                f"Участков: {len(distortion_by_vertex)} · худшее растяжение {error*100.0:.1f}% · "
                f"вершина {source_index} · интервал "
                f"{float(record.get('start_distance',0.0)):.2f}–{float(record.get('end_distance',0.0)):.2f} м"
            ),
            object_name=obj.name, world_position=record.get("position", (0.0, 0.0, 0.0)),
            radius=0.7, element_kind="CORNICE_VERTEX_GROUP",
            element_indices=",".join(map(str, indices)),
            suggested_action="Сделать этот угол Sharp — будет создан локальный UV seam",
            category="RECOVERABLE_WARNING", code="CORNICE_UV_DISTORTION",
            occurrence_count=len(distortion_by_vertex),
        )
        count += 1
    return count


def explain_build_failure(mode, message):
    """Translate engine/Blender failures into a useful user action and subsystem.

    Original exception remains intact for support logs. This is presentation
    metadata, not a replacement for the strict build validation.
    """
    mode=str(mode or "WALL").upper()
    name={"WALL":"Стена","SURFACE":"Поверхность","CORNICE":"Карниз"}.get(mode,mode)
    raw=str(message or "").strip()
    lowered=raw.lower()
    if "самопересеч" in lowered or "self-intersect" in lowered:
        return name,"Обнаружены пересекающиеся грани или участки пути", "Нажмите «Показать». Проверьте изгиб и размеры профиля в отмеченной области. Измените профиль или форму пути"
    if "t-junction" in lowered or "manifold" in lowered:
        return name,"Несовместимая топология в соединении модулей", "Проверьте соседние ребра и разрезы рядом с ошибкой. Исходный путь и последний рабочий результат сохранены"
    if "corner" in lowered or "угол" in lowered or "sharp" in lowered:
        return name,"Поворот пути требует проверки геометрии", "Для первого построения выберите «Плавные». Для точной подрезки переключитесь на «Острые» и укажите вершины Sharp"
    if "profile" in lowered or "профил" in lowered:
        return name,"Некорректный или неназначенный профиль", "Выберите Mesh-профиль в локальной плоскости XZ; Origin должен касаться вершины профиля"
    if "uv" in lowered or "текстур" in lowered or "материал" in lowered:
        return name,"Требования UV или материала не выполнены", "Проверьте разрешение 2K/4K, UV-границы и материал в инспекторе ресурсов"
    return name, raw[:180] or "Не удалось выполнить построение", "Откройте подробную диагностику, проверьте исходный путь и повторите построение после исправления"
