"""DWOS Surface-Driven experimental step-by-step native Blender laboratory.

Opt-in only: nothing in the legacy DWOS composer is modified or called.
Steps 1..8 have isolated operators and evidence snapshots.  Complex polygon
insets that would change topology are BLOCKED, never silently re-sized.
"""
from __future__ import annotations
import bpy
import bmesh
from bpy.app.handlers import persistent
from bpy.props import FloatProperty, PointerProperty, BoolProperty, IntProperty
from mathutils import Vector
from pathlib import Path
from datetime import datetime, timezone
import json, uuid, time, os, traceback, zipfile, hashlib, platform, math
from . import surface_step_core as core
from . import error_presentation
from . import surface_uv_modules as uvmod
from . import surface_lp_first as lpfirst

SCHEMA='DWOS_SURFACE_LAB_W33_W34_LP_FIRST_v2' # new version: old session must not silently migrate
ENGINE_REVISION='DWOS_SURFACE_LAB_W33_W34_LP_FIRST_MODULE_UV_BRICK_HOLE'
SESSION_KEY='DWOS_surface_session_v1'
HOST_NAME_KEY='DWOS_surface_host_name'
OBJ_ID_KEY='DWOS_surface_uuid'
STEPS={1:'MATERIAL',2:'INSET',3:'DEPTH',4:'REMOVE_FILL',5:'OPT_GLASS',6:'INTERIOR',7:'MULLIONS',8:'WIDTH_REVISION'}
MAT_NAMES={'FRAME':'DWOS LAB | FRAME','GLASS':'DWOS LAB | GLASS','INTERIOR':'DWOS LAB | INTERIOR','MULLION':'DWOS LAB | MULLION','PANEL':'DWOS LAB | PANEL'}


def _now():return datetime.now(timezone.utc).isoformat()
def _session_dir(sid):
    root=Path(bpy.app.tempdir or str(Path.home()))/'ISSMAKER_DWOS_SURFACE_LAB'/str(sid)
    root.mkdir(parents=True,exist_ok=True)
    return root


def _write_event(sid,step,status,**data):
    event={'utc':_now(),'schema':SCHEMA,'engine_revision':ENGINE_REVISION,'step':step,'name':STEPS.get(step,'AUX'),
           'status':status,'data':data}
    with (_session_dir(sid)/'events.jsonl').open('a',encoding='utf8') as out:
        out.write(json.dumps(event,ensure_ascii=False,default=str)+'\n')
    return event


def _material(name,color):
    mat=bpy.data.materials.get(name)
    if mat is None:
        mat=bpy.data.materials.new(name=name)
        mat.diffuse_color=(*color,1)
    return mat


def _ensure_material(obj,name,color):
    m=_material(name,color)
    idx=obj.data.materials.find(name)
    if idx<0:obj.data.materials.append(m);idx=len(obj.data.materials)-1
    return idx


def _collection(name, scene=None):
    # Explicit context avoids linking clone collections to a different scene
    # when an operator is invoked with a Blender context override.
    target_scene=scene if scene is not None else bpy.context.scene
    coll=bpy.data.collections.get(name)
    if coll is None:
        coll=bpy.data.collections.new(name)
    if coll not in target_scene.collection.children[:]:
        target_scene.collection.children.link(coll)
    return coll


def _mesh_fingerprint(obj):
    # Ignore materials, UV and selection: fingerprints are for topology + XYZ only.
    mesh=obj.data
    is_edit=obj.mode=='EDIT'
    bm=bmesh.from_edit_mesh(mesh) if is_edit else None
    h=hashlib.sha256()
    if bm is not None:
        bm.verts.ensure_lookup_table();bm.faces.ensure_lookup_table();bm.verts.index_update();bm.faces.index_update()
        if len(bm.verts)>150000 or len(bm.faces)>150000:raise ValueError('HOST_TOO_LARGE_FOR_SAFE_FINGERPRINT')
        for v in bm.verts:
            h.update(('%0.9f,%0.9f,%0.9f;'%tuple(v.co)).encode())
        for f in bm.faces:h.update((','.join(str(v.index) for v in f.verts)+';').encode())
    else:
        if len(mesh.vertices)>150000 or len(mesh.polygons)>150000 or len(mesh.edges)>300000:raise ValueError('HOST_TOO_LARGE_FOR_SAFE_FINGERPRINT')
        for v in mesh.vertices:h.update(('%0.9f,%0.9f,%0.9f;'%tuple(v.co)).encode())
        for f in mesh.polygons:h.update((','.join(map(str,f.vertices))+';').encode())
    return h.hexdigest()


def _uv_fingerprint(obj):
    """UV-loop SHA gate, independent of topology fingerprint and material edits."""
    h=hashlib.sha256();mesh=obj.data
    if obj.mode=='EDIT':
        bm=bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table();bm.faces.index_update()
        for name in [layer.name for layer in mesh.uv_layers]:
            layer=bm.loops.layers.uv.get(name)
            if layer is None:raise ValueError('UV_LAYER_MISSING:'+name)
            h.update(name.encode('utf8'))
            for f in bm.faces:
                for l in f.loops:
                    h.update(('%0.8f,%0.8f;'%tuple(l[layer].uv)).encode())
    else:
        for layer in mesh.uv_layers:
            h.update(layer.name.encode('utf8'))
            for loop in layer.data:h.update(('%0.8f,%0.8f;'%tuple(loop.uv)).encode())
    return h.hexdigest()


def _get_session_wall(context,allow_legacy=False):
    obj=context.active_object
    if obj and obj.get(HOST_NAME_KEY):obj=bpy.data.objects.get(str(obj[HOST_NAME_KEY]))
    if obj is None or not obj.get(SESSION_KEY):
        name=context.scene.get('DWOS_surface_current_wall','')
        obj=bpy.data.objects.get(str(name))
    if obj is None or obj.type!='MESH' or not obj.get(SESSION_KEY):
        raise ValueError('Сначала выделите полигоны стены и выполните шаг 1')
    data=json.loads(str(obj[SESSION_KEY]))
    if (data.get('backup_kind')=='MESH_DATABLOCK_FAKE_USER_V43'
            and int(data.get('step',0))==1 and not data.get('objects')
            and bpy.data.meshes.get(str(data.get('backup_name',''))) is None):
        raise ValueError('STEP1_UNDONE_PENDING_REDO: Старт окна отменён; выполните Redo или начните новый Step 1')
    if data.get('schema')!=SCHEMA and not allow_legacy:
        raise ValueError('LEGACY_SESSION: старую сессию можно экспортировать и восстановить; новую начните на копии стены')
    return obj,data


def _frame_of_boundary(bm,faces,obj):
    chosen=set(faces)
    if not chosen:raise ValueError('NO_SELECTED_FACES')
    if len(chosen)>10000:raise ValueError('TOO_MANY_SELECTED_FACES')
    selected_edges=[e for e in bm.edges if sum(f in chosen for f in e.link_faces)==1]
    if len(selected_edges)<3:raise ValueError('NO_SINGLE_BOUNDARY')
    by_vertex={}
    for e in selected_edges:
        for v in e.verts:by_vertex.setdefault(v,[]).append(e.other_vert(v))
    if any(len(neighbors)!=2 for neighbors in by_vertex.values()):
        raise ValueError('BRANCHED_BOUNDARY_OR_HOLES: expected degree 2')
    start=min(by_vertex,key=lambda v:v.index)
    path=[start];current=start;prev=None
    for _ in range(len(selected_edges)):
        nbrs=by_vertex[current]
        nxt=nbrs[0] if nbrs[0]!=prev else nbrs[1]
        if nxt==start:break
        if nxt in path:raise ValueError('SELF_CROSSING_BOUNDARY_GRAPH')
        path.append(nxt);prev,current=current,nxt
    if len(path)!=len(selected_edges) or start not in by_vertex[current]:
        raise ValueError('MULTIPLE_COMPONENTS_OR_OPEN_BOUNDARY')
    center=path[0].co.copy()
    n=sum((f.normal.copy()*f.calc_area() for f in faces),Vector((0,0,0)))
    if n.length<1e-12:raise ValueError('INVALID_SOURCE_NORMAL')
    n.normalize()
    for f in faces:
        if f.normal.dot(n)<.9999:raise ValueError('MULTIPLANAR_FACE_NORMALS')
        if any(abs((v.co-center).dot(n))>1e-5 for v in f.verts):
            raise ValueError('NONPLANAR_SELECTED_PATCH')
    direction=max((path[(i+1)%len(path)].co-v.co for i,v in enumerate(path)),key=lambda p:p.length)
    direction=direction-direction.dot(n)*n
    if direction.length<1e-9:raise ValueError('ZERO_PATH_BASIS')
    direction.normalize();up=n.cross(direction);up.normalize()
    projected=[((v.co-center).dot(direction),(v.co-center).dot(up)) for v in path]
    if core.signed_area(projected)<0:projected.reverse();path.reverse()
    validated=core.normalized_boundary(projected)
    coords=[tuple(map(float,v.co)) for v in path]
    if len(validated)!=len(path):raise ValueError('INVALID_PROJECTED_BOUNDARY')
    # Source data is retained even after faces are deleted at step 4.
    return {'vertices_local':coords,'points':validated,'origin':list(map(float,center)),
            'x_axis':list(map(float,direction)),'y_axis':list(map(float,up)),
            'normal':list(map(float,n)),
            'face_ids':sorted(int(f.index) for f in faces),
            'boundary_vertex_ids':[int(v.index) for v in path],
            'source_uv_layers':[uv.name for uv in obj.data.uv_layers],
            'selected_face_count':len(faces)}


def _source_uv_atlas(bm, selection, source):
    """Capture *per-loop* triangulated UV before assigning FRAME material."""
    layer=bm.loops.layers.uv.active
    if layer is None:
        raise ValueError('SOURCE_WALL_HAS_NO_ACTIVE_UV_LAYER')
    origin=Vector(source['origin']);ux=Vector(source['x_axis']);uy=Vector(source['y_axis'])
    def xy(v):
        q=v.co-origin
        return [float(q.dot(ux)),float(q.dot(uy))]
    chosen=set(selection);tris=[]
    for tri in bm.calc_loop_triangles():
        if tri[0].face not in chosen:continue
        tris.append({'face_id':int(tri[0].face.index),
                     'xy':[xy(l.vert) for l in tri],
                     'uv':[[float(t) for t in l[layer].uv] for l in tri]})
    if not tris:raise ValueError('SOURCE_UV_TRIANGULATION_EMPTY')
    if len(tris)>30000:raise ValueError('SOURCE_UV_ATLAS_TOO_LARGE: 30000 triangles limit')
    seams=[]
    for e in bm.edges:
        if len(e.link_faces)==2 and all(f in chosen for f in e.link_faces):
            seams.append({'face_ids':sorted(f.index for f in e.link_faces),
                          'xy':[xy(v) for v in e.verts]})
    module_cuts=uvmod.module_seams_from_edges(seams)
    return {'uv_layer':layer.name,'triangles':tris,'seams':seams,
            'module_cuts':module_cuts,'source_faces':sorted(f.index for f in selection)}


