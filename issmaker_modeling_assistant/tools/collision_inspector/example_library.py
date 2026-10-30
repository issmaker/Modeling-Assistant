"""Collision Inspector 55-case gallery. Never uses bpy.ops for generation/deletion.

Generated assets are identified by a private run token and conservative ownership
rules. A fixture is one Mesh object, optionally holding two or more disconnected
shells; texts are separate non-Mesh viewport helpers.
"""
import json
import hashlib
import struct
import traceback
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import bpy
from mathutils import Vector
from .fixture_geometry import CATEGORIES, build_case, specs, assemble_cases

ROOT_TAG = 'ci_gallery_schema'
SCHEMA = 'CI-55-1'
FINGERPRINT_TAG = 'ci_fixture_geometry_sha256'
RUN_TAG = 'ci_gallery_run'
OWNED_TAG = 'ci_gallery_owned'
COL_X = 6.8
ROW_Y = 7.3
LAYOUTS = ('SEPARATE', 'CATEGORY', 'ONE')
LABELS = ('FULL', 'ID', 'NONE')


def owned_root(scene, name):
    root = bpy.data.collections.get(name or '')
    if not root or root.get(ROOT_TAG) != SCHEMA:
        return None
    if not any(item == root for item in scene.collection.children):
        return None
    return root


def meshes_in(root):
    if root is None: return []
    token=root.get(RUN_TAG)
    result=[]
    groups = list(root.children) if root.children else [root]
    for group in groups:
        for obj in group.objects:
            if (obj.type == 'MESH' and obj.get(OWNED_TAG) == SCHEMA
                    and obj.get(RUN_TAG) == token):
                result.append(obj)
    order={kind:i for i,(kind,_) in enumerate(CATEGORIES)}
    return sorted(result, key=lambda obj:(order.get(obj.get('ci_kind'),99),int(obj.get('ci_variant',0))))


def _palette(root, token):
    result=[]
    for name,rgba in (('A amber',(.92,.38,.16,1)),('B mint',(.12,.73,.58,1))):
        mat=bpy.data.materials.new('CI55_' + token + '_' + name)
        mat.diffuse_color=rgba
        mat[OWNED_TAG]=SCHEMA
        mat[RUN_TAG]=token
        result.append(mat)
    return result


def _label(collection, name, body, xy, size, token, tint=None):
    curve=bpy.data.curves.new(name+'_FONT','FONT')
    curve.body=body
    curve.size=size
    curve.extrude=0
    curve.align_x='LEFT'
    obj=bpy.data.objects.new(name,curve)
    collection.objects.link(obj)
    obj.location=(xy[0],xy[1],1.8)
    obj[OWNED_TAG]=SCHEMA; obj[RUN_TAG]=token
    obj.hide_render=True
    return obj


def mesh_fingerprint(mesh):
    """Hash authored vertex order, coordinates, polygon loops and material slots.

    Compare mesh data as stored by Blender, not pre-quantized Python fixtures:
    Blender serializes the vertex coordinates at its own precision.
    """
    digest = hashlib.sha256()
    digest.update(struct.pack('<III', len(mesh.vertices), len(mesh.edges), len(mesh.polygons)))
    for vertex in mesh.vertices:
        digest.update(struct.pack('<3d', *(float(t) for t in vertex.co)))
    for edge in mesh.edges:
        digest.update(struct.pack('<2I', *(int(t) for t in edge.vertices)))
    for polygon in mesh.polygons:
        vertices = tuple(int(t) for t in polygon.vertices)
        digest.update(struct.pack('<II', len(vertices), int(polygon.material_index)))
        digest.update(struct.pack('<' + 'I'*len(vertices), *vertices))
    return digest.hexdigest()


