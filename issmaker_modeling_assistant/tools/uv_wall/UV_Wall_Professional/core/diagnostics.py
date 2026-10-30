"""Result diagnostics kept outside the production-generator monolith."""
from __future__ import annotations

import bmesh

from .semantic_geometry import (
    role_counts, expected_component_count, face_role_codes, face_role_name,
)


def _format_vector(vector):
    return f"({vector.x:.9f}, {vector.y:.9f}, {vector.z:.9f})"


def log_result_snapshot(obj, elapsed_seconds, detail, log_event, log_bulk):
    """Log authoritative post-cleanup result metrics without flooding the .blend.

    NORMAL is intentionally compact. DEEP includes representative geometry.
    FORENSIC keeps the historical large dump for explicit reproduction work.
    """
    try:
        mesh = obj.data
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            bm.normal_update()
            boundary_records = [
                (edge.index, edge.verts[0].co.copy(), edge.verts[1].co.copy(), edge.calc_length())
                for edge in bm.edges if len(edge.link_faces) == 1
            ]
            boundary = len(boundary_records)
            wire = sum(1 for edge in bm.edges if len(edge.link_faces) == 0)
            multi_face_edges = sum(1 for edge in bm.edges if len(edge.link_faces) > 2)
            invalid_manifold = wire + multi_face_edges
            degenerate = sum(1 for face in bm.faces if face.calc_area() <= 1.0e-12)
            remaining_faces = set(bm.faces)
            component_sizes = []
            while remaining_faces:
                seed = remaining_faces.pop()
                stack = [seed]
                size = 0
                while stack:
                    face = stack.pop()
                    size += 1
                    for edge in face.edges:
                        for neighbor in edge.link_faces:
                            if neighbor in remaining_faces:
                                remaining_faces.remove(neighbor)
                                stack.append(neighbor)
                component_sizes.append(size)
        finally:
            bm.free()

        normals_up = sum(1 for polygon in mesh.polygons if polygon.normal.z > 1.0e-6)
        normals_down = sum(1 for polygon in mesh.polygons if polygon.normal.z < -1.0e-6)
        normals_side = len(mesh.polygons) - normals_up - normals_down
        roles = role_counts(mesh)
        expected_components, component_reason = expected_component_count(mesh)
        component_status = (
            "UNKNOWN" if expected_components is None
            else ("OK" if len(component_sizes) == expected_components else "MISMATCH")
        )

        uv_layer = mesh.uv_layers.active
        uv_min_u = uv_min_v = float("inf")
        uv_max_u = uv_max_v = float("-inf")
        uv_degenerate = 0
        if uv_layer is not None:
            for loop in uv_layer.data:
                u, v = loop.uv
                uv_min_u, uv_max_u = min(uv_min_u, u), max(uv_max_u, u)
                uv_min_v, uv_max_v = min(uv_min_v, v), max(uv_max_v, v)
            for polygon in mesh.polygons:
                uvs = [uv_layer.data[index].uv for index in polygon.loop_indices]
                area = 0.5 * sum(a.x * b.y - b.x * a.y for a, b in zip(uvs, uvs[1:] + uvs[:1]))
                if abs(area) <= 1.0e-12:
                    uv_degenerate += 1

        log_event(
            f"RESULT name={obj.name}; elapsed={elapsed_seconds:.6f}s; "
            f"verts={len(mesh.vertices)}; edges={len(mesh.edges)}; faces={len(mesh.polygons)}; "
            f"open_boundary_edges={boundary}; invalid_manifold_edges={invalid_manifold}; "
            f"multi_face_edges={multi_face_edges}; wire_edges={wire}; "
            f"degenerate_faces={degenerate}; normals_up={normals_up}; "
            f"normals_down={normals_down}; normals_side={normals_side}; "
            f"face_components={len(component_sizes)}; expected_components={expected_components}; "
            f"component_status={component_status}; component_reason={component_reason}; "
            f"component_sizes={sorted(component_sizes, reverse=True)[:20]}; roles={roles}",
            "RESULT",
        )
        log_event(
            f"RESULT_NORMAL_INTEGRITY {obj.get('uvwall_normal_integrity', 'NOT_AVAILABLE')}",
            "NORMAL_CHECK",
        )
        log_event(
            f"RESULT_UV layer={uv_layer.name if uv_layer else 'NONE'}; "
            f"bounds=({uv_min_u:.9f}, {uv_min_v:.9f})..({uv_max_u:.9f}, {uv_max_v:.9f}); "
            f"degenerate_uv_faces={uv_degenerate}",
            "RESULT",
        )

        detail = str(detail or "NORMAL").upper()
        sample_limit = 12 if detail == "NORMAL" else (256 if detail == "DEEP" else 20_000)
        boundary_lines = [
            f"BOUNDARY_SAMPLE count={len(boundary_records)}; logged={min(len(boundary_records), sample_limit)}"
        ]
        boundary_lines.extend(
            f"BE {index}: {_format_vector(start)} -> {_format_vector(end)}; length={length:.9f}"
            for index, start, end, length in boundary_records[:sample_limit]
        )
        log_bulk(boundary_lines, "BOUNDARY")

        if detail in {"DEEP", "FORENSIC"}:
            vertex_limit = min(len(mesh.vertices), sample_limit)
            vertex_lines = [f"RESULT_VERTICES_BEGIN count={len(mesh.vertices)}; logged={vertex_limit}"]
            vertex_lines.extend(
                f"RV {vertex.index}: {_format_vector(vertex.co)}"
                for vertex in list(mesh.vertices)[:vertex_limit]
            )
            vertex_lines.append("RESULT_VERTICES_END")
            log_bulk(vertex_lines, "RESULT_GEOMETRY")

            face_limit = min(len(mesh.polygons), sample_limit)
            role_values = face_role_codes(mesh)
            face_lines = [f"RESULT_FACES_BEGIN count={len(mesh.polygons)}; logged={face_limit}"]
            face_lines.extend(
                f"RF {polygon.index}: role={face_role_name(role_values[polygon.index])}; "
                f"verts={list(polygon.vertices)}; normal={_format_vector(polygon.normal)}; "
                f"area={polygon.area:.12f}; material={polygon.material_index}"
                for polygon in list(mesh.polygons)[:face_limit]
            )
            face_lines.append("RESULT_FACES_END")
            log_bulk(face_lines, "RESULT_GEOMETRY")

        log_event(
            f"RESULT_LOG detail={detail}; geometry_dump={'ON' if detail in {'DEEP', 'FORENSIC'} else 'OFF'}",
            "RESULT",
        )
        log_event("=" * 72, "RESULT")
    except Exception as error:
        log_event(f"Не удалось полностью записать паспорт результата: {error}", "LOG_WARNING")