class SurfaceComposer:
    def __init__(self,shape,resolution=4096):
        self.frame=shape;self.vertices=[];self.faces=[];self.roles=[];self.uvs=[];self.parts=[];self.modules=[];self.keymap={}
        self.atlas=shape.get('uv_atlas')
        self.front_z=0.0
        self.res=resolution;self.scale=1000/float(resolution)
        self.origin=Vector(shape['origin']);self.x=Vector(shape['x_axis']);self.y=Vector(shape['y_axis']);self.n=Vector(shape['normal'])
        self.min_u=min(p[0] for p in shape['points']);self.min_v=min(p[1] for p in shape['points'])
    def idx(self,p,group='FRAME'):
        key=(group,)+tuple(round(float(v),9) for v in p)
        if key not in self.keymap:
            self.keymap[key]=len(self.vertices)
            v=self.origin+self.x*key[1]+self.y*key[2]+self.n*key[3]
            self.vertices.append(tuple(v))
        return self.keymap[key]
    def face(self,pts,role,uv=None,group=None,part=None,module=None,uv_origin=None,uv_edge=None):
        ids=[self.idx(p,group=group or role) for p in pts]
        if len(set(ids))<3:raise ValueError('ZERO_AREA_GENERATED_FACE')
        self.faces.append(ids);self.roles.append(role)
        if uv_edge is not None and role in ('FRAME','MULLION'):
            if module is None or self.atlas is None:
                raise ValueError('PROFILE_REVEAL_MISSING_SOURCE_UV_PROVENANCE')
            a,b=uv_edge
            ua=uvmod.face_uv_at(self.atlas,module,a)
            ub=uvmod.face_uv_at(self.atlas,module,b)
            du=(ub[0]-ua[0],ub[1]-ua[1]);l=math.hypot(*du)
            if l<1e-10:raise ValueError('PROFILE_UV_ZERO_SOURCE_EDGE')
            # Source face can have legacy texel density != 1000. Its UV stays
            # untouched; newly generated REVEAL is independently 1000px/m.
            # Such a mismatch intentionally introduces a seam at the source
            # edge, reported by the source density audit.
            physical=math.dist(a,b)
            if abs(l*self.res/physical-1000.)>2.:
                ul=physical*self.scale
                ub=(ua[0]+du[0]/l*ul,ua[1]+du[1]/l*ul)
            # Source UV tangent + metric normal-to-tangent across profile depth.
            perp=(-du[1]/l,du[0]/l)
            uv=[]
            for p in pts:
                nearest=ua if math.dist(p[:2],a)<=math.dist(p[:2],b) else ub
                depth=(self.front_z-p[2])*self.scale
                uv.append((nearest[0]+perp[0]*depth,nearest[1]+perp[1]*depth))
        if uv is None and role in ('FRAME','MULLION'):
            # Legacy fallback retained only for older ring helpers, not LP-FIRST.
            a,b,c=pts[:3]
            ab=Vector(b)-Vector(a);ac=Vector(c)-Vector(a)
            norm=ab.cross(ac)
            if norm.length<1e-12:raise ValueError('DEGENERATE_PROFILE_FACE')
            norm.normalize()
            axis=max(range(3),key=lambda i:abs(norm[i]))
            options=([1,2] if axis==0 else [0,2] if axis==1 else [0,1])
            uv=[((p[options[0]]-(self.min_u if options[0]==0 else self.min_v if options[0]==1 else 0))*self.scale,
                 (p[options[1]]-(self.min_u if options[1]==0 else self.min_v if options[1]==1 else 0))*self.scale) for p in pts]
        if uv is not None:
            area_uv=sum(u[0]*v[1]-v[0]*u[1] for u,v in zip(uv,uv[1:]+uv[:1]))
            if abs(area_uv)<1e-15:raise ValueError('UV_DEGENERATE_OR_TOO_SMALL')
            # Do NOT flip U independently per face: shared front corners would
            # acquire incompatible UV values and visible random seams.
        self.uvs.append(uv)
        self.parts.append(part or role)
        self.modules.append(module)
    def ring(self,outer,inner,z,role='FRAME',reverse=False,mapping=None):
        mapping=mapping if mapping is not None else list(range(len(outer)))
        for i in range(len(outer)):
            j=(i+1)%len(outer)
            face=[(*outer[i],z),(*outer[j],z),(*inner[mapping[j]],z),(*inner[mapping[i]],z)]
            if mapping[i]==mapping[j]:face.pop()
            uv=[((p[0]-self.min_u)*self.scale,(p[1]-self.min_v)*self.scale) for p in face]
            if reverse:face.reverse();uv.reverse()
            self.face(face,role,uv=uv,part='FRONT' if not reverse else 'REAR')
    def sides(self,points,z0,z1,role='FRAME',reverse=False):
        lengths=[0.]
        for a,b in zip(points,points[1:]+points[:1]):lengths.append(lengths[-1]+core.length(core.sub(a,b)))
        for i in range(len(points)):
            j=(i+1)%len(points)
            verts=[(*points[i],z0),(*points[j],z0),(*points[j],z1),(*points[i],z1)]
            uv=[(lengths[i]*self.scale,z0*self.scale),(lengths[i+1]*self.scale,z0*self.scale),
                (lengths[i+1]*self.scale,z1*self.scale),(lengths[i]*self.scale,z1*self.scale)]
            if reverse:verts.reverse();uv.reverse()
            self.face(verts,role,uv=uv if role in ('FRAME','MULLION') else None,part='INNER_REVEAL' if role=='FRAME' else role)
    def pane(self,poly,z):self.face([(*p,z) for p in poly],'GLASS',group=f'GLASS:{len(self.faces)}')
    def box(self,x0,x1,y0,y1,z0,z1,role='MULLION'):
        pts=[(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),
             (x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]
        group=f'{role}:BOX:{len(self.faces)}'
        for ids in ((0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)):
            self.face([pts[i] for i in ids],role,group=group,part='MULLION_PROFILE')
    def plane_band(self,x0,x1,y0,y1,z,role='MULLION'):
        self.face([(x0,y0,z),(x1,y0,z),(x1,y1,z),(x0,y1,z)],role,group=f'{role}:PLANE:{len(self.faces)}',part='MULLION_FRONT')


def _geometry(state,stage,params):
    """LP first, then extrude its GLASS region boundaries into recessed HP."""
    from . import surface_step_core as sc, surface_graph
    source=state['source'];boundary=source['points']; width=params['frame_width']
    atlas=source.get('uv_atlas')
    if not atlas or not atlas.get('triangles'):
        raise ValueError('SOURCE_UV_ATLAS_MISSING: start a NEW Surface Lab session from Step 1')
    shape=sc.safe_inset(boundary,width)
    outer=shape['outer'];inner=shape['inner'];z=params['window_offset']
    from .surface_graph import _orthogonal_bounds
    _orthogonal_bounds(outer)
    _orthogonal_bounds(inner)
    graph=surface_graph.make_graph(outer,inner,
        params['mullion_width'] if stage>=7 else None,
        atlas.get('module_cuts'),
        zone_tree_next=params.get('zone_tree_next') if stage>=7 else None)
    uvmod.annotate_graph(graph,atlas)
    hp=SurfaceComposer(source,params['texture_resolution'])
    lp=SurfaceComposer(source,params['texture_resolution'])
    hp.front_z=z;lp.front_z=z
    surface_graph.build_lp(lp,graph,z)
    derivation=lpfirst.build_hp(hp,graph,z,params['frame_depth'],
                              params['mullion_depth'],params['interior_depth'],stage,
                               classic_glass=bool(params.get('classic_glass_midframe',False)))
    # All stages are validated before any Blender mesh is published.
    for name,c in (('HP',hp),('LP',lp)):
        if not c.vertices or not c.faces:raise ValueError(name+'_EMPTY_BUILD')
        for fi,face in enumerate(c.faces):
            p=[Vector(c.vertices[i]) for i in face]
            if max((p[j]-p[0]).cross(p[j+1]-p[0]).length
                   for j in range(1,len(p)-1))<1e-10:
                raise ValueError(name+f'_DEGENERATE_FACE:{fi}')
        stats=sc.mesh_topology_stats(c.vertices,c.faces)
        if stats['nonmanifold_edges']:
            raise ValueError(name+'_NONMANIFOLD:'+str(stats))
        topo=sc.mesh_orientation_connectivity_audit(c.vertices,c.faces,c.roles)
        if topo['components']!=1 or topo['winding_conflicts']:
            raise ValueError(name+'_DISCONNECTED_OR_REVERSED:'+str(topo)[:850])
        uv_a=uvmod.module_front_uv_audit(c.vertices,c.faces,c.roles,
                c.parts,c.uvs,c.modules,params['texture_resolution'])
        if uv_a['unintended_seams'] or uv_a['missing_provenance']:
            raise ValueError(name+'_INVALID_SOURCE_MODULE_UV:'+str(uv_a)[:850])
        # Existing authored walls may deviate slightly from the requested
        # 1000 px/m. Never silently alter them; log a WARN, not a fake PASS.
        if c is hp and stage>=3 and topo['boundary_edges']<3:
            raise ValueError('HP_RECESS_UNEXPECTED_CLOSED_OR_EMPTY_BORDER')
    # Independent contract: one mesh with shared indices (not mere coincident XYZ).
    gstats=sc.mesh_orientation_connectivity_audit(hp.vertices,hp.faces,hp.roles)
    lstats=sc.mesh_orientation_connectivity_audit(lp.vertices,lp.faces,lp.roles)
    profile_uv=uvmod.reveal_metric_uv_audit(hp.vertices,hp.faces,hp.roles,hp.parts,hp.uvs,params['texture_resolution'])
    if profile_uv['bad_edge_count']:
        raise ValueError('BRICK_HOLE_REVEAL_UV_DENSITY:'+str(profile_uv)[:650])
    if stage>=3:
        if bool(params.get('classic_glass_midframe',False)):
            # The true pane is an independent 4-vertex Mesh, so the HP frame
            # has open inner rim(s); LP still owns temporary front cover faces.
            if gstats['boundary_edges']<=lstats['boundary_edges']:
                raise ValueError('CLASSIC_HP_EXPECTED_OPEN_GLAZING_BORDER:'+str({'HP':gstats,'LP':lstats})[:900])
        elif gstats['boundary_edges']!=lstats['boundary_edges']:
            raise ValueError('BRICK_HOLE_GLASS_RECESS_OPEN_EDGE_MISMATCH:'+str({'HP':gstats,'LP':lstats})[:900])
    shape['lp_first']={'derivation':derivation,'module_faces':graph['source_module_counts'],
                       'module_cuts':atlas.get('module_cuts',{}),'graph_cells':len(graph['cells']),
                       'zones':len(graph['zones']),'zone_fills':graph.get('zone_fills',{}),
                        'profile_uv':profile_uv}
    return hp,lp,shape

def _construct_mesh(name,buf):
    mesh=bpy.data.meshes.new(name)
    try:
        mesh.from_pydata(buf.vertices,[],buf.faces)
        mesh.update(calc_edges=True)
        for name,color in (('FRAME',(.23,.30,.36)),('GLASS',(.25,.62,.75)),
                           ('INTERIOR',(.82,.82,.83)),('MULLION',(.18,.22,.26)),
                           ('PANEL',(.51,.50,.47))):
            mesh.materials.append(_material(MAT_NAMES[name],color))
        roles={'FRAME':0,'GLASS':1,'INTERIOR':2,'MULLION':3,'PANEL':4,'TEMP_CAP':0}
        uv=mesh.uv_layers.new(name='DWOS_FRAME_METRIC_1000_PX_M')
        for pi,(role,face_uv) in enumerate(zip(buf.roles,buf.uvs)):
            poly=mesh.polygons[pi];poly.material_index=roles[role]
            for li in poly.loop_indices:
                ordinal=li-poly.loop_start
                uv.data[li].uv=face_uv[ordinal] if face_uv is not None else (0.,0.)
        mesh['DWOS_surface_uv_schema']='SOURCE_MODULE_FRONT_AND_METRIC_REVEAL_W34_LP_FIRST' 
        mesh['DWOS_surface_uv_unmapped_roles']='GLASS,PANEL,INTERIOR,TEMP_CAP'
        return mesh
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise


def _rollback_published(token):
    """Restore previous HP/LP datablocks if publishing or wall commit fails."""
    for obj,old_data,created,old_state in reversed(token.get('objects',[])):
        if created:
            bpy.data.objects.remove(obj,do_unlink=True)
        elif old_data is not None:
            obj.data=old_data
            obj.matrix_world=old_state['matrix']
            obj.hide_set(old_state['hidden'])
            for key,value in old_state['custom'].items():
                if value is None:
                    if key in obj:del obj[key]
                else:obj[key]=value
    for mesh in token.get('meshes',[]):
        if mesh.users==0:bpy.data.meshes.remove(mesh)


def _complete_published(token):
    for obj,old_data,created,old_state in token.get('objects',[]):
        if old_data is not None and old_data.users==0:
            bpy.data.meshes.remove(old_data)


def _w48_plane_uv(points, scale):
    x0=min(float(p[0]) for p in points)
    y0=min(float(p[1]) for p in points)
    coords=[((float(p[0])-x0)*scale,(float(p[1])-y0)*scale) for p in points]
    if not all(math.isfinite(a) and math.isfinite(b) for a,b in coords):
        raise ValueError("W48_NONFINITE_PLANE_UV")
    if max(a for a,b in coords)>1.+1e-5 or max(b for a,b in coords)>1.+1e-5:
        raise ValueError("W48_SINGLE_PLANE_EXCEEDS_UV_TILE")
    return coords


def _classic_glass_mesh(s, params, stage):
    """Production glass_builder: ONE 4-vertex plane across central mullion."""
    if not bool(params.get('classic_glass_midframe',False)) or stage<3:
        return None
    from . import glass_builder, assembly_core
    from .surface_step_core import rectangle_axes
    outer=s['source']['points']
    bbox=rectangle_axes(outer)
    fw=float(params['frame_width'])
    depth=float(params['frame_depth'])
    assembly_core.validate_glazing(depth,.003,0.,False)
    center,_=assembly_core.depth_anchors(depth,float(params['window_offset']),-1,0.,0.)
    if bbox is not None:
        x0,x1,y0,y1=bbox
        relative=assembly_core.glass_rectangle(x1-x0,y1-y0,fw,0.)
        cx=(x0+x1)*.5
        cy=(y0+y1)*.5
        rect=(relative[0]+cx,relative[1]+cx,relative[2]+cy,relative[3]+cy)
        verts,faces,roles=glass_builder.build_glass_mesh(rect,center,0.,full=False,gap=.003,interior_sign=-1)
        if len(verts)!=4 or len(faces)!=1 or len(faces[0])!=4:
            raise ValueError('CLASSIC_GLASS_NOT_ONE_QUAD')
        points=[verts[k] for k in faces[0]]
        kind='GLASS_SINGLE_QUAD'
    else:
        from . import surface_step_core, surface_graph
        surface_graph._orthogonal_bounds(outer)
        inset=fw*.5-assembly_core.glass_overlap(fw)
        if inset<=1e-7:
            raise ValueError('CLASSIC_GLASS_INVALID_FRAME_INSET')
        poly=surface_step_core.safe_inset(outer,inset)['inner']
        points=[(float(x),float(y),center) for x,y in poly]
        if len(points)<4:
            raise ValueError('CLASSIC_GLASS_DEGENERATE_POLYGON')
        kind='GLASS_SINGLE_ORTHOGONAL_SNAKE'
    composer=SurfaceComposer(s['source'],params['texture_resolution'])
    uv=None # W56: glass requires NO unwrap
    composer.face(points,'GLASS',uv=uv,group='SINGLE_LEGACY_GLASS',part=kind)
    mesh=_construct_mesh('DWOS_SURFACE_GLASS_'+s['id'][:8],composer)
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[0])
    mesh['DWOS_W56_UV_POLICY']='NO_GLASS_UV'
    mesh['DWOS_glass_depth_center_m']=center
    mesh['DWOS_glass_sheet_count']=1
    mesh['DWOS_glass_vertex_count']=len(points)
    mesh['DWOS_glass_legacy_builder']=bbox is not None
    mesh['DWOS_glass_geometry_kind']=kind
    mesh['DWOS_glass_uv_density_target']=1000.
    return mesh


