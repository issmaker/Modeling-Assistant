"""Blender-side BH-03.1 native evidence lab, no external dependencies.

The entry point run_lab(context) creates a separate scene in Object Mode,
uses actual bpy operators, archives result evidence and restores the window.
Only real assertions against Blender BMesh are labeled PASS.
"""
import json
import math
import os
import platform
import tempfile
import time
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import bpy
import bmesh


def _require(condition, message):
    # Never use bare assert: Blender may run with Python optimizations enabled.
    if not condition:
        raise RuntimeError(message)


def _inner_operator_log():
    """Read registered Brick Hole diagnostic text immediately on operator failure."""
    try:
        from . import LOG_TEXT_NAME
        block=bpy.data.texts.get(LOG_TEXT_NAME)
        return block.as_string()[-24000:] if block else 'MISSING_BRICK_HOLE_LOG'
    except Exception as exc:
        return 'READ_ERROR: '+repr(exc)


def _expected_rejection(case_name, exc):
    """Do not count arbitrary RuntimeErrors as correct rejection."""
    patterns={
        'REJECT_CONNECTED':('Edge Smart UV: связанные рёбра', 'связанные рёбра'),
        'REJECT_NO_UV':('Не найдена выбранная UV-карта','UV-карта не найдена'),
        'REJECT_ZERO_UV':('UV исходного ребра вырождена','UV исходного ребра'),
    }
    text=str(exc)
    return any(marker in text for marker in patterns.get(case_name,()))


def _uv_key(coords):
    return tuple(round(float(x), 7) for x in coords)


