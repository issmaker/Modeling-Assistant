"""Apply native Blender Smooth by Angle without external asset files.

The shading operation is transactional: preserve authored sharp edges and UVs,
verify topology, and restore shading state on failure. No GN modifier or
Asset Browser dependency is needed for Blender 4.5+ / 5.2.
"""
import math
import bpy


def apply_angle_smoothing(context, obj, angle):
    if obj is None or obj.type != "MESH" or context.mode != "OBJECT":
        raise ValueError("Для сглаживания выберите Mesh в Object Mode")
    if obj.data.users > 1:
        raise ValueError("Сначала сделайте Mesh Single User")

    angle = float(angle)
    if not math.isfinite(angle) or not (0.0 <= angle <= math.pi):
        raise ValueError("Угол сглаживания должен быть от 0° до 180°")

    mesh = obj.data
    if not len(mesh.polygons):
        raise ValueError("Нельзя сгладить пустой Mesh")
    if mesh.has_custom_normals:
        raise ValueError("Обнаружены пользовательские нормали: автоматическое сглаживание остановлено, чтобы не потерять их")

    counts_before = (len(mesh.vertices), len(mesh.edges),
                     len(mesh.polygons), len(mesh.loops))
    sharp_before = tuple(bool(e.use_edge_sharp) for e in mesh.edges)
    smooth_before = tuple(bool(f.use_smooth) for f in mesh.polygons)
    uv_before = {layer.name: tuple(tuple(item.uv) for item in layer.data)
                 for layer in mesh.uv_layers}
    modifiers_before = tuple(m.name for m in obj.modifiers)
    had_angle = "issmaker_smooth_angle" in obj
    old_angle = obj.get("issmaker_smooth_angle")

    try:
        # In Blender 5.2 this is a native edge-sharpness operation, NOT a
        # Geometry Nodes modifier. Keep hand-authored Sharp edges intact.
        with context.temp_override(
            object=obj, active_object=obj,
            selected_objects=[obj], selected_editable_objects=[obj],
        ):
            if not bpy.ops.object.shade_smooth_by_angle.poll():
                raise RuntimeError("Штатное сглаживание Blender сейчас недоступно")
            result = bpy.ops.object.shade_smooth_by_angle(
                angle=angle, keep_sharp_edges=True,
            )
        if "FINISHED" not in result:
            raise RuntimeError("Blender не применил Smooth by Angle")

        if mesh is not obj.data or counts_before != (
            len(mesh.vertices), len(mesh.edges), len(mesh.polygons), len(mesh.loops)
        ):
            raise RuntimeError("Сглаживание неожиданно изменило структуру Mesh")
        if tuple(m.name for m in obj.modifiers) != modifiers_before:
            raise RuntimeError("Сглаживание неожиданно изменило модификаторы")
        if any(original and not edge.use_edge_sharp
               for original, edge in zip(sharp_before, mesh.edges)):
            raise RuntimeError("Сглаживание изменило исходные Sharp-рёбра")

        if set(uv_before) != {layer.name for layer in mesh.uv_layers}:
            raise RuntimeError("Сглаживание изменило список UV-слоёв")
        for layer in mesh.uv_layers:
            if uv_before[layer.name] != tuple(tuple(item.uv) for item in layer.data):
                raise RuntimeError("Сглаживание изменило UV-координаты")

        obj["issmaker_smooth_angle"] = angle
        mesh.update()
    except Exception:
        # Restore all shading flags and UV loops if the native operation or a
        # postcondition fails. Never detach or finalize the owner's result here.
        if obj.data is mesh:
            for edge, old in zip(mesh.edges, sharp_before):
                edge.use_edge_sharp = old
            for face, old in zip(mesh.polygons, smooth_before):
                face.use_smooth = old
            for name, coords in uv_before.items():
                layer = mesh.uv_layers.get(name)
                if layer is not None and len(layer.data) == len(coords):
                    layer.data.foreach_set(
                        "uv", [component for uv in coords for component in uv]
                    )
            mesh.update()
        if had_angle:
            obj["issmaker_smooth_angle"] = old_angle
        elif "issmaker_smooth_angle" in obj:
            del obj["issmaker_smooth_angle"]
        raise


def apply_flat_shading(context,obj):
    """Uniform Flat shading on any local single-user Mesh, with full rollback.

    Unlike the UV Wall-only shading operator, this is safe on arbitrary meshes.
    Face smoothing flags change; Sharp edges, UV loops and topology do not.
    """
    if obj is None or obj.type!="MESH" or context.mode!="OBJECT":
        raise ValueError("Для Flat выберите Mesh в Object Mode")
    mesh=obj.data
    if mesh.users!=1 or obj.library is not None or mesh.library is not None:
        raise ValueError("Нужен локальный Mesh Single User")
    if not mesh.polygons:
        raise ValueError("Нельзя применить Flat к пустому Mesh")
    before=(len(mesh.vertices),len(mesh.edges),len(mesh.polygons),len(mesh.loops))
    smoothing=tuple(face.use_smooth for face in mesh.polygons)
    sharp=tuple(edge.use_edge_sharp for edge in mesh.edges)
    uvs={layer.name:tuple(tuple(item.uv) for item in layer.data)
         for layer in mesh.uv_layers}
    try:
        for face in mesh.polygons:
            face.use_smooth=False
        mesh.update()
        after=(len(mesh.vertices),len(mesh.edges),len(mesh.polygons),len(mesh.loops))
        if after!=before:
            raise RuntimeError("Flat изменил топологию Mesh")
        if any(old!=edge.use_edge_sharp for old,edge in zip(sharp,mesh.edges)):
            raise RuntimeError("Flat изменил Sharp-рёбра")
        for layer in mesh.uv_layers:
            if uvs.get(layer.name)!=tuple(tuple(item.uv) for item in layer.data):
                raise RuntimeError("Flat изменил UV-развёртку")
    except Exception:
        if obj.data is mesh:
            for face,old in zip(mesh.polygons,smoothing):
                face.use_smooth=old
            mesh.update()
        raise