def _classic_interior_mesh(s, params, stage):
    """W49 LP-FIRST Interior = open rear reveal sleeve, never a glass blocker.

    Original window_builder retains its quarter-depth BOX contract.
    LP-FIRST has a physically full-depth frame. Its rear boundary is
    at frame_depth, so a quarter-depth BOX would overlap that frame
    and place an opaque cap behind the glass.
    """
    if not bool(params.get('classic_glass_midframe',False)) or stage<6:
        return None
    from . import interior_core, surface_graph
    from .surface_step_core import rectangle_axes, safe_inset
    depth=float(params['interior_depth'])
    fd=float(params['frame_depth'])
    facade=float(params['window_offset'])
    from . import assembly_core
    if not (math.isfinite(depth) and .8-1e-8<=depth<=1.5+1e-8
            and math.isfinite(fd) and fd>=.005 and math.isfinite(facade)):
        raise ValueError('DWOS_W55_INTERIOR_DEPTH_MUST_BE_0_8_TO_1_5_M')
    fw=float(params['frame_width'])
    # Old DWOS v3.7 architecture: belt centerline XY (0.5*frame width)
    # and quarter-depth from exterior facade.  This is independent from
    # GLASS at exactly half-depth.  NEVER use W49 back-edge anchor here.
    offset=fw*.5
    outline=safe_inset(s['source']['points'],offset)['inner']
    rect=rectangle_axes(outline)
    _glass_anchor,start=assembly_core.depth_anchors(fd,facade,-1,0.,0.)
    if rect is not None:
        verts,faces,roles=interior_core.build_interior_mesh(
            rect,start,depth,-1,clearance=0.,mode='REVEALS')
        if len(faces)!=4 or any(role!='INTERIOR_REVEAL' for role in roles):
            raise ValueError('DWOS_W49_INTERIOR_SLEEVE_RECT_TOPOLOGY')
    else:
        surface_graph._orthogonal_bounds(outline)
        n=len(outline)
        verts=[(x,y,start) for x,y in outline]+[(x,y,start-depth) for x,y in outline]
        faces=[(i,(i+1)%n,(i+1)%n+n,i+n) for i in range(n)]
    composer=SurfaceComposer(s['source'],params['texture_resolution'])
    for face in faces:
        pts=[verts[i] for i in face]
        composer.face(pts,'INTERIOR',uv=None,
                      group='W56_INTERIOR_SHELL',part='INTERIOR_REVEAL_NO_UV')
    # W56: BACK CLOSED, FRONT OPEN. Nothing opaque may cover the pane/front.
    # Rear contour uses precisely the same vertex IDs as the side wall ends.
    n=len(verts)//2
    rear_indices=list(range(n,2*n))
    # The source extrusion proceeds toward negative local normal; rear
    # cap outward normal is negative local Z. Most authored outlines are CCW.
    rear_points=[verts[i] for i in rear_indices]
    composer.face(rear_points,'INTERIOR',uv=None,
                  group='W56_INTERIOR_SHELL',part='INTERIOR_TERMINAL_BACK_NO_UV')
    topo=core.mesh_orientation_connectivity_audit(composer.vertices,composer.faces,composer.roles)
    if topo['components']!=1 or topo['winding_conflicts']:
        raise ValueError('DWOS_W49_INTERIOR_TOPOLOGY:'+str(topo))
    mesh=_construct_mesh('DWOS_SURFACE_INTERIOR_'+s['id'][:8],composer)
    mesh['DWOS_interior_attachment_policy']='W55_BELT_CENTERLINE_QUARTER_DEPTH'
    mesh['DWOS_interior_start_depth_m']=start
    mesh['DWOS_interior_depth_m']=depth
    mesh['DWOS_interior_start_frame_width_fraction']=.5
    mesh['DWOS_interior_start_frame_depth_fraction']=.25
    while mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[0])
    mesh['DWOS_interior_rear_cap']=True
    mesh['DWOS_W56_UV_POLICY']='NO_INTERIOR_UV'
    mesh['DWOS_interior_uv_target_px_m']='NO_UV_REQUIRED'
    return mesh

