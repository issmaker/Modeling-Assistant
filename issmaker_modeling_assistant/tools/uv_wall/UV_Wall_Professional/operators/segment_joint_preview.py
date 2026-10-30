"""Opt-in true segment-junction preview. NEVER publishes a UV Wall Result.

Only closed profiles: physical solid CSG in Blender's Exact Boolean. The
experiment intentionally has NO production UV. It runs in a transaction on
private objects; the last good preview and all authored inputs survive errors.
"""
import bpy
import bmesh
import hashlib
import json
import math
import traceback
import uuid
from datetime import datetime
from mathutils import Vector
from bpy.props import EnumProperty
from ..production.path import ordered_path_vertices, is_path_closed
from ..production.cornice import cornice_profile_data, cornice_side_sign
from ..production.true_corner_trim import build_true_trim, TrimError
from ..production.segment_joint_graph import create_joint_graph, graph_diagnostics, JointGraphError
from ..core.support_logging import append_text, append_traceback

TAG = 'uvwall_segment_joint_1175_preview'
SOLO_KEY='uvwall_joint1175_solo_state'
FULL_SECTION_METHOD='FULL_SECTION'


def _snapshot(ob):
    """Hash immutable coordinates, edges, object matrix before/after operation."""
    if ob is None or ob.type != 'MESH':
        raise ValueError('Требуется Mesh объект пути и профиля')
    payload = {'co':[(float(v.co.x),float(v.co.y),float(v.co.z)) for v in ob.data.vertices],
               'edges':[tuple(int(i) for i in e.vertices) for e in ob.data.edges],
               'matrix':[list(map(float,row)) for row in ob.matrix_world]}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()


