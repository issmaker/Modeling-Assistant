"""Blender adapter for staged, connected DWOS Interior rebuilding.

All exact math is in smart_interior_core; this layer handles snapshots, matrices,
publication and rollback. FRAME/GLASS meshes are never modified here.
"""
from __future__ import annotations
import json
import math
import bpy
from bpy.types import Operator
from mathutils import Vector, Matrix
from .smart_interior_core import (collision_graph,contact_graph,solve_group,connected_components,
    POLICY as SMART_POLICY, UnresolvedInteriorContact)
from .scene_spatial_safety import _mesh_world
from .spatial_safety_core import self_intersections
from .dwos_log import event as log_event,exception as log_exception

CANONICAL_KEY='DWOS_interior_canonical_mesh_v370'
PENDING_POLICY='SMART_INTERIOR_PENDING_CONTACT_V372'
LINKS_KEY='DWOS_smart_interior_contact_links_v372'


def _mesh_data(obj):
    raw_roles=json.loads(obj.get('DWOS_face_roles','[]') or '[]')
    faces=[tuple(poly.vertices) for poly in obj.data.polygons]
    roles=raw_roles if len(raw_roles)==len(faces) else ['INTERIOR_REVEAL']*len(faces)
    return {'verts':[tuple(v.co) for v in obj.data.vertices],'faces':faces,'roles':roles}


def encode(mesh):return json.dumps(mesh,separators=(',',':'))


def decode(obj):
    raw=obj.get(CANONICAL_KEY)
    if raw:
        try:
            v=json.loads(raw)
            if all(k in v for k in ('verts','faces','roles')) and len(v['faces'])==len(v['roles']):
                return v
        except (TypeError,ValueError,KeyError):
            raise ValueError(f'Interior canonical state corrupted: {obj.name}')
    return _mesh_data(obj)  # forward migration from v3.6.x pristine meshes


def _world(mesh,matrix):
    return {'verts':[tuple(matrix@Vector(p)) for p in mesh['verts']],
            'faces':list(mesh['faces']),'roles':list(mesh['roles'])}