def _publish(context,host,s,step,params):
    hp,lp,geom=_geometry(s,step,params)
    mesh_hp=_construct_mesh(f'DWOS_SURFACE_HP_{s["id"][:8]}',hp)
    try:mesh_lp=_construct_mesh(f'DWOS_SURFACE_LP_{s["id"][:8]}',lp)
    except Exception:
        bpy.data.meshes.remove(mesh_hp);raise
    token={'objects':[],'meshes':[mesh_hp,mesh_lp]}
    try:
        interior_mesh=_classic_interior_mesh(s,params,step)
        if interior_mesh is not None:token['meshes'].append(interior_mesh)
        glass_mesh=_classic_glass_mesh(s,params,step)
        if glass_mesh is not None:token['meshes'].append(glass_mesh)
        objs=[]
        for mode,mesh in (('HP',mesh_hp),('LP',mesh_lp)):
            name=s.get('objects',{}).get(mode,'')
            obj=bpy.data.objects.get(name)
            created=obj is None
            if created:
                obj=bpy.data.objects.new(f'DWOS_SURFACE_{mode}_{s["id"][:8]}',mesh)
                _collection('ISSMAKER_WINDOWS_'+mode).objects.link(obj)
                old=None
            else:
                old=obj.data
            old_state={'matrix':obj.matrix_world.copy(),
                       'hidden':obj.hide_get(),
                       'custom':{k:obj.get(k) for k in (OBJ_ID_KEY,HOST_NAME_KEY,'DWOS_surface_stage')}}
            if not created:obj.data=mesh
            token['objects'].append((obj,old,created,old_state))
            obj.matrix_world=host.matrix_world.copy()
            obj[OBJ_ID_KEY]=s['id'];obj[HOST_NAME_KEY]=host.name;obj['DWOS_surface_stage']=step
            obj.hide_set(mode=='LP')
            objs.append(obj)
        published_objects={'HP':objs[0].name,'LP':objs[1].name}
        if interior_mesh is not None:
            mode='INTERIOR'
            old_name=s.get('objects',{}).get(mode,'')
            obj=bpy.data.objects.get(old_name)
            created=obj is None
            if created:
                obj=bpy.data.objects.new('DWOS_SURFACE_INTERIOR_'+s['id'][:8],interior_mesh)
                _collection('ISSMAKER_WINDOWS_INTERIOR').objects.link(obj)
                old=None
            else:
                old=obj.data
            old_state={'matrix':obj.matrix_world.copy(),'hidden':obj.hide_get(),
                       'custom':{k:obj.get(k) for k in (OBJ_ID_KEY,HOST_NAME_KEY,'DWOS_surface_stage')}}
            if not created:obj.data=interior_mesh
            token['objects'].append((obj,old,created,old_state))
            obj.matrix_world=host.matrix_world.copy()
            obj[OBJ_ID_KEY]=s['id'];obj[HOST_NAME_KEY]=host.name;obj['DWOS_surface_stage']=step
            obj.hide_set(False)
            published_objects[mode]=obj.name
        if glass_mesh is not None:
            mode='GLASS'
            old_name=s.get('objects',{}).get(mode,'')
            obj=bpy.data.objects.get(old_name)
            created=obj is None
            if created:
                obj=bpy.data.objects.new('DWOS_SURFACE_GLASS_'+s['id'][:8],glass_mesh)
                _collection('ISSMAKER_WINDOWS_GLASS').objects.link(obj)
                old=None
            else:
                old=obj.data
            old_state={'matrix':obj.matrix_world.copy(),'hidden':obj.hide_get(),
                       'custom':{k:obj.get(k) for k in (OBJ_ID_KEY,HOST_NAME_KEY,'DWOS_surface_stage')}}
            if not created:
                # W55: preserve explicit opt-in user-approved glass preview
                # material when a stage-7 mullion reconstruction republishes it.
                previous=old.materials[1] if len(old.materials)>1 else None
                if previous is not None and previous.name=='DWOS W54 | OPT Glass Physical Preview':
                    glass_mesh.materials[1]=previous
                obj.data=glass_mesh
            token['objects'].append((obj,old,created,old_state))
            obj.matrix_world=host.matrix_world.copy()
            obj[OBJ_ID_KEY]=s['id'];obj[HOST_NAME_KEY]=host.name;obj['DWOS_surface_stage']=step
            obj.hide_set(False)
            published_objects[mode]=obj.name
        result={'objects':published_objects,
                'hp':core.mesh_topology_stats(hp.vertices,hp.faces),
                'lp':core.mesh_topology_stats(lp.vertices,lp.faces),
                'inset':{'width':geom['width'],'source_area':geom['source_area'],'inner_area':geom['inner_area'],
                         'topology':geom['topology']},
                'geometry_gate':{'HP_LP_FIRST_SHELL':core.mesh_orientation_connectivity_audit(hp.vertices,hp.faces,hp.roles),
                                 'LP_FACADE':core.mesh_orientation_connectivity_audit(lp.vertices,lp.faces,lp.roles)},
                'uv_audit':{'HP':uvmod.module_front_uv_audit(hp.vertices,hp.faces,hp.roles,hp.parts,hp.uvs,hp.modules,params['texture_resolution']),
                            'LP':uvmod.module_front_uv_audit(lp.vertices,lp.faces,lp.roles,lp.parts,lp.uvs,lp.modules,params['texture_resolution'])},
                'geometry_contract':'W34_LP_FIRST_GLASS_ZONE_EXTRUSION',
                'lp_first':geom.get('lp_first'),
                'profile_uv_audit':uvmod.reveal_metric_uv_audit(hp.vertices,hp.faces,hp.roles,hp.parts,hp.uvs,params['texture_resolution']),
                'source_uv_audit':{'HP':uvmod.module_front_uv_audit(hp.vertices,hp.faces,hp.roles,hp.parts,hp.uvs,hp.modules,params['texture_resolution']),
                                   'LP':uvmod.module_front_uv_audit(lp.vertices,lp.faces,lp.roles,lp.parts,lp.uvs,lp.modules,params['texture_resolution'])}}
        return result,token
    except Exception:
        _rollback_published(token)
        raise


def _snapshot_live_mesh(obj):
    """Read real EDIT-BMesh rather than stale obj.data.polygons; includes UV."""
    mesh=obj.data
    if obj.mode=='EDIT':
        bm=bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table();bm.edges.ensure_lookup_table();bm.faces.ensure_lookup_table()
        bm.verts.index_update();bm.edges.index_update();bm.faces.index_update()
        if len(bm.verts)>150000 or len(bm.faces)>150000 or len(bm.edges)>300000:
            return {'name':obj.name,'skipped':'edit topology exceeds safe 150000 limit'}
        uv_layers={}
        for u in mesh.uv_layers:
            layer=bm.loops.layers.uv.get(u.name)
            if layer is not None:
                uv_layers[u.name]=[[float(v) for v in l[layer].uv]
                                   for f in bm.faces for l in f.loops]
        coords=[tuple(float(x) for x in v.co) for v in bm.verts]
        faces=[[int(v.index) for v in f.verts] for f in bm.faces]
        materials=[int(f.material_index) for f in bm.faces]
        edges=[[int(v.index) for v in e.verts] for e in bm.edges]
        wire_edges=[[int(v.index) for v in e.verts] for e in bm.edges if not e.link_faces]
        origin='LIVE_EDIT_BMESH'
    else:
        if len(mesh.vertices)>150000 or len(mesh.polygons)>150000 or len(mesh.edges)>300000:
            return {'name':obj.name,'skipped':'topology exceeds safe 150000 limit'}
        coords=[tuple(float(x) for x in v.co) for v in mesh.vertices]
        faces=[list(p.vertices) for p in mesh.polygons]
        materials=[int(p.material_index) for p in mesh.polygons]
        edges=[[int(v) for v in e.vertices] for e in mesh.edges]
        used={tuple(sorted((int(a),int(b)))) for f in faces for a,b in zip(f,f[1:]+f[:1])}
        wire_edges=[e for e in edges if tuple(sorted(e)) not in used]
        uv_layers={u.name:[[float(a) for a in l.uv] for l in u.data] for u in mesh.uv_layers}
        origin='MESH_DATABLOCK'
    return {'name':obj.name,'source':origin,
            'matrix_world':[[float(x) for x in row] for row in obj.matrix_world],
            'verts':coords,'faces':faces,'edges':edges,'wire_edges':wire_edges,
            'actual_edge_count':len(edges),'wire_edge_count':len(wire_edges),
            'material_indices':materials,
            'material_slots':[m.name if m else None for m in mesh.materials],
            'uv_layers':uv_layers,'mesh_attributes':[a.name for a in mesh.attributes],
            'topology':core.mesh_topology_stats(coords,faces)}


def _save_session(host,s):host[SESSION_KEY]=json.dumps(s,ensure_ascii=False)


def _backup_mesh(host,bm,sid):
    """Preserve authored Mesh as a fake-user datablock, NOT a linked Object.

    Creating an Object while its source is being edited reproduced a native
    Blender 5.2.3 Undo access violation. Do not publish backup objects here.
    """
    backup=bpy.data.meshes.new('DWOS_BACKUP_'+sid[:8])
    try:
        copied=bm.copy()
        try:copied.to_mesh(backup)
        finally:copied.free()
        for material in host.data.materials:
            backup.materials.append(material)
        backup['DWOS_surface_backup_for']=host.name
        backup['DWOS_surface_backup_kind']='MESH_DATABLOCK_FAKE_USER_V43'
        backup['DWOS_surface_matrix_world']=[float(v) for row in host.matrix_world for v in row]
        backup.use_fake_user=True
        return backup.name
    except Exception:
        bpy.data.meshes.remove(backup)
        raise


def _backup_reference(session,host):
    """Support old Object backups and new fake-user Mesh backups."""
    name=str(session.get('backup_name',''))
    if session.get('backup_kind')=='MESH_DATABLOCK_FAKE_USER_V43':
        mesh=bpy.data.meshes.get(name)
        if mesh is None or mesh.get('DWOS_surface_backup_for')!=host.name:
            raise ValueError('BACKUP_MESH_NOT_FOUND_OR_PROVENANCE_MISMATCH')
        raw=mesh.get('DWOS_surface_matrix_world')
        if raw is None or len(raw)!=16:
            raise ValueError('BACKUP_MATRIX_MISSING')
        from mathutils import Matrix
        from types import SimpleNamespace
        mat=Matrix([[float(raw[i*4+j]) for j in range(4)] for i in range(4)])
        return SimpleNamespace(name=mesh.name,data=mesh,matrix_world=mat,mode='OBJECT',
                               type='MESH',get=mesh.get)
    obj=bpy.data.objects.get(name)
    if obj is None or obj.type!='MESH':
        raise ValueError('BACKUP_OBJECT_NOT_FOUND')
    return obj


