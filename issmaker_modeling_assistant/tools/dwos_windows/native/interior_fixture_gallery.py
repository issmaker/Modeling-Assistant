"""Blender-only bridge for five controlled Smart Interior contact fixtures.

Creates one isolated SURFACE host Mesh with five wall patches and ten genuine
apertures. NEVER runs Create Windows or rewrites existing user geometry.
"""
from __future__ import annotations
import json
from collections import Counter
import bpy
from mathutils import Vector
from .interior_fixture_core import (SCHEMA_INTERIOR,build_interior_contact_gallery,
                                    validate_interior_contact_gallery)
from .adaptive_detection_core import discover_surface_openings
from .dwos_log import event as log_event,exception as log_exception


def is_interior_gallery(obj):
    return obj is not None and obj.type=='MESH' and obj.get('DWOS_fixture_schema')==SCHEMA_INTERIOR


def check_interior_openings(data, *, actual_vertices=None,actual_faces=None,actual_normals=None):
    vertices=actual_vertices if actual_vertices is not None else data['vertices']
    faces=actual_faces if actual_faces is not None else data['faces']
    info=discover_surface_openings(vertices,faces,face_normals=actual_normals,
                                   up_hint=(0,0,1),source='DWOS_INTERIOR_CONTACT_FIXTURE')
    opened=info['openings'];reports=[]
    for case in data['cases']:
        lo=case['vertex_start'];hi=lo+case['vertex_count']
        group=[r for r in opened if r.get('loop_vertex_ids') and
               all(lo<=vid<hi for vid in r['loop_vertex_ids'])]
        if len(group)!=2 or any(not r.get('build_supported') or r.get('classification')!='PLANAR' for r in group):
            raise ValueError(f"{case['id']}: expected two supported PLANAR openings, got "
                             f"{[(r.get('classification'),r.get('reasons')) for r in group]}")
        reports.append({'id':case['id'],'scenario':case['scenario'],
                        'openings':2,'detected':['PLANAR','PLANAR'],
                        'depth_m':case['depth_m'],
                        'projected_overlap_m2':case['projected_overlap_m2'],
                        'expected_contact':'POTENTIAL_WITH_INWARD_BOX'})
    if len(opened)!=10:raise ValueError(f"Expected 10 openings, detected {len(opened)}")
    return {'schema':SCHEMA_INTERIOR,'status':'GEOMETRY_READY','passed_openings':10,
            'expected_openings':10,'cases':reports,'boundary_edges':info['boundary_edges'],
            'face_components':info['face_components'],
            'note':'Overlap is a 2D stress fixture; actual repair requires DWOS windows and compatible Interior direction/depth.'}


def _window_center_in_host(frame,host):
    if not frame.data.vertices:raise ValueError(f'{frame.name}: empty frame')
    pts=[host.matrix_world.inverted() @ (frame.matrix_world @ v.co) for v in frame.data.vertices]
    return (sum(p.x for p in pts)/len(pts),sum(p.y for p in pts)/len(pts))


