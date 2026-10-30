"""One-click, bounded, support ZIP: no project file or image textures copied."""

import datetime
import io
import ctypes
import struct
import json
from itertools import islice
import os
import platform
import sys
import tempfile
import zipfile

import bpy
import bmesh
from bpy.types import Operator, Panel

from . import log, runtime, view_tools, modeling_tools, ui, uv_feedback, viewport_feedback, selection_core, history_guard, weld_preview, module_visualization
from .core import action_trace, registry, undo_audit, window_visibility
from .tools import brick_hole as bh
from .tools.brick_hole import integration as brick_integration
from .tools.collision_inspector import integration as collision_integration
from .tools import collision_inspector as ci
from .tools.uv_wall import integration as uv_wall_integration
from .tools.dwos_windows import integration as dwos_integration

def _gpu_modal_lifecycle_snapshot():
    """Read-only summaries only; a failed module must not break Support ZIP."""
    result = {}
    for key, getter in (
        ('collision_inspector', lambda: __import__(
            'issmaker_modeling_assistant.tools.collision_inspector.viewport_ui',
            fromlist=['lifecycle_snapshot']).lifecycle_snapshot()),
        ('dwos_overlay', lambda: __import__(
            'issmaker_modeling_assistant.tools.dwos_windows.native.viewport',
            fromlist=['overlay_snapshot']).overlay_snapshot()),
        ('dwos_smart_interior', lambda: __import__(
            'issmaker_modeling_assistant.tools.dwos_windows.native.smart_interior_auto',
            fromlist=['queue_snapshot']).queue_snapshot()),
        ('dwos_composer', lambda: __import__(
            'issmaker_modeling_assistant.tools.dwos_windows.native.composer',
            fromlist=['lifecycle_snapshot']).lifecycle_snapshot()),
    ):
        try:
            result[key] = getter()
        except Exception as exc:
            result[key] = {'error': f'{type(exc).__name__}: {exc}'[:200]}
    return result


MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_SAMPLED_FACES = 256


def _uvwall_performance_snapshot():
    try:
        from .tools.uv_wall.UV_Wall_Professional.core.performance import PERFORMANCE_REGISTRY
        from .tools.uv_wall.UV_Wall_Professional.viewport.controller import _ACTIVE_SESSIONS
        stages=("PREVIEW_UPDATE","HUD_DRAW_VIEW","HUD_DRAW_PIXEL","WALL_GEOMETRY","SURFACE_GEOMETRY")
        sessions=[]
        for s in tuple(_ACTIVE_SESSIONS):
            engine=getattr(s,"_engine",None)
            sessions.append({"path_points":getattr(engine,"last_point_count",0),
                "preview_hz":getattr(engine,"max_preview_hz",0),
                "fast_preview_frames":getattr(engine,"fast_preview_frames",0),
                "gpu_batches_built":getattr(s,"_preview_batches_built",0),
                "gpu_batches_reused":getattr(s,"_preview_batches_reused",0)})
        return {"stages":{k:PERFORMANCE_REGISTRY.summary(k) for k in stages},"sessions":sessions}
    except (ImportError,ReferenceError,RuntimeError,AttributeError,TypeError) as exc:
        return {"error":str(exc)[:200]}