def _start(context,props):
    host=context.active_object
    if not host or host.type!='MESH' or host.mode!='EDIT':
        raise ValueError('Шаг 1: выделите полигоны Mesh стены в Edit Mode')
    if host.get(SESSION_KEY):
        prior=json.loads(str(host[SESSION_KEY]))
        if (prior.get('id') in _UNDONE_STAGE1_IDS
                and int(prior.get('step',0))==1 and not prior.get('objects')
                and bpy.data.meshes.get(str(prior.get('backup_name',''))) is None):
            # Redo is intentionally discarded only when the user starts a NEW
            # operation. Do not rewrite Object properties from undo_post.
            _UNDONE_STAGE1_IDS.discard(prior['id'])
            del host[SESSION_KEY]
            if context.scene.get('DWOS_surface_current_wall')==host.name:
                context.scene['DWOS_surface_current_wall']=''
        else:
            raise ValueError('Сессия уже существует; не перезаписываю резервную копию. Используйте новую стену или восстановление')
    if host.data.shape_keys or host.data.users>1:
        raise ValueError('SHARED_MESH_OR_SHAPE_KEYS: опасно изменять исходную стену')
    m=host.matrix_world.to_3x3()
    scale=[m.col[i].length for i in range(3)]
    if any(abs(x-1.)>1e-6 for x in scale) or abs(abs(m.determinant())-1.)>1e-5:
        raise ValueError('NON_UNIT_SCALE: Apply Scale before laboratory for exact 1000px/m')
    if abs(context.scene.unit_settings.scale_length-1.)>1e-6:
        raise ValueError('UNIT_SCALE_NOT_ONE: laboratory expects Blender units = meters')
    bm=bmesh.from_edit_mesh(host.data)
    bm.verts.ensure_lookup_table();bm.edges.ensure_lookup_table();bm.faces.ensure_lookup_table()
    bm.verts.index_update();bm.faces.index_update();bm.normal_update()
    selection=[f for f in bm.faces if f.select]
    if not selection:raise ValueError('NO_SELECTED_POLYGONS')
    shape=_frame_of_boundary(bm,selection,host)
    shape['uv_atlas']=_source_uv_atlas(bm,selection,shape)
    sid=str(uuid.uuid4())
    fingerprint=_mesh_fingerprint(host)
    uv_fingerprint=_uv_fingerprint(host)
    name=_backup_mesh(host,bm,sid)
    # Preserve authored Host materials; publishing HP/LP assigns independent
    # material roles. A Host BMesh edit together with creating Backup Mesh in
    # Step 1 also triggered GUI Undo failure in Blender 5.2.3.
    s={'schema':SCHEMA,'engine_revision':ENGINE_REVISION,'id':sid,'created':_now(),'host':host.name,'step':1,'source':shape,
       'objects':{},'backup_name':name,'backup_kind':'MESH_DATABLOCK_FAKE_USER_V43',
        'fingerprint_before':fingerprint,'fingerprint_after':fingerprint,
       'uv_fingerprint_before':uv_fingerprint,'uv_fingerprint_after':uv_fingerprint,
       'parameters':_read_params(props),'warnings':['Step 4 removes selected front faces only; does not cut backfaces of a thick mesh.'],
       'history':[{'step':1,'status':'DONE','utc':_now()}]}
    _save_session(host,s);context.scene['DWOS_surface_current_wall']=host.name
    return host,s,{'selected_faces':len(selection),'boundary_vertices':len(shape['points']),
                   'source_uv_layers':shape['source_uv_layers'],
                   'source_uv_triangles':len(shape['uv_atlas']['triangles']),
                   'source_module_seams':len(shape['uv_atlas']['seams']),'backup':name}


def _read_params(p):
    return {'frame_width':p.frame_width,'frame_depth':p.frame_depth,'window_offset':p.window_offset,
            'interior_depth':p.interior_depth,'mullion_width':p.mullion_width,'mullion_depth':p.mullion_depth,
            'texture_resolution':p.texture_resolution,
            'classic_glass_midframe':bool(p.classic_glass_midframe)}


def _delete_source_faces(host,s):
    if host.mode!='EDIT':raise ValueError('Шаг 4: вернитесь в Edit Mode стены')
    if _mesh_fingerprint(host)!=s['fingerprint_before']:
        raise ValueError('SOURCE_TOPOLOGY_CHANGED: selection no longer matches saved wall; no faces removed')
    bm=bmesh.from_edit_mesh(host.data)
    bm.faces.ensure_lookup_table();bm.faces.index_update()
    ids=s['source']['face_ids']
    if any(i<0 or i>=len(bm.faces) for i in ids):raise ValueError('SOURCE_FACE_ID_INVALID')
    faces=[bm.faces[i] for i in ids]
    # FACES_ONLY is incorrect here: it leaves the interior edge of the two
    # removed source polygons as an unconnected wire. FACES removes selected
    # faces and *only* their edges/vertices that surviving faces do not use.
    affected_edges={e for f in faces for e in f.edges}
    before={'verts':len(bm.verts),'edges':len(bm.edges),'faces':len(bm.faces),
            'affected_edges':len(affected_edges)}
    shared_edges=sum(any(link not in faces for link in e.link_faces) for e in affected_edges)
    bmesh.ops.delete(bm,geom=faces,context='FACES')
    loose=[e for e in affected_edges if e.is_valid and not e.link_faces]
    if loose:
        # Blender versions may keep orphan edges under some BMesh contexts.
        bmesh.ops.delete(bm,geom=loose,context='EDGES')
    after={'verts':len(bm.verts),'edges':len(bm.edges),'faces':len(bm.faces)}
    after['kept_shared_edges_expected']=shared_edges
    after['removed_edges']=before['edges']-after['edges']
    after['removed_vertices']=before['verts']-after['verts']
    after['removed_faces']=before['faces']-after['faces']
    if after['removed_faces']!=len(faces):
        raise ValueError('HOST_CUT_FACE_COUNT_MISMATCH:'+str(after))
    bmesh.update_edit_mesh(host.data,loop_triangles=True,destructive=True)
    s['host_cut_audit']={'before':before,'after':after,
                         'loose_source_edges_after':sum(1 for e in affected_edges if e.is_valid and not e.link_faces)}
    return _mesh_fingerprint(host)


def _step(context,step):
    props=context.scene.dwos_surface_lab
    if step==1:return _start(context,props)
    host,s=_get_session_wall(context)
    if int(s.get('step',0))>=2:
        missing=[mode for mode in ('HP','LP')
                 if not bpy.data.objects.get(s.get('objects',{}).get(mode,''))]
        if missing:
            raise ValueError('PUBLISHED_LOD_MISSING_UNDO: '
                             +','.join(missing)
                             +'; восстановите стену из Backup или сохраните ZIP')
    if step!=s['step']+1:
        raise ValueError(f'STEP_ORDER: next={s["step"]+1} requested={step}')
    if step<=4 and _uv_fingerprint(host)!=s['uv_fingerprint_before']:
        raise ValueError('SOURCE_UV_CHANGED: refusing to cut authored modules')
    if step>4 and _uv_fingerprint(host)!=s['uv_fingerprint_after']:
        raise ValueError('HOST_UV_CHANGED_SINCE_CUT: refusing rebuild')
    if step<=4 and _mesh_fingerprint(host)!=s['fingerprint_before']:
        raise ValueError('HOST_CHANGED_SINCE_STEP_1')
    if step>4 and _mesh_fingerprint(host)!=s['fingerprint_after']:
        raise ValueError('HOST_CHANGED_SINCE_CUT: refusing rebuild')
    p=dict(s['parameters'])
    if step==2:p['frame_width']=props.frame_width
    if step==3:
        p['frame_depth']=props.frame_depth;p['window_offset']=props.window_offset
    if step==6:p['interior_depth']=props.interior_depth
    if step==7:
        p['interior_depth']=props.interior_depth
        p['mullion_width']=props.mullion_width
        p['mullion_depth']=props.mullion_depth
        p['zone_tree_next']=str(s.get('zone_tree_pending','') or '')
    if step==8:
        p['frame_width']=props.revised_frame_width
        p['mullion_width']=props.revised_mullion_width
    # Validate the generated geometry before modifying any accepted datablocks.
    if p['texture_resolution'] not in (2048,4096):
        raise ValueError('UV_RESOLUTION_MUST_BE_2048_OR_4096')
    # _publish runs geometry validation BEFORE allocating/replacing accepted meshes.
    # Do not generate every window twice on each button press.
    published,token=_publish(context,host,s,step,p)
    try:
        if step==4:
            s['fingerprint_after']=_delete_source_faces(host,s)
            s['uv_fingerprint_after']=_uv_fingerprint(host)
            published['host_cut_audit']=s.get('host_cut_audit',{})
    except Exception:
        _rollback_published(token)
        raise
    _complete_published(token)
    s['step']=step;s['parameters']=p;s['objects']=published['objects']
    s['history'].append({'step':step,'status':'DONE','utc':_now(),'summary':published})
    _save_session(host,s)
    return host,s,published


class DWOS_PG_surface_lab(bpy.types.PropertyGroup):
    frame_width: FloatProperty(name='Inset рамы, м',default=.08,min=.001,max=2.,precision=4,subtype='DISTANCE')
    frame_depth: FloatProperty(name='Глубина рамы, м',default=.12,min=.001,max=3.,precision=4,subtype='DISTANCE')
    window_offset: FloatProperty(name='Смещение окна от стены, м',default=0.,min=-5.,max=5.,precision=4,subtype='DISTANCE')
    interior_depth: FloatProperty(name='Глубина интерьера, м',default=1.2,min=.8,max=1.5,precision=4,subtype='DISTANCE')
    mullion_width: FloatProperty(name='Ширина импостов, м',default=.05,min=.001,max=1.,precision=4,subtype='DISTANCE')
    mullion_depth: FloatProperty(name='Глубина импостов, м',default=.08,min=.001,max=3.,precision=4,subtype='DISTANCE')
    revised_frame_width: FloatProperty(name='Новая ширина рамы, м',default=.10,min=.001,max=2.,precision=4,subtype='DISTANCE')
    revised_mullion_width: FloatProperty(name='Новая ширина импостов, м',default=.07,min=.001,max=1.,precision=4,subtype='DISTANCE')
    texture_resolution: IntProperty(name='Размер текстуры',default=4096,min=2048,max=4096)
    zone_split_thickness: FloatProperty(name='Толщина разделителя, м',default=.06,min=.001,max=1.,precision=4,subtype='DISTANCE')
    classic_glass_midframe: bpy.props.BoolProperty(name='Классическое стекло 1/2 глубины + отдельный интерьер [TEST]',default=False)