def object_transform_fingerprint(obj):
    """Local transform snapshot: move/rotate/scale invalidates fixture expectations."""
    values = tuple(float(v) for seq in (obj.location, obj.rotation_euler, obj.rotation_quaternion,
                                          obj.rotation_axis_angle, obj.scale) for v in seq)
    payload = repr((obj.rotation_mode, values, getattr(obj.parent, 'name_full', None)))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _gallery_inventory(root):
    """Validate complete run and all authored meshes before any analysis/deletion."""
    if root is None or root.get(ROOT_TAG) != SCHEMA:
        raise ValueError('Missing Collision Inspector owned root')
    token = root.get(RUN_TAG)
    if not token:
        raise ValueError('Missing gallery run token')
    mode = root.get('ci_gallery_mode', 'SEPARATE')
    legacy = 'ci_gallery_mode' not in root  # v3.0 and earlier root metadata.
    if mode != 'ONE' and root.objects:
        raise ValueError('Unexpected object at gallery root; refusing operation')
    if len(root.children) != (0 if mode == 'ONE' else 11):
        raise ValueError('Wrong number of gallery category collections')
    if root.users > 1:
        raise ValueError('Gallery collection is linked elsewhere; refusing operation')
    if mode not in LAYOUTS:
        raise ValueError('Unknown diagnostic gallery mode')
    all_meshes = []
    if mode == 'ONE':
        if len(root.objects) != 1:
            raise ValueError('Single-object gallery must contain exactly one Mesh')
        obj = list(root.objects)[0]
        if obj.type != 'MESH' or obj.get(OWNED_TAG) != SCHEMA or obj.get(RUN_TAG) != token:
            raise ValueError('Unexpected user object at single-Mesh gallery root')
        if obj.get(FINGERPRINT_TAG) != mesh_fingerprint(obj.data):
            raise ValueError(f'Fixture geometry was modified: {obj.name}')
        if obj.get('ci_fixture_transform_sha256') != object_transform_fingerprint(obj):
            raise ValueError(f'Fixture transform was modified: {obj.name}')
        all_meshes.append(obj)
    for group in root.children:
        if group.get(ROOT_TAG) != SCHEMA or group.get(RUN_TAG) != token:
            raise ValueError('Unexpected user collection inside gallery')
        if group.children or group.users > 1:
            raise ValueError('Category contains nested/shared collections; refusing operation')
        meshes = []
        for obj in group.objects:
            if obj.get(OWNED_TAG) != SCHEMA or obj.get(RUN_TAG) != token:
                raise ValueError('Unexpected user object inside gallery')
            if obj.type == 'MESH':
                if not obj.get(FINGERPRINT_TAG) or obj.get(FINGERPRINT_TAG) != mesh_fingerprint(obj.data):
                    raise ValueError(f'Fixture geometry was modified: {obj.name}')
                if obj.get('ci_fixture_transform_sha256') != object_transform_fingerprint(obj):
                    raise ValueError(f'Fixture transform was modified: {obj.name}')
                meshes.append(obj)
            elif obj.type != 'FONT':
                raise ValueError(f'Unexpected object type: {obj.name}')
        category_kind = group.get('ci_gallery_kind')
        if category_kind is None and legacy and mode == 'SEPARATE':
            # Older galleries had no group-kind custom property. Trust only the
            # exact generator-owned group title, NEVER a renamed user group.
            category_kind = next((kind for i,(kind,title) in enumerate(CATEGORIES)
                                  if group.name == f'{i+1:02d} · {title}'),None)
        row = next((i for i, entry in enumerate(CATEGORIES) if category_kind == entry[0]), None)
        if row is None:
            raise ValueError('Unknown category group')
        expected_group_count = 5 if mode == 'SEPARATE' else 1 if mode == 'CATEGORY' or (mode == 'ONE' and row == 0) else 0
        if len(meshes) != expected_group_count:
            raise ValueError(f'Wrong fixture count in {group.name}: {len(meshes)}/{expected_group_count}')
        all_meshes.extend(meshes)
    if mode == 'SEPARATE':
        if len(all_meshes) != 55:
            raise ValueError('Missing fixtures in gallery')
        keys = [(obj.get('ci_kind'), int(obj.get('ci_variant', 0))) for obj in all_meshes]
        expected = {(kind, number) for kind, number, _ in specs()}
        if set(keys) != expected or len(set(keys)) != 55:
            raise ValueError('Duplicate or missing fixture identities')
    else:
        expected_count = 11 if mode == 'CATEGORY' else 1
        if len(all_meshes) != expected_count:
            raise ValueError('Wrong aggregate fixture count')
        case_keys=[]
        for obj in all_meshes:
            try:
                record = json.loads(obj.get('ci_combined_cases',''))
            except (ValueError, TypeError):
                raise ValueError('Invalid aggregate case manifest') from None
            if not isinstance(record, list) or not all(
                isinstance(row,list) and len(row)==4 and isinstance(row[0],str)
                and isinstance(row[1],int) and 1 <= row[1] <= 5
                and all(type(v) in (int,float) for v in row[2:]) for row in record
            ):
                raise ValueError('Invalid aggregate case manifest')
            if mode == 'CATEGORY' and any(row[0] != obj.get('ci_kind') for row in record):
                raise ValueError('Aggregate category manifest does not match Mesh')
            case_keys.extend((row[0],row[1]) for row in record)
        expected = {(kind, number) for kind, number, _ in specs()}
        if set(case_keys) != expected or len(case_keys) != 55:
            raise ValueError('Duplicate or missing fixture identities')
    return all_meshes