def collect_snapshot(context):
    obj = context.active_object
    state = {
        'schema': 'ima.diagnostics.2',
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'addon_version': '.'.join(map(str,__import__('issmaker_modeling_assistant',fromlist=['bl_info']).bl_info['version'])),
        'blender_version': bpy.app.version_string,
        'python_version': sys.version,
        'os': platform.platform(),
        'mode': context.mode,
        'file': bpy.data.filepath,
        'active_object': obj.name_full if obj else None,
        'selected_objects': [item.name_full for item in context.selected_objects][:1000],
        'selection_settings': {
            'mesh_select_mode': list(context.tool_settings.mesh_select_mode),
            'uv_select_mode': context.tool_settings.uv_select_mode,
            'uv_sync': bool(context.tool_settings.use_uv_select_sync),
        },
        'addon_state': {
            'issues_active': view_tools.issues_active(),
            'issues_kind': view_tools.issue_kind(),
            'uv_navigation': view_tools.issue_navigation_status(),
            'uv_feedback': uv_feedback.status(),
            'viewport_feedback': viewport_feedback.status(),
            'undo_redo': history_guard.status(),
            'global_undo_audit': undo_audit.snapshot(),
            'global_undo_lifecycle': undo_audit.lifecycle_status(),
            'gpu_modal_lifecycle': _gpu_modal_lifecycle_snapshot(),
            'unified_tool_sessions': __import__('issmaker_modeling_assistant.core.active_tools', fromlist=['ownership_snapshot']).ownership_snapshot(context),
            'ppa_wave_build_id': 'ISSMAKER-PPA-WAVE21-WAVE22-20261027',
            'styled_popup_lifecycle': __import__('issmaker_modeling_assistant.styled_panels',fromlist=['popup_registry_snapshot']).popup_registry_snapshot(),
            'unified_issue_navigation': __import__('issmaker_modeling_assistant.unified_issue_navigator', fromlist=['snapshot']).snapshot(),
            'analysis_running': view_tools.analysis_running(),
            'quick_linked_active': modeling_tools.linked_active(),
            'weld_preview': weld_preview.state(),
            'navigator_hud': ui.navigator_hud_diagnostics(context),
            'radial_hud': ui.radial_hud_diagnostics(context),
            'unified_tool_hud': ui.tool_hud_diagnostics(context),
            'active_operations': sorted(runtime._ACTIVE),
            'registered_tools': [vars(spec) for spec in registry.TOOLS],
            'brick_hole': brick_integration.status(),
            'brick_hole_build_id': getattr(bh, 'BH0472_BUILD_ID', getattr(bh, 'BH0468_BUILD_ID', 'UNKNOWN')),
            'reliability_foundation': __import__('issmaker_modeling_assistant.core.reliability.architecture',fromlist=['snapshot']).snapshot(),
            'collision_inspector': collision_integration.status(),
            'uv_wall': uv_wall_integration.status(context),
            'uv_wall_performance': _uvwall_performance_snapshot(),
            'module_visualization': module_visualization.status(context),
            'uv_orientation_quality': module_visualization.uv_quality_diagnostics(context),
            'role_surface_fast': module_visualization.role_surface_diagnostics(context, mode='FAST'),
            'dwos': dwos_integration.status(context),
            'persistent_notice': {name:getattr(context.scene,'ima_notice_'+name,'') for name in ('message','tool','kind')},
            'collision_actions': list(ci._ACTION_TRACE)[-300:],
            'tool_action_trace': action_trace.snapshot(),
            'viewport_window_visibility': window_visibility.snapshot_all(),
            'viewport_fixed_reservations': window_visibility.fixed_snapshot(),
            'visual_kit': __import__('issmaker_modeling_assistant.core.workspace_components',
                                   fromlist=['diagnostics']).diagnostics(),
            'adaptive_workspace': __import__('issmaker_modeling_assistant.core.adaptive_workspace',
                                            fromlist=['diagnostics']).diagnostics(),
            'ui_layout': __import__('issmaker_modeling_assistant.core.ui_layout_scaling', fromlist=['snapshot_layout']).snapshot_layout(),
            'brick_hole_actions': list(bh._ACTION_TRACE)[-300:],
        },
        'windows': [],
    }
    prefs = ui._preferences(context)
    if prefs:
        state['addon_state']['visual_ui'] = {
            name: getattr(prefs, name, None)
            for name in ('accent', 'density', 'button_scale', 'font_scale', 'window_scale', 'hud_display_preset', 'ui_debug_bounds',
                         'show_uv_issue_overlay', 'overlay_opacity',
                         'show_3d_issue_overlay', 'overlay_3d_fill', 'overlay_3d_xray',
                         'radial_center_cursor', 'radial_restore_cursor')
        }
    if prefs:
        from .core.ui_layout_scaling import readable_snapshot
        state['addon_state']['effective_readability'] = readable_snapshot(prefs)
    wm = getattr(context, 'window_manager', None)
    for window in (wm.windows if wm else ()):
        try:
            entry = {'screen': window.screen.name, 'areas': []}
            for area in window.screen.areas:
                entry['areas'].append({
                    'type': area.type, 'x': area.x, 'y': area.y,
                    'width': area.width, 'height': area.height,
                    'regions': [{'type': r.type, 'x': r.x, 'y': r.y,
                                 'width': r.width, 'height': r.height}
                                for r in area.regions],
                })
            state['windows'].append(entry)
        except (ReferenceError, RuntimeError, ValueError):
            continue
    if obj is not None and obj.type == 'MESH':
        mesh = obj.data
        info = {
            'mesh_name': mesh.name, 'data_users': mesh.users,
            'vertex_count': len(mesh.vertices),
            'edge_count': len(mesh.edges),
            'face_count': len(mesh.polygons),
            'uv_maps': [layer.name for layer in mesh.uv_layers],
            'active_uv': mesh.uv_layers.active.name if mesh.uv_layers.active else None,
            'active_uv_index': mesh.uv_layers.active_index,
            'uv_layer_names': [layer.name for layer in mesh.uv_layers],
            'dwos_uv_generation': {
                key:mesh.get(key,None) for key in (
                    'DWOS_uv_stage_state','DWOS_uv_stage_reason','DWOS_uv_schema',
                    'DWOS_uv_resolution','DWOS_uv_texel_px_m',
                    'DWOS_uv_triage_state','DWOS_uv_triage_oversized',
                    'DWOS_uv_triage_unsafe')
            },
        }
        if context.mode == 'EDIT_MESH':
            try:
                bm = bmesh.from_edit_mesh(mesh)
                layer = bm.loops.layers.uv.active
                info['edit_uv_layer'] = layer.name if layer else None
                info['mesh_selection_counts'] = {
                    'vertices': sum(v.select for v in bm.verts),
                    'edges': sum(e.select for e in bm.edges),
                    'faces': sum(f.select for f in bm.faces),
                }
                bm.verts.index_update()
                bm.faces.index_update()
                info['selected_face_samples'] = [
                    {'face_index': f.index,
                     'verts': [v.index for v in f.verts],
                     'uv': [[round(float(l[layer].uv.x), 7),
                             round(float(l[layer].uv.y), 7)] for l in f.loops]
                     if layer is not None else None}
                    for f in islice((f for f in bm.faces if f.select), MAX_SAMPLED_FACES)
                ]
                # Raw loop selection flags can stay set even when UV Sync is on.
                # Report both raw flags and actual authoritative selection.
                raw_uv_count = (sum(all(l[layer].select for l in f.loops)
                                    for f in bm.faces if not f.hide)
                                if layer is not None else None)
                sync = bool(context.tool_settings.use_uv_select_sync)
                # Mirror AUTO scope semantics: only the CURRENT screen matters.
                # A UV Editor in a second Blender window cannot silently alter
                # the active operator's source of selection.
                uv_editor_visible = any(
                    area.type == 'IMAGE_EDITOR' and
                    getattr(area.spaces.active, 'mode', None) == 'UV'
                    for area in (context.screen.areas if context.screen else ()))
                effective_source = ('MESH' if sync or not uv_editor_visible else 'UV')
                if effective_source == 'UV' and layer is not None:
                    effective_uv, partial_uv = selection_core.selected_uv_faces(
                        bm, layer, context.tool_settings.uv_select_mode)
                    effective_count = len(effective_uv)
                else:
                    effective_count = info['mesh_selection_counts']['faces']
                    partial_uv = 0
                info['selection_source_effective'] = effective_source
                info['uv_editor_visible'] = uv_editor_visible
                info['uv_selected_faces_raw'] = raw_uv_count
                info['uv_selected_faces'] = effective_count
                info['uv_partial_faces'] = partial_uv
                info['selection_count_note'] = (
                    'uv_selected_faces means authoritative faces for AUTO in current editor; '
                    'uv_selected_faces_raw reflects stored loop flags, not necessarily active selection')
                info['sample_truncated'] = (
                    info['mesh_selection_counts']['faces'] > MAX_SAMPLED_FACES)
            except (ReferenceError, RuntimeError, ValueError) as exc:
                info['snapshot_error'] = str(exc)
        state['mesh'] = info
    return state