def check_live_interior_contacts(context,host,data):
    """Read-only actual contact validation; expected collisions are NOT fabricated."""
    from .smart_interior_scene import decode,_world,_mesh_data
    from .smart_interior_core import collision_graph
    by_case={c['id']:[] for c in data['cases']}
    for frame in context.scene.objects:
        if frame.type!='MESH':continue
        prop=getattr(frame,'dwos_window',None)
        if not prop or not getattr(prop,'is_window',False) or prop.host_name!=host.name:continue
        px,py=_window_center_in_host(frame,host)
        found=None;best=float('inf')
        for case in data['cases']:
            nearest=min((px-cx)**2+(py-cy)**2 for cx,cy in case['opening_centers_xy'])
            if nearest<best:found=case['id'];best=nearest
        if best<4.0**2:by_case[found].append(frame)
    rows=[]
    for case in data['cases']:
        frames=by_case[case['id']]
        row={'id':case['id'],'scenario':case['scenario'],'window_count':len(frames),
             'expected_windows':2,'projected_overlap_m2':case['projected_overlap_m2']}
        if len(frames)!=2:
            row.update(status='WAITING_WINDOWS' if len(frames)<2 else 'EXTRA_WINDOWS',
                       note='Create exactly two windows on this wall before checking actual Smart Interior.')
            rows.append(row);continue
        cavities=[]
        for frame in frames:
            children=[o for o in frame.children if o.type=='MESH' and o.get('DWOS_role')=='INTERIOR']
            if len(children)!=1:break
            cavities.append(children[0])
        if len(cavities)!=2:
            row.update(status='WAITING_INTERIORS',interior_count=len(cavities))
            rows.append(row);continue
        source={frame.name:_world(decode(cavity),cavity.matrix_world)
                for frame,cavity in zip(frames,cavities)}
        published={frame.name:_world(_mesh_data(cavity),cavity.matrix_world)
                   for frame,cavity in zip(frames,cavities)}
        canonical_pairs=collision_graph(source)
        residual=collision_graph(published)
        from .interior_footprint_core import footprint_clearance_xy
        actual_gap=footprint_clearance_xy(*published.values())
        required_gap=float(context.scene.dwos.smart_interior_gap)
        gap_ok=(actual_gap is None or actual_gap+1e-5>=required_gap)
        pending=any(bool(cavity.get('DWOS_smart_interior_pending',False) or
                             frame.get('DWOS_smart_interior_pending',False))
                    for frame,cavity in zip(frames,cavities))
        if pending:status='PENDING'
        elif residual:status='UNRESOLVED'
        elif canonical_pairs and actual_gap is None:status='UNVERIFIED_CLEARANCE'
        elif canonical_pairs and not gap_ok:status='INSUFFICIENT_CLEARANCE'
        elif canonical_pairs:status='REPAIRED'
        else:status='NO_CONTACT'
        reasons=[{'window':frame.name,
                  'code':str(frame.get('DWOS_smart_interior_reason_code','')),
                  'reason':str(frame.get('DWOS_smart_interior_reason',''))}
                 for frame in frames if frame.get('DWOS_smart_interior_pending',False)]
        row.update(status=status,canonical_pairs=len(canonical_pairs),
                   remaining_pairs=len(residual),pending=pending,
                   actual_clearance_xy_m=(round(actual_gap,6) if actual_gap is not None else None),
                   requested_clearance_m=required_gap,
                   reason_details=reasons,
                   owners=[frame.name for frame in frames])
        rows.append(row)
    return {'status':'PASS' if all(x['status']=='REPAIRED' for x in rows) else 'REVIEW',
            'cases':rows,'smart_interior_enabled':bool(context.scene.dwos.smart_interior_enabled),
            'clearance_m':float(context.scene.dwos.smart_interior_gap),
            'mode':str(context.scene.dwos.smart_interior_mode),
            'note':'REPAIRED requires a real canonical collision and no remaining physical mesh collision; PENDING is not PASS.'}


class DWOS_OT_set_interior_gap_003(bpy.types.Operator):
    bl_idname='dwos.set_interior_gap_003'
    bl_label='Установить зазор 0,03 м'
    bl_description='Установить расстояние между Interior равным 0,03 метра (3 см)'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        context.scene.dwos.smart_interior_gap=0.03
        self.report({'INFO'},'Зазор Smart Interior: 0,03 м (3 см)')
        return {'FINISHED'}