def _create_combined_mesh(group, material_palette, token, mode, group_rows):
    """Combine raw fixture coordinates without weld/remesh, keeping every shell."""
    vertices, edges, faces, face_materials, manifest = assemble_cases(
        group_rows,mode,COL_X,ROW_Y)
    mesh=bpy.data.meshes.new('CI55_MERGED_'+token)
    mesh.from_pydata(vertices,edges,faces)
    mesh.update(calc_edges=True)
    for mat in material_palette: mesh.materials.append(mat)
    for polygon, idx in zip(mesh.polygons,face_materials):
        polygon.material_index=int(idx)
    category = group_rows[0][2] if mode == 'CATEGORY' else 'ALL'
    name = f'CI55_{category}_5_CASES' if mode=='CATEGORY' else 'CI55_ALL_55_CASES'
    obj=bpy.data.objects.new(name, mesh)
    group.objects.link(obj)
    obj.location=(0.0, -group_rows[0][0]*ROW_Y if mode=='CATEGORY' else 0.0, 0.0)
    obj[OWNED_TAG]=SCHEMA; obj[RUN_TAG]=token
    obj['ci_kind']=category
    obj['ci_variant']=0
    obj['ci_combined_cases']=json.dumps(manifest, separators=(',',':'))
    obj['ci_expected_kinds']=json.dumps(sorted({row[2] for row in group_rows}))
    obj[FINGERPRINT_TAG]=mesh_fingerprint(mesh)
    obj['ci_fixture_transform_sha256']=object_transform_fingerprint(obj)
    return obj