class IMA_OT_DiagnosticsZip(Operator):
    bl_idname = 'ima.diagnostics_zip'
    bl_label = 'Собрать диагностику ZIP'
    bl_description = 'Создать ZIP с журналом действий, UV/Mesh, состоянием окон и окружением; скопировать путь'
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            state = collect_snapshot(context)
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            target = os.path.join(bpy.app.tempdir or tempfile.gettempdir(),
                                  f'ISSMAKER_18872_SUPPORT_BUNDLE_{stamp}.zip')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('state.json', json.dumps(
                    state, ensure_ascii=False, indent=2, default=str))
                archive.writestr('README.txt',
                    'ISSMAKER diagnostic snapshot. Blender file and textures are NOT included.\n'
                    'Report may include scene/object names and project file path.\n'
                    'The window positions are snapshots, not screenshots.\n'
                    'Send archive to developer only if comfortable sharing this metadata.\n')
                archive.writestr('tool_action_trace.json', json.dumps(
                    action_trace.snapshot(), ensure_ascii=False, indent=2))
                archive.writestr('undo_redo_audit.json', json.dumps(
                    undo_audit.snapshot(), ensure_ascii=False, indent=2, default=str))
                archive.writestr('brick_hole_action_trace.json', json.dumps(
                    list(bh._ACTION_TRACE)[-300:], ensure_ascii=False, indent=2, default=str))
                archive.writestr('collision_action_trace.json', json.dumps(
                    list(ci._ACTION_TRACE)[-300:], ensure_ascii=False, indent=2, default=str))
                try:
                    collision_log = ci._make_log(context)
                    archive.writestr('logs/Collision_Inspector.log',
                                     collision_log[-MAX_LOG_BYTES:])
                except Exception as exc:
                    archive.writestr('logs/Collision_Log_Error.txt', repr(exc))
                try:
                    from .tools.dwos_windows.native import dwos_log
                    dwos_path=getattr(dwos_log,'_LOG_PATH',None)
                    if dwos_path and os.path.isfile(str(dwos_path)):
                        with open(dwos_path,'rb') as f:
                            f.seek(max(0,os.path.getsize(dwos_path)-MAX_LOG_BYTES))
                            archive.writestr('logs/DWOS_Window_Core.log',f.read())
                except Exception as exc:
                    archive.writestr('logs/DWOS_Log_Error.txt',repr(exc))
                # UV Wall owns a separate session logger; include only its
                # actual named session/latest/failure paths, never scan disks.
                for label, path in _uv_wall_log_paths():
                    try:
                        if os.path.isfile(path):
                            with open(path, 'rb') as fh:
                                fh.seek(max(0, os.path.getsize(path)-MAX_LOG_BYTES))
                                log_payload=fh.read()
                                archive.writestr('logs/UV_Wall_'+label, log_payload)
                                if label.startswith('latest'):
                                    from .core.contour_log_audit import summarize
                                    cadence=summarize(log_payload.decode('utf-8','replace').splitlines())
                                    archive.writestr('logs/UV_Wall_contour_frequency.json',
                                        json.dumps(cadence,ensure_ascii=False,indent=2))
                    except (OSError, ValueError) as exc:
                        archive.writestr('logs/UV_Wall_'+label+'_ERROR.txt', repr(exc))
                source_text = bpy.data.texts.get(getattr(bh, 'LOG_TEXT_NAME', ''))
                if source_text is not None:
                    archive.writestr('logs/Brick_Hole_Text.log',
                        source_text.as_string()[-MAX_LOG_BYTES:])
                # BH0468: the exact five-loop geometry/UV witness was previously
                # stored only in the separate Brick Hole diagnostics ZIP. The
                # one-click ISSMAKER support ZIP must include it as well.
                try:
                    witnesses=list(bh._TESSELLATION_WITNESSES)
                    archive.writestr('brick_hole_tessellation_witnesses.json',
                        json.dumps(witnesses,ensure_ascii=False,indent=2))
                    archive.writestr('brick_hole_tessellation_evidence_status.json',
                        json.dumps({'schema':1,'count':len(witnesses),
                                    'max_kept':12,'source':'Brick Hole session',
                                    'relevant_to_latest_operation':'UNVERIFIED',
                                    'note':'Coordinates only, no .blend file included'},
                                   ensure_ascii=False,indent=2))
                except Exception as exc:
                    archive.writestr('brick_hole_tessellation_evidence_ERROR.txt',
                        f'{type(exc).__name__}: {exc}'[:500])
                # RF1: one support ZIP carries the common architecture contract and
                # a bounded cross-tool read-only Reliability Lab result.
                try:
                    from .core.reliability.architecture import snapshot as reliability_architecture_snapshot
                    from .core.reliability.lab import run_lab as run_reliability_lab
                    from .core.reliability.ledger import snapshot as reliability_ledger_snapshot
                    archive.writestr('reliability_foundation.json', json.dumps(
                        reliability_architecture_snapshot(),ensure_ascii=False,indent=2,default=str))
                    archive.writestr('reliability_lab.json', json.dumps(
                        run_reliability_lab(),ensure_ascii=False,indent=2,default=str))
                    archive.writestr('reliability_decision_ledger.json', json.dumps(
                        reliability_ledger_snapshot(),ensure_ascii=False,indent=2,default=str))
                    from .unified_issue_navigator import snapshot as unified_nav_snapshot
                    archive.writestr('unified_issue_navigator.json', json.dumps(
                        unified_nav_snapshot(),ensure_ascii=False,indent=2,default=str))
                except Exception as exc:
                    archive.writestr('reliability_lab_ERROR.txt',
                        f'{type(exc).__name__}: {exc}'[:1200])
                # W18: bounded read-only UI test. It never claims user-click Native PASS.
                try:
                    from .core.hud_acceptance import run_lab as run_hud_acceptance
                    from . import unified_issue_navigator as _unav
                    archive.writestr('hud_acceptance_lab.json', json.dumps(
                        run_hud_acceptance(context,
                                           expected_scope=getattr(_unav, '_BOTTOM_SCOPE', None),
                                           enabled=getattr(_unav, '_BOTTOM_ENABLED', False)),
                        ensure_ascii=False, indent=2, default=str))
                except Exception as exc:
                    archive.writestr('hud_acceptance_lab_ERROR.txt',
                                     ('%s: %s' % (type(exc).__name__, str(exc)))[:700])
                source = log.path()
                if os.path.isfile(source):
                    with open(source, 'rb') as stream:
                        size = os.path.getsize(source)
                        if size > MAX_LOG_BYTES:
                            stream.seek(size - MAX_LOG_BYTES)
                        archive.writestr('logs/ISSMAKER_Modeling_Assistant.log', stream.read())
            # The outer ZIP also contains physically embedded standalone ZIPs.
            # A text path to a file on the user's disk is NOT a transferable log.
            pack_nested_support_archives(target, state)
            context.window_manager.clipboard = target
            log.write(f'Diagnostic ZIP created: {target} (nested module ZIPs)')
            self.report({'INFO'}, 'Диагностика ZIP создана; путь скопирован в буфер обмена')
            return {'FINISHED'}
        except Exception as exc:
            log.write_exception('Diagnostic ZIP failed', exc)
            self.report({'ERROR'}, f'Ошибка диагностики: {exc}')
            return {'CANCELLED'}