class DWOS_OT_run_interior_fixture_auto(bpy.types.Operator):
    bl_idname='dwos.run_interior_fixture_auto'
    bl_label='AUTO · исправить 5 стен'
    bl_description='Включить Smart Interior, установить 0,03 м, пересчитать все контакты и проверить реальные пять примеров'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        return is_interior_gallery(context.active_object) and context.mode=='OBJECT'

    def execute(self,context):
        host=context.active_object
        try:
            # This is an EXPLICIT user action. Do not silently migrate legacy
            # scenes with user-authored 0.0003m values during add-on load.
            state=context.scene.dwos
            state.smart_interior_enabled=True
            state.smart_interior_gap=0.03
            state.smart_interior_mode='BALANCED'
            from .smart_interior_auto import refresh
            stats=refresh(context.scene,all_contacts=True)
            current=build_interior_contact_gallery()
            report=check_live_interior_contacts(context,host,current)
            host['DWOS_fixture_auto_report_json']=json.dumps(
                {'schema':SCHEMA_INTERIOR,'version':'3.7.26-rc27',
                 'requested_clearance_m':0.03,'auto_refresh':stats,
                 'live_interiors':report},ensure_ascii=False,separators=(',',':'))
            host['DWOS_fixture_validation_json']=json.dumps(
                {'schema':SCHEMA_INTERIOR,'geometry':check_interior_openings(current),
                 'live_interiors':report,'status':report['status']},
                ensure_ascii=False,separators=(',',':'))
            tally=Counter(x['status'] for x in report['cases'])
            summary=', '.join(f'{name}={n}' for name,n in sorted(tally.items()))
            log_event('INTERIOR_FIXTURE_AUTO_EXPLICIT',host=host.name,clearance_m=0.03,
                      refresh=stats,results=dict(tally))
            if report['status']!='PASS':
                failed=[x for x in report['cases'] if x['status']!='REPAIRED']
                self.report({'WARNING'},'AUTO 0,03 м: '+summary+'; подробности в диагностическом ZIP')
                return {'FINISHED'}
            self.report({'INFO'},'AUTO 0,03 м: I01–I05 REPAIRED (5/5)')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('INTERIOR_FIXTURE_AUTO_FAILED',exc,host=host.name)
            self.report({'ERROR'},'AUTO 0,03 м: '+str(exc))
            return {'CANCELLED'}


class DWOS_OT_create_interior_fixture_gallery(bpy.types.Operator):
    bl_idname='dwos.create_interior_fixture_gallery'
    bl_label='Создать 5 стен с конфликтами Interior'
    bl_description='Пять независимых стен (один Mesh), десять настоящих проёмов. Каждая пара может дать контакт Interior BOX'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):return context.scene is not None and context.mode=='OBJECT'

    def execute(self,context):
        old_active=context.view_layer.objects.active;old_selected=tuple(context.selected_objects)
        obj=None;mesh=None
        try:
            data=build_interior_contact_gallery()
            validate_interior_contact_gallery(data)
            preliminary=check_interior_openings(data)
            mesh=bpy.data.meshes.new('DWOS_5_Interior_Contact_Walls_Mesh')
            mesh.from_pydata(data['vertices'],[],data['faces']);mesh.update(calc_edges=True)
            if len(mesh.vertices)!=len(data['vertices']) or len(mesh.polygons)!=len(data['faces']):
                raise RuntimeError('Blender unexpectedly altered fixture topology')
            result=check_interior_openings(data,
                  actual_vertices=[tuple(v.co) for v in mesh.vertices],
                  actual_faces=[tuple(p.vertices) for p in mesh.polygons],
                  actual_normals=[tuple(p.normal) for p in mesh.polygons])
            if result['passed_openings']!=preliminary['passed_openings']:raise ValueError('Fixture classifications changed in Blender')
            obj=bpy.data.objects.new('DWOS_5_Interior_Contact_Fixtures',mesh)
            context.collection.objects.link(obj)
            material=bpy.data.materials.get('DWOS · Fixture INTERIOR CONTACT')
            if material is None:
                material=bpy.data.materials.new('DWOS · Fixture INTERIOR CONTACT')
                material.diffuse_color=(.47,.30,.63,1.0)
            mesh.materials.append(material)
            attr=mesh.attributes.new('DWOS_fixture_case','INT','FACE')
            for fi,case_index in enumerate(data['face_case_ids']):attr.data[fi].value=case_index+1
            obj['DWOS_fixture_schema']=SCHEMA_INTERIOR
            obj['DWOS_fixture_variant']='INTERIOR'
            obj['DWOS_fixture_version']='3.7.26-rc27'
            obj['DWOS_fixture_manifest_json']=json.dumps(data['cases'],ensure_ascii=False,separators=(',',':'))
            obj['DWOS_fixture_validation_json']=json.dumps(result,ensure_ascii=False,separators=(',',':'))
            obj['DWOS_fixture_expected_count']=10
            obj['DWOS_fixture_wall_mode']='SURFACE'
            obj['DWOS_fixture_hint']='Find 10 openings → create windows → Interior BOX → check 5 Smart Interior contacts'
            obj.location=context.scene.cursor.location.copy()
            for selected in old_selected:
                try:selected.select_set(False)
                except (ReferenceError,RuntimeError):pass
            obj.select_set(True);context.view_layer.objects.active=obj
            log_event('INTERIOR_FIXTURE_GALLERY_CREATE',object=obj.name,cases=5,openings=10,
                      projected_overlap=[c['projected_overlap_m2'] for c in data['cases']])
            self.report({'INFO'},'5 стен / 10 проёмов: включите Smart Interior и создайте окна с BOX')
            return {'FINISHED'}
        except Exception as exc:
            if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            if mesh is not None and mesh.users==0:bpy.data.meshes.remove(mesh)
            for selected in old_selected:
                try:selected.select_set(True)
                except (ReferenceError,RuntimeError):pass
            try:context.view_layer.objects.active=old_active
            except (ReferenceError,RuntimeError):pass
            log_exception('INTERIOR_FIXTURE_CREATE_FAILED',exc)
            self.report({'ERROR'},f'Interior fixture: {type(exc).__name__}: {exc}')
            return {'CANCELLED'}