def create_gallery(scene, labels='NONE', mode='SEPARATE'):
    """Create isolated gallery or non-welded grouped stress mesh(es)."""
    if labels not in LABELS or mode not in LAYOUTS:
        raise ValueError('Invalid fixture presentation/structure mode')
    if mode == 'ONE' and labels != 'NONE':
        raise ValueError('Single-Mesh mode creates no labels; select NONE')
    token=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    root=bpy.data.collections.new('CI55_DIAGNOSTICS_' + token)
    root[ROOT_TAG]=SCHEMA; root[RUN_TAG]=token
    root['ci_gallery_mode']=mode; root['ci_gallery_labels']=labels
    scene.collection.children.link(root)
    try:
        materials=_palette(root,token)
        groups=[]
        for row,(kind,category) in enumerate(CATEGORIES) if mode != 'ONE' else ():
            group=bpy.data.collections.new(f'{row+1:02d} · {category}')
            group[ROOT_TAG]=SCHEMA; group[RUN_TAG]=token
            group['ci_gallery_kind']=kind
            root.children.link(group)
            groups.append(group)
            if labels != 'NONE':
                body = f'{row+1:02d}' if labels=='ID' else f'{row+1:02d}   {category.upper()}  /  {kind}'
                _label(group,f'GROUP_{row+1:02d}',body,(-3.0,-row*ROW_Y+2.6),.45,token)
            if mode != 'SEPARATE':
                continue
            for column in range(5):
                number=column+1
                shape,name=build_case(kind,number)
                if not shape.vertices or len(shape.faces) != len(shape.face_materials):
                    raise ValueError(f'Invalid fixture: {kind}/{number}')
                ident=f'{row+1:02d}-{number:02d}'
                mesh=bpy.data.meshes.new('CI55_' + ident + '_' + kind)
                mesh.from_pydata(shape.vertices,shape.edges,shape.faces)
                mesh.update(calc_edges=True)
                for mat in materials: mesh.materials.append(mat)
                for poly, m in zip(mesh.polygons,shape.face_materials):
                    poly.material_index=m
                obj=bpy.data.objects.new(f'{ident}  {name}',mesh)
                group.objects.link(obj)
                obj.location=(column*COL_X,-row*ROW_Y,0)
                obj[OWNED_TAG]=SCHEMA; obj[RUN_TAG]=token
                obj['ci_kind']=kind; obj['ci_variant']=number
                obj['ci_expected_kinds']=json.dumps([kind])
                obj['ci_case_description']=name
                obj['ci_mesh_faces']=len(shape.faces)
                obj[FINGERPRINT_TAG]=mesh_fingerprint(mesh)
                obj['ci_fixture_transform_sha256']=object_transform_fingerprint(obj)
                if labels!='NONE':
                    body = ident if labels=='ID' else f'{ident}   {name}'
                    _label(group,f'LABEL_{ident}',body,(column*COL_X-2.5,-row*ROW_Y-2.7),.24,token)
        if mode == 'CATEGORY':
            for row,(kind,_title) in enumerate(CATEGORIES):
                _create_combined_mesh(groups[row],materials,token,mode,
                                      [(row,col,kind,col+1) for col in range(5)])
        elif mode == 'ONE':
            _create_combined_mesh(root,materials,token,mode,
                                  [(row,col,kind,col+1) for row,(kind,_title) in enumerate(CATEGORIES) for col in range(5)])
        root['ci_expected_mesh_count']=55 if mode=='SEPARATE' else 11 if mode=='CATEGORY' else 1
        root['ci_category_count']=11
        _gallery_inventory(root)  # Preflight before returning newly-created artifacts.
        return root
    except Exception:
        _rollback_incomplete_gallery(root,scene,token)
        raise


def _rollback_incomplete_gallery(root, scene, token):
    for obj in list(root.objects):
        if obj.get(RUN_TAG) != token:
            continue
        data=obj.data
        kind=obj.type
        bpy.data.objects.remove(obj,do_unlink=True)
        if data and data.users == 0 and kind == 'MESH':
            bpy.data.meshes.remove(data)
    for group in list(root.children):
        for obj in list(group.objects):
            if obj.get(RUN_TAG) != token:
                continue
            data = obj.data
            kind = obj.type
            bpy.data.objects.remove(obj, do_unlink=True)
            if data and data.users == 0:
                if kind == 'MESH': bpy.data.meshes.remove(data)
                elif kind == 'FONT': bpy.data.curves.remove(data)
        root.children.unlink(group)
        if group.users == 0: bpy.data.collections.remove(group)
    if root in scene.collection.children:
        scene.collection.children.unlink(root)
    if root.users == 0: bpy.data.collections.remove(root)
    for mat in list(bpy.data.materials):
        if mat.get(OWNED_TAG) == SCHEMA and mat.get(RUN_TAG) == token and not mat.users:
            bpy.data.materials.remove(mat)


