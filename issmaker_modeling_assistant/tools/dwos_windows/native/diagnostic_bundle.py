"""Field diagnostics bundle; no destructive operations and no external services."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from datetime import datetime
import zipfile
import bpy
from .dwos_log import log_dir, event as log_event, exception as log_exception


DWOS_VERSION = '3.7.37-rc38'


_LAST_ACTIVE_UI_SNAPSHOT = None


def _snapshot_for_composer(active):
    """Read-only positions and selection; avoid forcing any Blender redraw."""
    def rect(r):
        if r is None:return None
        try:return [round(float(v),2) for v in r]
        except (ValueError,TypeError):return None
    controls=[]
    for sid,box in getattr(active,'_scissor_controls',[]) or []:
        controls.append({'split_id':str(sid),'rect':rect(box)})
    buttons=[]
    for key,x0,y0,x1,y1,payload in (getattr(active,'_hits',[]) or [])[-200:]:
        buttons.append({'key':str(key),'rect':rect((x0,y0,x1-x0,y1-y0)),
                        'target_id':str(payload) if key=='INTERIOR_SCISSOR' else ''})
    return {'schema':'DWOS_viewport_UI_state_RC33','active':True,
            'captured_at':datetime.now().isoformat(timespec='seconds'),
            'window':str(getattr(active,'_obj_name','')),
            'page':str(getattr(active,'_page','')),
            'compact':bool(getattr(active,'_compact',True)),
            'hud_rect':rect(getattr(active,'_panel_rect',None)),
            'hud_pinned':bool(getattr(active,'_hud_pinned',False)),
            'hud_custom':bool(getattr(active,'_hud_custom',False)),
            'hud_fallback':bool(getattr(active,'_hud_fallback',False)),
            'hud_custom_position':list(getattr(active,'_hud_custom_position',(.0,.0))),
            'floating_rect':rect(getattr(active,'_floating_rect',None)),
            'footer_rect':rect(getattr(active,'_footer_rect',None)),
            'hud_window_scale':float(getattr(active,'_hud_pixel_scale',1.0)),
            'hud_font_scale':float(getattr(active,'_hud_font_scale',1.0)),
            'layout_issues':[str(v)[:150] for v in (getattr(active,'_layout_issues',[]) or [])[:100]],
            'layout_skipped':[str(v)[:100] for v in (getattr(active,'_layout_skips',[]) or [])[:100]],
            'hover_split':str(getattr(active,'_hover_split','')),
            'hover_zone':str(getattr(active,'_hover_zone','')),
            'selected_zone_ids':[str(v) for v in (getattr(active,'_selected_zone_ids',[]) or [])[:100]],
            'scissor_controls':controls[:200],
            'control_hits':buttons}


def cache_composer_ui_snapshot(active):
    """Called before modal teardown; stores no active object references."""
    global _LAST_ACTIVE_UI_SNAPSHOT
    if active is not None:
        _LAST_ACTIVE_UI_SNAPSHOT=_snapshot_for_composer(active)
    return _LAST_ACTIVE_UI_SNAPSHOT


def _composer_ui_snapshot():
    from . import composer
    active=getattr(composer,'_ACTIVE',None)
    if active is not None:
        return _snapshot_for_composer(active)
    if _LAST_ACTIVE_UI_SNAPSHOT:
        # No stale coordinates claimed as live; previous state is explicitly tagged.
        return dict(_LAST_ACTIVE_UI_SNAPSHOT,active=False,
                    source='LAST_ACTIVE_COMPOSER',last_active_available=True)
    return {'schema':'DWOS_viewport_UI_state_RC33','active':False,
            'source':'NO_COMPOSER_SESSION', 'last_active_available':False}


def _host_mesh_data(host, max_vertices=100000, max_faces=100000):
    """Optional full topology for offline reproduction (never modifies mesh)."""
    mesh = host.data
    if host.mode == 'EDIT':
        # Do not switch modes or force an edit-mesh commit merely to export.
        from .geometry_inspector import _current_mesh
        snap = _current_mesh(host)
        nverts, nfaces = len(snap['coordinates_local']), len(snap['polygons'])
        if nverts > max_vertices or nfaces > max_faces:
            return {'name':host.name,'skipped':'edit mesh exceeds diagnostic safety limit',
                    'vertices':nverts,'faces':nfaces}
        return {'format':'DWOS_host_mesh_v1','source':'EDIT_BMESH','name':host.name,
                'matrix_world':[[float(v) for v in row] for row in host.matrix_world],
                'vertices':snap['coordinates_local'],
                'edges':snap['edges'],'polygons':snap['polygons'],
                'edge_attributes':snap['edge_attributes'], 'uv_maps':snap['uv_maps']}
    nverts, nfaces = len(mesh.vertices), len(mesh.polygons)
    if nverts > max_vertices or nfaces > max_faces:
        return {'name':host.name, 'skipped':'host topology exceeds diagnostic safety limit',
                'vertices':nverts,'faces':nfaces}
    attrs = {}
    mark = mesh.attributes.get('DWOS_opening_id')
    if mark and mark.domain == 'EDGE' and mark.data_type == 'INT':
        attrs['DWOS_opening_id'] = [int(item.value) for item in mark.data]
    face_attrs={}
    fixture_cases=mesh.attributes.get('DWOS_fixture_case')
    if fixture_cases and fixture_cases.domain=='FACE' and fixture_cases.data_type=='INT':
        face_attrs['DWOS_fixture_case']=[int(item.value) for item in fixture_cases.data]
    return {'format':'DWOS_host_mesh_v1','name':host.name,
            'matrix_world':[[float(v) for v in row] for row in host.matrix_world],
            'vertices':[[float(c) for c in v.co] for v in mesh.vertices],
            'edges':[[int(i) for i in e.vertices] for e in mesh.edges],
            'polygons':[[int(i) for i in p.vertices] for p in mesh.polygons],
            'edge_attributes':attrs,'face_attributes':face_attrs}


def _host_objects(scene, active=None):
    names=set()
    state=getattr(scene,'dwos',None)
    if state and getattr(state,'wall_name',''):
        names.add(state.wall_name)
    if active and active.type=='MESH':
        settings=getattr(active,'dwos_window',None)
        if settings and settings.is_window:
            names.add(settings.host_name)
        else:
            names.add(active.name)
    for obj in scene.objects:
        settings=getattr(obj,'dwos_window',None)
        if settings and settings.is_window:
            names.add(settings.host_name)
        if obj.type=='MESH' and obj.get('DWOS_fixture_schema') in {'DWOS_fixture_gallery_v1','DWOS_fixture_gallery_long_v1','DWOS_fixture_interior_contact_v1'}:
            names.add(obj.name)
    return [obj for name in sorted(names) if (obj:=bpy.data.objects.get(name)) is not None
            and obj.type=='MESH']


def _snapshot(scene, active=None):
    from . import smart_interior_auto
    from .health_blender import health_of
    out = {'dwos_version':DWOS_VERSION,
           'auto_rebuild_queue':smart_interior_auto.queue_snapshot(),
           'blender_version':bpy.app.version_string,
           'windows':[], 'hosts':[], 'opening_candidates':[],
           'runtime_module_paths': {
               'diagnostic_bundle':str(Path(__file__).resolve()),
               'smart_interior_scene':str(getattr(sys.modules.get(__package__+'.smart_interior_scene'),'__file__','NOT_LOADED')),
               'window_builder':str(getattr(sys.modules.get(__package__+'.window_builder'),'__file__','NOT_LOADED'))
           }}
    state=getattr(scene,'dwos',None)
    if state is not None:
        out['smart_interior_settings']={
            'enabled':bool(getattr(state,'smart_interior_enabled',False)),
            'clearance_m':float(getattr(state,'smart_interior_gap',.03)),
            'mode':str(getattr(state,'smart_interior_mode','BALANCED')),
            'manual_strategy':str(getattr(state,'manual_interior_strategy','PRIORITY')),
        }
        for candidate in getattr(state,'openings',[]):
            try:
                payload=json.loads(candidate.payload_json or '{}')
            except Exception:
                payload={}
            out['opening_candidates'].append({
                'candidate_id':candidate.candidate_id,
                'enabled':bool(candidate.enabled),
                'width':candidate.width_text,'height':candidate.height_text,
                'wall_depth':candidate.wall_depth_text,
                'classification':payload.get('adaptive_classification','PLANAR_LEGACY'),
                'coordinate_system':payload.get('coordinate_system','PLANAR_XY'),
                'glass_policy':payload.get('glass_policy',''),
                'wall_mode':payload.get('wall_mode',''),
                'source':(payload.get('adaptive_topology') or {}).get('source','LEGACY'),
                'topology_reasons':(payload.get('adaptive_topology') or {}).get('reasons',[]),
            })
    for obj in scene.objects:
        s=getattr(obj,'dwos_window',None)
        if not s or not s.is_window:continue
        out['windows'].append({
          'name':obj.name, 'window_id':s.window_id,'host':s.host_name,
          'dwos_health':health_of(obj,scene),
          'width':s.width_text,'height':s.height_text,
          'frame_width':s.frame_width_text,'frame_depth':s.frame_depth_text,
          'interior_mode':s.interior_mode,'interior_depth':s.interior_depth_text,
          'interior_depth_limit_m':1.5,
          'interior_groups_requested':int(getattr(s,'interior_groups_count',0)),
          'interior_partition_mode':str(getattr(s,'interior_partition_mode','AUTO')),
          'interior_dividers_json':str(getattr(s,'interior_dividers_json','[]')),
          'interior_joint_gap_text':str(getattr(s,'interior_joint_gap_text','0,01')),
          'interior_partition_plan':next((ch.get('DWOS_interior_partition_plan','{}') for ch in obj.children if ch.get('DWOS_role')=='INTERIOR'),'{}'),
          'smart_interior_methods':next((ch.get('DWOS_smart_interior_solver_methods','[]') for ch in obj.children if ch.get('DWOS_role')=='INTERIOR'),'[]'),
          'smart_interior_manual_error':str(obj.get('DWOS_smart_interior_manual_error','')),
          'interior_depth_offset':s.interior_offset_text,
          'glass_edge_offset':s.glass_edge_offset_text,
          'glass_depth_offset':s.glass_offset_text,'glass_gap':s.glass_gap_text,
          'optimization':s.optimization,
          'corner_post_variant':getattr(s,'corner_post_variant','LEGACY'),
          'corner_post_width_requested_text':getattr(s,'corner_post_width_text','0,06'),
          'corner_post_width_effective_m':float(obj.get('DWOS_corner_post_width_effective_m',0.0)),
          'corner_post_width_max_m':float(obj.get('DWOS_corner_post_width_max_m',0.0)),
          'corner_post_independent':bool(obj.get('DWOS_corner_post_independent',False)),
          'zone_tree_json':s.zone_tree_json,
          'split_count':len(s.splits),'finalized':bool(s.finalized),
          'adaptive_classification':obj.get('DWOS_adaptive_classification','PLANAR_LEGACY'),
          'opening_topology_schema':obj.get('DWOS_opening_topology_schema',''),
          'outer_contact_policy':obj.get('DWOS_outer_contact_policy',''),
          'contact_front_vertices':obj.get('DWOS_contact_front_vertices',None),
          'glass_policy':next((child.get('DWOS_glass_policy','') for child in obj.children
                               if child.get('DWOS_role')=='GLASS'),''),
          'corner_invariant_policy':obj.get('DWOS_corner_invariant_policy',''),
          'corner_glass_panel_count':obj.get('DWOS_corner_glass_panel_count',0),
          'corner_full_geometry_locked':bool(obj.get('DWOS_corner_full_geometry_locked',False)),
          'corner_assembly':{
              'policy':obj.get('DWOS_corner_assembly_policy',''),
              'components':obj.get('DWOS_corner_assembly_components',0),
              'profiles':obj.get('DWOS_corner_assembly_profiles',0),
              'graph_edges':obj.get('DWOS_corner_assembly_graph_edges',0),
              'host_faces_removed':obj.get('DWOS_corner_assembly_host_faces_removed',0),
              'vertices_welded':obj.get('DWOS_corner_assembly_vertices_welded',0),
              'duplicate_faces_removed':obj.get('DWOS_corner_assembly_duplicate_faces_removed',0),
              'profile_policy':obj.get('DWOS_branch_profile_attachment_policy',''),
              'branch_back_rim_vertices_moved':obj.get('DWOS_corner_branch_back_rim_vertices_moved',0),
              'vertical_profile_policy':obj.get('DWOS_corner_v_profile_policy',''),
              'vertical_profile_count':obj.get('DWOS_corner_v_profile_count',0),
              'vertical_max_branch_angle_error_deg':obj.get('DWOS_corner_v_max_branch_angle_error_deg',0.0),
              'vertical_max_straightness_error':obj.get('DWOS_corner_v_max_straightness_error',0.0),
              'vertical_max_section_error_m':obj.get('DWOS_corner_v_max_section_error_m',0.0),
              'vertical_max_orthogonality_error':obj.get('DWOS_corner_v_max_orthogonality_error',0.0),
              'vertical_corner_shape_propagation':bool(obj.get('DWOS_corner_v_corner_shape_propagation',False)),
              'vertical_profiles_json':obj.get('DWOS_corner_v_profiles_json','[]'),
          },
          'spatial_validation':obj.get('DWOS_spatial_validation',''),
          'smart_interior':{
              'policy':next((child.get('DWOS_smart_interior_policy','')
                  for child in obj.children if child.get('DWOS_role')=='INTERIOR'),''),
              'contacts':next((child.get('DWOS_smart_interior_contacts',0)
                  for child in obj.children if child.get('DWOS_role')=='INTERIOR'),0),
              'canonical_available':any(bool(child.get('DWOS_interior_canonical_mesh_v370',''))
                  for child in obj.children if child.get('DWOS_role')=='INTERIOR'),
          },
          'curved_invariant':{
              'policy':next((child.get('DWOS_curved_invariant_policy','')
                  for child in obj.children if child.get('DWOS_role')=='GLASS'),''),
              'glass_layers':next((child.get('DWOS_curved_glass_layer_count',0)
                  for child in obj.children if child.get('DWOS_role')=='GLASS'),0),
          },
          'interior_repair':{
              'applied':bool(obj.get('DWOS_interior_repair_applied',False)),
              'policy':obj.get('DWOS_interior_repair_policy','NONE'),
              'raw_self_hits':obj.get('DWOS_interior_repair_raw_hits',0),
              'clip_events':obj.get('DWOS_interior_repair_clip_events',0),
              'segments':obj.get('DWOS_interior_repair_segments',0),
              'seam_gap_m':obj.get('DWOS_interior_repair_seam_gap_m',0.0),
          },
          'last_validation_error':obj.get('DWOS_last_validation_error',''),
          'last_group_split_error':obj.get('DWOS_last_group_split_error',''),
          'smart_interior_pending':bool(obj.get('DWOS_smart_interior_pending',False)),
          'smart_interior_reason':str(obj.get('DWOS_smart_interior_reason','')),
          'smart_interior_reason_code':str(obj.get('DWOS_smart_interior_reason_code','')),
          'smart_interior_active_links':next((child.get('DWOS_smart_interior_contact_links_v372','[]')
              for child in obj.children if child.get('DWOS_role')=='INTERIOR'),'[]'),
          'geometry':{'vertices':len(obj.data.vertices),'edges':len(obj.data.edges),
                      'faces':len(obj.data.polygons)} if obj.type=='MESH' else None,
        })
    for host in _host_objects(scene, active):
        from .geometry_inspector import _sizes
        nv,ne,nf = _sizes(host)
        out['hosts'].append({'name':host.name,'type':host.type,
            'mesh':{'vertices':nv,'edges':ne,'faces':nf} if host.type=='MESH' else None,
            'weld_shared_edges':host.get('DWOS_last_weld_shared_edges',None),
            'weld_mode':host.get('DWOS_last_weld_mode',None)})
    return out


def create_bundle(scene, active=None):
    folder=Path(log_dir())
    timestamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    bundle=folder/('DWOS_FIELD_'+timestamp+'_issmaker.zip')
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('DWOS_scene_snapshot.json',json.dumps(_snapshot(scene,active),
                         ensure_ascii=False,indent=2))
        archive.writestr('DWOS_viewport_UI_state.json',json.dumps(_composer_ui_snapshot(),
                         ensure_ascii=False,indent=2))
        # Physical objects, not the expected controller metadata. Keep it in
        # every one-click ZIP, with an explicit error record on audit failure.
        try:
            from .mesh_consistency_blender import inspect_scene
            mesh_check=inspect_scene(scene)
        except Exception as exc:
            log_exception('FIELD_MESH_CONSISTENCY_WARNING',exc)
            mesh_check={'schema':'DWOS_mesh_consistency_v1','status':'AUDIT_FAILED',
                        'error':f'{type(exc).__name__}: {exc}'}
        archive.writestr('DWOS_mesh_consistency.json',json.dumps(mesh_check,
                         ensure_ascii=False,indent=2))
        # W58.13: read-only per-window UV forensic audit, including seams,
        # real-world texel density, missing UV and single-tile violations.
        # A UV seam is evidence, not proof of a visual texture defect.
        try:
            from .uv_seam_diagnostics import audit_scene as _audit_uv_seams
            _uv_report = _audit_uv_seams(scene)
        except Exception as exc:
            log_exception('FIELD_UV_SEAM_DIAGNOSTIC_WARNING',exc)
            _uv_report={'schema':'ISSMAKER_DWOS_UV_SEAM_AUDIT_W58_13',
                        'status':'AUDIT_FAILED',
                        'error':f'{type(exc).__name__}: {exc}'}
        archive.writestr('DWOS_UV_seam_audit.json',
                         json.dumps(_uv_report,ensure_ascii=False,indent=2))
        # Fixture evidence is captured alongside the real Host mesh.  Keep the
        # authored manifest distinct from the most recent actual-mesh verdict.
        for index, obj in enumerate(scene.objects):
            if obj.type!='MESH' or obj.get('DWOS_fixture_schema') not in {'DWOS_fixture_gallery_v1','DWOS_fixture_gallery_long_v1','DWOS_fixture_interior_contact_v1'}:
                continue
            try:
                manifest=json.loads(obj.get('DWOS_fixture_manifest_json','[]'))
                verdict=json.loads(obj.get('DWOS_fixture_validation_json','{}'))
                auto_report=json.loads(obj.get('DWOS_fixture_auto_report_json','{}'))
                archive.writestr(f'fixture_gallery/{index:03}_manifest.json',
                                 json.dumps({'object':obj.name,'schema':obj.get('DWOS_fixture_schema','DWOS_fixture_gallery_v1'),
                                             'cases':manifest,'last_verdict':verdict,
                                             'last_explicit_auto_report':auto_report},
                                            ensure_ascii=False,indent=2))
            except (ValueError,TypeError) as exc:
                log_exception('FIELD_FIXTURE_GALLERY_REPORT_WARNING',exc,object=obj.name)
        # The two new forensic reports are already saved by explicit user actions.
        # Never substitute stale selections belonging to a different Host.
        expected_host = None
        if active is not None and getattr(active, 'type', '') == 'MESH':
            dwos_win=getattr(active, 'dwos_window', None)
            expected_host = dwos_win.host_name if dwos_win and dwos_win.is_window else active.name
        analysis_files = []
        for base in ('dwos-host-analysis-latest', 'dwos-selected-openings-latest'):
            report = folder / (base + '.json')
            summary = folder / (base + '.txt')
            if not report.exists():
                continue
            try:
                info = json.loads(report.read_text(encoding='utf8'))
                stored_host = info.get('host', info.get('object_name'))
                if expected_host and stored_host != expected_host:
                    log_event('FIELD_ANALYSIS_STALE_SKIPPED', report=base,
                              stored_host=stored_host, active_host=expected_host)
                    continue
                archive.write(report, 'geometry_inspector/' + report.name)
                analysis_files.append(report.name)
                if summary.exists():
                    archive.write(summary, 'geometry_inspector/' + summary.name)
            except Exception as exc:
                log_exception('FIELD_ANALYSIS_ATTACHMENT_WARNING', exc, report=base)
        archive.writestr('geometry_inspector/README_RU.txt',
            'DWOS 3.1 — диагностические отчёты Adaptive Opening Core.\n'
            'dwos-host-analysis-latest.json содержит исходную топологию стены.\n'
            'dwos-selected-openings-latest.json содержит РУЧНОЕ выделение в Edit Mode.\n'
            'Ручной выбор означает намерение пользователя, но всё равно проходит геометрическую классификацию/валидацию.\n'
            'Классы 3.1: PLANAR / CURVED / CORNER / COMPLEX / INVALID.\n'
            'Если отчёта нет, пользователь ещё не выполнил соответствующий анализ.\n'
            'Внимание: IDs вершин/рёбер относятся к снимку сетки и неустойчивы к её изменениям.\n'
            'Included: ' + ', '.join(analysis_files) + '\n')
        for index, host in enumerate(_host_objects(scene,active),1):
            try:
                payload=_host_mesh_data(host)
                archive.writestr(f'host_geometry/host_{index:03d}.json',
                    json.dumps(payload,ensure_ascii=False,separators=(',',':')))
            except Exception as exc:
                log_exception('FIELD_HOST_GEOMETRY_WARNING',exc,host=host.name)
        for filename in ('dwos-latest.log','dwos-mesh-analysis-latest.txt','dwos-frame-reference-latest.txt'):
            filepath=folder/filename
            if filepath.exists():archive.write(filepath,filename)
    log_event('FIELD_DIAGNOSTIC_BUNDLE',path=str(bundle),windows=sum(1 for o in scene.objects
              if getattr(o,'dwos_window',None) and o.dwos_window.is_window))
    return str(bundle)


class DWOS_OT_export_field_bundle(bpy.types.Operator):
    bl_idname='dwos.export_field_bundle'
    bl_label='Собрать диагностический ZIP'
    bl_options={'REGISTER'}

    def execute(self,context):
        try:
            from .mesh_diagnostics import write_report
            write_report(context.scene, context.active_object, 'FIELD_BUNDLE')
        except Exception as exc:
            log_exception('FIELD_BUNDLE_MESH_WARNING',exc)
        try:
            path=create_bundle(context.scene,context.active_object)
        except Exception as exc:
            log_exception('FIELD_BUNDLE_FAILED',exc)
            self.report({'ERROR'},str(exc))
            return {'CANCELLED'}
        self.report({'INFO'},f'Пакет DWOS сохранён: {path}')
        return {'FINISHED'}
