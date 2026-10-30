"""BH-04B.6.3 Blender-native filter and read-only BMesh contract tests.

These tests do NOT verify GPU draw handlers, cursor hover, or Esc on a user
workstation. A successful native run must not be reported as UI acceptance.
"""
import traceback
import bpy
import bmesh
from .seam_inspector import filtered_issue_indices, classify_pair, scan_bmesh
from .bh0462_native_cases import _make_seam_object


def run_native_geometry_ui_cases(scene):
    result=[]
    def check(name,fn):
        try:
            evidence=fn()
            result.append({'case':'BH0463_'+name,'status':'PASS',
                           'evidence':evidence if evidence is not None else {}})
        except Exception as exc:
            result.append({'case':'BH0463_'+name,'status':'FAIL',
                           'evidence':{'reason':str(exc),
                                       'traceback':traceback.format_exc()[-12000:]}})
    sample=({'severity':'ERROR'},{'severity':'REVIEW'},
            {'severity':'ERROR'},{'severity':'REVIEW'})
    for name,errors,reviews,expected in (
        ('FILTER_ERRORS',True,False,(0,2)),
        ('FILTER_REVIEW',False,True,(1,3)),
        ('FILTER_BOTH',True,True,(0,1,2,3)),
        ('FILTER_NONE',False,False,())):
        def verify(e=errors,r=reviews,want=expected):
            actual=filtered_issue_indices(sample,e,r)
            assert actual==want, (actual,want)
            return {'indices':actual,'read_only':True}
        check(name,verify)
    def verify_semantics():
        span=4000/4096
        edge=((.1,.2),(.2,.2))
        periodic=tuple((u+span,v) for u,v in edge)
        assert classify_pair(edge,edge)['kind']=='OK'
        assert classify_pair(edge,periodic)['kind']=='PERIODIC_UV_REPEAT'
        assert classify_pair(edge,edge,material_difference=True)['severity']=='REVIEW'
        assert classify_pair(edge,((.4,.2),(.2,.2)),marked=True)['severity']=='REVIEW'
        assert classify_pair(edge,((.4,.2),(.2,.2)))['severity']=='ERROR'
        return {'periodic_not_error':True,'material_boundary_review':True,
                'unexpected_error':True,'image_pixels_tested':False}
    check('SEMANTIC_SEPARATION',verify_semantics)
    obj=None
    try:
        obj=_make_seam_object(scene)
        bm=bmesh.from_edit_mesh(obj.data)
        layer=bm.loops.layers.uv.active
        bm.faces.index_update()
        def snapshot():
            return (len(bm.verts),len(bm.edges),len(bm.faces),
                    tuple(tuple(tuple(l[layer].uv) for l in f.loops) for f in bm.faces),
                    tuple(bool(e.seam) for e in bm.edges))
        def verify_read_only():
            before=snapshot()
            hits,stats=scan_bmesh(bm,obj)
            assert before==snapshot(),'Inspector mutated BMesh UV/topology'
            assert len(hits)==1 and stats['broken']==1,(hits,stats)
            assert hits[0]['reason_code']=='UNEXPECTED_UV_BREAK'
            assert len(hits[0]['uv_endpoints_a'])==2
            assert len(hits[0]['uv_endpoints_b'])==2
            return {'UV_preserved':True,'mesh_preserved':True,
                    'reason_code':hits[0]['reason_code'],'hits':len(hits)}
        check('READ_ONLY_WITNESS',verify_read_only)
        def verify_marked():
            internal=next(e for e in bm.edges if len(e.link_faces)==2)
            internal.seam=True
            before=snapshot()
            hits,stats=scan_bmesh(bm,obj)
            assert before==snapshot(),'Inspector changed authored seam'
            assert stats['review']==1 and stats['broken']==0,(hits,stats)
            assert filtered_issue_indices(hits,True,False)==()
            assert filtered_issue_indices(hits,False,True)==(0,)
            return {'marked_class':'REVIEW','filtering_keeps_mesh_intact':True}
        check('AUTHORED_SEAM_FILTER',verify_marked)
    except Exception as exc:
        result.append({'case':'BH0463_BMESH_SETUP','status':'FAIL',
                       'evidence':{'reason':str(exc),
                                   'traceback':traceback.format_exc()[-12000:]}})
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode=='EDIT':
                bpy.ops.object.mode_set(mode='OBJECT')
            if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
        except Exception as exc:
            result.append({'case':'BH0463_BMESH_CLEANUP','status':'FAIL',
                           'evidence':{'reason':str(exc)}})
    return result