def _local(mesh,matrix):
    # inverted_safe() silently substitutes a different transform for singular
    # matrices; publishing that result permanently corrupts the cavity.
    try:
        if (not all(math.isfinite(float(x)) for row in matrix for x in row) or
                not math.isfinite(float(matrix.determinant())) or
                abs(float(matrix.determinant())) <= 1e-12):
            raise ValueError('Interior world matrix is singular or non-finite')
        inv = matrix.inverted()
    except (ArithmeticError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError('Smart Interior: cannot safely invert the Interior transform') from exc
    return {'verts':[tuple(inv@Vector(p)) for p in mesh['verts']],
            'faces':list(mesh['faces']),'roles':list(mesh['roles'])}


def _equal(a,b):
    if len(a['verts'])!=len(b['verts']) or a['faces']!=b['faces'] or a['roles']!=b['roles']:return False
    return all(all(abs(float(x)-float(y))<=1e-7 for x,y in zip(p,q)) for p,q in zip(a['verts'],b['verts']))


def _contact_links(obj):
    """Persistent previous adjacency, needed to restore clipped peers after separation."""
    if obj is None:
        return ()
    raw=obj.get(LINKS_KEY,'[]')
    try:
        values=json.loads(raw)
    except (ValueError,TypeError) as exc:
        raise ValueError(f'Smart Interior linked state corrupted: {obj.name}') from exc
    if not isinstance(values,list) or not all(isinstance(v,str) for v in values):
        raise ValueError(f'Smart Interior linked state corrupted: {obj.name}')
    return tuple(sorted(set(values)))


def _affected_components(active_id,canonical,neighbors,pairs,frame_obj):
    """Find only live and previously connected interiors that active edit may affect.

    Unrelated scene collisions are discovered (for diagnostic context) but NEVER
    solved during another window's creation. Previous links are essential to
    restore canonical geometry when an old contact has disappeared.
    """
    prior={name:set(_contact_links(obj)) for name,obj in neighbors.items()}
    old_interior=next((o for o in frame_obj.children if o.get('DWOS_role')=='INTERIOR'),None)
    prior[active_id]=set(_contact_links(old_interior))
    adjacent={name:set() for name in canonical}
    for a,b in pairs:
        adjacent[a].add(b);adjacent[b].add(a)
    # Include reciprocal historical links as well as current contact edges.
    for a,links in prior.items():
        for b in links:
            if a in adjacent and b in adjacent:
                adjacent[a].add(b);adjacent[b].add(a)
    relevant=set();to_visit=[active_id]
    while to_visit:
        name=to_visit.pop()
        if name in relevant:continue
        relevant.add(name)
        to_visit.extend(adjacent.get(name,set())-relevant)
    return relevant


def stage(frame_obj,raw_mesh,matrix,scene,*,force=False,allow_deferred=False,active_class=None,mode_override=None):
    """Stage only the active contact neighborhood, never unrelated scene groups.

    Returns (active mesh, peer updates, metadata) without mutating Blender meshes.
    Known geometrically unresolvable *active* contacts may be deferred on creation;
    malformed input, unexpected solver failures and unsupported modes are errors.
    """
    # W58.9: do not adjust the hidden provenance shells belonging to a
    # shared MERGE, even if another window is rebuilt next to them.
    if any(child.get("DWOS_W58_9_active_group")
           for child in frame_obj.children if child.get("DWOS_role")=="INTERIOR"):
        raise ValueError("DWOS_MERGED_GROUP_LOCKED_BEFORE_SMART_STAGE")
    state=scene.dwos
    if not bool(getattr(state,'smart_interior_enabled',False)) and not force:
        from .scene_spatial_safety import validate_staged_interior
        validate_staged_interior(frame_obj,raw_mesh['verts'],raw_mesh['faces'],scene)
        return raw_mesh,[],{'policy':'OFF','pairs':[],'active_links':[]}
    has_active=bool(raw_mesh.get('faces'))
    if has_active:
        verts=raw_mesh.get('verts',());faces=raw_mesh.get('faces',());roles=raw_mesh.get('roles',())
        if len(faces)!=len(roles) or not verts or any(not math.isfinite(float(c)) for p in verts for c in p):
            raise ValueError('Smart Interior: invalid canonical source mesh/roles')
        for face in faces:
            if len(face)<3 or len(set(face))!=len(face) or any(not isinstance(i,int) or i<0 or i>=len(verts) for i in face):
                raise ValueError('Smart Interior: invalid canonical source polygon indices')
    if has_active and self_intersections(raw_mesh['verts'],raw_mesh['faces']):
        raise ValueError('Smart Interior: source canonical cavity contains self-intersections')
    # W58.10 / ASK FIRST. Never stage peer edits while creating/rebuilding a
    # window. Preserve original generated Interior (including its overlap)
    # and let the viewport conflict navigator select a resolution explicitly.
    # The self-intersection and source-mesh validation above still applies.
    # Explicit "Rebuild Smart Interiors" and manual repair operators pass
    # force=True; they can invoke the old geometry strategy intentionally.
    if (not force and bool(getattr(state,"smart_interior_enabled",False)) and
        str(getattr(state,"smart_interior_conflict_behavior","ASK_FIRST"))
            =="ASK_FIRST"):
        return raw_mesh,[],{"policy":"ASK_FIRST_W58_10",
            "pairs":[],"neighbor_updates":0,"active_links":[],
            "deferred_to_user":True}

    canonical=({frame_obj.name:_world(raw_mesh,matrix)} if has_active else {})
    classes={frame_obj.name:str(active_class or frame_obj.get('DWOS_adaptive_classification','PLANAR_LEGACY'))}
    neighbors={};originals={};frozen=[]
    for other in scene.objects:
        if (other.get('DWOS_role')!='INTERIOR' or other.type!='MESH'
                or other.parent is None or other.parent is frame_obj
                or other.get('DWOS_W58_9_active_group')):
            continue
        if getattr(other.parent.dwos_window,'finalized',False):
            frozen.append(other)
            continue
        owner=other.parent.name
        if owner in canonical:raise ValueError('Duplicate Interior controller ID in scene')
        baseline=decode(other)
        canonical[owner]=_world(baseline,other.matrix_world)
        originals[owner]=baseline
        neighbors[owner]=other
        classes[owner]=str(other.parent.get('DWOS_adaptive_classification','PLANAR_LEGACY'))
    clearance=float(getattr(state,'smart_interior_gap',.03))
    mode=str(mode_override if mode_override is not None else getattr(state,'smart_interior_mode','BALANCED'))
    # Invalid settings are never treated as acceptable geometric contacts.
    if not math.isfinite(clearance) or clearance<0:
        raise ValueError('Smart Interior: clearance must be finite and nonnegative')
    if mode not in ('BALANCED','PRIORITY','CONTOUR_FOLLOW'):
        raise ValueError('Smart Interior: unsupported resolution mode')
    pairs=contact_graph(canonical,clearance=clearance,classes=classes)
    affected=_affected_components(frame_obj.name,canonical,neighbors,pairs,frame_obj)
    current_pairs=[(a,b) for a,b in pairs if a in affected and b in affected]
    def defer(reason,code,ids):
        log_event('SMART_INTERIOR_CONTACT_DEFERRED',window=frame_obj.name,
                  affected=tuple(sorted(ids)),reason=str(reason),reason_code=code,
                  clearance_m=clearance,mode=mode,stage='STAGE_ACTIVE_CONTACT')
        return ({'verts':[],'faces':[],'roles':[]},[],
                {'policy':PENDING_POLICY,'pairs':current_pairs,'active_pending':True,
                 'reason':str(reason),'reason_code':code,
                 'affected_ids':tuple(sorted(ids)), 'clearance_m':clearance,
                 'mode':mode,'neighbor_updates':0,'active_links':[]})
    result={};metadata=[];skipped_peer_groups=[];links={name:[] for name in affected}
    # Store only CURRENT contacts in links; historical adjacency is used once for
    # restoration, never accumulates into an ever-growing rebuild group.
    for group in connected_components(affected,current_pairs):
        for name in group:links[name]=sorted(other for other in group if other!=name)
        if len(group)<2:
            if group[0] in canonical:result[group[0]]=canonical[group[0]]
            continue
        if frame_obj.name not in group:
            # Previously linked peers may now contact one another without
            # contacting the active window. Do not turn their independent
            # unsolved group into an error for this active window. Preserve
            # their *published* meshes and state until their own rebuild.
            skipped_peer_groups.append(tuple(group))
            for name in group:
                other=neighbors[name]
                result[name]=_world(_mesh_data(other),other.matrix_world)
                links[name]=list(_contact_links(other))
            continue
        try:
            staged,info=solve_group({k:canonical[k] for k in group},clearance=clearance,
                                    mode=mode,priority_id=(frame_obj.name if mode=='PRIORITY' else None),
                                    classes={k:classes[k] for k in group})
        except UnresolvedInteriorContact as exc:
            if not (allow_deferred and has_active and frame_obj.name in group):
                raise
            return defer(exc,getattr(exc,'reason_code','CONTACT_NON_SEPARABLE'),group)
        result.update(staged);metadata.append(info)
    updates=[]
    for name,other in neighbors.items():
        if name not in affected:continue
        local=_local(result[name],other.matrix_world)
        published=_mesh_data(other)
        peer_links=links.get(name,[])
        change_geometry=not _equal(local,published)
        change_meta=(tuple(peer_links)!=_contact_links(other) or
                     bool(other.get('DWOS_smart_interior_pending',False)))
        if not (change_geometry or change_meta):continue
        if change_geometry:
            if getattr(other.data,'users',1)>1:
                msg=f'Smart Interior: linked/shared Interior mesh cannot be modified safely: {other.name}'
                if has_active and allow_deferred:return defer(msg,'PROTECTED_LINKED_MESH',(frame_obj.name,name))
                raise ValueError(msg)
            if len(other.data.uv_layers)>0 or getattr(other.data,'shape_keys',None) is not None:
                msg=f'Smart Interior cannot safely replace manually UV-mapped/shapekey Interior: {other.name}'
                if has_active and allow_deferred:return defer(msg,'PROTECTED_UV_SHAPEKEY_MESH',(frame_obj.name,name))
                raise ValueError(msg)
        updates.append({'object':other,'mesh':local,'canonical':originals[name],
                        'links':peer_links,'replace_geometry':change_geometry})
    from .spatial_safety_core import mesh_intersections
    for static in frozen:
        fv,ff=_mesh_world(static)
        for name,mesh in result.items():
            if mesh_intersections(mesh['verts'],mesh['faces'],fv,ff,max_hits=1):
                msg=f'Smart Interior intersects finalized protected object: {static.name}'
                if has_active and allow_deferred:return defer(msg,'FINALIZED_INTERIOR_CONTACT',(frame_obj.name,name))
                raise ValueError(msg)
    active=(_local(result[frame_obj.name],matrix) if has_active else raw_mesh)
    log_event('SMART_INTERIOR_STAGE_V372',window=frame_obj.name,
              affected=tuple(sorted(affected)),active_pairs=len(current_pairs),
              unrelated_pairs=len(pairs)-len(current_pairs),updates=len(updates),
              skipped_peer_groups=len(skipped_peer_groups))
    return active,updates,{'policy':SMART_POLICY,'pairs':current_pairs,
                           'unrelated_contacts':len(pairs)-len(current_pairs),
                           'affected_ids':tuple(sorted(affected)),
                           'groups':len(metadata),'neighbor_updates':len(updates),
                           'skipped_peer_groups':skipped_peer_groups,
                           'active_links':links.get(frame_obj.name,[]),
                           'mode':mode,'clearance_m':clearance,
                           'solver_methods':[str(p[2]) for info in metadata for p in info.get('planes',[])]}

def _validate_publish_mesh(mesh, label):
    """Validate an entire staged payload without touching a Blender Mesh."""
    if not isinstance(mesh, dict) or not all(k in mesh for k in ('verts','faces','roles')):
        raise ValueError(f'Smart Interior: missing staged mesh fields for {label}')
    verts, faces, roles = mesh['verts'], mesh['faces'], mesh['roles']
    if not all(isinstance(seq, (list, tuple)) for seq in (verts, faces, roles)):
        raise ValueError(f'Smart Interior: malformed staged mesh for {label}')
    if len(faces) != len(roles):
        raise ValueError(f'Smart Interior: mismatched face roles for {label}')
    if not all(isinstance(role, str) and role for role in roles):
        raise ValueError(f'Smart Interior: invalid face role for {label}')
    for pos in verts:
        if (not isinstance(pos, (list, tuple)) or len(pos) != 3 or
                any(not isinstance(v, (int,float)) or not math.isfinite(float(v)) for v in pos)):
            raise ValueError(f'Smart Interior: invalid coordinate for {label}')
    for face in faces:
        if (not isinstance(face, (list, tuple)) or len(face) < 3 or
                len(set(face)) != len(face) or
                any(type(i) is not int or i < 0 or i >= len(verts) for i in face)):
            raise ValueError(f'Smart Interior: invalid polygon for {label}')
    if bool(verts) != bool(faces):
        raise ValueError(f'Smart Interior: incomplete staged mesh for {label}')


def _preflight_updates(updates):
    """Fail before the first mutation if ANY target or payload is unsafe."""
    seen = set()
    for row in updates:
        if not isinstance(row, dict) or 'object' not in row or 'mesh' not in row or 'canonical' not in row:
            raise ValueError('Smart Interior: incomplete transaction row')
        obj = row['object']
        if obj is None or getattr(obj, 'type', None) != 'MESH' or getattr(obj, 'data', None) is None:
            raise ValueError('Smart Interior: invalid target object in staged transaction')
        key = obj.as_pointer() if hasattr(obj, 'as_pointer') else id(obj)
        if key in seen:
            raise ValueError(f'Smart Interior: duplicate transaction target: {obj.name}')
        seen.add(key)
        _validate_publish_mesh(row['mesh'], obj.name)
        _validate_publish_mesh(row['canonical'], obj.name + ' canonical')
        if row.get('replace_geometry', True):
            if getattr(obj, 'mode', 'OBJECT') != 'OBJECT':
                raise ValueError(f'Smart Interior: target is in Edit Mode: {obj.name}')
            if getattr(obj, 'library', None) is not None or getattr(obj.data, 'library', None) is not None:
                raise ValueError(f'Smart Interior: linked library Mesh is read-only: {obj.name}')
            if getattr(obj.data, 'users', 1) > 1:
                raise ValueError(f'Smart Interior: shared Mesh cannot be overwritten: {obj.name}')
            if len(getattr(obj.data, 'uv_layers', ())) or getattr(obj.data, 'shape_keys', None) is not None:
                raise ValueError(f'Smart Interior: protected UV/shapekey data: {obj.name}')
            # clear_geometry() would destroy vertex weights and user-authored
            # mesh attributes. Never silently lose modeling data in auto mode.
            if len(getattr(obj, 'vertex_groups', ())) > 0:
                raise ValueError(f'Smart Interior: vertex groups are protected: {obj.name}')
            blender_builtin = {'position', '.edge_verts', 'material_index',
                               'sharp_face', 'sharp_edge', 'crease_edge',
                               '.corner_vert', '.corner_edge', '.select_vert', '.select_edge',
                               '.select_poly', '.hide_vert', '.hide_edge', '.hide_poly'}
            if any(getattr(attr, 'name', '') not in blender_builtin
                   for attr in getattr(obj.data, 'attributes', ())):
                raise ValueError(f'Smart Interior: custom Mesh attributes are protected: {obj.name}: '+ ', '.join(a.name for a in obj.data.attributes if a.name not in blender_builtin))
            if any(getattr(face, 'material_index', 0) != 0
                   for face in getattr(obj.data, 'polygons', ())):
                raise ValueError(f'Smart Interior: per-face material assignment is protected: {obj.name}')
        links = row.get('links', ())
        if not isinstance(links, (list, tuple)) or any(not isinstance(v, str) for v in links):
            raise ValueError(f'Smart Interior: invalid contact links: {obj.name}')


def _replace(obj,mesh):
    me=obj.data
    me.clear_geometry()
    me.from_pydata(mesh['verts'],[],mesh['faces'])
    me.validate(clean_customdata=False);me.update()
    obj['DWOS_face_roles']=json.dumps(mesh['roles'])


def publish_neighbors(updates):
    """Transactional publication with a zero-mutation preflight for every row."""
    updates = list(updates)
    _preflight_updates(updates)
    tracked_obj=('DWOS_face_roles',CANONICAL_KEY,'DWOS_smart_interior_policy',
                 'DWOS_smart_interior_pending','DWOS_smart_interior_reason',LINKS_KEY)
    tracked_parent=('DWOS_smart_interior_pending','DWOS_smart_interior_reason',
                    'DWOS_last_validation_error','DWOS_smart_interior_reason_code')
    def snapshot(obj,keys):
        return [(k,k in obj,obj.get(k)) for k in keys] if obj is not None else []
    def restore(obj,state):
        if obj is None:return
        for key,had,value in state:
            if had:obj[key]=value
            elif key in obj:del obj[key]
    undo=[]
    try:
        for row in updates:
            obj=row['object'];parent=obj.parent;previous=_mesh_data(obj)
            original_obj=snapshot(obj,tracked_obj)
            original_parent=snapshot(parent,tracked_parent)
            undo.append((obj,previous,original_obj,parent,original_parent,row.get('replace_geometry',True)))
            if row.get('replace_geometry',True):_replace(obj,row['mesh'])
            obj[CANONICAL_KEY]=encode(row['canonical'])
            obj[LINKS_KEY]=json.dumps(row.get('links',[]),separators=(',',':'))
            obj['DWOS_smart_interior_policy']=SMART_POLICY
            obj['DWOS_smart_interior_pending']=False
            if 'DWOS_smart_interior_reason' in obj:del obj['DWOS_smart_interior_reason']
            if parent is not None:
                parent['DWOS_smart_interior_pending']=False
                if 'DWOS_smart_interior_reason' in parent:del parent['DWOS_smart_interior_reason']
                if 'DWOS_smart_interior_reason_code' in parent:del parent['DWOS_smart_interior_reason_code']
                if str(parent.get('DWOS_last_validation_error','')).startswith('Smart Interior:'):
                    del parent['DWOS_last_validation_error']
    except Exception:
        for obj,previous,old_obj,parent,old_parent,geometry_changed in reversed(undo):
            try:
                if geometry_changed:_replace(obj,previous)
                restore(obj,old_obj)
                restore(parent,old_parent)
            except Exception as exc:log_exception('SMART_INTERIOR_ROLLBACK_FAILED',exc,window=obj.name)
        raise
    return len(undo)


# Used only by a multi-window CREATE operation: a later fatal error must not
# leave already-published neighbors clipped after the new frames were deleted.
_TRACKED_OBJ=(CANONICAL_KEY,LINKS_KEY,'DWOS_face_roles',
              'DWOS_smart_interior_policy','DWOS_smart_interior_pending',
              'DWOS_smart_interior_reason','DWOS_smart_interior_contacts')
_TRACKED_PARENT=('DWOS_smart_interior_pending','DWOS_smart_interior_reason',
                 'DWOS_last_validation_error','DWOS_smart_interior_reason_code')


def _properties_snapshot(obj,keys):
    return {key:(key in obj,obj.get(key)) for key in keys}


def _restore_properties(obj,values):
    for key,(present,value) in values.items():
        if present:obj[key]=value
        elif key in obj:del obj[key]


def snapshot_scene_interiors(scene):
    """Capture old scene state before batch create. No geometry is changed."""
    records=[]
    for obj in scene.objects:
        if obj.get('DWOS_role')!='INTERIOR' or obj.type!='MESH' or obj.parent is None:continue
        records.append({'object':obj,'mesh':_mesh_data(obj),
                        'props':_properties_snapshot(obj,_TRACKED_OBJ),
                        'parent':obj.parent,
                        'parent_props':_properties_snapshot(obj.parent,_TRACKED_PARENT)})
    return records


def restore_scene_interiors(records):
    """Restore pre-batch geometry/metadata; report failures, never silently swallow."""
    failures=[];changed=0
    for record in records:
        obj=record['object']
        try:
            if not _equal(_mesh_data(obj),record['mesh']):
                if len(obj.data.uv_layers)>0 or getattr(obj.data,'shape_keys',None) is not None:
                    raise ValueError(f'Cannot safely restore modified UV/shapekey mesh: {obj.name}')
                _replace(obj,record['mesh']);changed+=1
            _restore_properties(obj,record['props'])
            _restore_properties(record['parent'],record['parent_props'])
        except Exception as exc:
            failures.append(f'{getattr(obj,"name","UNKNOWN")}: {exc}')
            log_exception('SMART_INTERIOR_BATCH_ROLLBACK_FAILED',exc,
                          window=getattr(obj,'name','UNKNOWN'))
    if failures:
        raise RuntimeError('DWOS batch rollback was incomplete: '+'; '.join(failures[:5]))
    return changed


def _active_anchor(context,interiors):
    """Prioritize explicitly active window, then a pending window, never index 0."""
    active=getattr(context,'active_object',None)
    if active is not None:
        frame=active if getattr(active,'get',lambda *_:None)('DWOS_role')!='INTERIOR' else active.parent
        for item in interiors:
            if item.parent is frame:return item
    pending=[item for item in interiors
             if item.parent.get('DWOS_smart_interior_pending',False) or
             item.get('DWOS_smart_interior_pending',False)]
    return pending[0] if pending else interiors[0]

class DWOS_OT_rebuild_smart_interiors(Operator):
    bl_idname='dwos.rebuild_smart_interiors'
    bl_label='Пересчитать связанные Interior'
    bl_description='Перестроить пересекающиеся Interior из сохранённых канонических оболочек; не изменяет рамы и стекло'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        interiors=sorted((o for o in context.scene.objects
                          if o.type=='MESH' and o.get('DWOS_role')=='INTERIOR' and o.parent),key=lambda o:o.name)
        if not interiors:
            self.report({'INFO'},'Нет Interior для перестройки');return {'FINISHED'}
        anchor=_active_anchor(context,interiors);frame=anchor.parent
        baseline=decode(anchor)
        try:
            result,updates,report=stage(frame,baseline,anchor.matrix_world,context.scene,force=True,allow_deferred=True)
            if report.get('active_pending'):
                frame['DWOS_smart_interior_pending']=True
                frame['DWOS_smart_interior_reason']=str(report['reason'])
                anchor['DWOS_smart_interior_pending']=True
                anchor['DWOS_smart_interior_reason']=str(report['reason'])
                self.report({'WARNING'},'Interior всё ещё ожидает согласования: '+str(report['reason']))
                return {'FINISHED'}
            if not _equal(result,_mesh_data(anchor)) or anchor.get('DWOS_smart_interior_pending',False) or _contact_links(anchor)!=tuple(report.get('active_links',[])):
                updates.insert(0,{'object':anchor,'mesh':result,'canonical':baseline,
                                  'links':report.get('active_links',[]),
                                  'replace_geometry':not _equal(result,_mesh_data(anchor))})
            count=publish_neighbors(updates)
            frame['DWOS_smart_interior_pending']=False
            if 'DWOS_smart_interior_reason' in frame:del frame['DWOS_smart_interior_reason']
            if 'DWOS_smart_interior_reason_code' in frame:del frame['DWOS_smart_interior_reason_code']
            if str(frame.get('DWOS_last_validation_error','')).startswith('Smart Interior:'):
                del frame['DWOS_last_validation_error']
            log_event('SMART_INTERIOR_REBUILT',count=count,contacts=len(report.get('pairs',[])))
            self.report({'INFO'},f'Smart Interior: обновлено {count}, контактов {len(report.get("pairs",[]))}')
        except Exception as exc:
            log_exception('SMART_INTERIOR_REBUILD_REJECTED',exc)
            self.report({'ERROR'},f'Smart Interior: {exc}');return {'CANCELLED'}
        return {'FINISHED'}

class DWOS_OT_manual_interior_repair(Operator):
    bl_idname='dwos.manual_interior_repair'
    bl_label='Исправить Interior вручную'
    bl_description='Немедленно обработать Interior выбранного окна и его соседей выбранной стратегией; без ожидания AUTO и без разрушительного удаления'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        obj=context.active_object
        if obj is not None and obj.get('DWOS_role')=='INTERIOR':obj=obj.parent
        if obj is None or not getattr(obj.dwos_window,'is_window',False):
            self.report({'ERROR'},'Выберите окно DWOS либо его Interior');return {'CANCELLED'}
        cavity=next((o for o in obj.children if o.get('DWOS_role')=='INTERIOR' and o.type=='MESH'),None)
        if cavity is None:
            self.report({'ERROR'},'У окна нет Interior BOX. Включите BOX и перестройте окно.');return {'CANCELLED'}
        strategy=str(context.scene.dwos.manual_interior_strategy)
        try:
            baseline=decode(cavity)
            # Fail-closed: a failed explicit repair must never publish a PENDING/empty mesh.
            result,updates,report=stage(obj,baseline,cavity.matrix_world,context.scene,
                force=True,allow_deferred=False,mode_override=strategy)
            if report.get('active_pending'):
                raise ValueError(str(report.get('reason') or 'UNRESOLVED'))
            if (not _equal(result,_mesh_data(cavity)) or
                cavity.get('DWOS_smart_interior_pending',False) or
                _contact_links(cavity)!=tuple(report.get('active_links',()))):
                updates.insert(0,{'object':cavity,'mesh':result,'canonical':baseline,
                    'links':report.get('active_links',[]),
                    'replace_geometry':not _equal(result,_mesh_data(cavity))})
            count=publish_neighbors(updates)
            obj['DWOS_smart_interior_pending']=False
            cavity['DWOS_smart_interior_pending']=False
            if 'DWOS_smart_interior_manual_error' in obj:del obj['DWOS_smart_interior_manual_error']
            if str(obj.get('DWOS_last_validation_error','')).startswith('Smart Interior:'):
                del obj['DWOS_last_validation_error']
            for prop in ('DWOS_smart_interior_reason','DWOS_smart_interior_reason_code'):
                if prop in obj:del obj[prop]
                if prop in cavity:del cavity[prop]
            log_event('SMART_INTERIOR_MANUAL_SUCCESS',window=obj.name,strategy=strategy,
                gap_m=float(context.scene.dwos.smart_interior_gap),updated=count,contacts=len(report.get('pairs',())))
            self.report({'INFO'},f'Ручное Interior: обновлено {count}; метод {strategy}; зазор {context.scene.dwos.smart_interior_gap:.3f} м')
            return {'FINISHED'}
        except Exception as exc:
            obj['DWOS_smart_interior_manual_error']=str(exc)[:2000]
            log_exception('SMART_INTERIOR_MANUAL_REJECTED',exc,window=obj.name,strategy=strategy)
            self.report({'ERROR'},'Ручная обработка отклонена; Mesh сохранён: '+str(exc)[:180])
            return {'CANCELLED'}

CLASSES=(DWOS_OT_rebuild_smart_interiors,DWOS_OT_manual_interior_repair,)