def nested_support_groups():
    """Explicit names avoid recursive filesystem scans and accidental .blend capture."""
    return {
        '01_ISSMAKER_Core.zip': ('state.json','README.txt','tool_action_trace.json',
                                 'logs/ISSMAKER_Modeling_Assistant.log',
                                 'reliability_foundation.json','reliability_lab.json',
                                 'reliability_decision_ledger.json','reliability_lab_ERROR.txt',
                                 'unified_issue_navigator.json',
                                 'hud_acceptance_lab.json', 'hud_acceptance_lab_ERROR.txt'),
        '02_Brick_Hole.zip': ('brick_hole_action_trace.json','logs/Brick_Hole_Text.log',
                              'brick_hole_tessellation_witnesses.json',
                              'brick_hole_tessellation_evidence_status.json'),
        '03_Collision_Inspector.zip': ('collision_action_trace.json','logs/Collision_Inspector.log'),
        '04_UV_Wall.zip': ('logs/UV_Wall_',),
        '05_DWOS_Windows.zip': ('logs/DWOS_Window_Core.log',),
        '06_Undo_Redo.zip': ('undo_redo_audit.json',),
    }


def pack_nested_support_archives(outer_path, snapshot=None):
    """Add six genuine independent ZIP payloads to the already written outer ZIP."""
    groups=nested_support_groups()
    with zipfile.ZipFile(outer_path,'r') as outer:
        original={name:outer.read(name) for name in outer.namelist()}
    pieces={}
    for archive_name, patterns in groups.items():
        buffer=io.BytesIO()
        entries=[]
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as inner:
            for name,data in original.items():
                if any(name==pattern or (pattern.endswith('_') and name.startswith(pattern)) for pattern in patterns):
                    inner.writestr(name,data)
                    entries.append(name)
            if snapshot is not None and archive_name.startswith(('02_','03_','04_','05_')):
                key={'02_':'brick_hole','03_':'collision_inspector',
                     '04_':'uv_wall','05_':'dwos'}[archive_name[:3]]
                inner.writestr('module_status.json',json.dumps(
                    snapshot.get('addon_state',{}).get(key,{}),ensure_ascii=False,indent=2,default=str))
                entries.append('module_status.json')
            inner.writestr('CONTENTS.txt','\n'.join(entries)+'\n')
        pieces[archive_name]=buffer.getvalue()
    with zipfile.ZipFile(outer_path,'a',zipfile.ZIP_DEFLATED) as outer:
        for name,data in pieces.items():
            outer.writestr('module_archives/'+name,data,compress_type=zipfile.ZIP_STORED)
        outer.writestr('module_archives/MANIFEST.json',json.dumps(
            {'schema':'ISSMAKER_NESTED_SUPPORT_V1',
             'modules':list(pieces),
             'notice':'ZIP contains log excerpts and metadata, not the .blend project or textures.'},
            ensure_ascii=False,indent=2))
    return list(pieces)


