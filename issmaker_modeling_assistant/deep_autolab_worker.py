"""Executed by a *separate*, factory-startup Blender process (never user .blend).

Only this worker creates Mesh objects and tests real builders. All evidence is
checkpointed before and after cases. A crash leaves the last case marked RUNNING.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys
import time
import traceback

# Blender executes script by pathname; give it the addon parent to resolve
# relative imports without depending on the user's enabled-addon setting.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bpy
import bmesh
from mathutils import Vector
from issmaker_modeling_assistant.core import deep_lab_supervisor as sup

ROOT = None
COUNT = 0
SEED = 231024


def checkpoint(key, data):
    global COUNT
    if key == 'state':
        sup.atomic_json(ROOT / 'worker_state.json', data)
    elif key == 'case':
        COUNT += 1
        sup.atomic_json(ROOT / 'cases' / f'{COUNT:03d}_{data["case_id"]}.json', data)


def case(case_id, module, expected, fn, *, parameters=None):
    start = time.perf_counter()
    checkpoint('state', {'current':case_id,'completed':False, 'count':COUNT})
    out = {'case_id':case_id,'module':module,'expected':expected,
           'seed':SEED,'parameters':parameters or {}, 'stage':'RUNNING',
           'status':'ERROR', 'witness':None}
    try:
        result = fn()
        if not isinstance(result, dict):
            raise AssertionError('Test must provide structured evidence')
        out.update({'status':'PASS','stage':'COMPLETE','output':result})
    except AssertionError as exc:
        out.update({'status':'FAIL','stage':'ASSERTION', 'error':str(exc),
                    'traceback':traceback.format_exc()[-9000:]})
    except Exception as exc:
        out.update({'status':'ERROR','stage':'EXCEPTION',
                    'error':f'{type(exc).__name__}: {exc}',
                    'traceback':traceback.format_exc()[-9000:]})
    out['duration_ms'] = round((time.perf_counter()-start)*1000, 3)
    checkpoint('case', out)
    return out


def blocked(case_id,module,why):
    return case(case_id,module,'NATIVE_GUI',lambda: {
        'status':'NOT_TESTED_GUI', 'reason':why})


def mesh_object(name, points, edges=(), faces=()):
    mesh=bpy.data.meshes.new('LAB_MESH_'+name)
    mesh.from_pydata(points,edges,faces)
    mesh.update()
    obj=bpy.data.objects.new('LAB_'+name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj['ISSMAKER_DEEP_LAB_FIXTURE'] = name
    return obj


def setup_source():
    # UV Wall production requires a REAL 4K image, quad, material, and safe
    # 1000 px/m reference. This isn't a mock or bypass of source_quad_data.
    image = bpy.data.images.new('LAB_4K_1000px_m', width=4096, height=4096,
                                alpha=False, float_buffer=False)
    image.generated_color = (.28, .36, .42, 1.0)
    image.filepath_raw = str(ROOT / 'fixtures' / 'LAB_4K_SOURCE.png')
    image.file_format = 'PNG'
    try:
        image.save()
        # Portable fixture: image lives alongside extracted synthetic .blend.
        image.filepath='//LAB_4K_SOURCE.png'
    except Exception as exc:
        # Still report a tangible setup problem; do not install a fake image.
        raise RuntimeError('4K source-image export failed: '+str(exc)) from exc
    material=bpy.data.materials.new('LAB_1000_TEXEL_DIFFUSE')
    material.use_nodes=True
    node=material.node_tree.nodes.new('ShaderNodeTexImage')
    node.image=image
    node.extension='REPEAT'
    source=mesh_object('4x4_SOURCE',[(0,0,0),(4,0,0),(4,0,4),(0,0,4)],faces=[(0,1,2,3)])
    source.data.materials.append(material)
    uv=source.data.uv_layers.new(name='UVMap')
    low=.01171875; high=1.0-low
    for loop, point in zip(source.data.polygons[0].loop_indices,
                           [(low,low),(high,low),(high,high),(low,high)]):
        uv.data[loop].uv=point
    return source


def polygon_path(label, points, closed=False):
    pts=list(points)
    if len(pts)<2:
        raise AssertionError('invalid fixture: fewer than two vertices')
    edges=[(i,i+1) for i in range(len(pts)-1)]
    if closed:
        edges.append((len(pts)-1,0))
    return mesh_object(label,pts,edges=edges)


def fingerprint(obj):
    mesh=obj.data
    hash_value=hashlib.sha256()
    # Bounded but deterministic digest of actual output geometry, no secrets.
    for v in mesh.vertices:
        hash_value.update(('%0.7f,%0.7f,%0.7f;' % tuple(v.co)).encode('ascii'))
    for p in mesh.polygons:
        hash_value.update((','.join(str(v) for v in p.vertices)+';').encode('ascii'))
    return {'vertices':len(mesh.vertices),'edges':len(mesh.edges),
            'faces':len(mesh.polygons),'uv_layers':[x.name for x in mesh.uv_layers],
            'geometry_sha256':hash_value.hexdigest()}



def audit_mesh_topology(obj):
    # Native Blender BMesh inspection: zero-area, shared-edge winding and UV
    # finiteness. Non-manifold rim counts are observations, not automatic FAIL:
    # intentionally open wall strips have legitimate boundary edges.
    mesh=obj.data
    bm=bmesh.new()
    try:
        bm.from_mesh(mesh)
        zero=[]; conflicts=[]; boundary=0; nonmanifold=0
        bm.faces.index_update();bm.edges.index_update()
        for face in bm.faces:
            if face.calc_area() <= 1e-12:
                zero.append(face.index)
        for edge in bm.edges:
            if len(edge.link_faces)==1:boundary+=1
            if len(edge.link_faces)>2:nonmanifold+=1
            if len(edge.link_faces)==2:
                face_dirs=[]
                for f in edge.link_faces:
                    loop=next((l for l in f.loops if l.edge is edge),None)
                    if loop is not None:
                        face_dirs.append(loop.vert is edge.verts[0])
                if len(face_dirs)==2 and face_dirs[0]==face_dirs[1]:
                    conflicts.append(edge.index)
        uv_layer=mesh.uv_layers.active
        bad_uv=[]
        if uv_layer is not None:
            for i,l in enumerate(uv_layer.data):
                if not math.isfinite(float(l.uv.x)) or not math.isfinite(float(l.uv.y)):
                    bad_uv.append(i)
        result={'zero_area_faces':len(zero),'zero_area_samples':zero[:16],
                'shared_edge_winding_conflicts':len(conflicts),
                'winding_edge_samples':conflicts[:16],
                'nonmanifold_gt2_edges':nonmanifold,
                'boundary_edges_expected_on_open_wall':boundary,
                'nonfinite_uv':len(bad_uv),'nonfinite_uv_samples':bad_uv[:16]}
        if zero or conflicts or bad_uv:
            raise AssertionError('Mesh validation discrepancy: '+json.dumps(result))
        return result
    finally:
        bm.free()

def wall_build(label, points, source, *, closed=False, double=False, target=None):
    from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.production.wall import build_wall
    path=polygon_path(label,points,closed)
    # Production builder, not a geometry imitation; default contract applied.
    output, face_count, inset = build_wall(
        bpy.context, source, path, 4.0, 'LAB_RESULT_'+label,
        bool(double), 0.25, 'LEFT', False, False, False, 'BETWEEN',
        target_obj=target)
    if output is None or output.type!='MESH' or len(output.data.polygons)<1:
        raise AssertionError('Production builder returned an empty wall')
    if face_count != len(output.data.polygons):
        raise AssertionError('Builder face_count disagrees with committed Mesh')
    info=fingerprint(output)
    info['native_topology_audit']=audit_mesh_topology(output)
    info.update({'actual_inset':float(inset),'path_points':len(points),
                 'source_path_name':path.name,'result_name':output.name})
    return info, output


def fixture_cases(source):
    # Tests intentionally cover real invalid geometry, difficult valid geometry,
    # and long paths; deterministic values are exported with each case.
    paths=[
      ('WALL_01_L_CORNER',[(0,0,0),(4,0,0),(4,4,0)],False,False),
      ('WALL_02_3D_Z_GRADIENT',[(0,0,0),(4,0,.25),(4,4,1.0),(8,4,1.8),(8,8,.1)],False,False),
      ('WALL_03_CONCAVE',[(0,0,0),(8,0,0),(8,2,0),(3,2,0),(3,7,0),(0,7,0)],False,False),
      ('WALL_04_NEAR_MISS',[(0,0,0),(3,0,0),(3,3,0),(6,3.0005,0),(6,7,0)],False,False),
      ('WALL_05_CAD_TRANSLATION',[(1e6,1e6,0),(1e6+4,1e6,0),(1e6+4,1e6+4,0)],False,False),
      ('WALL_06_DOUBLE_OFFSET',[(0,0,0),(4,0,0),(4,4,0),(8,4,0)],False,True),
      ('WALL_07_CLOSED_LOOP',[(0,0,0),(8,0,0),(8,8,0),(0,8,0)],True,False),
    ]
    for label,points,closed,double in paths:
        case(label,'UV_WALL','ACCEPT',lambda a=label,p=points,c=closed,d=double:
             wall_build(a,p,source,closed=c,double=d)[0],
             parameters={'path':points,'closed':closed,'double':double})
    crossing=[(0,0,0),(4,4,0),(0,4,0),(4,0,0)]
    def expected_reject():
        try:
            wall_build('WALL_09_SELF_CROSS',crossing,source)
        except ValueError as exc:
            return {'decision':'REJECTED','reason':str(exc)[:1200],
                    'proof':'The authored path has a proper segment intersection'}
        raise AssertionError('A genuine self-crossing path was accepted')
    case('WALL_09_PROVEN_SELF_CROSS','UV_WALL','REJECT',expected_reject,
         parameters={'path':crossing})



def surface_case(source):
    from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.production.path import source_quad_data
    from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.production.surface import build_triangulated_surface
    import math
    quad = source_quad_data(source, 4.0)
    points = [Vector((0.,0.,0.)), Vector((8.,0.,.2)),
              Vector((8.,2.,.5)), Vector((3.,2.,.8)),
              Vector((3.,6.,1.1)), Vector((0.,6.,.1))]
    result = build_triangulated_surface(points, quad, 4.0, angle_rad=math.radians(27.0))
    if not isinstance(result,(tuple,list)) or len(result)!=3:
        raise AssertionError('Surface production returned unexpected result')
    vertices, faces, uvs = result
    if not faces or len(faces)!=len(uvs):
        raise AssertionError('Surface production lost faces/UV mapping')
    if any(len(f)!=len(uv) for f,uv in zip(faces,uvs)):
        raise AssertionError('Surface UV loop length does not match geometry')
    if not all(math.isfinite(float(coord)) for v in vertices for coord in v):
        raise AssertionError('Surface contains NaN or Inf')
    return {'vertices':len(vertices),'faces':len(faces),
            'uv_loops':sum(map(len,uvs)), 'angle_deg':27.0,
            'path_points':len(points),'method':'production.build_triangulated_surface'}


def cornice_case(source):
    from issmaker_modeling_assistant.tools.uv_wall.UV_Wall_Professional.production.cornice import build_cornice
    # Independent XZ profile and sharp 90-degree authored corner.
    profile=mesh_object('CORNICE_PROFILE',[(0,0,0),(.20,0,0),(.20,0,.12),(0,0,.12)],
                        edges=[(0,1),(1,2),(2,3),(3,0)])
    path=polygon_path('CORNICE_PATH',[(0,0,2.8),(5,0,2.8),(5,5,2.8)])
    obj,count = build_cornice(bpy.context,source,path,profile,4.0,'LAB_CORNICE_RESULT',
                              corner_mode='AUTHORED')
    if obj.type!='MESH' or len(obj.data.polygons)!=count or count<1:
        raise AssertionError('Cornice output is empty or face count disagrees')
    return {'profile_points':4,'path_points':3, 'cornice_mesh':fingerprint(obj),
            'method':'production.build_cornice'}


def accepted_result_rollback_case(source):
    # Rebuild into a known accepted object with a deliberately crossing path.
    baseline, target = wall_build('TRANSACTION_BASE', [(0,0,0),(4,0,0),(4,4,0)],source)
    before = fingerprint(target)
    try:
        wall_build('TRANSACTION_INVALID', [(0,0,0),(4,4,0),(0,4,0),(4,0,0)],
                   source, target=target)
    except ValueError as exc:
        after = fingerprint(target)
        if before['geometry_sha256']!=after['geometry_sha256']:
            raise AssertionError('Accepted wall geometry changed after invalid rebuild')
        return {'result':'REJECTED_WITH_ROLLBACK','reason':str(exc)[:800],
                'accepted_mesh_sha256':before['geometry_sha256']}
    raise AssertionError('Invalid rebuild accepted; expected transaction rollback')


def independent_geometry_oracle():
    # Separate exact integer oracle, not a copy of shared Fraction predicate.
    from issmaker_modeling_assistant.core.reliability.predicates import exact_segment_contact_2d
    rng=random.Random(SEED)
    def orient(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def ours(a,b,c,d):
        o1,o2,o3,o4 = orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
        def between(p,q,r):
            return (min(p[0],q[0])<=r[0]<=max(p[0],q[0]) and
                    min(p[1],q[1])<=r[1]<=max(p[1],q[1]))
        if o1==0 and between(a,b,c):return True
        if o2==0 and between(a,b,d):return True
        if o3==0 and between(c,d,a):return True
        if o4==0 and between(c,d,b):return True
        return (o1*o2<0) and (o3*o4<0)
    tested=0;hits=0
    for _ in range(6000):
        pts=[(rng.randint(-300,300),rng.randint(-300,300)) for _ in range(4)]
        a,b,c,d=pts
        if a==b or c==d:continue
        ref=ours(a,b,c,d)
        got=bool(exact_segment_contact_2d(a,b,c,d))
        tested+=1;hits+=int(ref)
        if got!=ref:
            raise AssertionError(f'INDEPENDENT_ORACLE disagreement: {pts} ref={ref} got={got}')
    return {'integer_oracle_cases':tested,'expected_true':hits,
            'seed':SEED,'comparison':'original exact_segment_contact_2d vs independently coded integer test'}


def heavy_wall_cases(source):
    def path_of(n):
        return [(i*0.18, math.sin(i*0.075)*0.27, math.cos(i*0.037)*0.12)
                for i in range(n)]
    for n in (144, 1024):
        case(f'WALL_STRESS_{n}_POINTS','UV_WALL','ACCEPT',
             lambda n=n:wall_build(f'WALL_STRESS_{n}',path_of(n),source)[0],
             parameters={'count':n,'seed':SEED,'generator':'3D sinusoid'})

def dwos_case():
    # A 4x4 planar grid with an intentional missing interior cell.
    from issmaker_modeling_assistant.tools.dwos_windows.native.opening_detection import detect_openings
    coords=[(float(i),0.0,float(j)) for j in range(4) for i in range(4)]
    faces=[]
    for y in range(3):
        for x in range(3):
            if (x,y)==(1,1): continue
            a=y*4+x;faces.append((a,a+1,a+5,a+4))
    obj=mesh_object('DWOS_OPENING_GRID',coords,faces=faces)
    r1=detect_openings(obj)
    r2=detect_openings(obj)
    if not isinstance(r1,(tuple,list)) or len(r1)<1:
        raise AssertionError('No opening candidate for synthetic polygon-hole fixture')
    if len(r1)!=len(r2):
        raise AssertionError('DWOS cached detection unstable on unchanged geometry')
    return {'candidates_first':len(r1),'candidates_second':len(r2),
            'source_faces':len(faces),'fixture':fingerprint(obj)}


def collision_case():
    from issmaker_modeling_assistant.tools.dwos_windows.native.scene_spatial_safety import scan_scene
    def cube(name,center):
        x,y,z=center;r=.75
        vs=[(x+a*r,y+b*r,z+c*r) for a,b,c in (
            (-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
            (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1))]
        fs=[(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)]
        o=mesh_object(name,vs,faces=fs);o['DWOS_role']='INTERIOR';return o
    a=cube('DWOS_INTERIOR_A',(10,0,0));b=cube('DWOS_INTERIOR_B',(10.6,.3,0))
    try:
        hits=scan_scene(bpy.context.scene)
        matching=[x for x in hits if x.get('type')=='INTERIOR_INTERIOR_COLLISION']
        if not matching: raise AssertionError('Missed physically overlapping interior boxes')
        return {'collision_rows':len(matching),'first_witness':matching[0],
                'a':fingerprint(a),'b':fingerprint(b)}
    finally:
        a['DWOS_role']='TEST_DONE';b['DWOS_role']='TEST_DONE'


def reliability():
    from issmaker_modeling_assistant.core.reliability.lab import run_lab
    result=run_lab()
    if result.get('failed') or not result.get('total'):
        raise AssertionError('Cross-tool Reliability Lab failed '+repr(result.get('rows'))[:800])
    sup.atomic_json(ROOT / 'fixtures' / 'reliability_lab.json',result)
    return {'passed':result['passed'],'total':result['total'],
            'actual_tools':['Brick Hole','Collision','DWOS','UV Wall']}


def run():
    global ROOT
    argv=sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
    args=argparse.ArgumentParser()
    args.add_argument('--output',required=True)
    ROOT=Path(args.parse_args(argv).output)
    (ROOT/'cases').mkdir(parents=True,exist_ok=True)
    (ROOT/'fixtures').mkdir(parents=True,exist_ok=True)
    sup.atomic_json(ROOT/'native_limits.json', {
        'native_background':'RUNNING',
        'native_gui':'NOT_TESTED_GUI', 'real_user_scene':'NOT_TESTED',
        'gui_undo_redo':'NOT_TESTED_GUI',
        'dwos_corner_window_build':'NOT_TESTED',
        'cornice_profile_editor':'NOT_TESTED',
        'notes':'Worker uses Blender bpy and production Wall builder. No fake Blender API.'})
    sup.atomic_json(ROOT/'environment.json',{
        'blender_version':bpy.app.version_string,
        'executable':str(bpy.app.binary_path),
        'scene_loaded_from_user':False,
        'worker_script_version':'23.0', 'seed':SEED})
    case('ADDON_IMPORT','STARTUP','SUCCESS',lambda: {
        'module':'issmaker_modeling_assistant',
        'blender':bpy.app.version_string,
        'background':bool(bpy.app.background)})
    # Intentionally use production code without registering modal/UI classes:
    # this ensures no global handlers from the test addon can alter the user.
    case('CROSS_TOOL_RELIABILITY','RELIABILITY','ALL_PASS',reliability)
    case('INDEPENDENT_6000_SEGMENT_ORACLE','RELIABILITY','NO_MISMATCH',independent_geometry_oracle)
    setup_result=case('WALL_SOURCE_PREFLIGHT','UV_WALL','VALID_4K_SOURCE',
                      lambda: {'reference':fingerprint(setup_source()),'image':'LAB_4K_SOURCE.png'})
    if setup_result['status']=='PASS':
        # case() expects JSON evidence; source builder must be called again only
        # if evidence is an object, so source is cached in bpy.data.objects.
        source=bpy.data.objects.get('LAB_4x4_SOURCE')
    else:
        source=None
    if source is None:
        checkpoint('case',{'case_id':'WALL_SUITE_BLOCKED','module':'UV_WALL',
            'status':'BLOCKED','stage':'SOURCE_PREFLIGHT','duration_ms':0,
            'error':'4K source reference unavailable; wall production builds not executed'})
    else:
        fixture_cases(source)
    if source is not None:
        case('SURFACE_27DEG_NONPLANAR','SURFACE','ACCEPT',lambda:surface_case(source))
        case('CORNICE_SHARP_90_PROFILE','CORNICE','ACCEPT',lambda:cornice_case(source))
        case('WALL_ACCEPTED_TRANSACTION_ROLLBACK','UV_WALL','ROLLBACK',
             lambda:accepted_result_rollback_case(source))
    case('DWOS_OPENING_ACTUAL_SCANNER','DWOS','>=1_AND_STABLE',dwos_case)
    case('DWOS_INTERIOR_COLLISION_SCANNER','DWOS','TRUE_OVERLAP',collision_case)
    # Save a re-openable native fixture BEFORE the heaviest production build;
    # if a 1024-point build crashes, earlier evidence remains available.
    case('EXPORT_SYNTHETIC_BLEND_CHECKPOINT','EVIDENCE','SAVED',lambda:save_scene())
    if source is not None:
        heavy_wall_cases(source)
    # Explicit blockers document actual untested domain generation paths.
    for name,tool,reason in (
        ('DWOS_CORNER_MINUS_90_BUILD','DWOS','Full 3D corner-window Composer requires GUI acceptance'),
        ('GUI_ESC_DPI_CLICK','UI','No actual GUI input in background Blender'),
        ('GUI_UNDO_REDO_STRESS','UNDO','No interactive Undo context in background Blender'),
    ):
        checkpoint('state',{'current':name,'completed':False,'count':COUNT})
        checkpoint('case',{'case_id':name,'module':tool,'expected':'NATIVE_GUI_OR_COMPLEX_BUILD',
                           'status':'BLOCKED' if not name.startswith('GUI_') else 'NOT_TESTED_GUI',
                           'stage':'NOT_EXECUTED','duration_ms':0,'error':reason,'seed':SEED})
    # Actual synthetic fixtures saved as a standalone Blender scene.
    case('EXPORT_SYNTHETIC_BLEND_FINAL','EVIDENCE','SAVED',lambda:save_scene())
    sup.atomic_json(ROOT/'worker_state.json',{
        'current':'COMPLETE','count':COUNT,'completed':True})
    sup.atomic_json(ROOT/'native_limits.json',{
        'native_background':'EXECUTED',
        'native_gui':'NOT_TESTED_GUI','real_user_scene':'NOT_TESTED',
        'gui_undo_redo':'NOT_TESTED_GUI',
        'notes':'See all FAIL/ERROR/BLOCKED cases. Background PASS is not GUI PASS.'})


def save_scene():
    path=str(ROOT/'fixtures'/'ISSMAKER_DEEP_AUTOLAB_SYNTHETIC.blend')
    bpy.context.preferences.filepaths.save_version = 0  # lab-only factory preferences
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=True)
    p=Path(path)
    if not p.exists(): raise AssertionError('Blender did not save lab fixture')
    return {'blend_file':p.name,'bytes':p.stat().st_size}


if __name__=='__main__':
    try:
        run()
    except BaseException as exc:
        if ROOT is not None:
            sup.atomic_json(ROOT/'worker_state.json',{'completed':False,
                'current':'FATAL '+repr(exc), 'traceback':traceback.format_exc()[-9000:]})
        traceback.print_exc()
        sys.exit(4)
