"""Read-only Blender adapter for DWOS Mesh Consistency RC13.

Never rebuilds meshes or changes selection/active objects/modes.
The heavy traversal is opt-in through operator or support ZIP export.
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
import bpy
from .mesh_consistency_core import audit_window
from .dwos_log import log_dir, event as log_event, exception as log_exception


def _mesh_payload(obj,roles_key):
    if obj is None or getattr(obj,'type',None)!='MESH':
        return {'verts':[],'faces':[],'roles':[]}
    try:
        roles=json.loads(obj.get(roles_key,'[]') or '[]')
        if not isinstance(roles,list):roles=[]
    except (TypeError,ValueError):roles=[]
    return {'verts':[tuple(float(x) for x in v.co) for v in obj.data.vertices],
            'faces':[tuple(int(i) for i in p.vertices) for p in obj.data.polygons],
            'roles':roles}


def inspect_frame(frame):
    cls=str(frame.get('DWOS_adaptive_classification','')).upper()
    rec={'name':frame.name,'classification':cls,'corner_mode':str(frame.get('DWOS_corner_post_mode','')),
         'corner_post_count':frame.get('DWOS_corner_post_count'), 'generated_at':datetime.now().isoformat()}
    if cls not in ('CURVED','CORNER'):
        rec.update(status='NOT_APPLICABLE',issues=[])
        return rec
    if getattr(frame,'mode','OBJECT')=='EDIT':
        rec.update(status='UNVERIFIED_EDIT_MODE',issues=['FRAME_EDIT_MODE_DATA_NOT_SYNCHRONIZED'])
        return rec
    # Using exact child labels avoids accidentally examining another window's
    # GLASS child. Missing, duplicate and stale role metadata are failures.
    glass=[o for o in frame.children if o.get('DWOS_role')=='GLASS' and o.type=='MESH']
    if any(getattr(o,'mode','OBJECT')=='EDIT' for o in glass):
        rec.update(status='UNVERIFIED_EDIT_MODE',issues=['GLASS_EDIT_MODE_DATA_NOT_SYNCHRONIZED'])
        return rec
    if len(glass)!=1:
        rec.update(status='FAIL',issues=[f'GLASS_CHILD_COUNT_{len(glass)}'])
        return rec
    expected=frame.get('DWOS_corner_glass_panel_count') if cls=='CORNER' else None
    independent=bool(frame.get('DWOS_corner_post_independent',False))
    contract=audit_window(_mesh_payload(frame,'DWOS_face_roles'),
                          _mesh_payload(glass[0],'DWOS_glass_roles'),cls,
                          expected_panels=expected)
    rec.update(contract)
    if independent and cls=='CORNER':
        from .corner_post_experiment_core import audit_open_shell
        rec['independent_corner_post']=audit_open_shell(_mesh_payload(frame,'DWOS_face_roles'))
        rec['independent_corner_post']['mode']=str(frame.get('DWOS_corner_post_variant',''))
        rec['independent_corner_post']['width_effective_m']=float(frame.get('DWOS_corner_post_width_effective_m',0))
        if rec['independent_corner_post']['status']!='PASS':
            rec['status']='FAIL'
            rec['issues'].extend(rec['independent_corner_post']['issues'])
    rec['glass_name']=glass[0].name
    rec['published_frame_role_faces']=len(frame.data.polygons)
    rec['published_glass_role_faces']=len(glass[0].data.polygons)
    if cls=='CURVED' and glass[0].get('DWOS_curved_glass_layer_count') is not None:
        if int(glass[0].get('DWOS_curved_glass_layer_count'))!=2:
            rec['issues'].append('CURVED_LAYER_METADATA_MISMATCH')
            rec['status']='FAIL'
    if cls=='CORNER':
        # Physical 3D check missed by legacy mesh-consistency: two layers can
        # exist while the pane is visibly twisted relative to its facade.
        from .glass_quality_core import corner_glass_plane_quality
        try:
            payload=json.loads(frame.dwos_window.opening_payload_json or '{}')
            topo=payload.get('adaptive_topology')
            rec['glass_plane_quality']=corner_glass_plane_quality(_mesh_payload(glass[0],'DWOS_glass_roles'),topo)
            if rec['glass_plane_quality']['status']=='WARNING':
                rec['issues'].extend(rec['glass_plane_quality']['issues'])
                if rec['status']=='PASS':rec['status']='WARNING'
        except Exception as exc:
            rec['glass_plane_quality']={'status':'UNVERIFIED','issues':[str(exc)]}
        # RC19: physical face / strictly planar pane feasibility is a
        # separate contract. Four panes alone never certify central contact.
        try:
            from .payload_core import decode_opening_payload, adaptive_topology_from_payload
            from .corner_rules import corner_posts
            from .corner_section_quality_core import corner_section_feasibility
            from .numeric import parse_meter_text
            st=frame.dwos_window
            topology=adaptive_topology_from_payload(decode_opening_payload(st.opening_payload_json))
            if topology is None:
                raise ValueError('CORNER topology not recoverable from the authored Host payload')
            mode=str(getattr(st,'corner_post_mode','AUTO'))
            depth=parse_meter_text(st.frame_depth_text)
            posts=corner_posts(topology,mode,parse_meter_text(st.internal_width_text),depth)
            if posts and independent:
                rec['glass_central_section']={'status':'EXPERIMENTAL_OPEN_SHELL',
                    'reason':'Independent post has 4 uncapped quads; physical glazing stops not claimed'}
                rec['issues'].append('CORNER_POST_EXPERIMENT_CONTACT_UNVERIFIED')
                if rec['status']=='PASS':rec['status']='WARNING'
            elif posts:
                if glass[0].get('DWOS_corner_glass_contact_policy')=='RIGID_TWO_BRANCHES_UNDER_POST_RC20':
                    rec['glass_post_clearance']={'status':'UNVERIFIED',
                        'reason':'Two rigid glass branches continue under central post; possible concealed intersection. Geometric post glazing-stop not modeled.'}
                    rec['issues'].append('CORNER_GLASS_POST_CLEARANCE_UNVERIFIED')
                    if rec['status']=='PASS':rec['status']='WARNING'
                sign=1 if st.flip_interior else -1
                front=float(frame.get('DWOS_frame_front_z',parse_meter_text(st.frame_offset_text,allow_negative=True)))
                offset=parse_meter_text(st.glass_offset_text,allow_negative=True)
                center=front+sign*(depth*.5+offset)
                gap=parse_meter_text(st.glass_gap_text)
                frame_data=_mesh_payload(frame,'DWOS_face_roles')
                frame_data['z_front']=front
                frame_data['z_back']=front+sign*depth
                physical=corner_section_feasibility(frame_data,topology,posts,center,gap,sign)
                rec['glass_central_section']=physical
                # RC22: quantify the inner-sheet miter lying beyond the physical
                # post's back-tip, a frequent hidden source of interior gaps.
                from .corner_collision_core import inner_miter_to_post_distance
                inner_depth=center+sign*gap*.5
                miter=inner_miter_to_post_distance(topology,posts,depth,front,inner_depth)
                rec['glass_inner_miter_vs_post']=miter
                if miter.get('status')=='WARNING':
                    rec['issues'].append('CORNER_INNER_MITER_BEYOND_POST_TIP')
                    if rec['status']=='PASS':rec['status']='WARNING'
                if physical['status']!='PASS':
                    rec['issues'].extend(physical.get('issues',[]))
                    if rec['status']=='PASS':rec['status']='WARNING'
            else:
                rec['glass_central_section']={'status':'NOT_APPLICABLE','issues':[],
                    'reason':'No physical central post; planar miter audited separately'}
            # Existing invalid layouts must remain inspectable without rebuild.
            # The same physical gate is used before publishing future changes.
            from .corner_collision_core import audit_vertical_collisions
            from .branch_core import decode_state, rows_as_dicts
            fw=parse_meter_text(st.frame_width_text)
            branch_state=decode_state(getattr(st,'branch_state_json',''),topology)
            physical_rows=rows_as_dicts(branch_state,topology,float(topology['height']),fw,posts)
            custom_poly=None
            if independent:
                from .corner_post_experiment_core import plan_open_post
                post=plan_open_post(topology,posts,fw,depth,front,1 if st.flip_interior else -1,
                    parse_meter_text(st.corner_post_width_text),str(st.corner_post_variant))
                custom_poly=post['footprint']
            collisions=audit_vertical_collisions(topology,physical_rows,posts,depth,front,
                1 if st.flip_interior else -1,post_polygon=custom_poly)
            rec['corner_vertical_collisions']=collisions
            if collisions:
                rec['issues'].extend(sorted(set(q['code'] for q in collisions)))
                if rec['status']=='PASS':rec['status']='WARNING'
        except Exception as exc:
            rec['glass_central_section']={'status':'UNVERIFIED',
                'issues':['CORNER_SECTION_AUDIT_UNAVAILABLE'],
                'detail':f'{type(exc).__name__}: {exc}'}
            rec['issues'].append('CORNER_SECTION_AUDIT_UNAVAILABLE')
            if rec['status']=='PASS':rec['status']='WARNING'
        post_count=int(frame.get('DWOS_corner_post_count',-1))
        # AUTO may deliberately resolve to OFF for an acute corner. Compare
        # physical geometry to the effective mode, not the saved preference.
        mode=str(frame.get('DWOS_corner_post_effective_mode',frame.get('DWOS_corner_post_mode','')))
        if (mode=='OFF' and post_count!=0) or (mode=='ON' and post_count!=1):
            rec['issues'].append('CORNER_POST_MODE_METADATA_MISMATCH')
            rec['status']='FAIL'
    return rec


def inspect_scene(scene):
    reports=[]
    for obj in scene.objects:
        s=getattr(obj,'dwos_window',None)
        if getattr(s,'is_window',False):
            try:reports.append(inspect_frame(obj))
            except Exception as exc:
                log_exception('MESH_CONSISTENCY_WINDOW_WARNING',exc,window=obj.name)
                reports.append({'name':obj.name,'status':'FAIL','issues':[f'INSPECT_EXCEPTION: {type(exc).__name__}: {exc}']})
    return {'schema':'DWOS_mesh_consistency_v1','report_version':'3.7.37-rc38',
            'windows':reports,'failed':sum(r['status']=='FAIL' for r in reports),
            'passed':sum(r['status']=='PASS' for r in reports),
            'not_applicable':sum(r['status']=='NOT_APPLICABLE' for r in reports),
            'unverified':sum(r['status']=='UNVERIFIED_EDIT_MODE' for r in reports),
            'warnings':sum(r['status']=='WARNING' for r in reports)}


def save_report(scene):
    report=inspect_scene(scene)
    path=Path(log_dir())/'dwos-mesh-consistency-latest.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    log_event('MESH_CONSISTENCY_AUDIT',pass_count=report['passed'],fail_count=report['failed'],warnings=report.get('warnings',0),report=str(path))
    return report,path


class DWOS_OT_audit_mesh_consistency(bpy.types.Operator):
    bl_idname='dwos.audit_mesh_consistency'
    bl_label='Проверить геометрию DWOS'
    bl_description='Прочитать реальные Mesh рам и стекла, проверить связность и два слоя без изменения сцены'
    bl_options={'REGISTER'}

    def execute(self,context):
        try:rep,path=save_report(context.scene)
        except Exception as exc:
            log_exception('MESH_CONSISTENCY_AUDIT_FAILED',exc)
            self.report({'ERROR'},str(exc));return {'CANCELLED'}
        if rep['failed'] or rep['unverified'] or rep.get('warnings',0):
            self.report({'WARNING'},f"Mesh Consistency: {rep['failed']} ошибок, {rep.get('warnings',0)} предупреждений, {rep['unverified']} не проверены; {path}")
        else:
            self.report({'INFO'},f"Mesh Consistency: {rep['passed']} PASS, {rep['not_applicable']} N/A; {path}")
        return {'FINISHED'}