def remove_gallery(root, scene):
    """Delete only complete, pristine galleries; fail before any destructive step."""
    if root is None or root.get(ROOT_TAG) != SCHEMA:
        raise ValueError('Collection is not a Collision Inspector diagnostic gallery')
    token = root.get(RUN_TAG)
    _gallery_inventory(root)   # Atomic preflight: not one object deleted yet.
    groups = list(root.children)
    # ONE mode contains its sole Mesh directly in the root collection.
    for obj in list(root.objects):
        if len(obj.users_collection) > 1:
            root.objects.unlink(obj)
            continue
        data=obj.data
        kind=obj.type
        bpy.data.objects.remove(obj,do_unlink=True)
        if data and data.users==0 and kind=='MESH':
            bpy.data.meshes.remove(data)
    # The gallery is standalone. If an object was linked elsewhere, do not delete it.
    for group in groups:
        for obj in list(group.objects):
            if len(obj.users_collection) > 1:
                group.objects.unlink(obj)
                continue
            data=obj.data
            obj_type=obj.type
            bpy.data.objects.remove(obj,do_unlink=True)
            if data and data.users==0:
                if obj_type=='MESH':
                    bpy.data.meshes.remove(data)
                elif obj_type=='FONT':
                    bpy.data.curves.remove(data)
        root.children.unlink(group)
        if group.users == 0:
            bpy.data.collections.remove(group)
    if root in scene.collection.children:
        scene.collection.children.unlink(root)
    if root.users==0:
        bpy.data.collections.remove(root)
    for mat in list(bpy.data.materials):
        if mat.get(OWNED_TAG)==SCHEMA and mat.get(RUN_TAG)==token and mat.users==0:
            bpy.data.materials.remove(mat)


def case_object(root,kind,number):
    mode=root.get('ci_gallery_mode','SEPARATE') if root else 'SEPARATE'
    for obj in meshes_in(root):
        if mode=='SEPARATE' and obj.get('ci_kind')==kind and int(obj.get('ci_variant',0))==number:
            return obj
        if mode=='CATEGORY' and obj.get('ci_kind')==kind:
            return obj
        if mode=='ONE':
            return obj
    return None


def report_path():
    downloads=Path.home()/'Downloads'
    directory=downloads if downloads.is_dir() else Path(tempfile.gettempdir())
    return directory/('CollisionInspector_FixtureVerification_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')