def _snapshot(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    uv = bm.loops.layers.uv.active
    vs = sorted(tuple(round(float(x), 6) for x in v.co) for v in bm.verts)
    es = sorted(tuple(sorted((tuple(round(float(x), 6) for x in v.co) for v in e.verts))) for e in bm.edges)
    fs = sorted(tuple(sorted(tuple(round(float(x), 6) for x in v.co) for v in f.verts)) for f in bm.faces)
    # Full loop coordinates keyed by world-local vertex, not unstable indices
    uvfaces = sorted(tuple(sorted((tuple(round(float(c),6) for c in l.vert.co),
                                   _uv_key(l[uv].uv)) for l in f.loops))
                     for f in bm.faces) if uv is not None else []
    return {'vertices':vs,'edges':es,'faces':fs,'uvfaces':uvfaces}


def _build_case(scene, label, length=4., density=750., choice='top_bottom',
                include_uv=True, corrupt_uv=False):
    mesh = bpy.data.meshes.new('ISSMAKER_BH031_NATIVE_'+label+'_MESH')
    mesh.from_pydata([(0.,0.,0.),(length,0.,0.),(length,0.,4.),(0.,0.,4.)],[],[(0,1,2,3)])
    mesh.update()
    if include_uv:
        uv=mesh.uv_layers.new(name='UVMap')
        half=length*density/4096./2.
        raw=[(.5-half,.27),(.5+half,.27),(.5+half,.73),(.5-half,.73)]
        if corrupt_uv:
            raw=[(.5,.27),(.5,.27),(.5,.73),(.5,.73)]
        for corner_idx, idx in enumerate(mesh.polygons[0].loop_indices):
            uv.data[idx].uv=raw[corner_idx]
    obj=bpy.data.objects.new('ISSMAKER_BH031_NATIVE_'+label,mesh)
    scene.collection.objects.link(obj)
    # Each test starts from OBJECT in an isolated scene; previously active
    # meshes are deselected before changing active object.
    if bpy.context.object and bpy.context.object.mode=='EDIT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for other in bpy.context.selected_objects:
        other.select_set(False)
    bpy.context.view_layer.objects.active=obj
    obj.select_set(True)
    bpy.context.tool_settings.mesh_select_mode=(False,True,False)
    bpy.ops.object.mode_set(mode='EDIT')
    bm=bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:f.select_set(False)
    for e in bm.edges:e.select_set(False)
    for v in bm.verts:v.select_set(False)
    sel=[]
    for e in bm.edges:
        z=[float(v.co.z) for v in e.verts]
        x=[float(v.co.x) for v in e.verts]
        if max(z)-min(z)<1e-6 and max(x)-min(x)>length-1e-5:
            if choice in ('top_bottom','top','connected') and abs(z[0]-4.)<1e-5:sel.append(e)
            if choice in ('top_bottom','bottom') and abs(z[0])<1e-5:sel.append(e)
        if choice=='connected' and max(z)-min(z)>3.99 and all(abs(t)<1e-5 for t in x):
            sel.append(e)
    for e in sel:e.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
    return obj,mesh,len(sel)


def _validate_success(obj, before, mode, edges, length, source_ppm):
    bm=bmesh.from_edit_mesh(obj.data)
    uv=bm.loops.layers.uv.active
    _require(uv is not None, 'UV layer missing after operation')
    policy=bm.faces.layers.int.get('brick_hole_uv_policy')
    _require(policy is not None, 'UV policy layer missing')
    target_policy=1 if mode=='CONTINUE' else 2
    generated=[f for f in bm.faces if f[policy]==target_policy]
    unchanged=[f for f in bm.faces if f[policy]==0]
    _require(len(unchanged)==1, f'original face count={len(unchanged)}, expected 1')
    expected=edges*(1 if mode=='CONTINUE' else 4)
    _require(len(generated)==expected, f'generated faces={len(generated)}, expected={expected}')
    original_uvfaces=tuple(sorted((tuple(round(float(c),6) for c in l.vert.co),
                                    _uv_key(l[uv].uv)) for l in unchanged[0].loops))
    _require(original_uvfaces==before['uvfaces'][0], 'original authored UV changed')
    # Existing original physical vertices must remain present.
    current_vs={tuple(round(float(c),6) for c in v.co) for v in bm.verts}
    _require(set(before['vertices']) <= current_vs, 'original vertex moved or deleted')
    _require(all(f.calc_area()>1e-8 for f in generated), 'degenerate new face')
    uv_safe_lo=48/4096-2e-6; uv_safe_hi=1-48/4096+2e-6
    for f in generated:
        for l in f.loops:
            _require(all(uv_safe_lo<=float(z)<=uv_safe_hi for z in l[uv].uv), 'UV outside safe tile')
    seam=[]
    # Check the UV values on original boundary vertices where new faces touch them.
    old_by_vert={tuple(round(float(c),6) for c in l.vert.co): _uv_key(l[uv].uv)
                 for l in unchanged[0].loops}
    for f in generated:
        for l in f.loops:
            key=tuple(round(float(c),6) for c in l.vert.co)
            if key in old_by_vert:
                diff=math.dist(tuple(l[uv].uv),old_by_vert[key])
                _require(diff<2e-6, f'UV seam mismatch={diff} at {key}')
                seam.append(diff)
    _require(len(seam)>=2*edges, f'only {len(seam)} UV seam corners validated')
    # New strip has generated vertices offset from original wall, not just original faces.
    yoff=[abs(float(v.co.y)) for v in bm.verts if abs(float(v.co.y))>1e-6]
    _require(yoff and abs(max(yoff)-.3)<1e-5, f'bad depth: {yoff}')
    far_uvs=[]
    far_world={}
    for f in generated:
        for l in f.loops:
            co=l.vert.co
            if abs(abs(float(co.y))-.3)<1e-5:
                # grouping by Z distinguishes top/bottom wall-edge strips
                group=round(float(co.z),5)
                far_world.setdefault(group,{})[round(float(co.x),5)]=tuple(float(t) for t in l[uv].uv)
    target=source_ppm if mode=='CONTINUE' else 1000.
    for z, group in far_world.items():
        if len(group)>=2:
            pts=sorted(group.items())
            span=math.dist(pts[0][1],pts[-1][1])
            measured=span*4096./length
            _require(abs(measured-target)<.2, f'far UV density={measured:.3f}, target={target}')
            far_uvs.append({'z':z,'ppm':round(measured,5)})
    _require(len(far_uvs)==edges, f'far edges measured={len(far_uvs)} expected={edges}')
    return {'new_faces':len(generated),'source_uv_protected':True,
            'seam_corners_checked':len(seam),'far_uv_density':far_uvs,
            'vertices':len(bm.verts),'faces':len(bm.faces)}


def run_lab(context):
    if context.window is None or context.mode!='OBJECT':
        raise RuntimeError('BH Native Lab: откройте Blender в Object Mode и обычном окне')
    original_scene=context.window.scene
    lab_scene=None
    old_mesh_mode=tuple(context.tool_settings.mesh_select_mode)
    started=time.time()
    records=[]
    warnings=[]
    restored=False
    case_matrix=[
      ('CONTINUE_750_TWO','CONTINUE',4.,750.,'top_bottom',True,False,True),
      ('ADAPTIVE_750_TWO','ADAPTIVE',4.,750.,'top_bottom',True,False,True),
      ('CONTINUE_750_TOP','CONTINUE',4.,750.,'top',True,False,True),
      ('ADAPTIVE_1200_SHORT','ADAPTIVE',2.,1200.,'bottom',True,False,True),
      ('REJECT_CONNECTED','CONTINUE',4.,750.,'connected',True,False,False),
      ('REJECT_NO_UV','CONTINUE',4.,750.,'top',False,False,False),
      ('REJECT_ZERO_UV','ADAPTIVE',4.,750.,'bottom',True,True,False),
    ]
    try:
        lab_scene=bpy.data.scenes.new('ISSMAKER_BH031_LAB_'+uuid.uuid4().hex[:8])
        context.window.scene=lab_scene
        for name, mode, length, ppm, selection, has_uv, corrupt_uv, should_pass in case_matrix:
            obj=None
            case_start=time.perf_counter()
            try:
                obj,mesh,selected=_build_case(lab_scene,name,length,ppm,selection,
                                               has_uv,corrupt_uv)
                lab_scene.brick_hole_edge_uv_mode=mode
                lab_scene.brick_hole_uv_policy=mode
                lab_scene.brick_hole_uv_map='UVMap'
                lab_scene.brick_hole_uv_transition_m=.25
                lab_scene.brick_hole_depth=.3
                lab_scene.brick_hole_direction='OUTWARD'
                lab_scene.brick_hole_direction_source='AUTO'
                lab_scene.brick_hole_resolution='4096'
                lab_scene.brick_hole_material_mode='INHERIT'
                before=_snapshot(obj)
                try:
                    outcome=bpy.ops.mesh.brick_hole_edge_smart_uv()
                except RuntimeError as exc:
                    if should_pass or not _expected_rejection(name,exc):
                        raise
                    # Blender Python API can raise RuntimeError for an operator
                    # that safely returns CANCELLED after self.report(ERROR).
                    # This is PASS only for the EXPECTED failure reason and
                    # bitwise-equivalent source Mesh/UV snapshot.
                    outcome={'CANCELLED'}
                after=_snapshot(obj)
                if should_pass:
                    _require('FINISHED' in outcome, 'operator rejected a valid case')
                    data=_validate_success(obj,before,mode,selected,length,ppm)
                    status='PASS';detail=data
                else:
                    _require('CANCELLED' in outcome, 'invalid case was accepted')
                    _require(after==before, 'rollback changed original geometry/UV')
                    status='PASS';detail={'expected_rejection':True,'rollback_identical':True}
            except Exception as exc:
                status='FAIL';detail={'reason':str(exc),'traceback':traceback.format_exc()[-14000:],
                                      'brick_hole_internal_log':_inner_operator_log()}
            records.append({'case':name,'status':status,'mode':mode,
                            'elapsed_ms':round((time.perf_counter()-case_start)*1000,2),
                            'selected_edges':selected if obj else None,'evidence':detail})
            # Reset the previous test to OBJECT before selecting the next mesh.
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as exc:
                warnings.append('exit_edit_'+name+': '+str(exc))
            if obj is not None:
                try:
                    bpy.data.objects.remove(obj,do_unlink=True)
                except Exception as exc:
                    warnings.append('cleanup_'+name+': '+str(exc))
            # Do not leave lab-created orphan Mesh data in the real .blend.
            for mesh_data in tuple(bpy.data.meshes):
                if mesh_data.name.startswith('ISSMAKER_BH031_NATIVE_') and mesh_data.users == 0:
                    try: bpy.data.meshes.remove(mesh_data)
                    except Exception as exc: warnings.append('cleanup_mesh_'+name+': '+str(exc))
        # BH-03.2A independent native tessellation fixtures. These examine
        # Blender's real BMesh tessellation and density gate, not fake stubs.
        try:
            from .bh032a_native_cases import run_native_coverage_cases
            records.extend(run_native_coverage_cases())
        except Exception as exc:
            records.append({'case':'BH032A_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2B swept collision cases run on ACTUAL Blender BMesh but
        # modify only objects owned by this temporary laboratory scene.
        try:
            from .bh032b_native_cases import run_native_swept_cases
            records.extend(run_native_swept_cases(lab_scene, _build_case, _snapshot))
        except Exception as exc:
            records.append({'case':'BH032B_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2C native safe-depth candidates: real production preflight, no
        # production edits, results include exact independently inspected mesh.
        try:
            from .bh032c_native_cases import run_native_safe_depth_cases
            records.extend(run_native_safe_depth_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH032C_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2D read-only axis and vertex projection checks on real BMesh.
        # They do NOT pretend to automate actual mouse interaction or GPU HUD.
        try:
            from .bh032d_native_cases import run_native_direction_snap_cases
            records.extend(run_native_direction_snap_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH032D_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2E: BMesh-backed real vertex ids and projected snap targets.
        # The pixel cursor is a deterministic fixture; no live GUI claim.
        try:
            from .bh032e_native_cases import run_native_vertex_snap_cases
            records.extend(run_native_vertex_snap_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH032E_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2F: actual BVH occlusion checks and 12k BMesh vertices.
        # This is native Blender geometry validation, not live mouse automation.
        try:
            from .bh032f_native_cases import run_native_occlusion_cases
            records.extend(run_native_occlusion_cases(lab_scene))
        except Exception as exc:
            records.append({'case':'BH032F_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2G.1: real BMesh vertex identity decides whether a near-vertex
        # numerical contact is authorized. Actual Blender test, not mock IDs.
        try:
            from .bh032g1_native_cases import run_native_shared_vertex_cases
            records.extend(run_native_shared_vertex_cases())
        except Exception as exc:
            records.append({'case':'BH032G1_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2H.1: independent +/- axis probing and no mutation of BMesh.
        try:
            from .bh032h1_native_cases import run_native_alternative_cases
            records.extend(run_native_alternative_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH032H1_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-03.2H.2: compare indexed and reference Guard on real BMesh.
        try:
            from .bh032h2_native_cases import run_native_broadphase_cases
            records.extend(run_native_broadphase_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH032H2_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-04A read-only corner cut-line oracle on actual Blender BMesh.
        try:
            from .bh04a_native_cases import (run_native_corner_oracle_cases,
                                           run_native_corner_operator_cases)
            records.extend(run_native_corner_oracle_cases())
            records.extend(run_native_corner_operator_cases(lab_scene,_snapshot))
        except Exception as exc:
            records.append({'case':'BH04A_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-10000:]}})
        # BH-04B: real guarded miter geometry Apply, UV and atomic rollback.
        try:
            from .bh04b_native_cases import run_native_corner_build_cases
            records.extend(run_native_corner_build_cases(lab_scene,_snapshot))
        except Exception as exc:
            records.append({'case':'BH04B_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-12000:]}})
        # BH-04B.1: exercise the actual Blender BMesh vertex identity and the
        # localized SOURCE-vs-NEW degenerate guard. Do not mark PASS offline.
        try:
            from .bh04b5_native_cases import run_native_source_ledger_cases
            records.extend(run_native_source_ledger_cases())
        except Exception as exc:
            records.append({'case':'BH04B5_LEDGER_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-9000:]}})
        try:
            from .bh04b1_native_cases import run_native_degenerate_guard_cases
            records.extend(run_native_degenerate_guard_cases())
        except Exception as exc:
            records.append({'case':'BH04B1_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-12000:]}})
        # BH-04B.2: degenerate triangle from a genuine area-bearing BMesh
        # polygon. Test welded point-only touching versus unsafe contacts.
        try:
            from .bh04b2_native_cases import run_native_source_tessellation_cases
            records.extend(run_native_source_tessellation_cases())
        except Exception as exc:
            records.append({'case':'BH04B2_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-12000:]}})
        # BH-04B.6: actual Blender/BMesh policy UI-to-Apply routing.
        # These are not live GPU/mouse tests; see runtime.not_tested.
        try:
            from .bh046_native_cases import run_native_uv_policy_cases
            records.extend(run_native_uv_policy_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH046_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        # BH0462 Single-Tile UV clipping + independent UV seam BMesh oracle.
        # Does not claim a live GPU Esc/mouse test or real image seam test.
        try:
            from .bh0462_native_cases import run_native_single_tile_cases
            records.extend(run_native_single_tile_cases(lab_scene,_build_case,_snapshot))
        except Exception as exc:
            records.append({'case':'BH0462_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-14000:]}})
        # BH0463 native: true BMesh scan + independent filter matrix.
        # GPU HUD / Esc / clicks remain manual acceptance requirements.
        try:
            from .bh0463_native_cases import run_native_geometry_ui_cases
            records.extend(run_native_geometry_ui_cases(lab_scene))
        except Exception as exc:
            records.append({'case':'BH0463_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-14000:]}})
        # BH0464: 138-edge SAFE DEPTH and reproducible collision witness.
        # New code is only Native VERIFIED after this real Blender run.
        try:
            from .bh0464_native_cases import run_native_large_contour_cases
            records.extend(run_native_large_contour_cases(lab_scene))
        except Exception as exc:
            records.append({'case':'BH0464_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-14000:]}})
        # BH0465: version evidence + recoverable collision helpers.
        # Does NOT claim GPU mouse / key events have been executed.
        try:
            from .bh0465_native_cases import run_native_recovery_cases
            records.extend(run_native_recovery_cases())
        except Exception as exc:
            records.append({'case':'BH0465_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        # BH0466: prove that the explicit ON/OFF toggle actually controls
        # the swept collision gate on Blender BMesh geometry.
        try:
            from .bh0466_native_cases import run_native_collision_switch_cases
            records.extend(run_native_collision_switch_cases(lab_scene))
        except Exception as exc:
            records.append({'case':'BH0466_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-12000:]}})
        # BH0468: new conservative tessellation predicate regression; no GPU proof.
        try:
            from .bh0468_native_cases import run_native_tessellation_cases
            records.extend(run_native_tessellation_cases())
        except Exception as exc:
            records.append({'case':'BH0468_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'error':f'{type(exc).__name__}: {exc}'}})
        # BH0467: independent authored 4×4 wall UV-gap oracle and Preview gate.
        # These are real BMesh tests; still not mouse/GPU pixel verification.
        try:
            from .bh0467_native_cases import run_native_module_seam_cases
            records.extend(run_native_module_seam_cases(lab_scene))
        except Exception as exc:
            records.append({'case':'BH0467_NATIVE_SUITE','status':'FAIL',
                'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-14000:]}})
        # BH0471: world-coordinate precision, affine frame and UV invariants.
        # This does NOT certify an interactive production-wall rebuild.
        try:
            from .bh0471_native_cases import run_native_precision_cases
            records.extend(run_native_precision_cases())
        except Exception as exc:
            records.append({'case':'BH0471_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-12000:]}})
        # BH0472: professional geometry decision kernel. Hard blocks need
        # proven geometry faults; near/precision/resource ambiguity is advisory.
        try:
            from .bh0472_native_cases import run_native_geometry_decision_cases
            records.extend(run_native_geometry_decision_cases())
        except Exception as exc:
            records.append({'case':'BH0472_NATIVE_SUITE','status':'FAIL',
                            'evidence':{'reason':str(exc),
                                        'traceback':traceback.format_exc()[-12000:]}})
        # Object scene is private, no workspace blend data saved.
    except Exception as exc:
        records.append({'case':'LAB_SETUP','status':'FAIL',
                        'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-14000:]}})
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            context.window.scene=original_scene
            restored=(context.window.scene is original_scene)
            context.tool_settings.mesh_select_mode=old_mesh_mode
        except Exception:
            records.append({'case':'LAB_RESTORE','status':'FAIL',
                            'evidence':{'traceback':traceback.format_exc()[-14000:]}})
        if lab_scene is not None and restored:
            try:
                # Only remove our own temporary scene and any remaining objects.
                for obj in tuple(lab_scene.objects):
                    bpy.data.objects.remove(obj,do_unlink=True)
                bpy.data.scenes.remove(lab_scene)
            except Exception as exc:
                warnings.append('delete_lab_scene: '+str(exc))
    pass_count=sum(r['status']=='PASS' for r in records)
    fail_count=sum(r['status']=='FAIL' for r in records)
    from . import BH0472_BUILD_ID
    artifact={
        'schema':'issmaker.bh031.native.evidence.v1',
        'build_id':BH0472_BUILD_ID,
        'utc':datetime.now(timezone.utc).isoformat(),
        'blender_version':bpy.app.version_string,
        'platform':platform.system(),
        'runtime':{'operator':'mesh.brick_hole_edge_smart_uv','source':'real Blender bpy and BMesh',
                   'visual_hud':'NOT_TESTED','mouse_hover':'NOT_TESTED',
                   'undo_redo':'NOT_TESTED','screen_resolution':'NOT_TESTED'},
        'cases':records,'passed':pass_count,'failed':fail_count,
        'scene_restored':restored,'warnings':warnings,
        'elapsed_seconds':round(time.time()-started,3),
    }
    directory=Path.home()/'Downloads'/'ISSMAKER_Diagnostics'
    try: directory.mkdir(parents=True,exist_ok=True)
    except OSError: directory=Path(tempfile.gettempdir())
    timestamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    filename=directory/('ISSMAKER_BH031_NATIVE_'+timestamp+'_'+uuid.uuid4().hex[:6]+'.zip')
    with zipfile.ZipFile(filename,'w',compression=zipfile.ZIP_DEFLATED) as zipf:
        zipf.writestr('BH031_NATIVE_RESULTS.json',json.dumps(artifact,ensure_ascii=False,indent=2))
        zipf.writestr('BH032D_DIRECTION_SNAP_RESULTS.json',json.dumps([r for r in records if str(r.get('case','')).startswith('BH032D_')],ensure_ascii=False,indent=2))
        zipf.writestr('BH032F_OCCLUSION_PERFORMANCE_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH032F_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH04B2_SOURCE_TESSELLATION_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH04B2_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH04B1_DEGENERATE_GUARD_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH04B1_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH04B5_SOURCE_LEDGER_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH04B5_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH0462_SINGLE_TILE_AND_SEAM_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH0462_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH0463_SMART_GEOMETRY_UI_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH0463_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH046_SMART_UV_POLICY_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH046_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH04B_MITER_NATIVE_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH04B_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH04A_CORNER_TRIM_ORACLE_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH04A_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH032H2_COLLISION_BROADPHASE_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH032H2_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH032H1_DIRECTION_ALTERNATIVES_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH032H1_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH032G1_TOPO_CONTACT_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH032G1_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('BH032E_PROJECTED_VERTEX_RESULTS.json',json.dumps([r for r in records if str(r.get('case','')).startswith('BH032E_')],ensure_ascii=False,indent=2))
        zipf.writestr('BH032A_TESSELLATION_RESULTS.json',json.dumps([r for r in records if str(r.get('case','')).startswith('BH032A_')],ensure_ascii=False,indent=2))
        zipf.writestr('BH0472_PRO_GEOMETRY_KERNEL_RESULTS.json',json.dumps(
            [r for r in records if str(r.get('case','')).startswith('BH0472_')],
            ensure_ascii=False,indent=2))
        zipf.writestr('README_RU.txt',
            'ISSMAKER Brick Hole BH-04B.7.2: реальные автоматические Blender/BMesh проверки.\n'
            'Отправьте ZIP в ChatGPT. PASS относится только к указанным кейсам.\n'
            'Управление мышью, UI, Undo/Redo и визуальные швы НЕ проверялись.\n')
    return filename,artifact