def _validate_profile_shape(ring):
    """2D closed silhouette with nonzero area, no self intersections."""
    n = len(ring)
    if n < 3: raise ValueError('PROFILE_TOO_SHORT')
    area = sum(ring[i][0]*ring[(i+1)%n][1]-ring[(i+1)%n][0]*ring[i][1] for i in range(n))*.5
    if abs(area) < 1.e-8: raise ValueError('PROFILE_ZERO_AREA')
    def orient(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    def cross(a,b,c,d):
        p,q,r,s=orient(a,b,c),orient(a,b,d),orient(c,d,a),orient(c,d,b)
        return (p*r < -1.e-12 and q*s < -1.e-12)
    for i in range(n):
        a,b=ring[i],ring[(i+1)%n]
        if math.hypot(a[0]-b[0],a[1]-b[1]) < .0021:raise ValueError('PROFILE_SHORT_EDGE')
        for j in range(i+1,n):
            if j == i or j == (i+1)%n or i == (j+1)%n: continue
            if cross(a,b,ring[j],ring[(j+1)%n]):raise ValueError('PROFILE_SELF_INTERSECTION')
    return area


def _make_solid(collection, ring_a, ring_b, index, profile=None):
    """Closed sweep shell with two caps; reorient volume for mirror parity."""
    n = len(ring_a)
    if n != len(ring_b) or n < 3:raise ValueError('BAD_RING_SIZE')
    coords = [tuple(p) for p in ring_a] + [tuple(p) for p in ring_b]
    faces = [(i, i+n, ((i+1)%n)+n, (i+1)%n) for i in range(n)]
    faces.extend((tuple(range(n-1,-1,-1)),tuple(range(n,2*n))))
    mesh = bpy.data.meshes.new('Joint1174_Segment_Mesh')
    mesh.from_pydata(coords,[],faces);mesh.update()
    if profile is not None:
        materials=_diagnostic_materials()
        for mat in materials: mesh.materials.append(mat)
        # Colours indicate the origin of a band in the ORIGINAL XZ section,
        # not a guessed final geometric inner/outer side after mirror.
        xvals=[float(p[0]) for p in profile]
        xmin,xmax=min(xvals),max(xvals)
        midpoint=(xmin+xmax)*0.5
        for k, polygon in enumerate(mesh.polygons[:n]):
            xmid=(xvals[k]+xvals[(k+1)%n])*0.5
            if xmid < midpoint-0.04*(xmax-xmin): polygon.material_index=1
            elif xmid > midpoint+0.04*(xmax-xmin): polygon.material_index=0
            else:polygon.material_index=2
        for polygon in mesh.polygons[n:]:polygon.material_index=2
    ob = bpy.data.objects.new('Joint1174_Segment_%02d'%index,mesh)
    collection.objects.link(ob)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        if not all(e.is_manifold for e in bm.edges):raise ValueError('PART_NONMANIFOLD')
        volume=bm.calc_volume(signed=True)
        if abs(volume) < 1.e-10:raise ValueError('PART_ZERO_VOLUME')
        if volume < 0:
            bmesh.ops.reverse_faces(bm,faces=list(bm.faces))
            bm.to_mesh(mesh);mesh.update()
    finally:bm.free()
    return ob


def _diagnostic_materials():
    '''Solid diagnostic material: A/B rails remain distinguishable with no UV.'''
    materials=[]
    for name,color in (
        ('JOINT1175_RAIL_A',(0.24,0.74,0.61,1.0)),
        ('JOINT1175_RAIL_B',(0.29,0.51,0.86,1.0)),
        ('JOINT1175_JOINT',(0.95,0.65,0.32,1.0)),
    ):
        material=bpy.data.materials.get(name)
        if material is None:
            material=bpy.data.materials.new(name)
            material.diffuse_color=color
            material.use_nodes=True
            bsdf=material.node_tree.nodes.get('Principled BSDF')
            if bsdf:bsdf.inputs['Base Color'].default_value=color
        materials.append(material)
    return materials


def _owned_classic_results(pathob):
    '''Do NOT touch wall results from a different authored path.'''
    return [o for o in bpy.data.objects if o.type=='MESH'
            and o.get('uvwall_owner_path')==pathob.name
            and o.get('uv_wall_mode')=='CORNICE'
            and not o.get('uvwall_experimental')]


def _preview_objects():
    return [o for o in bpy.data.objects if o.type=='MESH'
            and o.get('uvwall_experimental') and o.name.startswith('UVWall_JOINT')]


def _restore_comparison(scene, *, reveal_original=True):
    '''Restore old hide_set states; no mesh/source modifications.'''
    packed=scene.get(SOLO_KEY,'')
    try:state=json.loads(packed) if packed else {}
    except (TypeError,ValueError):state={}
    for name,hidden in state.get('results',{}).items():
        ob=bpy.data.objects.get(name)
        if ob is not None:
            try:ob.hide_set(bool(hidden) if reveal_original else True)
            except RuntimeError:pass
    if SOLO_KEY in scene:del scene[SOLO_KEY]


def _activate_preview(context, preview, pathob):
    scene=context.scene
    _restore_comparison(scene)
    originals=_owned_classic_results(pathob)
    states={o.name:bool(o.hide_get()) for o in originals}
    scene[SOLO_KEY]=json.dumps({'results':states,'preview':preview.name,
                                'path':pathob.name,'version':'1.17.5'},ensure_ascii=False)
    for ob in originals:ob.hide_set(True)
    for ob in _preview_objects():ob.hide_set(ob is not preview)
    preview.hide_set(False)
    bpy.ops.object.select_all(action='DESELECT')
    preview.select_set(True)
    context.view_layer.objects.active=preview
    append_text(f'[JOINT1175] DISPLAY_ACTIVE=PREVIEW name={preview.name} '
                f'old_hidden={[o.name for o in originals]}; production_uv=False\n')


def _mesh_metrics(obj):
    bm=bmesh.new()
    try:
        bm.from_mesh(obj.data)
        if not bm.faces: raise ValueError('NO_RESULT_FACES')
        bad=[e for e in bm.edges if not e.is_manifold]
        bad_faces=sum(1 for f in bm.faces if f.calc_area()<=1.e-10)
        # A Boolean can be watertight yet leave arbitrarily thin triangles.
        # Report their geometry rather than calling such a joint architecturally PASS.
        quality=[]
        for face in bm.faces:
            area=face.calc_area()
            perimeter=sum(e.calc_length() for e in face.edges)
            if perimeter>1e-9:quality.append(4.*math.pi*area/(perimeter*perimeter))
        min_compactness=min(quality) if quality else 0.
        thin_faces=sum(q<0.0005 for q in quality)
        volume=bm.calc_volume(signed=True)
        # A union can return multiple closed but disconnected islands; each
        # island is manifold, yet an architectural joint is still missing.
        seen=set();components=0
        for seed in bm.verts:
            if seed in seen: continue
            components+=1;pending=[seed];seen.add(seed)
            while pending:
                v=pending.pop()
                for e in v.link_edges:
                    u=e.other_vert(v)
                    if u not in seen:seen.add(u);pending.append(u)
        result={'vertices':len(bm.verts),'edges':len(bm.edges),'faces':len(bm.faces),
                'nonmanifold_edges':len(bad),'degenerate_faces':bad_faces,
                'components':components,'volume_m3':volume,
                'min_face_compactness':round(min_compactness,9),'thin_faces':thin_faces,
                'inner_seam_production_verified':False}
        if bad or bad_faces or components!=1 or volume<=1.e-10:
            raise ValueError('INVALID_BOOLEAN_OUTPUT:'+json.dumps(result))
        return result
    finally:bm.free()


def _ring(a,b,profile,side,along):
    dx,dy=b[0]-a[0],b[1]-a[1]
    length=math.hypot(dx,dy);ux,uy=dx/length,dy/length
    nx,ny=-side*uy,side*ux
    return tuple((a[0]+ux*along+nx*x, a[1]+uy*along+ny*x,
                  a[2]+along*(b[2]-a[2])/length+z) for x,z in profile)


def _drop_collection(c):
    for obj in tuple(c.objects):
        mesh=obj.data if obj.type=='MESH' else None
        bpy.data.objects.remove(obj,do_unlink=True)
        if mesh and mesh.users==0:bpy.data.meshes.remove(mesh)
    bpy.data.collections.remove(c)


def build_preview(context, *, method='PLANE_TRIM'):
    """Atomic scratch build, no source mutation, no change to production Result."""
    if context.mode!='OBJECT':raise ValueError('Только Object Mode: не вмешиваться в Edit Mode')
    s=context.scene.uv_wall_settings
    pathob=s.path_mesh;profileob=s.cornice_profile
    if pathob is None or profileob is None:raise ValueError('Назначьте путь и профиль карниза')
    if pathob==profileob:raise ValueError('Путь и профиль должны быть разными объектами')
    before=(_snapshot(pathob),_snapshot(profileob))
    old_select=tuple(context.selected_objects);old_active=context.view_layer.objects.active
    path=ordered_path_vertices(pathob)
    closed=is_path_closed(path)
    if not closed: raise ValueError('Исследовательский Solid Joint: пока только замкнутый Path')
    path=path[:-1]
    if s.cornice_reverse_path:path=list(reversed(path))
    xyz=tuple(tuple(float(c) for c in p) for p in path)
    src_profile,profile_closed=cornice_profile_data(profileob)
    if not profile_closed:raise ValueError('Открытый профиль требует Surface Trim: Solid Boolean запрещён')
    section=[(float(p.x),float(p.z)) for p in src_profile[:-1]]
    rot=math.radians(float(s.cornice_profile_rotation));cs,sn=math.cos(rot),math.sin(rot)
    section=[(x*(-1 if s.cornice_profile_flip_x else 1)*cs-z*(-1 if s.cornice_profile_flip_y else 1)*sn,
              x*(-1 if s.cornice_profile_flip_x else 1)*sn+z*(-1 if s.cornice_profile_flip_y else 1)*cs)
             for x,z in section]
    _validate_profile_shape(section)
    # Same parity convention as production cornice, after path reversal.
    sign=int(cornice_side_sign(path+[path[0]],bool(s.cornice_flip_side)))
    graph=create_joint_graph(xyz,section,closed=True,side_sign=sign)
    if method in ('PLANE_TRIM','FULL_SECTION') and not graph.safe_independent:
        raise ValueError('JOINT_COUPLING_REQUIRED: зоны срезов соседних острых углов пересекаются. '
                         'Для этого контура используйте режим Finite Solid либо отдельный групповой решатель.')
    cuts=None
    if method in ('PLANE_TRIM','FULL_SECTION'):
        # T1 independently cuts each segment on a vertical bisector plane.
        # No weld across mismatched Z; exact CSG decides contact/overlap.
        cuts=build_true_trim(graph.path,graph.profile,closed=True,side_sign=sign)
    token=uuid.uuid4().hex[:9]
    collection=bpy.data.collections.new('UVWall JointGraph PENDING '+token)
    context.scene.collection.children.link(collection)
    context.view_layer.update()
    append_text(f'[JOINT1175] BEGIN token={token} method={method} path={pathob.name} profile={profileob.name}\n')
    try:
        objects=[]
        for i in range(len(graph.lengths)):
            if method in ('PLANE_TRIM','FULL_SECTION'):
                piece=cuts.segments[i]
                # Two analytically coincident 2D cut faces can only TOUCH in
                # 3D, particularly with unequal grades. Introduce a 0.5 mm
                # *local* CSG overlap (0.25 mm per side). This never alters
                # the authored path or an untrimmed portion of the profile.
                # It avoids degenerate coplanar/contact-only Boolean edges.
                a,b=xyz[i],xyz[(i+1)%len(xyz)]
                dist=graph.lengths[i]
                # FULL_SECTION uses *whole* closed profile body CSG; the
                # adaptive seam overlap is bounded locally, never changes the
                # user's path, and makes grade-disparate caps physically meet.
                if method=='FULL_SECTION':
                    local_gaps=[cut.max_z_gap for cut in cuts.corners
                                if cut.vertex in (i,(i+1)%len(xyz))]
                    max_grade_gap=max(local_gaps,default=0.)
                    step=min(0.0125,max(0.00025,max_grade_gap*.15))
                else:step=0.00025
                ux=(b[0]-a[0])/dist;uy=(b[1]-a[1])/dist
                uz=(b[2]-a[2])/dist
                start=tuple((x-ux*step,y-uy*step,z-uz*step)
                            for x,y,z in piece.start_ring)
                end=tuple((x+ux*step,y+uy*step,z+uz*step)
                          for x,y,z in piece.end_ring)
            else:
                a,b=xyz[i],xyz[(i+1)%len(xyz)]
                start=_ring(a,b,graph.profile,sign,0.)
                end=_ring(a,b,graph.profile,sign,graph.lengths[i])
            objects.append(_make_solid(collection,start,end,i,profile=graph.profile))
        # Stable order and explicit one-piece-at-a-time Boolean, Blender 4/5.
        base=objects[0]
        for other in objects[1:]:
            mod=base.modifiers.new('JointGraph1175_Exact','BOOLEAN')
            mod.solver='EXACT';mod.operation='UNION';mod.object=other
            bpy.ops.object.select_all(action='DESELECT');base.select_set(True)
            context.view_layer.objects.active=base
            bpy.ops.object.modifier_apply(modifier=mod.name)
            mesh=other.data
            bpy.data.objects.remove(other,do_unlink=True)
            if mesh.users==0:bpy.data.meshes.remove(mesh)
        metrics=_mesh_metrics(base)
        seam_cut_risks=([{'vertex':c.vertex, 'turn_deg':round(c.turn_deg,4),
                         'max_grade_gap_mm':round(c.max_z_gap*1000,3),
                         'status':c.status}
                        for c in cuts.corners] if cuts else [])
        # Full section CSG is not an affine warp of the profile; its
        # physical seam must be inspected in Blender and UV recomputed.
        if method=='FULL_SECTION' and metrics['min_face_compactness']<=1.e-12:
            raise ValueError('SECTION_SEAM_COLLAPSED: zero quality Boolean face')
        if before!=(_snapshot(pathob),_snapshot(profileob)):
            raise ValueError('AUTHOR_SOURCE_MUTATED')
        diag=graph_diagnostics(graph)
        diag.update({'algorithm':method,'metrics':metrics,'production_uv':False,
                     'joint_seam_overlap_m':(0.0005 if method=='PLANE_TRIM' else ('ADAPTIVE_UP_TO_0.025' if method=='FULL_SECTION' else 0.0)),
                     'cut_grade_risks':seam_cut_risks,'section_bands':len(graph.profile),
                     'joint_engine':'CLOSED_FULL_SECTION_CSG',
                     'version':'1.17.5-FIELD-PREVIEW','path_checksum':before[0],
                     'profile_checksum':before[1]})
        base.name='UVWall_JOINT1175_PREVIEW_'+method
        base['uvwall_experimental']=True
        base['uvwall_production_uv']=False
        base['uvwall_joint_diagnostics']=json.dumps(diag,ensure_ascii=False)
        collection.name='UVWall JointGraph PREVIEW '+token
        append_text('[JOINT1175] GEOMETRY_PASS_NOT_VISUAL_CERTIFICATION '+json.dumps(diag,ensure_ascii=False)+'\n')
        return base,diag
    except Exception:
        append_traceback('JOINT1175_FAILED',traceback.format_exc(),stage='JOINT_GRAPH_PREVIEW')
        _drop_collection(collection)
        raise
    finally:
        try:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in old_select:
                if obj.name in bpy.data.objects:obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                context.view_layer.objects.active=old_active
        except Exception:pass


class UVWALL_OT_segment_joint_preview(bpy.types.Operator):
    bl_idname='uvwall.segment_joint_preview'
    bl_label='Segment Trim · 3D Preview'
    bl_description='Экспериментальное 3D-соединение без производственной UV; исходный карниз не изменяется'
    bl_options={'REGISTER','UNDO'}
    method: EnumProperty(name='Стыковка',items=[
        ('PLANE_TRIM','Plane Trim + Exact Union','Независимые пространственные профили с общей плоскостью среза'),
        ('FINITE_UNION','Finite Solid Union','Независимые сегменты строго до авторских вершин, Exact Union'),
        ('FULL_SECTION','Full Section · 3D Union','Полное сечение каждого сегмента, объединение объёмов и диагностика внутреннего шва')],
        default='FULL_SECTION')
    @classmethod
    def poll(cls,context):
        return context.mode=='OBJECT' and getattr(context.scene,'uv_wall_settings',None) is not None
    def execute(self,context):
        try:
            obj,info=build_preview(context,method=self.method)
            _activate_preview(context,obj,context.scene.uv_wall_settings.path_mesh)
            self.report({'WARNING'},f'JOINT 1.17.5 PREVIEW ONLY: {obj.name}; исходный Result скрыт, UV не производственная')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},f'JointGraph preview failed: {str(exc)[:220]}')
            return {'CANCELLED'}