class DWOS_OT_surface_step(bpy.types.Operator):
    bl_idname='dwos.surface_step'
    bl_label='DWOS Surface: один шаг'
    bl_description='Выполнить один конкретный шаг на сохранённом выделении стены; логировать ввод/вывод'
    bl_options={'REGISTER','UNDO'}
    number: IntProperty(default=1,min=1,max=8)

    def execute(self,context):
        t=time.perf_counter();sid='UNKNOWN'
        try:
            if self.number not in STEPS:raise ValueError('UNKNOWN_STEP')
            wall,s,result=_step(context,self.number)
            sid=s['id']
            elapsed=round(time.perf_counter()-t,6)
            _write_event(sid,self.number,'DONE',wall=wall.name,elapsed_s=elapsed,
                         parameters=s['parameters'],result=result,
                         source=s['source'] if self.number in (1,3,4,7) else None)
            self.report({'INFO'},f'DWOS STEP {self.number}: DONE · {elapsed:.2f}с')
            return {'FINISHED'}
        except Exception as exc:
            try:
                if sid=='UNKNOWN':
                    try:_,s=_get_session_wall(context);sid=s['id']
                    except Exception:sid='PRESTART'
                classification=error_presentation.explain(exc)
                detail={'error':str(exc),'type':type(exc).__name__,
                        'classification':classification,
                        'traceback':traceback.format_exc(),'elapsed_s':time.perf_counter()-t,
                        'active_object':getattr(context.active_object,'name',None),
                        'mode':getattr(context,'mode',None)}
                _write_event(sid,self.number,'BLOCKED',**detail)
            except Exception:pass
            self.report({'ERROR'},f'DWOS STEP {self.number}: {error_presentation.user_message(exc)[:240]}')
            return {'CANCELLED'}


class DWOS_OT_surface_recover_missing_lods(bpy.types.Operator):
    """Explicit, evidence-gated recovery; NEVER runs inside undo handlers."""
    bl_idname='dwos.surface_recover_missing_lods'
    bl_label='DWOS NEXT · восстановить отсутствующие HP/LP'
    bl_description='Пересоздать только отсутствующие LOD из сохранённой сессии, без изменения исходной стены'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        created=[]
        try:
            host,s=_get_session_wall(context)
            if host.mode!='OBJECT':
                raise ValueError('RECOVER_NEEDS_OBJECT_MODE')
            if int(s.get('step',0)) not in (2,3):
                raise ValueError('RECOVER_ONLY_PRECUT_STAGES_2_3')
            if s.get('schema')!=SCHEMA:
                raise ValueError('RECOVER_LEGACY_SESSION_BLOCKED')
            if _mesh_fingerprint(host)!=s.get('fingerprint_before'):
                raise ValueError('RECOVER_SOURCE_TOPOLOGY_CHANGED')
            if _uv_fingerprint(host)!=s.get('uv_fingerprint_before'):
                raise ValueError('RECOVER_SOURCE_UV_CHANGED')
            original=_backup_reference(s,host)
            if (_mesh_fingerprint(original)!=s.get('fingerprint_before')
                    or _uv_fingerprint(original)!=s.get('uv_fingerprint_before')):
                raise ValueError('RECOVER_BACKUP_INTEGRITY_MISMATCH')
            expected=s.get('objects',{})
            if any(not isinstance(expected.get(mode),str) or not expected[mode]
                   for mode in ('HP','LP')):
                raise ValueError('RECOVER_INVALID_LOD_REFERENCE')
            missing=[]
            for mode in ('HP','LP'):
                obj=bpy.data.objects.get(expected[mode])
                if obj is None:
                    missing.append(mode)
                elif (obj.type!='MESH' or obj.get(OBJ_ID_KEY)!=s['id']
                      or obj.get(HOST_NAME_KEY)!=host.name
                      or obj.get('DWOS_surface_stage')!=s['step']):
                    raise ValueError('RECOVER_EXISTING_LOD_PROVENANCE_MISMATCH:'+mode)
            if not missing:
                raise ValueError('RECOVER_NOTHING_MISSING')
            hp,lp,_report=_geometry(s,int(s['step']),dict(s['parameters']))
            for mode,buf in (('HP',hp),('LP',lp)):
                if mode not in missing:continue
                mesh=_construct_mesh('DWOS_RECOVER_'+mode+'_'+s['id'][:8],buf)
                obj=None
                try:
                    obj=bpy.data.objects.new(expected[mode],mesh)
                    if obj.name!=expected[mode]:
                        raise ValueError('RECOVER_OBJECT_NAME_CONFLICT')
                    _collection('ISSMAKER_WINDOWS_'+mode).objects.link(obj)
                    obj.matrix_world=host.matrix_world.copy()
                    obj[OBJ_ID_KEY]=s['id']
                    obj[HOST_NAME_KEY]=host.name
                    obj['DWOS_surface_stage']=s['step']
                    obj.hide_set(mode=='LP')
                    created.append((obj,mesh))
                except Exception:
                    if obj is not None:bpy.data.objects.remove(obj,do_unlink=True)
                    if mesh.users==0:bpy.data.meshes.remove(mesh)
                    raise
            if _mesh_fingerprint(host)!=s['fingerprint_before']:
                raise ValueError('RECOVER_HOST_CHANGED_DURING_REBUILD')
            self.report({'INFO'},'DWOS: восстановлены '+', '.join(missing)+'; исходная стена не изменена')
            _write_event(s['id'],int(s['step']),'RECOVER_MISSING_LODS',
                         recovered=missing,source_verified=True,uv_verified=True)
            return {'FINISHED'}
        except Exception as ex:
            for obj,mesh in reversed(created):
                if obj.name in bpy.data.objects:
                    bpy.data.objects.remove(obj,do_unlink=True)
                if mesh.users==0:bpy.data.meshes.remove(mesh)
            try:_write_event('RECOVERY',0,'BLOCKED',error=repr(ex),
                             traceback=traceback.format_exc()[-4000:])
            except Exception:pass
            self.report({'ERROR'},error_presentation.user_message(ex))
            return {'CANCELLED'}


class DWOS_OT_surface_view(bpy.types.Operator):
    bl_idname='dwos.surface_view'
    bl_label='DWOS Surface Lab — просмотр HP/LP'
    bl_description='Переключить отображение без изменения сеток; BOTH может давать z-fighting'
    bl_options={'REGISTER'}
    mode: bpy.props.EnumProperty(items=[('HP','HP','High-poly'),('LP','LP','Low-poly'),
                                        ('BOTH','BOTH','Оба'),('NONE','NONE','Скрыть')],default='HP')

    def execute(self,context):
        try:
            host,s=_get_session_wall(context)
            targets={k:bpy.data.objects.get(name) for k,name in s.get('objects',{}).items()}
            if not targets or not all(targets.get(k) for k in ('HP','LP')):
                raise ValueError('LP/HP ещё не созданы: выполните шаг 2')
            for role,obj in targets.items():
                if obj.get(OBJ_ID_KEY)!=s['id']:
                    raise ValueError('LODs do not match active window UUID')
                obj.hide_set((self.mode not in ('HP','BOTH')) if role in ('INTERIOR','GLASS') else self.mode not in (role,'BOTH'))
            from . import surface_corner_union
            surface_corner_union.sync_lod_group(host,self.mode)
            _write_event(s['id'],0,'VIEW_CHANGE',mode=self.mode)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}


class DWOS_OT_surface_cleanup_legacy(bpy.types.Operator):
    """Explicit repair of wires left by W27's FACES_ONLY, from old sessions.

    Matches original selected face edges by BACKUP vertex coordinates. This
    keeps the outer hole boundary attached to other faces and leaves unrelated
    authored loose edge paths alone. Existing sessions must not be silently
    modified during addon registration or normal testing.
    """
    bl_idname='dwos.surface_cleanup_legacy'
    bl_label='Очистить рёбра старого шага 4'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        try:
            host,s=_get_session_wall(context,allow_legacy=True)
            if s['step']<4:raise ValueError('ОЧИСТКА: нужен уже выполненный шаг 4')
            if s.get('engine_revision')==ENGINE_REVISION:
                raise ValueError('ОЧИСТКА: новая сессия уже использует FACES')
            if host.mode!='EDIT':raise ValueError('ОЧИСТКА: Edit Mode исходной стены')
            if _mesh_fingerprint(host)!=s['fingerprint_after']:
                raise ValueError('ОЧИСТКА: стена изменена, не трогаю авторскую геометрию')
            backup=_backup_reference(s,host)
            selected_ids=s['source']['face_ids']
            source=backup.data
            if any(i<0 or i>=len(source.polygons) for i in selected_ids):
                raise ValueError('ОЧИСТКА: исходные индексы повреждены')
            def vertex_key(v):return tuple(round(float(x),8) for x in v)
            def edge_key(a,b):return tuple(sorted((vertex_key(a),vertex_key(b))))
            source_edges=set()
            for fi in selected_ids:
                seq=list(source.polygons[fi].vertices)
                for i,j in zip(seq,seq[1:]+seq[:1]):
                    source_edges.add(edge_key(source.vertices[i].co,source.vertices[j].co))
            bm=bmesh.from_edit_mesh(host.data)
            loose=[e for e in bm.edges if e.is_valid and not e.link_faces and
                   edge_key(e.verts[0].co,e.verts[1].co) in source_edges]
            count=len(loose)
            if count:bmesh.ops.delete(bm,geom=loose,context='EDGES')
            bmesh.update_edit_mesh(host.data,loop_triangles=True,destructive=bool(count))
            s['fingerprint_after']=_mesh_fingerprint(host)
            s['legacy_edge_cleanup']={'removed_loose_source_edges':count,'utc':_now()}
            _save_session(host,s)
            _write_event(s['id'],4,'LEGACY_WIRE_CLEANUP',removed_edges=count)
            self.report({'INFO'},f'DWOS: удалено оставшихся рёбер: {count}')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},f'DWOS legacy cleanup: {str(exc)[:160]}')
            return {'CANCELLED'}