def _copy_zip_file_windows(path):
    """Copy an actual ZIP file to Windows Explorer/Telegram-desktop clipboard.

    CF_HDROP is not necessarily supported by Telegram in a web browser.
    Callers keep the file on disk and always provide a manual attach fallback.
    """
    if os.name!='nt':return False
    from ctypes import wintypes
    class DROPFILES(ctypes.Structure):
        _fields_=[('pFiles',wintypes.DWORD),('pt',wintypes.POINT),
                  ('fNC',wintypes.BOOL),('fWide',wintypes.BOOL)]
    kernel=ctypes.windll.kernel32
    user=ctypes.windll.user32
    kernel.GlobalAlloc.argtypes=[wintypes.UINT,ctypes.c_size_t]
    kernel.GlobalAlloc.restype=ctypes.c_void_p
    kernel.GlobalLock.argtypes=[ctypes.c_void_p]
    kernel.GlobalLock.restype=ctypes.c_void_p
    kernel.GlobalUnlock.argtypes=[ctypes.c_void_p]
    kernel.GlobalFree.argtypes=[ctypes.c_void_p]
    user.SetClipboardData.argtypes=[wintypes.UINT,ctypes.c_void_p]
    user.SetClipboardData.restype=ctypes.c_void_p
    if not os.path.isfile(path):return False
    names=(os.path.abspath(path)+'\0\0').encode('utf-16le')
    header=DROPFILES()
    header.pFiles=ctypes.sizeof(DROPFILES)
    header.fWide=True
    size=ctypes.sizeof(DROPFILES)+len(names)
    handle=kernel.GlobalAlloc(0x0042,size)  # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not handle:return False
    ptr=kernel.GlobalLock(handle)
    if not ptr:
        kernel.GlobalFree(handle)
        return False
    ctypes.memmove(ptr,ctypes.byref(header),ctypes.sizeof(header))
    ctypes.memmove(ptr+ctypes.sizeof(header),names,len(names))
    kernel.GlobalUnlock(handle)
    if not user.OpenClipboard(None):
        kernel.GlobalFree(handle)
        return False
    transferred=False
    try:
        if user.EmptyClipboard() and user.SetClipboardData(15,handle):
            transferred=True
    finally:
        user.CloseClipboard()
    if not transferred:kernel.GlobalFree(handle)
    return transferred


