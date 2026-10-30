"""Persistent diagnostics and one-click support export for UV Wall NEXTGEN.

The production logger writes both to Blender's Text datablock and to durable files.
This module deliberately stores only strings/JSON-safe metadata, never Blender IDs or
BMesh elements, so diagnostics cannot extend datablock lifetimes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import platform
import sys
import threading
import traceback as _traceback
import zipfile

try:
    import bpy  # type: ignore
except Exception:  # pragma: no cover - offline unit tests
    bpy = None

_LOCK = threading.RLock()
_SESSION_ID = ""
_SESSION_PATH: Path | None = None
_LATEST_PATH: Path | None = None
_CURRENT_STAGE = "IDLE"
_CURRENT_TOKEN = ""
_LAST_STATUS = "IDLE"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_directory() -> Path:
    """Stable user-writable log directory outside the add-on package."""
    if bpy is not None:
        try:
            value = bpy.utils.user_resource(
                "CONFIG", path="UV_Wall_Professional/logs", create=True
            )
            if value:
                path = Path(value)
                path.mkdir(parents=True, exist_ok=True)
                return path
        except Exception:
            pass
    # Offline/fallback location. In Blender this is normally never used.
    path = Path.home() / "UV_Wall_Professional" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def initialize_logging_session(force: bool = False) -> dict:
    global _SESSION_ID, _SESSION_PATH, _LATEST_PATH
    with _LOCK:
        if _SESSION_PATH is not None and not force:
            return paths_info()
        root = log_directory()
        _SESSION_ID = f"{_stamp()}_{os.getpid()}"
        _SESSION_PATH = root / f"uvwall-session-{_SESSION_ID}.log"
        _LATEST_PATH = root / "uvwall-latest.log"
        header = (
            "UV Wall Professional NEXTGEN diagnostic session\n"
            f"session_id={_SESSION_ID}\n"
            f"utc={_iso_utc()}\n"
            f"python={sys.version.replace(chr(10), ' ')}\n"
            f"platform={platform.platform()}\n"
            + (f"blender={getattr(bpy.app, 'version_string', 'UNKNOWN')}\n" if bpy is not None else "blender=OFFLINE\n")
            + "=" * 80 + "\n"
        )
        for path in (_SESSION_PATH, _LATEST_PATH):
            try:
                path.write_text(header, encoding="utf-8")
            except OSError:
                pass
        return paths_info()


def paths_info() -> dict:
    return {
        "session_id": _SESSION_ID,
        "log_dir": str(log_directory()),
        "latest_log": str(_LATEST_PATH or (log_directory() / "uvwall-latest.log")),
        "session_log": str(_SESSION_PATH or ""),
        "last_failure": str(log_directory() / "uvwall-last-failure.json"),
    }


def _ensure_session() -> None:
    if _SESSION_PATH is None or _LATEST_PATH is None:
        initialize_logging_session()


def append_text(payload: str) -> None:
    if not payload:
        return
    with _LOCK:
        _ensure_session()
        for path in (_SESSION_PATH, _LATEST_PATH):
            if path is None:
                continue
            try:
                with path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(payload)
            except OSError:
                pass


def set_build_runtime(stage: str | None = None, token: str | None = None, status: str | None = None) -> None:
    global _CURRENT_STAGE, _CURRENT_TOKEN, _LAST_STATUS
    with _LOCK:
        if stage is not None:
            _CURRENT_STAGE = str(stage)
        if token is not None:
            _CURRENT_TOKEN = str(token)
        if status is not None:
            _LAST_STATUS = str(status)


def build_runtime_state() -> dict:
    return {
        "stage": _CURRENT_STAGE,
        "transaction_token": _CURRENT_TOKEN,
        "status": _LAST_STATUS,
    }


def append_traceback(label: str, traceback_text: str, *, stage: str | None = None, token: str | None = None) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stage = stage or _CURRENT_STAGE
    token = token or _CURRENT_TOKEN
    payload = (
        f"[{stamp}] [TRACEBACK] {label}_BEGIN stage={stage}; token={token}\n"
        f"{traceback_text.rstrip()}\n"
        f"[{stamp}] [TRACEBACK] {label}_END stage={stage}; token={token}\n"
    )
    append_text(payload)
    if bpy is not None:
        try:
            text = bpy.data.texts.get("UV_Wall_Diagnostics") or bpy.data.texts.new("UV_Wall_Diagnostics")
            text.write(payload)
        except Exception:
            pass


def _safe_name(value):
    try:
        return value.name if value is not None else None
    except Exception:
        return None


def _safe_matrix(obj):
    try:
        return [[float(v) for v in row] for row in obj.matrix_world]
    except Exception:
        return None


def settings_snapshot(context) -> dict:
    if bpy is None or context is None:
        return {}
    try:
        settings = context.scene.uv_wall_settings
    except Exception:
        return {}
    fields = (
        "build_mode", "module_standard", "tile_length", "stories", "story_direction",
        "custom_first_story", "first_story_height", "create_inner", "inset_distance",
        "inner_wall_height_delta", "inner_wall_height_overrides_json", "secondary_points_json",
        "inset_side", "wall_alignment", "close_ends", "close_start_end", "close_finish_end",
        "keep_inner_path", "create_top_cap",
        "create_bottom_cap", "cap_rotation", "wall_module_rotation", "wall_uv_origin_mode",
        "wall_normal_mode", "wall_offset", "roof_normal", "surface_method",
        "surface_module_rotation", "surface_height_offset", "cornice_uv_mode",
        "cornice_corner_mode", "cornice_flip_side", "cornice_profile_rotation",
        "cornice_profile_flip_x", "cornice_profile_flip_y", "cornice_reverse_path",
        "cornice_linear_step", "cornice_smooth_warning_angle", "problem_auto_scan", "diagnostic_detail", "output_name",
    )
    result = {}
    for field in fields:
        try:
            value = getattr(settings, field)
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[field] = value
            else:
                result[field] = str(value)
        except Exception:
            continue
    for field in ("path_mesh", "source_plane", "cornice_profile"):
        try:
            result[field] = _safe_name(getattr(settings, field))
        except Exception:
            result[field] = None
    return result


def object_snapshot(obj) -> dict:
    if obj is None:
        return {}
    data = {
        "name": _safe_name(obj),
        "type": getattr(obj, "type", None),
        "matrix_world": _safe_matrix(obj),
    }
    try:
        data["hide_viewport"] = bool(obj.hide_viewport)
        data["hide_render"] = bool(obj.hide_render)
        data["selected"] = bool(obj.select_get())
    except Exception:
        pass
    try:
        mesh = obj.data
        data["data_name"] = mesh.name
        data["data_users"] = int(mesh.users)
        if getattr(obj, "type", None) == "MESH":
            data["verts"] = len(mesh.vertices)
            data["edges"] = len(mesh.edges)
            data["faces"] = len(mesh.polygons)
    except Exception:
        pass
    try:
        for key in (
            "uvwall_definition_uid", "uvwall_result_uid", "uv_wall_mode",
            "uvwall_owner_path", "uvwall_definition_revision", "uvwall_source_uid",
            "uvwall_experimental", "uvwall_production_uv", "uvwall_joint_diagnostics",
        ):
            if key in obj:
                data[key] = obj.get(key)
    except Exception:
        pass
    return data


def scene_inventory(context) -> list[dict]:
    if bpy is None:
        return []
    result = []
    try:
        objects = list(bpy.data.objects)
    except Exception:
        return result
    for obj in objects:
        try:
            uv_related = any(key in obj for key in (
                "uvwall_definition_uid", "uvwall_result_uid", "uv_wall_mode",
                "uvwall_source_uid", "uv_wall_stress_test", "uvwall_experimental",
            ))
            if not uv_related:
                # Authored Path references matter even before a Result exists.
                settings = getattr(getattr(context, "scene", None), "uv_wall_settings", None)
                uv_related = obj in {
                    getattr(settings, "path_mesh", None),
                    getattr(settings, "source_plane", None),
                    getattr(settings, "cornice_profile", None),
                }
            if uv_related:
                result.append(object_snapshot(obj))
        except Exception:
            continue
    return result


def runtime_report(context=None) -> dict:
    root = sys.modules.get("UV_Wall_Professional")
    version = getattr(root, "bl_info", {}).get("version") if root else None
    report = {
        "created_utc": _iso_utc(),
        "addon_version": ".".join(map(str, version)) if version else "UNKNOWN",
        "addon_path": str(getattr(root, "__file__", "UNKNOWN")) if root else "UNKNOWN",
        "python": sys.version,
        "platform": platform.platform(),
        "build_runtime": build_runtime_state(),
        "log_paths": paths_info(),
    }
    if bpy is not None:
        report.update({
            "blender": getattr(bpy.app, "version_string", "UNKNOWN"),
            "background": bool(getattr(bpy.app, "background", False)),
        })
        try:
            report["scene"] = context.scene.name if context is not None and context.scene else None
            report["mode"] = getattr(context, "mode", None)
            report["active_object"] = _safe_name(getattr(context, "active_object", None))
            report["selected_objects"] = [_safe_name(obj) for obj in getattr(context, "selected_objects", [])]
        except Exception:
            pass
    try:
        from .runtime_identity import operation_identity
        report["runtime_identity"] = operation_identity()
    except Exception as exc:
        report["runtime_identity_error"] = f"{type(exc).__name__}: {exc}"
    report["settings"] = settings_snapshot(context)
    if bpy is not None and context is not None:
        try:
            from ..ui.preferences import active_ui_mode
            from ..viewport.controller import ui_layout_snapshot
            report["ui_mode"] = active_ui_mode(context)
            report["ui_layout"] = ui_layout_snapshot()
        except (AttributeError,RuntimeError,TypeError,ImportError) as exc:
            report["ui_layout_error"] = str(exc)
    return report


def record_failure(*, stage: str, exception: BaseException | None = None,
                   traceback_text: str | None = None, context=None,
                   category: str = "BUILD", extra: dict | None = None) -> dict:
    if traceback_text is None:
        traceback_text = _traceback.format_exc()
    payload = {
        "created_utc": _iso_utc(),
        "category": category,
        "stage": stage or _CURRENT_STAGE,
        "transaction_token": _CURRENT_TOKEN,
        "exception_type": type(exception).__name__ if exception is not None else None,
        "exception": str(exception) if exception is not None else None,
        "traceback": traceback_text,
        "runtime": runtime_report(context),
        "scene_inventory": scene_inventory(context),
    }
    if extra:
        payload["extra"] = extra
    path = log_directory() / "uvwall-last-failure.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass
    return payload


def diagnostics_text() -> str:
    if bpy is None:
        return ""
    try:
        text = bpy.data.texts.get("UV_Wall_Diagnostics")
        return text.as_string() if text is not None else ""
    except Exception:
        return ""


def _support_text(context=None) -> str:
    report = runtime_report(context)
    header = [
        "UV WALL SUPPORT REPORT",
        "=" * 80,
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        "",
        "UV_Wall_Diagnostics",
        "=" * 80,
        diagnostics_text() or "<empty>",
    ]
    failure = log_directory() / "uvwall-last-failure.json"
    if failure.exists():
        try:
            header.extend(["", "LAST FAILURE", "=" * 80, failure.read_text(encoding="utf-8")])
        except OSError:
            pass
    return "\n".join(header)


def clipboard_report(context=None) -> str:
    return _support_text(context)


def save_diagnostics_log(context=None, destination: Path | None = None) -> Path:
    if destination is None:
        downloads = Path.home() / "Downloads"
        root = downloads if downloads.exists() else log_directory()
        destination = root / f"UV_Wall_Diagnostics_{_stamp()}.log"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_support_text(context), encoding="utf-8")
    return destination


def _add_if_exists(zf: zipfile.ZipFile, path: Path, arcname: str | None = None) -> None:
    if path.exists() and path.is_file():
        zf.write(path, arcname or path.name)


def problem_registry_snapshot(context=None) -> list[dict]:
    if bpy is None or context is None:
        return []
    try:
        from .problem_registry import serialize_problems
        return serialize_problems(context.scene.uv_wall_settings)
    except Exception:
        return []


def cornice_snapshot(context=None) -> dict:
    if bpy is None or context is None:
        return {}
    try:
        settings = context.scene.uv_wall_settings
    except Exception:
        return {}
    if getattr(settings, "build_mode", None) != "CORNICE":
        return {}
    result = {
        "path": _safe_name(getattr(settings, "path_mesh", None)),
        "profile": _safe_name(getattr(settings, "cornice_profile", None)),
        "uv_mode": getattr(settings, "cornice_uv_mode", None),
        "corner_mode": getattr(settings, "cornice_corner_mode", None),
        "corner_methods": {str(m.vertex_uid):str(getattr(m,"corner_method","CLASSIC")) for m in settings.cornice_corners},
        "smooth_warning_angle": getattr(settings, "cornice_smooth_warning_angle", None),
        "sharp_corner_count": len(getattr(settings, "cornice_corners", ())),
        "sharp_corners": [],
    }
    for marker in getattr(settings, "cornice_corners", ()):
        result["sharp_corners"].append({
            "vertex_uid": int(getattr(marker, "vertex_uid", 0)),
            "cached_index": int(getattr(marker, "cached_index", -1)),
            "corner_type": str(getattr(marker, "corner_type", "SHARP")),
        })
    path = getattr(settings, "path_mesh", None)
    profile = getattr(settings, "cornice_profile", None)
    if path is not None:
        result["path_object"] = object_snapshot(path)
    if profile is not None:
        result["profile_object"] = object_snapshot(profile)
    return result



def support_geometry_snapshot(context, max_vertices=250_000, max_faces=250_000):
    """Lossless, bounded geometric evidence; Blender scene and objects stay untouched.

    The historic bundle captured only object *counts*, making screenshots of
    broken corners impossible to reproduce. Full local vertices, face indices,
    loop UV, path vertices and profile edges let the engineer rebuild that case.
    No .blend or unrelated scene objects are exported.
    """
    if bpy is None or context is None:
        return {"status": "UNAVAILABLE", "reason": "No live Blender context"}
    settings=getattr(getattr(context,'scene',None),'uv_wall_settings',None)
    if settings is None:
        return {"status": "UNAVAILABLE", "reason": "UV Wall settings absent"}
    objects=[]
    for name in ('path_mesh','cornice_profile'):
        obj=getattr(settings,name,None)
        if obj is not None and obj not in objects:objects.append(obj)
    # Only UV-Wall-owned generated meshes; no unrelated user scene assets.
    for obj in getattr(bpy.data,'objects',()):
        try:
            if getattr(obj,'type',None)=='MESH' and ('uvwall_result_uid' in obj or 'uv_wall_mode' in obj or 'uvwall_experimental' in obj):
                if obj not in objects:objects.append(obj)
        except (ReferenceError,AttributeError):continue
    output={'status':'OK','units':'Blender world metres','objects':[],'source_path_not_modified':True}
    for obj in objects:
        if getattr(obj,'type',None)!='MESH':continue
        mesh=obj.data
        nv,nf=len(mesh.vertices),len(mesh.polygons)
        record={'name':obj.name,'mesh':mesh.name,'vertices':nv,'faces':nf,
                'type':'JOINT_PREVIEW' if obj.get('uvwall_experimental') else ('RESULT' if 'uvwall_result_uid' in obj else 'SOURCE'),
                'matrix_world':[[float(x) for x in row] for row in obj.matrix_world],
                'hide_get':bool(obj.hide_get()),'hide_viewport':bool(obj.hide_viewport)}
        if obj.get('uvwall_experimental'):
            record['production_uv']=bool(obj.get('uvwall_production_uv'))
            try:record['joint_diagnostics']=json.loads(obj.get('uvwall_joint_diagnostics','{}'))
            except (TypeError,ValueError):record['joint_diagnostics_error']='BAD_JSON'
        if nv>max_vertices or nf>max_faces:
            record['status']='TOO_LARGE_FOR_BOUNDED_SNAPSHOT'
        else:
            record['status']='COMPLETE'
            record['vertex_local_xyz']=[[float(v.co.x),float(v.co.y),float(v.co.z)] for v in mesh.vertices]
            record['edges']=[[int(i) for i in edge.vertices] for edge in mesh.edges]
            record['faces_vertex_indices']=[[int(i) for i in poly.vertices] for poly in mesh.polygons]
            uv=mesh.uv_layers.active
            if uv is not None:
                record['uv_layer']=uv.name
                record['per_face_uv']=[[
                    [float(uv.data[loop].uv.x),float(uv.data[loop].uv.y)]
                    for loop in poly.loop_indices] for poly in mesh.polygons]
            record['material_index']=[int(poly.material_index) for poly in mesh.polygons]
        output['objects'].append(record)
    return output




def support_action_trace(max_events=600) -> dict:
    """Extract last meaningful persisted action lines; never delete existing logs."""
    from collections import deque
    path = _SESSION_PATH if _SESSION_PATH is not None and _SESSION_PATH.exists() else _LATEST_PATH
    if path is None or not path.exists():
        return {"status": "NO_SESSION_LOG", "events": []}
    count = 0
    events = deque(maxlen=max_events)
    tokens = ("[LIFECYCLE]", "[CANCELLED]", "[ERROR]", "[TRACEBACK]", "[RESULT]", "[CORNICE_GEOMETRY]", "[CORNER]", "[NORMALS]", "[UNDO]", "[DIAGNOSTICS]", "[WALL]", "[CAP]", "[UV]", "[UI]")
    try:
        with path.open('r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if any(token in line for token in tokens):
                    count += 1
                    events.append(line.rstrip('\r\n')[:2200])
        return {"status": "OK", "source": path.name, "matched_actions": count,
                "retained_actions": len(events), "truncated": count > max_events, "events": list(events)}
    except OSError as exc:
        return {"status": "ERROR", "reason": str(exc), "events": []}


def viewport_snapshot(context=None) -> dict:
    """Read-only layout and camera state, including screen-region positions."""
    output = {"status": "NO_CONTEXT", "screens": []}
    if bpy is None or context is None:
        return output
    try:
        for window in getattr(context.window_manager, 'windows', ()):
            screen = getattr(window, 'screen', None)
            if screen is None:
                continue
            areas = []
            for area in screen.areas:
                record = {"type": area.type, "x": int(area.x), "y": int(area.y),
                          "width": int(area.width), "height": int(area.height),
                          "regions": [{"type": r.type, "x": int(r.x), "y": int(r.y),
                                       "width": int(r.width), "height": int(r.height)} for r in area.regions]}
                if area.type == 'VIEW_3D':
                    space = area.spaces.active
                    r3d = getattr(space, 'region_3d', None)
                    if r3d is not None:
                        record['camera'] = {
                            "view_distance": float(r3d.view_distance),
                            "view_location": list(map(float, r3d.view_location)),
                            "view_rotation": list(map(float, r3d.view_rotation)),
                            "view_perspective": str(r3d.view_perspective),
                            "view_matrix": [[float(c) for c in row] for row in r3d.view_matrix],
                        }
                    record['view_flags'] = {"show_gizmo": bool(space.show_gizmo),
                                            "clip_start": float(space.clip_start),
                                            "clip_end": float(space.clip_end)}
                areas.append(record)
            output['screens'].append({"screen_name": screen.name, "areas": areas})
        output['status'] = 'OK'
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError) as exc:
        output['status'] = 'PARTIAL_ERROR'
        output['reason'] = f'{type(exc).__name__}: {exc}'
    return output


def full_geometry_forensics(context=None, max_objects=35) -> dict:
    """Built-in version of WALL/CAP forensic audit; invoked by EXISTING ZIP button."""
    report = {"status": "NO_BLENDER", "schema": "WALL_CAP_FORENSICS_1", "objects": [],
              "wall_result_count": 0, "errors": [], "captured_utc": _iso_utc(),
              "hypotheses": [], "settings": settings_snapshot(context)}
    if bpy is None or context is None:
        return report
    try:
        from .support_mesh_audit import inspect_mesh, height_hypothesis_snapshot
        settings = report['settings']
        wall_objects = [o for o in bpy.data.objects if getattr(o, 'type', None) == 'MESH'
                        and o.get('uv_wall_mode') == 'WALL' and 'uvwall_result_uid' in o]
        related = [o for o in bpy.data.objects if getattr(o, 'type', None) == 'MESH'
                   and (o.get('uvwall_result_uid') or o.get('uvwall_experimental'))]
        report['wall_result_count'] = len(wall_objects)
        report['eligible_objects'] = len(related)
        report['objects_truncated'] = len(related) > max_objects
        path_selected = getattr(context.scene.uv_wall_settings, 'path_mesh', None)
        for obj in related[:max_objects]:
            try:
                analysed = inspect_mesh(obj, float(settings.get('tile_length', 4.0)))
                if obj in wall_objects:
                    linked_name = obj.get('uvwall_owner_path') or obj.get('path_object')
                    authored = bpy.data.objects.get(linked_name) if linked_name else path_selected
                    analysed['heightline_hypotheses'] = height_hypothesis_snapshot(authored, analysed, settings)
                report['objects'].append(analysed)
            except Exception as error:
                report['errors'].append({"object": obj.name, "error": f'{type(error).__name__}: {error}'})
        report['status'] = 'OK_WITH_ERRORS' if report['errors'] else ('NO_WALL_RESULT_IN_SCENE' if not wall_objects else 'OK')
        report['hypotheses'] = [
            {"id": "H-Z-1", "claim": "global vertical UV Z-grid adds full-wall geometry bands", "status": "SOURCE_CODE_HYPOTHESIS"},
            {"id": "H-Z-2", "claim": "upward and downward path segments have different local cuts", "status": "REQUIRES_MESH_COMPARISON"},
            {"id": "H-CAP-1", "claim": "cap and wall must share exact topological edges", "status": "CHECK_MESH_REPORT"},
            {"id": "H-UV-1", "claim": "UV module seams need not force coplanar mesh edges", "status": "REQUIRES_TEXEL_AND_ROLE_TEST"},
        ]
    except Exception as error:
        report['status'] = 'ERROR'
        report['errors'].append({"section": "FULL_GEOMETRY_FORENSICS", "error": f'{type(error).__name__}: {error}'})
    return report


def _json_report(path: Path, producer):
    """One failed optional section cannot prevent collecting the one-click ZIP."""
    try:
        result = producer()
        status = result.get('status', 'OK') if isinstance(result, dict) else 'OK'
    except Exception as error:
        result = {"status": "ERROR", "reason": f'{type(error).__name__}: {error}'}
        status = 'ERROR'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return {"file": path.name, "status": status, "bytes": path.stat().st_size}

def recovery_snapshot_report(context=None):
    """Read-only, bounded metadata for one-click Support ZIP (no Mesh hashing)."""
    if bpy is None or context is None:
        return {'status':'UNAVAILABLE'}
    try:
        from .mesh_recovery import recovery_status, RECOVERY_STATS, RECOVERY_DIGEST
        objects = []
        # Bound report size independently of scene complexity.
        for obj in bpy.data.objects:
            if len(objects) >= 64:
                break
            if getattr(obj, 'type', None) != 'MESH' or not obj.get('uvwall_generated'):
                continue
            if obj.get('uv_wall_mode') != 'WALL':
                continue
            state = recovery_status(obj, bpy.data)
            objects.append({
                'object': obj.name,
                'owner_uid': str(obj.get('uvwall_owner_uid',''))[:72],
                'result_uid': str(obj.get('uvwall_result_uid',''))[:72],
                'snapshot': state,
                'stats': str(obj.get(RECOVERY_STATS,''))[:300],
                'fingerprint_prefix': str(obj.get(RECOVERY_DIGEST,''))[:16],
            })
        return {'status':'OK', 'read_only':True, 'deep_hash_performed':False,
                'items':objects, 'limit':64}
    except Exception as exc:
        return {'status':'INTERNAL_ERROR', 'reason':type(exc).__name__}


def create_support_bundle(context=None, destination: Path | None = None) -> Path:
    """Create one ZIP the user can attach to ChatGPT without hunting files."""
    root = log_directory()
    if destination is None:
        downloads = Path.home() / "Downloads"
        out_root = downloads if downloads.exists() else root
        destination = out_root / f"UV_Wall_SUPPORT_{_stamp()}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)

    runtime_path = root / "runtime_identity.json"
    settings_path = root / "settings_snapshot.json"
    inventory_path = root / "scene_inventory.json"
    problems_path = root / "problem_registry.json"
    cornice_path = root / "cornice_snapshot.json"
    recovery_path = root / "recovery_snapshots.json"
    geometry_path = root / "geometry_snapshot.json"
    text_path = root / "UV_Wall_Diagnostics.log"
    forensic_path = root / "wall_cap_uv_forensics.json"
    trace_path = root / "user_action_trace.json"
    ui_path = root / "viewport_layout_diagnostics.json"
    capture_manifest_path = root / "support_capture_manifest.json"
    readme_path = root / "SEND_THIS_TO_CHATGPT.txt"

    runtime_path.write_text(json.dumps(runtime_report(context), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    settings_path.write_text(json.dumps(settings_snapshot(context), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    inventory_path.write_text(json.dumps(scene_inventory(context), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    problems_path.write_text(json.dumps(problem_registry_snapshot(context), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    cornice_path.write_text(json.dumps(cornice_snapshot(context), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    recovery_path.write_text(json.dumps(recovery_snapshot_report(context), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    try:
        geometry_data=support_geometry_snapshot(context)
    except Exception as geometry_error:
        geometry_data={'status':'ERROR','reason':str(geometry_error)}
    geometry_path.write_text(json.dumps(geometry_data, ensure_ascii=False, separators=(',',':'), default=str), encoding="utf-8")
    text_path.write_text(diagnostics_text(), encoding="utf-8")
    new_sections = [
        _json_report(forensic_path, lambda: full_geometry_forensics(context)),
        _json_report(trace_path, support_action_trace),
        _json_report(ui_path, lambda: viewport_snapshot(context)),
    ]
    previous_sections = []
    for path in (runtime_path, settings_path, inventory_path, problems_path,
                 cornice_path, recovery_path, geometry_path, text_path):
        previous_sections.append({"file": path.name,
                                  "status": ("OK" if path.is_file() else "MISSING"),
                                  "bytes": path.stat().st_size if path.is_file() else 0})
    capture_manifest = {
        "schema": "UV_WALL_SUPPORT_CAPTURE_1", "created_utc": _iso_utc(),
        "existing_support_button": "object.uv_wall_collect_support",
        "read_only": True, "scene_or_mesh_edits": False,
        "sections": previous_sections + new_sections,
        "warnings": ["Potential redundant edge samples are NOT proof of safe removal.",
                     "Missing WALL results must not be interpreted as a WALL PASS.",
                     "No .blend or external textures automatically embedded."],
    }
    capture_manifest_path.write_text(json.dumps(capture_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    readme_path.write_text(
        "Отправьте весь этот ZIP в чат. Он содержит диагностику UV Wall, runtime identity, "
        "настройки, инвентарь UV Wall объектов и последний failure snapshot.\n"
        "Файл .blend автоматически НЕ включается. Геометрия UV Wall и экспериментальных Joint Preview включена в geometry_snapshot.json, если не превышен лимит.\n"
        "НОВОЕ: recovery_snapshots.json (статусы сохранённых Mesh, без тяжёлого хэширования), wall_cap_uv_forensics.json + user_action_trace.json + viewport_layout_diagnostics.json + support_capture_manifest.json — из той же кнопки.\n"
        "Если WALL отсутствует в открытой сцене — статус NO_WALL_RESULT_IN_SCENE, не PASS.\n",
        encoding="utf-8",
    )

    addon_root = Path(__file__).resolve().parents[1]
    try:
        from .version import RUNTIME_MANIFEST, INTEGRITY_MANIFEST
        manifest_names = (RUNTIME_MANIFEST, INTEGRITY_MANIFEST)
    except Exception:
        manifest_names = ()

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _add_if_exists(zf, Path(paths_info()["latest_log"]), "uvwall-latest.log")
        if _SESSION_PATH is not None:
            _add_if_exists(zf, _SESSION_PATH, _SESSION_PATH.name)
        _add_if_exists(zf, root / "uvwall-last-failure.json")
        for path in (runtime_path, settings_path, inventory_path, problems_path, cornice_path, recovery_path, geometry_path, text_path, forensic_path, trace_path, ui_path, capture_manifest_path, readme_path):
            _add_if_exists(zf, path)
        for name in manifest_names:
            _add_if_exists(zf, addon_root / name, name)
        for name in ("START_HERE_NEXTGEN_1_17_8_RU.md", "CHANGELOG_NEXTGEN_1_17_8_RU.md",
                     "QA_REPORT_NEXTGEN_1_17_8_RU.md"):

            _add_if_exists(zf, addon_root / name, "release_docs/" + name)
        tools_dir = addon_root / "tools"
        if tools_dir.is_dir():
            for pattern in ("UV_WALL_*_FIELD_TEST.log", "UV_WALL_*_IMPORT_TEST.log"):
                for log_path in sorted(tools_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:4]:
                    _add_if_exists(zf, log_path, "field/" + log_path.name)
    return destination