class DWOS_OT_surface_clone_legacy(bpy.types.Operator):
    """Start an LP-FIRST trial on an isolated snapshot of an earlier lab session.

    Unlike Restore this operator NEVER changes the original host or old HP/LP.
    The preserved backup is read-only input and the clone gets its own mesh.
    """
    bl_idname='dwos.surface_clone_legacy'
    bl_label='Новый LP-FIRST тест из Backup'
    bl_description='Создать отдельную тестовую стену из сохранённого исходного Mesh и выбрать прежние полигоны; старую сцену не менять'
    bl_options={'REGISTER','UNDO'}

    def execute(self,context):
        clone=None
        old_active=context.view_layer.objects.active
        old_selected=[obj for obj in context.selected_objects]
        old_current=str(context.scene.get('DWOS_surface_current_wall',''))
        old_mode=getattr(old_active,'mode','OBJECT') if old_active is not None else 'OBJECT'
        old_mesh_select_mode=tuple(context.tool_settings.mesh_select_mode)
        clone_collection_name='ISSMAKER_DWOS_LP_FIRST_TEST_COPIES'
        had_clone_collection=bpy.data.collections.get(clone_collection_name) is not None
        changed_selection=False
        try:
            host,s=_get_session_wall(context,allow_legacy=True)
            if s.get('schema')==SCHEMA:
                raise ValueError('CURRENT_SESSION: продолжайте следующий шаг либо используйте другую стену')
            backup=_backup_reference(s,host)
            if backup.get('DWOS_surface_backup_for')!=host.name:
                raise ValueError('LEGACY_BACKUP_PROVENANCE_MISMATCH')
            if backup.data.shape_keys:
                raise ValueError('LEGACY_BACKUP_SHAPE_KEYS_UNSUPPORTED')
            expected=s.get('fingerprint_before')
            if not isinstance(expected,str) or len(expected)!=64 or _mesh_fingerprint(backup)!=expected:
                raise ValueError('LEGACY_BACKUP_FINGERPRINT_MISMATCH: backup повреждён или изменён')
            ids=s.get('source',{}).get('face_ids',[])
            if not isinstance(ids,list) or not ids or any(type(i)!=int or i<0 or i>=len(backup.data.polygons) for i in ids):
                raise ValueError('LEGACY_SOURCE_SELECTION_INVALID')
            if not backup.data.uv_layers:
                raise ValueError('LEGACY_BACKUP_NO_UV: исходные модули не имеют UV')
            # Validate orientation and unit scale up front; the step-1 gate must
            # agree with the view-copy preflight, without changing the source.
            m=backup.matrix_world.to_3x3()
            if any(abs(m.col[i].length-1.)>1e-6 for i in range(3)) or abs(abs(m.determinant())-1.)>1e-5:
                raise ValueError('LEGACY_BACKUP_NON_UNIT_SCALE')
            if abs(context.scene.unit_settings.scale_length-1.)>1e-6:
                raise ValueError('LEGACY_UNIT_SCALE_NOT_ONE')
            # Source face indices, materials, per-loop UV and edge selection are
            # preserved by an independent Mesh datablock copy.
            new_mesh=backup.data.copy()
            try:
                new_mesh.name=f'DWOS_LP_FIRST_SOURCE_{s["id"][:8]}'
                clone=bpy.data.objects.new(f'DWOS_LP_FIRST_TEST_{s["id"][:8]}',new_mesh)
                _collection(clone_collection_name,context.scene).objects.link(clone)
            except Exception:
                if clone is not None:bpy.data.objects.remove(clone,do_unlink=True)
                if new_mesh.users==0:bpy.data.meshes.remove(new_mesh)
                clone=None
                raise
            clone.matrix_world=backup.matrix_world.copy()
            # Move ONLY the clone beside the original so a second facade does
            # not hide the old window in coincident geometry / z-fighting.
            world_x=[(clone.matrix_world @ Vector(c)).x for c in clone.bound_box]
            if not world_x or not all(math.isfinite(v) for v in world_x):
                raise ValueError('LEGACY_COPY_INVALID_WORLD_BOUNDS')
            clone.location.x+=max(world_x)-min(world_x)+2.0
            clone['DWOS_surface_clone_of']=host.name
            clone['DWOS_surface_clone_session']=s['id']
            clone['DWOS_surface_clone_source_backup']=backup.name
            # No prior session pointer, LOD UUID or legacy stage may escape into
            # the new experimental source object.
            for key in (SESSION_KEY,HOST_NAME_KEY,OBJ_ID_KEY,'DWOS_surface_stage'):
                if key in clone:del clone[key]
            selected_ids=set(ids)
            selected_vert_ids=set()
            for poly in clone.data.polygons:
                poly.select=poly.index in selected_ids
                if poly.select:selected_vert_ids.update(poly.vertices)
            for v in clone.data.vertices:v.select=v.index in selected_vert_ids
            # Face mode guarantees the recovered selection is usable at step 1.
            if context.mode!='OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for obj in context.selected_objects:obj.select_set(False)
            changed_selection=True
            clone.select_set(True)
            context.view_layer.objects.active=clone
            context.tool_settings.mesh_select_mode=(False,False,True)
            context.scene['DWOS_surface_current_wall']=''
            bpy.ops.object.mode_set(mode='EDIT')
            # Blender N-panel is in a VIEW_3D context; framing is best-effort
            # and must never roll back an otherwise usable copy.
            try:
                if context.area and context.area.type=='VIEW_3D':
                    bpy.ops.view3d.view_selected(use_all_regions=False)
            except Exception:pass
            _write_event(s['id'],0,'LEGACY_CLONED_FOR_NEW_TEST',
                         old_host=host.name,clone=clone.name,
                         new_selected_face_count=len(selected_ids),
                         original_backup_sha256=expected,
                         original_wall_unchanged=True)
            self.report({'INFO'},'LP-FIRST: создана отдельная копия. Исходные грани выделены — нажмите шаг 1.')
            return {'FINISHED'}
        except Exception as exc:
            # Roll back ALL observable state, including the previous Edit mode
            # and mesh-select mode.  A mock-only check previously missed this.
            failure_trace=traceback.format_exc()
            rollback_errors=[]
            if clone is not None:
                try:
                    if clone.mode=='EDIT':bpy.ops.object.mode_set(mode='OBJECT')
                    mesh=clone.data
                    bpy.data.objects.remove(clone,do_unlink=True)
                    if mesh and mesh.users==0:bpy.data.meshes.remove(mesh)
                except Exception as rollback_exc:
                    rollback_errors.append('CLONE_REMOVE: '+repr(rollback_exc))
            if not had_clone_collection:
                try:
                    coll=bpy.data.collections.get(clone_collection_name)
                    if coll is not None and not len(coll.objects):
                        bpy.data.collections.remove(coll,do_unlink=True)
                except Exception as rollback_exc:
                    rollback_errors.append('COLLECTION_REMOVE: '+repr(rollback_exc))
            if changed_selection:
                try:
                    for obj in context.selected_objects:obj.select_set(False)
                    for obj in old_selected:obj.select_set(True)
                    if old_active is not None:context.view_layer.objects.active=old_active
                except Exception as rollback_exc:
                    rollback_errors.append('SELECTION_RESTORE: '+repr(rollback_exc))
            try:
                context.scene['DWOS_surface_current_wall']=old_current
                context.tool_settings.mesh_select_mode=old_mesh_select_mode
                if (old_active is not None and old_mode=='EDIT'
                        and context.view_layer.objects.active==old_active
                        and old_active.mode!='EDIT'):
                    bpy.ops.object.mode_set(mode='EDIT')
            except Exception as rollback_exc:
                rollback_errors.append('MODE_OR_STATE_RESTORE: '+repr(rollback_exc))
            try:
                _write_event('PRESTART',0,'LEGACY_CLONE_BLOCKED',error=str(exc),
                             traceback=failure_trace,rollback_errors=rollback_errors)
            except Exception:pass
            if rollback_errors:
                self.report({'ERROR'},'DWOS LP-FIRST rollback incomplete: '+', '.join(rollback_errors)[:140])
            else:
                self.report({'ERROR'},'DWOS LP-FIRST copy: '+str(exc)[:160])
            return {'CANCELLED'}


class DWOS_OT_surface_export(bpy.types.Operator):
    bl_idname='dwos.surface_export'
    bl_label='Экспорт полного Surface Lab ZIP'
    bl_description='Снимок исходной/полученной геометрии, UV, параметров и полного журнала по шагам'
    bl_options={'REGISTER'}
    def execute(self,context):
        try:
            try:
                host,s=_get_session_wall(context,allow_legacy=True)
                folder=_session_dir(s['id'])
                session=s
            except ValueError:
                # Failed initial selections must also be exportable.
                host=context.active_object if context.active_object and context.active_object.type=='MESH' else None
                folder=_session_dir('PRESTART')
                session={'schema':SCHEMA,'step':0,'id':'PRESTART',
                         'status':'BLOCKED_BEFORE_INITIALIZATION'}
            scene={'utc':_now(),'schema':SCHEMA,'engine_revision':ENGINE_REVISION,'blender':bpy.app.version_string,
                   'addon_module':__file__,'system':platform.platform(),
                   'stage':session['step'],'session':session,'scene':context.scene.name,
                   'host_fingerprint':_mesh_fingerprint(host) if host is not None else None,
                   'host_uv_fingerprint':_uv_fingerprint(host) if host is not None else None,
                   'source_module_count':len(set(t['face_id'] for t in session.get('source',{}).get('uv_atlas',{}).get('triangles',[]))),
                   'objects':{}}
            targets=[]
            if host is not None:targets.append(('HOST',host.name))
            if session['step']:
                targets.extend([('BACKUP',session['backup_name']),
                                ('HP',session['objects'].get('HP')),
                                ('LP',session['objects'].get('LP'))])
            for label,name in targets:
                if label=='BACKUP':
                    try:obj=_backup_reference(session,host)
                    except ValueError as exc:
                        scene['objects']['BACKUP_ERROR']=str(exc)
                        obj=None
                else:
                    obj=bpy.data.objects.get(name) if name else None
                if obj is not None and obj.type=='MESH':
                    scene['objects'][label]=_snapshot_live_mesh(obj)
            (folder/'snapshot.json').write_text(json.dumps(scene,ensure_ascii=False,indent=2),encoding='utf8')
            (folder/'README.txt').write_text('DWOS SURFACE TEST evidence. Contains host geometry, object names and local file paths. Review before sharing.\n',encoding='utf8')
            out=Path.home()/'Downloads'/'ISSMAKER_Diagnostics'
            out.mkdir(parents=True,exist_ok=True)
            path=out/f'DWOS_SURFACE_{session["id"][:8]}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
            manifest=[]
            with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
                if folder.name != 'PRESTART':
                    prestart=_session_dir('PRESTART')/'events.jsonl'
                    if prestart.exists():z.write(prestart,'bootstrap_events.jsonl')
                for f in sorted(folder.glob('*')):
                    if not f.is_file():continue
                    dat=f.read_bytes();digest=hashlib.sha256(dat).hexdigest()
                    z.writestr(f.name,dat);manifest.append({'file':f.name,'sha256':digest,'bytes':len(dat)})
                z.writestr('manifest.json',json.dumps(manifest,indent=2))
            try:context.window_manager.clipboard=str(path)
            except Exception:pass
            self.report({'INFO'},'DWOS ZIP: '+str(path)[:180])
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},f'DWOS Surface ZIP: {exc}')
            return {'CANCELLED'}


