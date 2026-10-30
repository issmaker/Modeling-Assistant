"""BH-04B actual Blender BMesh/Operator smoke fixtures, not run offline."""
import bmesh
import bpy
import math
import traceback


def run_native_corner_build_cases(scene, snapshot):
    """Exercise real registered mesh.brick_hole_corner_miter_build.

    Every case builds its OWN temporary mesh in the dedicated laboratory scene.
    Checks exact source Mesh+UV preservation, topology, UV metric and rollback.
    """
    results=[]
    cases=(('SAFE_MITER_OUTWARD',.3,2,True),
           ('OVERLONG_MITER_REJECT',.95,2,False),
           ('INVALID_ONE_EDGE_REJECT',.3,1,False),
           ('NEGATIVE_MITER_SAFE',-.3,2,True),
           ('UNSUPPORTED_ADAPTIVE_REJECT',.3,2,False),
           ('UNSUPPORTED_CONTINUE_REJECT',.3,2,False))
    for label,depth,count,expected_success in cases:
        obj=None
        try:
            mesh=bpy.data.meshes.new('ISSMAKER_BH04B_'+label)
            mesh.from_pydata([(0,0,0),(1,0,0),(1,0,1),(0,0,1),
                              (0,1,0),(0,1,1)],[],[(0,1,2,3),(0,3,5,4)])
            mesh.update()
            uv=mesh.uv_layers.new(name='UVMap')
            for face in mesh.polygons:
                for index in face.loop_indices:
                    vertex=mesh.vertices[mesh.loops[index].vertex_index].co
                    uv.data[index].uv=(.35+(float(vertex.x)+float(vertex.y))*1000/4096,
                                       .35+float(vertex.z)*1000/4096)
            obj=bpy.data.objects.new('ISSMAKER_BH04B_'+label,mesh)
            scene.collection.objects.link(obj)
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for other in tuple(bpy.context.selected_objects):other.select_set(False)
            bpy.context.view_layer.objects.active=obj
            obj.select_set(True)
            bpy.context.tool_settings.mesh_select_mode=(False,True,False)
            bpy.ops.object.mode_set(mode='EDIT')
            bm=bmesh.from_edit_mesh(obj.data)
            for f in bm.faces:f.select_set(False)
            for e in bm.edges:e.select_set(False)
            for v in bm.verts:v.select_set(False)
            edges=[e for e in bm.edges if all(abs(float(v.co.z)-1)<1e-7 for v in e.verts)]
            if len(edges)!=2:raise AssertionError('Unexpected source boundary fixture')
            for edge in edges[:count]:edge.select_set(True)
            bm.select_flush_mode()
            bmesh.update_edit_mesh(obj.data,loop_triangles=False,destructive=False)
            scene.brick_hole_uv_policy=('ADAPTIVE' if label=='UNSUPPORTED_ADAPTIVE_REJECT' else
                                        'CONTINUE' if label=='UNSUPPORTED_CONTINUE_REJECT' else 'STRICT')
            scene.brick_hole_depth=abs(depth)
            scene.brick_hole_direction='OUTWARD' if depth>=0 else 'INWARD'
            scene.brick_hole_direction_source='AUTO'
            scene.brick_hole_resolution='4096'
            scene.brick_hole_uv_map='UVMap'
            scene.brick_hole_material_mode='INHERIT'
            before=snapshot(obj)
            rejection=None
            try:
                outcome=bpy.ops.mesh.brick_hole_corner_miter_build()
            except RuntimeError as exc:
                rejection=str(exc)
                expected_fragments={
                    'OVERLONG_MITER_REJECT':'срез заходит слишком далеко',
                    'INVALID_ONE_EDGE_REJECT':'выберите ровно два смежных',
                    'UNSUPPORTED_ADAPTIVE_REJECT':'пока нет подтверждённого UV',
                    'UNSUPPORTED_CONTINUE_REJECT':'пока нет подтверждённого UV',
                }
                if expected_success or expected_fragments.get(label,'NO_EXPECTED_REJECTION') not in rejection:
                    raise
                outcome={'CANCELLED'}
            after=snapshot(obj)
            success='FINISHED' in outcome
            if success!=expected_success:
                raise AssertionError('Expected success=%s, got %s' % (expected_success,sorted(outcome)))
            evidence={'operator':sorted(outcome),'source_faces_before':2,
                      'vert_count_before':len(before['vertices']),
                      'vert_count_after':len(after['vertices'])}
            if success:
                bm=bmesh.from_edit_mesh(obj.data)
                if len(bm.faces)!=8 or len(bm.verts)!=10:
                    raise AssertionError('Expected 6 new triangles and 4 new vertices')
                uv_layer=bm.loops.layers.uv.active
                if uv_layer is None:raise AssertionError('Lost active UVMap')
                original=[f for f in bm.faces if set(tuple(v.co) for v in f.verts) in (
                    set([(0,0,0),(1,0,0),(1,0,1),(0,0,1)]),
                    set([(0,0,0),(0,0,1),(0,1,1),(0,1,0)]))]
                if len(original)!=2:
                    raise AssertionError('Original faces not preserved')
                # The common Auto Lab snapshot uses 6 decimals for 3D coordinates
                # and 7 decimals for UV (see bh031_native_lab._snapshot/_uv_key).
                # Using 6 UV decimals here causes a false FAIL on unchanged float32
                # values (e.g. 0.5941406488 -> 0.594141 vs 0.5941406).
                original_uvs=sorted(tuple(sorted((tuple(round(float(x),6) for x in l.vert.co),
                                                     tuple(round(float(x),7) for x in l[uv_layer].uv))
                                                    for l in f.loops)) for f in original)
                if original_uvs!=before['uvfaces']:
                    expected=before['uvfaces']
                    first_diff=next(({'face_index':i, 'expected':expected[i],
                                      'actual':original_uvs[i]}
                                     for i in range(min(len(expected),len(original_uvs)))
                                     if expected[i]!=original_uvs[i]), None)
                    raise AssertionError('Authored wall UV changed: '+repr({
                        'before_faces':len(expected),'after_faces':len(original_uvs),
                        'uv_round_decimals':7,'first_diff':first_diff})[:3000])
                evidence['source_uv_comparison_decimals']=7
                layer=bm.faces.layers.int.get('brick_hole_result')
                if layer is None:raise AssertionError('Missing result provenance')
                new_faces=[f for f in bm.faces if f not in original]
                if len(new_faces)!=6 or any(f.calc_area()<=1e-8 for f in new_faces):
                    raise AssertionError('Invalid new faces')
                # Independent TOPOLOGY oracle: a welded seam is S--Q--M,
                # with Q the midpoint of M and S. This is invariant under
                # negative depth, transforms and BMEdge vertex ordering.
                s_vert=next(v for v in bm.verts
                            if all(abs(float(v.co[i])-(0.,0.,1.)[i])<1e-6
                                   for i in range(3)))
                before_xyz=set(before['vertices'])
                new_verts=[v for v in bm.verts
                           if tuple(round(float(c),6) for c in v.co) not in before_xyz]
                seam_options=[]
                for mid in new_verts:
                    for end in new_verts:
                        if mid is end:continue
                        if sum((float(mid.co[i])-(float(s_vert.co[i])+float(end.co[i]))*.5)**2
                               for i in range(3))>1e-12:continue
                        first=bm.edges.get((s_vert,mid))
                        second=bm.edges.get((mid,end))
                        if first is None or second is None:continue
                        pair=(first,second)
                        if all(len(edge.link_faces)==2 and
                               all(face in new_faces for face in edge.link_faces)
                               for edge in pair):
                            seam_options.append(pair)
                if len(seam_options)!=1:
                    raise AssertionError('Expected unique welded S-Q-M seam: '
                                         +repr({'candidates':len(seam_options),
                                                'new_vertices':[tuple(v.co) for v in new_verts]}))
                seam=list(seam_options[0])
                evidence['seam_vertex_xyz']=[tuple(round(float(c),7) for c in v.co)
                                             for v in (s_vert,seam[0].other_vert(s_vert),
                                                       seam[1].other_vert(seam[0].other_vert(s_vert)))]
                evidence['new_face_count']=len(new_faces)
                evidence['welded_seam_edges']=len(seam)
                evidence['authored_uv_unchanged']=True
            else:
                if before!=after:raise AssertionError('Failed operation changed Mesh/UV')
                evidence['atomic_rollback_verified']=True
                if rejection:evidence['expected_reason']=rejection[:400]
            results.append({'case':'BH04B_'+label,'status':'PASS','evidence':evidence})
        except Exception as exc:
            # Keep the production operator's inner traceback, not just bpy.ops
            # RuntimeError. It is required for locating invalid BMesh handles.
            try:
                from . import LOG_TEXT_NAME
                text=bpy.data.texts.get(LOG_TEXT_NAME)
                internal_log=text.as_string()[-24000:] if text is not None else 'MISSING_BRICK_HOLE_LOG'
            except Exception as log_exc:
                internal_log='READ_ERROR: '+repr(log_exc)
            results.append({'case':'BH04B_'+label,'status':'FAIL',
                            'evidence':{'reason':str(exc),'traceback':traceback.format_exc()[-11000:],
                                        'brick_hole_internal_log':internal_log}})
        finally:
            try:
                if bpy.context.object and bpy.context.object.mode=='EDIT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
            except Exception as exc:
                results.append({'case':'BH04B_'+label+'_CLEANUP','status':'FAIL',
                                'evidence':{'reason':str(exc)}})
    return results