class DWOS_OT_check_interior_fixture_gallery(bpy.types.Operator):
    bl_idname='dwos.check_interior_fixture_gallery'
    bl_label='Проверить 5 стен / Smart Interior'
    bl_description='Перепроверить реальные проёмы и состояние ремонта существующих Interior, не изменяя сцену'
    bl_options={'REGISTER'}

    @classmethod
    def poll(cls,context):return is_interior_gallery(context.active_object) and context.mode=='OBJECT'

    def execute(self,context):
        obj=context.active_object
        try:
            reference=build_interior_contact_gallery()
            actual=check_interior_openings(reference,
                    actual_vertices=[tuple(v.co) for v in obj.data.vertices],
                    actual_faces=[tuple(p.vertices) for p in obj.data.polygons],
                    actual_normals=[tuple(p.normal) for p in obj.data.polygons])
            live=check_live_interior_contacts(context,obj,reference)
            report={'schema':SCHEMA_INTERIOR,'geometry':actual,'live_interiors':live,'status':live['status']}
            obj['DWOS_fixture_validation_json']=json.dumps(report,ensure_ascii=False,separators=(',',':'))
            summary=Counter(x['status'] for x in live['cases'])
            log_event('INTERIOR_FIXTURE_CHECK',object=obj.name,openings=10,live=dict(summary))
            self.report({'INFO'},'10/10 проёмов. Interior: '+', '.join(f'{s}={n}' for s,n in sorted(summary.items())))
            return {'FINISHED'}
        except Exception as exc:
            obj['DWOS_fixture_validation_json']=json.dumps({'schema':SCHEMA_INTERIOR,
                'status':'FAIL','error':f'{type(exc).__name__}: {exc}'},ensure_ascii=False)
            log_exception('INTERIOR_FIXTURE_CHECK_FAILED',exc,object=obj.name)
            self.report({'ERROR'},f'Interior fixture: {type(exc).__name__}: {exc}')
            return {'CANCELLED'}

CLASSES=(DWOS_OT_set_interior_gap_003,DWOS_OT_run_interior_fixture_auto,DWOS_OT_create_interior_fixture_gallery,DWOS_OT_check_interior_fixture_gallery)