def _uv_wall_log_paths():
    # The canonical module is already loaded by UV Wall integration. NEVER
    # import a second name of its package during diagnostics: strict source
    # identity checks can treat it as a foreign release.
    import sys as _sys
    logger = _sys.modules.get('UV_Wall_Professional.core.support_logging')
    if logger is None:
        return ()
    try:
        info = logger.paths_info()
    except (RuntimeError, OSError, AttributeError):
        return ()
    output = []
    seen = set()
    for label in ('latest_log', 'session_log', 'last_failure'):
        path = str(info.get(label, ''))
        if path and path not in seen:
            seen.add(path)
            output.append((label + ('.json' if label == 'last_failure' else '.log'), path))
    return output


def _bounded_text(text, max_chars=120_000):
    text = str(text)
    if len(text) > max_chars:
        return '[ПРЕДЫДУЩИЕ ДАННЫЕ УСЕЧЕНЫ: показаны последние символы]\n' + text[-max_chars:]
    return text


def _read_log_tail(path, max_bytes=128_000):
    if not path or not os.path.isfile(path):
        return '[Файл отсутствует в этой сессии]'
    with open(path, 'rb') as fh:
        fh.seek(max(0, os.path.getsize(path)-max_bytes))
        return _bounded_text(fh.read().decode('utf-8', 'replace'))


def collect_full_support_text(context):
    """One clipboard-friendly report, only installed/active ISSMAKER logs."""
    sections=[]
    def section(name, callback):
        try:
            body=callback()
            sections.append('\n\n===== '+name+' =====\n'+_bounded_text(body))
        except Exception as exc:
            sections.append('\n\n===== '+name+' =====\n[НЕ УДАЛОСЬ СОБРАТЬ: '+repr(exc)+']')
    section('СОСТОЯНИЕ И ОКРУЖЕНИЕ', lambda: json.dumps(
        collect_snapshot(context), ensure_ascii=False, indent=2, default=str))
    section('ISSMAKER MODELING', lambda: _read_log_tail(log.path()))
    section('ISSMAKER TOOL ACTION TRACE', lambda: json.dumps(
        action_trace.snapshot(), ensure_ascii=False, indent=2, default=str))
    section('BRICK HOLE', lambda: (bpy.data.texts.get(bh.LOG_TEXT_NAME).as_string()
        if bpy.data.texts.get(bh.LOG_TEXT_NAME) is not None else
        json.dumps(list(bh._ACTION_TRACE)[-300:], ensure_ascii=False, default=str)))
    section('BRICK HOLE ACTION TRACE', lambda: json.dumps(list(bh._ACTION_TRACE)[-300:], ensure_ascii=False, indent=2, default=str))
    section('COLLISION INSPECTOR', lambda: ci._make_log(context))
    section('COLLISION ACTION TRACE', lambda: json.dumps(list(ci._ACTION_TRACE)[-300:], ensure_ascii=False, indent=2, default=str))
    for label,path in _uv_wall_log_paths():
        section('UV WALL '+label, lambda path=path: _read_log_tail(path))
    if not _uv_wall_log_paths():
        sections.append('\n\n===== UV WALL =====\n[Файлы логов не обнаружены]')
    def _dwos_log():
        from .tools.dwos_windows.native import dwos_log
        return _read_log_tail(str(getattr(dwos_log, '_LOG_PATH', '') or ''))
    section('DWOS WINDOWS', _dwos_log)
    return (
        'ISSMAKER 1.8.4 — ДИАГНОСТИКА ДЛЯ ТЕХЛИДА\n'
        'ВНИМАНИЕ: могут присутствовать пути к .blend, имена объектов, настройки, '
        'текст исключений и технические сведения о компьютере. Проверьте текст.\n'
        'Отправка вручную через Telegram.\n'
        + ''.join(sections)
    )


