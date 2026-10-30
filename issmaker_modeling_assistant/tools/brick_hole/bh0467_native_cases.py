"""BH0467 native Blender oracle for 4×4 module seam and 414-edge Preview gate.

No results in this file imply live GPU/mouse acceptance. Temporary BMesh
fixtures are created and destroyed inside the private native lab scene.
"""
import bpy
import bmesh
import traceback
from .seam_inspector import scan_bmesh
from . import MAX_SELECTED_EDGES, _MAX_INTERACTIVE_EDGES, BH0467_BUILD_ID


def run_native_module_seam_cases(scene):
    results=[]
    def record(name,fn):
        try:
            detail=fn()
            results.append({'case':'BH0467_'+name,'status':'PASS',
                            'evidence':detail if isinstance(detail,dict) else {}})
        except Exception as exc:
            results.append({'case':'BH0467_'+name,'status':'FAIL',
                            'evidence':{'error':str(exc),
                                        'traceback':traceback.format_exc()[-9000:]}})
    def preview_gate():
        assert _MAX_INTERACTIVE_EDGES==MAX_SELECTED_EDGES, 'Preview hidden 300-edge gate'
        assert _MAX_INTERACTIVE_EDGES>=414
        return {'selected_edges':414,'preview_max':_MAX_INTERACTIVE_EDGES,
                'no_independent_300_gate':True}
    record('PREVIEW_414_GATE',preview_gate)
    record('BUILD_ID',lambda: {'build_id':BH0467_BUILD_ID} if 'BH0467' in BH0467_BUILD_ID else 1/0)
    for size in (4.,2.):
        mesh=bpy.data.meshes.new('BH0467_SOURCE_MESH_'+str(int(size)))
        obj=bpy.data.objects.new('BH0467_SOURCE_'+str(int(size)),mesh)
        scene.collection.objects.link(obj)
        bm=bmesh.new()
        try:
            v=[bm.verts.new(p) for p in ((0.,0.,0.),(size/2,0.,0.),(size/2,size,0.),(0.,size,0.),
                                            (size,0.,0.),(size,size,0.))]
            left=bm.faces.new((v[0],v[1],v[2],v[3]))
            right=bm.faces.new((v[1],v[4],v[5],v[2]))
            bm.normal_update()
            uv=bm.loops.layers.uv.new('UVMap')
            density=(size*1000)/(4096 if size==4. else 2048)
            for f in bm.faces:
                for l in f.loops:
                    l[uv].uv=(float(l.vert.co.x)/size*density,
                              float(l.vert.co.y)/size*density)
            shared=next(e for e in bm.edges if len(e.link_faces)==2)
            bm.edges.index_update();bm.faces.index_update()
            def snapshot():
                return (len(bm.verts),len(bm.edges),len(bm.faces),
                        tuple(tuple(tuple(float(x) for x in l[uv].uv) for l in f.loops)
                              for f in bm.faces),tuple(bool(e.seam) for e in bm.edges))
            def verify_clean():
                before=snapshot();issues,stats=scan_bmesh(bm,obj,resolution=4096 if size==4. else 2048)
                assert stats['broken']==0 and stats['review']==0,(issues,stats)
                assert before==snapshot(), 'Scan mutated BMesh'
                return {'module_m':size,'issues':0,'read_only':True}
            record(f'SIZE_{int(size)}_SPLIT_CONTINUOUS',verify_clean)
            for l in right.loops:
                l[uv].uv.x += .25
            def verify_shift():
                before=snapshot();hits,stats=scan_bmesh(bm,obj,resolution=4096 if size==4. else 2048)
                assert stats['broken']==1 and stats['module_internal_breaks']==1,(stats,hits)
                assert hits[0]['kind']=='MODULE_INTERNAL_UV_GAP',hits
                assert hits[0]['module_witness']['module_size_m']==size
                assert before==snapshot(), 'Scan mutated shifted UV'
                return {'module_m':size,'class':hits[0]['kind'], 'gap_px':hits[0]['gap_px']}
            record(f'SIZE_{int(size)}_INTERNAL_SHIFT_RED',verify_shift)
            shared.seam=True
            record(f'SIZE_{int(size)}_MARKED_SHIFT_STILL_RED',verify_shift)
            for l in right.loops:
                l[uv].uv.x-=.25
            record(f'SIZE_{int(size)}_RESTORED_CLEAN',verify_clean)
            shared.seam=False
        except Exception as exc:
            results.append({'case':f'BH0467_FIXTURE_{int(size)}','status':'FAIL',
                            'evidence':{'error':str(exc),'traceback':traceback.format_exc()[-9000:]}})
        finally:
            bm.free()
            bpy.data.objects.remove(obj,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
    return results