class UVWALL_OT_cleanup_joint_previews(bpy.types.Operator):
    bl_idname='uvwall.cleanup_joint_previews'
    bl_label='Очистить тестовые карнизы JointGraph'
    bl_description='Удаляет только экспериментальные коллекции JointGraph. Основные Result, Path, профили не трогает'
    bl_options={'REGISTER','UNDO'}

    def invoke(self,context,event):
        return context.window_manager.invoke_confirm(self,event)

    def execute(self,context):
        _restore_comparison(context.scene)
        removed=0
        for collection in tuple(bpy.data.collections):
            if not collection.name.startswith('UVWall JointGraph PREVIEW '):continue
            objects=tuple(collection.objects)
            if not objects or not all(bool(ob.get('uvwall_experimental')) for ob in objects):continue
            _drop_collection(collection);removed+=1
        append_text(f'[JOINT1175] PREVIEW_CLEANUP removed={removed}\n')
        self.report({'INFO'},f'Удалено тестовых коллекций: {removed}')
        return {'FINISHED'}


class UVWALL_OT_joint_compare1175(bpy.types.Operator):
    bl_idname='uvwall.joint_compare1175'
    bl_label='Сравнить Classic / Joint 3D'
    bl_description='Показать только выбранный Result, не меняя геометрию и UV'
    bl_options={'REGISTER','UNDO'}
    mode: EnumProperty(name='Вид',items=[
        ('PREVIEW','Joint Preview','Новый 3D-стык без производственных UV'),
        ('CLASSIC','Classic Result','Существующий производственный карниз с UV')],
        default='PREVIEW')

    def execute(self,context):
        scene=context.scene
        s=scene.uv_wall_settings
        pathob=s.path_mesh
        if pathob is None:
            self.report({'ERROR'},'Нужно назначить путь')
            return {'CANCELLED'}
        previews=_preview_objects()
        if self.mode=='PREVIEW':
            previews=[o for o in previews if o.get('uvwall_joint_diagnostics')]
            if not previews:
                self.report({'ERROR'},'Сначала создайте Joint Preview')
                return {'CANCELLED'}
            candidate=previews[-1]
            _activate_preview(context,candidate,pathob)
        else:
            _restore_comparison(scene)
            for ob in previews:ob.hide_set(True)
            originals=_owned_classic_results(pathob)
            if originals:
                originals[0].hide_set(False)
                bpy.ops.object.select_all(action='DESELECT')
                originals[0].select_set(True)
                context.view_layer.objects.active=originals[0]
            append_text('[JOINT1175] DISPLAY_ACTIVE=CLASSIC production_uv=True\n')
        self.report({'INFO'},'Показан '+('Joint Preview: НЕ production UV' if self.mode=='PREVIEW' else 'Classic Result'))
        return {'FINISHED'}