def validate_gallery(root, analyze, source_settings):
    """Caller supplies an analyzer(settings,obj)->counts. Scratch Scene owns results.

    No source object is activated or selected; the user's existing event list
    and inspector state stay untouched. Results are evidence, NOT an assertion
    that fixture metadata is correct or Blender GUI was tested.
    """
    objects=_gallery_inventory(root)
    if root.get('ci_gallery_mode','SEPARATE') != 'SEPARATE':
        return validate_grouped_gallery(root,objects,analyze,source_settings)
    scratch=bpy.data.scenes.new('CI55_validation_temporary')
    rows=[]
    try:
        st=scratch.collision_inspector
        for key in ('check_degenerate','check_self_intersections','check_gaps',
                    'convex_angle','gap_tolerance_mm'):
            setattr(st,key,getattr(source_settings,key))
        # Case library's declared default tolerance is 0.2mm. Test at that value,
        # independent of the currently active user's preferences.
        st.check_degenerate=True
        st.check_self_intersections=True
        st.check_gaps=True
        st.gap_tolerance_mm=.2
        for obj in objects:
            wanted=obj.get('ci_kind')
            st.events.clear()
            try:
                counts=analyze(st,obj)
                found=sorted({ev.kind for ev in st.events})
                ok=wanted in found
                issue_events=[event for event in st.events if event.kind==wanted]
                traces=sum(len(json.loads(event.trace_segments or '[]'))
                           for event in issue_events if getattr(event, 'trace_segments', ''))
                gap_witness=any(event.has_witness_pair for event in issue_events)
                overlap_regions=sum(len(json.loads(event.overlap_polygons or '[]'))
                                    for event in issue_events if getattr(event, 'overlap_polygons', ''))
                exact_visual_ok=(traces>0 if wanted=='INTERSECTION' else
                                 gap_witness if wanted=='GAP' else
                                 (traces>0 or overlap_regions>0) if wanted=='SELF_INTERSECTION' else
                                 overlap_regions>0 if wanted=='SURFACE_OVERLAP' else True)
                row={'id':obj.name[:5],'category':wanted,'variant':int(obj.get('ci_variant',0)),
                     'object':obj.name,'expected':[wanted], 'found':found,
                     'detection_status':'PASS' if ok else 'FAIL',
                     'visual_status':('PASS' if exact_visual_ok else 'FAIL') if ok else 'NOT_TESTED',
                     'visual_evidence':{'intersection_segments':traces,
                                        'coplanar_regions':overlap_regions,
                                        'has_gap_witness':gap_witness},
                     'status':'PASS' if ok and exact_visual_ok else 'FAIL',
                     'counts':dict(counts)}
            except Exception:
                row={'id':obj.name[:5], 'category':wanted, 'variant':int(obj.get('ci_variant',0)),
                     'object':obj.name,'expected':[wanted],'found':[],
                     'status':'ERROR','traceback':traceback.format_exc()}
            rows.append(row)
        pass_count=sum(row['status']=='PASS' for row in rows)
        return {'schema':SCHEMA,'created_utc':datetime.now(timezone.utc).isoformat(),
                'collection':root.name,'expected_cases':55,'total':len(rows),
                'passed':pass_count,'failed':len(rows)-pass_count,
                'detection_passed':sum(r.get('detection_status')=='PASS' for r in rows),
                'visual_passed':sum(r.get('visual_status')=='PASS' for r in rows),
                'notes':('Primary expected category required. INTERSECTION also requires an actual 3D trace; '
                         'GAP requires computed witness points. SELF_INTERSECTION requires a verified '
                         'segment or coplanar region; SURFACE_OVERLAP requires a coplanar region. '
                         'Other errors require correct category only. '
                         'GPU/UI rendering is NOT verified by this background test.'),
                'cases':rows}
    finally:
        bpy.data.scenes.remove(scratch)


def validate_grouped_gallery(root, objects, analyze, source_settings):
    """Aggregate stress checks, NOT 55 independent case verifications.

    Individual case certification is only available with mode SEPARATE.
    """
    scratch=bpy.data.scenes.new('CI55_aggregate_validation_temporary')
    rows=[]
    try:
        st=scratch.collision_inspector
        for key in ('check_degenerate','check_self_intersections','check_gaps',
                    'convex_angle','gap_tolerance_mm'):
            setattr(st,key,getattr(source_settings,key))
        st.check_degenerate=True
        st.check_self_intersections=True
        st.check_gaps=True
        st.gap_tolerance_mm=.2
        for obj in objects:
            expected=json.loads(obj.get('ci_expected_kinds','[]'))
            st.events.clear()
            try:
                counts=analyze(st,obj)
                found=sorted({ev.kind for ev in st.events})
                missing=sorted(set(expected)-set(found))
                rows.append({'object':obj.name,'expected':expected,'found':found,
                             'missing':missing,'counts':dict(counts),
                             'status':'PASS' if not missing else 'FAIL'})
            except Exception:
                rows.append({'object':obj.name,'expected':expected,'found':[],
                             'status':'ERROR','traceback':traceback.format_exc()})
        passed=sum(row['status']=='PASS' for row in rows)
        return {'schema':SCHEMA,'analysis_level':'AGGREGATE',
                'mode':root.get('ci_gallery_mode'), 'collection':root.name,
                'created_utc':datetime.now(timezone.utc).isoformat(),
                'expected_cases':55,'total':len(rows),'passed':passed,
                'failed':len(rows)-passed,
                'notes':('This is an aggregate Mesh stress check; it CANNOT certify the '
                         '55 individual cases. Background tests do NOT verify GPU drawing.'),
                'cases':rows}
    finally:
        bpy.data.scenes.remove(scratch)