class DWOS_OT_surface_restore(bpy.types.Operator):
    bl_idname='dwos.surface_restore'
    bl_label='Восстановить исходную стену Surface Lab'
    bl_description='ОПАСНО: полностью заменяет Mesh стены сохранённой резервной копией после сверки ожидаемого хеша'
    bl_options={'REGISTER','UNDO'}
    def invoke(self,context,event):return context.window_manager.invoke_confirm(self,event)
    def execute(self,context):
        try:
            host,s=_get_session_wall(context,allow_legacy=True)
            if host.mode=='EDIT':raise ValueError('Перейдите в Object Mode для восстановления')
            allowed={s['fingerprint_before'],s['fingerprint_after']}
            if _mesh_fingerprint(host) not in allowed:
                raise ValueError('HOST_CHANGED: не удаляю новые авторские изменения')
            backup=_backup_reference(s,host)
            prior=host.data;host.data=backup.data.copy()
            host.data.use_fake_user=False
            if prior.users==0:bpy.data.meshes.remove(prior)
            # Restore is an explicit destructive cleanup of this lab's objects.
            for name in s.get('objects',{}).values():
                child=bpy.data.objects.get(name)
                if child is not None and child.get(OBJ_ID_KEY)==s['id']:
                    bpy.data.objects.remove(child,do_unlink=True)
            del host[SESSION_KEY]
            if s.get('backup_kind')=='MESH_DATABLOCK_FAKE_USER_V43':
                source_mesh=backup.data
                source_mesh.use_fake_user=False
                if source_mesh.users==0:bpy.data.meshes.remove(source_mesh)
            context.scene['DWOS_surface_current_wall']=''
            _write_event(s['id'],0,'RESTORED',host=host.name)
            self.report({'INFO'},'Исходная стена восстановлена из Mesh backup')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}


# Blender's RNA expects a live PropertyDeferred, not a postponed string
# annotation (PEP563). Bind the new view enum explicitly before registration.
DWOS_OT_surface_view.__annotations__['mode']=bpy.props.EnumProperty(
    items=[('HP','HP','High-poly'),('LP','LP','Low-poly'),
           ('BOTH','BOTH','Оба (совпадающие плоскости)'),('NONE','NONE','Скрыть')],
    default='HP')

CLASSES=(DWOS_PG_surface_lab,DWOS_OT_surface_step,DWOS_OT_surface_recover_missing_lods,DWOS_OT_surface_view,DWOS_OT_surface_cleanup_legacy,DWOS_OT_surface_clone_legacy,DWOS_OT_surface_export,DWOS_OT_surface_restore)


_UNDONE_STAGE1_IDS=set()


@persistent
def _on_surface_undo_post(_dummy):
    """Record cancelled Step 1, but NEVER modify the redo stack."""
    for host in tuple(bpy.data.objects):
        raw=host.get(SESSION_KEY)
        if not raw or host.type!='MESH':continue
        try:s=json.loads(str(raw))
        except (ValueError,TypeError):continue
        if (s.get('backup_kind')=='MESH_DATABLOCK_FAKE_USER_V43'
                and int(s.get('step',0))==1 and not s.get('objects')
                and bpy.data.meshes.get(str(s.get('backup_name',''))) is None):
            _UNDONE_STAGE1_IDS.add(s['id'])


@persistent
def _on_surface_redo_post(_dummy):
    """Restore original immutable backup after Blender restores edit state.

    Undo removes a fake-user Mesh without restoring Object IDProperties.
    Recreating only the backup Mesh AFTER a matching Redo does not overwrite
    any authored geometry. Never reconstruct from a changed source.
    """
    for host in tuple(bpy.data.objects):
        raw=host.get(SESSION_KEY)
        if not raw or host.type!='MESH':continue
        try:s=json.loads(str(raw))
        except (ValueError,TypeError):continue
        sid=s.get('id')
        if sid not in _UNDONE_STAGE1_IDS:continue
        if int(s.get('step',0))!=1 or s.get('objects'):continue
        if bpy.data.meshes.get(str(s.get('backup_name',''))) is not None:
            _UNDONE_STAGE1_IDS.discard(sid)
            continue
        if _mesh_fingerprint(host)!=s.get('fingerprint_before'):
            continue
        if _uv_fingerprint(host)!=s.get('uv_fingerprint_before'):
            continue
        temporary=None
        try:
            if host.mode=='EDIT':
                bm=bmesh.from_edit_mesh(host.data)
                own=False
            else:
                temporary=bmesh.new()
                temporary.from_mesh(host.data)
                bm=temporary;own=True
            bm.faces.ensure_lookup_table()
            bm.faces.index_update()
            source_ids=set(s['source']['face_ids'])
            for face in bm.faces:
                face.select_set(face.index in source_ids)
            rebuilt=_backup_mesh(host,bm,sid)
            if rebuilt!=s.get('backup_name'):
                mesh=bpy.data.meshes.get(rebuilt)
                if mesh is not None:bpy.data.meshes.remove(mesh)
                continue
            _UNDONE_STAGE1_IDS.discard(sid)
        except Exception:
            # No silent reuse of an incorrect backup. Failure remains diagnosable.
            pass
        finally:
            if temporary is not None:temporary.free()


def register():
    bpy.types.Scene.dwos_surface_lab=PointerProperty(type=DWOS_PG_surface_lab)
    if _on_surface_undo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_on_surface_undo_post)
    if _on_surface_redo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_on_surface_redo_post)


def unregister():
    for collection,callback in ((bpy.app.handlers.undo_post,_on_surface_undo_post),
                                (bpy.app.handlers.redo_post,_on_surface_redo_post)):
        if callback in collection:collection.remove(callback)
    _UNDONE_STAGE1_IDS.clear()
    if hasattr(bpy.types.Scene,'dwos_surface_lab'):del bpy.types.Scene.dwos_surface_lab


def draw_diagnostic(layout,context):
    box=layout.box();box.label(text='DWOS · HP/LP · ARCHITECTURAL W55',icon='MESH_GRID')
    s=context.scene.dwos_surface_lab
    active=context.active_object
    try:
        # A newly selected mesh without any lab session is a valid new
        # start, even while a legacy session lives on a different wall.
        if active is not None and active.type=='MESH' and not active.get(SESSION_KEY) and not active.get(HOST_NAME_KEY):
            state={'step':0,'schema':SCHEMA}
            box.label(text='Шаг 1: Edit Mode + выделение граней',icon='INFO')
        else:
            wall,state=_get_session_wall(context,allow_legacy=True)
            if state.get('schema')==SCHEMA:
                box.label(text=f'{wall.name[:25]} · DONE {state["step"]}/8',icon='CHECKMARK')
            else:
                box.label(text=f'LEGACY SESSION · DONE {state.get("step","?")}/8',icon='ERROR')
                box.label(text='Старая сессия сохранена; шаги не совместимы.',icon='INFO')
                box.operator('dwos.surface_clone_legacy',text='НОВЫЙ LP-FIRST ТЕСТ ИЗ BACKUP',icon='DUPLICATE')
                box.label(text='Копия отдельно; оригинал и HP/LP не меняются.')
    except Exception:
        state={'step':0,'schema':SCHEMA}
        box.label(text='Шаг 1: Edit Mode + выделение граней',icon='INFO')
    captions=('1 · Материал рамы','2 · Inset безопасный','3 · Глубина профиля',
              '4 · Удалить заполнение','5 · Стекло OPT','6 · Интерьер / откосы',
              '7 · Импосты из дерева зон','8 · Изменить ширину')
    params={2:('frame_width',),3:('frame_depth','window_offset'),6:('interior_depth',),
            7:('mullion_width','mullion_depth'),8:('revised_frame_width','revised_mullion_width')}
    for i,name in enumerate(captions,1):
        for setting in params.get(i,()):box.prop(s,setting)
        row=box.row(align=True)
        row.enabled=(state.get('schema',SCHEMA)==SCHEMA) and (i==state['step']+1) and (i!=1 or context.mode in {'EDIT','EDIT_MESH'})
        op=row.operator('dwos.surface_step',text=name,icon='CHECKMARK' if i<=state['step'] else 'PLAY')
        op.number=i
    if state.get('step',0)>=2 and state.get('schema')==SCHEMA:
        row=box.row(align=True)
        for mode in ('HP','LP','BOTH','NONE'):
            op=row.operator('dwos.surface_view',text=mode)
            op.mode=mode
    extra=box.box()
    extra.label(text='W55 · Interior: 0.8–1.5 m',icon='MOD_SOLIDIFY')
    extra.label(text='Start: 1/2 frame width, 1/4 frame depth')
    extra.prop(s,'interior_depth',text='Interior, m')
    if state.get('step',0)==6:
        row=extra.row(align=True)
        for ident,label in (('V','1V'),('H','1H'),('CROSS','Cross')):
            op=row.operator('dwos.surface_mullion_preset',text=label)
            op.preset=ident
        row=extra.row(align=True)
        for ident,label in (('TWO_H','2H'),('TWO_V','2V'),('GRID6','2×3')):
            op=row.operator('dwos.surface_mullion_preset',text=label)
            op.preset=ident
    extra.operator('dwos.surface_detect_corner_neighbors',text='Detect corner windows (Path)',icon='OUTLINER_OB_CURVE')
    extra.operator('dwos.surface_resolve_corner_interiors',text='Merge interior collisions 45/90',icon='MOD_BOOLEAN')
    extra.operator('dwos.surface_reset_corner_joins',text='Reset shared Interior groups',icon='LOOP_BACK')
    extra.operator('dwos.surface_rebuild_closed_interior',text='W56: Close rear + no UV',icon='MOD_SOLIDIFY')
    box.label(text='LP -> GLASS ZONES -> Brick Hole HP')
    box.label(text='UV: по исходным модулям стены · 1000px/m')
    if (state['step']>=4 and state.get('engine_revision')!=ENGINE_REVISION
            and not state.get('legacy_edge_cleanup')):
        box.operator('dwos.surface_cleanup_legacy',text='4b · Очистить остаточные рёбра',icon='X')
    box.operator('dwos.surface_export',text='ПОЛНЫЙ ЛОГ + ГЕОМЕТРИЯ ZIP',icon='PACKAGE')
    box.operator('dwos.surface_restore',text='Восстановить стену (с подтверждением)',icon='LOOP_BACK')
    box.label(text='Ортогональные плоские пути: поддержка TEST',icon='INFO')
    box.label(text='Пространственные CORNER / CURVED: отдельное ядро',icon='INFO')