def collect_telegram_digest(context, archive_path):
    """Short pasteable diagnostic; complete logs are in local ZIP.

    Telegram messages cannot reliably contain hundreds of kilobytes. Keep the
    clipboard under 3900 chars and include each module's most recent problem.
    """
    obj=getattr(context, 'active_object', None)
    title=[
        'ISSMAKER 1.8.4 · ДИАГНОСТИКА',
        'Blender: '+str(bpy.app.version_string),
        'Режим: '+str(getattr(context, 'mode', '?')),
        'Объект: '+(str(obj.name)[:90] if obj is not None else 'нет'),
        'Полные журналы: '+str(archive_path),
    ]
    sources={
        'ISSMAKER': _read_log_tail(log.path(),max_bytes=50_000),
        'BRICK HOLE': (bpy.data.texts.get(bh.LOG_TEXT_NAME).as_string()[-50_000:]
                       if bpy.data.texts.get(bh.LOG_TEXT_NAME) else ''),
        'COLLISION': str(ci._make_log(context))[-50_000:],
    }
    for name,path in _uv_wall_log_paths():
        if name.startswith('latest') or name.startswith('last_failure'):
            sources['UV WALL '+name]=_read_log_tail(path,max_bytes=45_000)
    try:
        from .tools.dwos_windows.native import dwos_log
        sources['DWOS']=_read_log_tail(str(getattr(dwos_log,'_LOG_PATH','') or ''),max_bytes=50_000)
    except (ImportError,AttributeError,RuntimeError,OSError):
        sources['DWOS']='[Лог недоступен]'
    needles=('ERROR','FAIL','BLOCK','CANCEL','EXCEPTION','ROLLBACK',
             'TRACEBACK','SEAM','GAP','WINDING','ВЫРОЖД','ОШИБ')
    remaining=3650-sum(len(line)+1 for line in title)
    for name,body in sources.items():
        rows=[r.strip() for r in str(body).splitlines() if r.strip()]
        relevant=[row for row in rows if any(word in row.upper() for word in needles)]
        last=(relevant[-3:] if relevant else rows[-1:])
        excerpt=' | '.join(row[:240] for row in last) or '[Событий нет]'
        line=name+': '+excerpt
        allowance=min(540,max(120, remaining//max(1,len(sources))))
        line=line[:allowance]
        title.append(line)
        remaining-=len(line)+1
    title.append('Проверьте текст перед отправкой. Для полного отчёта приложите ZIP.')
    return '\n'.join(title)[:3850]


class IMA_OT_ReliabilityLab(Operator):
    bl_idname='ima.reliability_lab'
    bl_label='RELIABILITY LAB'
    bl_description='Read-only: единый математический тест Brick Hole, Collision, DWOS и UV Wall'
    bl_options={'REGISTER','INTERNAL'}

    def execute(self, context):
        try:
            from .core.reliability.lab import run_lab
            report=run_lab()
            stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            target=os.path.join(bpy.app.tempdir or tempfile.gettempdir(),
                                f'ISSMAKER_RELIABILITY_LAB_{stamp}.json')
            with open(target,'w',encoding='utf-8') as stream:
                json.dump(report,stream,ensure_ascii=False,indent=2,default=str)
            context.window_manager.clipboard=target
            if report['failed']:
                self.report({'WARNING'},f"Reliability Lab: {report['passed']}/{report['total']} PASS; путь скопирован")
            else:
                self.report({'INFO'},f"Reliability Lab: {report['passed']}/{report['total']} PASS; путь скопирован")
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},'Reliability Lab failed: '+str(exc)[:180])
            return {'CANCELLED'}


class IMA_OT_CopyFullSupportLog(Operator):
    bl_idname='ima.copy_full_support_log'
    bl_label='Собрать и скопировать логи ISSMAKER'
    bl_description=('Собрать ZIP всех модулей, скопировать краткий отчёт; '
                    'создать ZIP, скопировать текст в буфер. Ничего не отправляет')
    bl_options={'REGISTER', 'INTERNAL'}

    def execute(self, context):
        try:
            action_trace.record('FULL_LOG_COPY_REQUEST', 'CORE')
            # Also create a local ZIP; its creation copies a path first.
            result=bpy.ops.ima.diagnostics_zip('EXEC_DEFAULT')
            if 'FINISHED' not in result:
                raise RuntimeError('Диагностический ZIP не создан')
            archive_path=str(context.window_manager.clipboard)
            full=collect_telegram_digest(context, archive_path)
            file_clipboard=False
            try:
                file_clipboard=_copy_zip_file_windows(archive_path)
            except (OSError,RuntimeError,AttributeError,TypeError) as clip_exc:
                action_trace.record('FULL_LOG_FILE_CLIPBOARD_FAILED','CORE',repr(clip_exc))
            if not file_clipboard:
                context.window_manager.clipboard=full
            if getattr(context, 'scene', None) and hasattr(context.scene, 'ima_notice_message'):
                context.scene.ima_notice_message = (
                    'ZIP скопирован как файл. Вставьте в Telegram Desktop, либо прикрепите ZIP вручную.'
                    if file_clipboard else
                    'ZIP сохранён; текстовый отчёт скопирован. Для передачи логов прикрепите ZIP вручную.')
                context.scene.ima_notice_tool = 'SUPPORT'
                context.scene.ima_notice_kind = 'INFO'
            action_trace.record('FULL_LOG_COPY_READY', 'CORE',
                                f'chars={len(full)} zip=True file_clipboard={file_clipboard}')
            # Browser opens only AFTER successful local ZIP and clipboard copy.
            try:
                opened=bpy.ops.wm.url_open('EXEC_DEFAULT',url='https://t.me/mishashkonda')
            except (RuntimeError,ReferenceError,AttributeError) as exc:
                action_trace.record('SUPPORT_BROWSER_FAILED','CORE',repr(exc))
                self.report({'WARNING'},'Логи скопированы. Откройте t.me/mishashkonda вручную.')
                return {'FINISHED'}
            if 'FINISHED' not in opened:
                self.report({'WARNING'},'Логи скопированы, но Telegram не открылся.')
            else:
                action_trace.record('SUPPORT_BROWSER_OPEN','CORE','TECHLEAD')
                self.report({'INFO'},'ZIP готов. В Telegram Desktop вставьте файл либо прикрепите ZIP вручную.')
            return {'FINISHED'}
        except Exception as exc:
            action_trace.record('FULL_LOG_COPY_FAILED','CORE',repr(exc))
            self.report({'ERROR'},f'Не удалось собрать логи: {str(exc)[:180]}')
            return {'CANCELLED'}


class IMA_OT_OpenSupportLink(Operator):
    bl_idname='ima.open_support_link'
    bl_label='Открыть Telegram в браузере'
    bl_description='Открывает Telegram через браузер по умолчанию; не отправляет логи и сообщения'
    bl_options={'INTERNAL'}
    target: bpy.props.EnumProperty(items=(
        ('AUTHOR','Автор ISSMAKER','@isssmaker'),
        ('TECHLEAD','Техлид','@mishashkonda')), default='TECHLEAD')

    def execute(self, context):
        url='https://t.me/isssmaker' if self.target=='AUTHOR' else 'https://t.me/mishashkonda'
        try:
            result=bpy.ops.wm.url_open('EXEC_DEFAULT',url=url)
            if 'FINISHED' not in result:
                self.report({'WARNING'},'Не удалось открыть браузер')
                return {'CANCELLED'}
            action_trace.record('SUPPORT_BROWSER_OPEN','CORE',self.target)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'WARNING'},'Ошибка открытия Telegram: '+str(exc)[:150])
            return {'CANCELLED'}


CLASSES = (IMA_OT_DiagnosticsZip, IMA_OT_ReliabilityLab, IMA_OT_CopyFullSupportLog, IMA_OT_OpenSupportLink)

def register():
    runtime.register_classes_atomic(CLASSES)

def unregister():
    runtime.unregister_classes_safe(CLASSES)
