"""W54 reusable opt-in operator: exact 90deg open Interior union for HP windows."""
from __future__ import annotations
import bpy, json, math, hashlib
from bpy.props import StringProperty, BoolProperty
from mathutils import Vector
from . import interior_corner_union_core as kernel
from . import interior_angular_union_core as angular
from . import smart_interior_core as old_kernel
from . import spatial_safety_core as spatial

JOIN_ROLE="DWOS_W54_INTERIOR_CORNER_JOIN"
JOIN_GROUP_PROP="DWOS_W54_joint_object"
STATE_PROP="DWOS_W54_lod_mode"

def _mesh_fingerprint(obj):
    if obj is None or obj.type!='MESH':
        raise ValueError('W54_FINGERPRINT_NO_MESH')
    raw=[(tuple(round(float(x),6) for x in v.co) for v in obj.data.vertices),
         (tuple(int(i) for i in p.vertices) for p in obj.data.polygons)]
    h=hashlib.sha256()
    h.update(repr(tuple(tuple(row) for row in raw[0])).encode('ascii'))
    h.update(repr(tuple(tuple(row) for row in raw[1])).encode('ascii'))
    return h.hexdigest()

def _original(host):
    from .surface_steps import SESSION_KEY
    state=json.loads(host[SESSION_KEY])
    if int(state['step'])<6 or not state['parameters'].get("classic_glass_midframe",False):
        raise ValueError("W54_NEEDS_STEP6_CLASSIC_HP")
    obj=bpy.data.objects.get(state['objects'].get('INTERIOR',''))
    if obj is None or obj.type!="MESH" or len(obj.data.polygons) not in (4,5):
        raise ValueError("W54_EXPECT_4_FACE_OPEN_INTERIOR:"+host.name)
    if obj.data.shape_keys is not None or obj.data.users!=1:
        raise ValueError("W54_PROTECTED_INTERIOR_MESH:"+host.name)
    if len(obj.data.polygons)==5 and not obj.data.get('DWOS_interior_rear_cap'):
        raise ValueError('W56_BACK_CAP_METADATA_MISSING')
    if len(obj.data.polygons)==4:
        raise ValueError('W56_OLD_OPEN_INTERIOR_MUST_BE_REFRESHED_FIRST')
    from mathutils import Vector
    src=state['source'];pars=state['parameters']
    origin=Vector(src['origin']);n=Vector(src['normal'])
    rear=origin+n*(float(pars['window_offset'])-.25*float(pars['frame_depth'])-float(pars['interior_depth']))
    payload={'verts':[tuple(obj.matrix_world@v.co) for v in obj.data.vertices],
             'faces':[tuple(p.vertices) for p in list(obj.data.polygons)[:4]],
             'roles':['INTERIOR_REVEAL']*4,
             'rear_line':(float(rear.x),float(rear.y),float(n.x),float(n.y))}
    return state,obj,payload

def _mesh_for(join,key):
    mesh=bpy.data.meshes.new("DWOS_W54_CORNER_INTERIOR_"+key)
    try:
        mesh.from_pydata(join["verts"],[],join["faces"])
        if mesh.validate(clean_customdata=False):
            raise ValueError("W54_MERGED_MESH_REQUIRED_UNSAFE_REPAIR")
        mesh.update()
        if any(p.area<1e-8 for p in mesh.polygons):
            raise ValueError("W54_ZERO_AREA_AFTER_BLENDER_CONSTRUCTION")
        if len(mesh.polygons)!=len(join["faces"]):
            raise ValueError("W54_MISSING_FACES_AFTER_CONSTRUCTION")
        mesh["DWOS_W56_UV_POLICY"]="NO_INTERIOR_UV"
        mesh["DWOS_W56_BACK_CLOSED"]=True
        from . import surface_step_core as sc
        topo=sc.mesh_orientation_connectivity_audit(join['verts'],join['faces'],join['roles'])
        if topo['components']!=1 or topo['winding_conflicts']:
            raise ValueError("W54_JOINT_TOPOLOGY:"+repr(topo))
        return mesh
    except Exception:
        bpy.data.meshes.remove(mesh)
        raise

def sync_lod_group(host, mode):
    """Called by ordinary DWOS HP/LP viewport switch; never mutates geometry."""
    host[STATE_PROP]=str(mode)
    name=host.get(JOIN_GROUP_PROP)
    if not name:return
    joined=bpy.data.objects.get(name)
    if joined is None:return
    from .surface_steps import SESSION_KEY
    peers=json.loads(joined["DWOS_W54_member_hosts"])
    hashes=json.loads(joined["DWOS_W54_member_fingerprints"])
    from .surface_steps import SESSION_KEY
    stale=False
    for name in peers:
        peer=bpy.data.objects.get(name)
        if peer is None or SESSION_KEY not in peer:
            stale=True
            continue
        state=json.loads(peer[SESSION_KEY])
        obj=bpy.data.objects.get(state["objects"].get("INTERIOR",""))
        if obj is None or _mesh_fingerprint(obj)!=hashes.get(name):
            stale=True
    all_hp=not stale and all(str((host if x==host.name else bpy.data.objects[x]).get(STATE_PROP,"HP")) in ("HP","BOTH") for x in peers)
    joined.hide_set(not all_hp)
    joined["DWOS_W54_stale_requires_rebuild"]=bool(stale)
    for hname in peers:
        peer=bpy.data.objects.get(hname)
        if peer is None or SESSION_KEY not in peer:continue
        info=json.loads(peer[SESSION_KEY])
        interior=bpy.data.objects.get(info["objects"].get("INTERIOR",""))
        if interior is not None:
            wanted=str(peer.get(STATE_PROP,"HP"))
            interior.hide_set(all_hp or wanted not in ("HP","BOTH"))

def _opt_glass_preview():
    """Named opt-in shader: never mutate the user's authored materials."""
    name="DWOS W54 | OPT Glass Physical Preview"
    mat=bpy.data.materials.get(name)
    if mat is not None:return mat,False
    mat=bpy.data.materials.new(name)
    mat.use_nodes=True
    mat.diffuse_color=(0.18,0.43,0.54,0.55)
    mat.surface_render_method='DITHERED'
    bsdf=next(n for n in mat.node_tree.nodes if n.type=='BSDF_PRINCIPLED')
    bsdf.inputs["Base Color"].default_value=(0.18,0.43,0.54,1.)
    bsdf.inputs["Alpha"].default_value=.55
    bsdf.inputs["Transmission Weight"].default_value=.82
    bsdf.inputs["Roughness"].default_value=.08
    bsdf.inputs["IOR"].default_value=1.45
    mat["DWOS_W54_generated_opt_glass"]=True
    return mat,True

class DWOS_OT_surface_resolve_corner_interiors(bpy.types.Operator):
    bl_idname='dwos.surface_resolve_corner_interiors'
    bl_label='DWOS W54 · Combine Intersecting Corner Interiors'
    bl_description='Group perpendicular overlapping HP Interior sleeves into an exact open L-shaped mesh, without modifying authored windows'
    bl_options={'REGISTER','UNDO'}
    collection_name:StringProperty(name='Window sources collection',default='ISSMAKER | W53 NINE LP NO MULLIONS')
    apply_opt_glass_preview:BoolProperty(name='Correct OPT glass preview material',default=False)
    def execute(self,context):
        stage_objects=[]
        staged_meshes=[]
        changed_props=[]
        changed_vis=[]
        changed_glass=[]
        created_material=None
        try:
            if context.mode!='OBJECT':
                raise ValueError("W54_OBJECT_MODE_REQUIRED")
            coll=bpy.data.collections.get(self.collection_name)
            if coll is None:raise ValueError("W54_UNKNOWN_WINDOW_COLLECTION")
            from .surface_steps import SESSION_KEY
            hosts=sorted([obj for obj in coll.objects if obj.type=="MESH" and SESSION_KEY in obj],key=lambda o:o.name)
            if len(hosts)<2:raise ValueError("W54_AT_LEAST_TWO_HP_WINDOWS")
            data={};metadata={}
            for host in hosts:
                if host.get(JOIN_GROUP_PROP) and bpy.data.objects.get(host.get(JOIN_GROUP_PROP)):
                    raise ValueError('W54_ALREADY_RESOLVED_USE_EXISTING_CORNER_GROUP')
                state,original,payload=_original(host)
                data[host.name]=payload
                metadata[host.name]=(host,state,original)
            contact=old_kernel.contact_graph(data,clearance=0.)
            if not contact:
                self.report({'INFO'},"W54: no overlapping Interiors found")
                return {'FINISHED'}
            used=set();jobs=[]
            for a,b in contact:
                if a in used or b in used:
                    raise ValueError("W54_MULTIPLE_CONTACT_GROUP_UNSUPPORTED")
                proposed=angular.merge_two(data[a],data[b],resolution=4096)
                if spatial.self_intersections(proposed['verts'],proposed['faces'],max_hits=1):
                    raise ValueError("W54_UNION_SELF_INTERSECTION")
                if any(spatial.mesh_intersections(proposed['verts'],proposed['faces'],data[other]['verts'],data[other]['faces'],max_hits=1)
                       for other in data if other not in (a,b)):
                    raise ValueError("W54_UNION_CONTACTS_UNRELATED_WINDOW")
                jobs.append((a,b,proposed));used.update((a,b))
            coll_out=bpy.data.collections.get("ISSMAKER | W54 SHARED CORNER INTERIORS")
            created_collection=False
            if coll_out is None:
                coll_out=bpy.data.collections.new("ISSMAKER | W54 SHARED CORNER INTERIORS")
                created_collection=True
            # No modifications of existing Interior until ALL output objects ready.
            for a,b,result in jobs:
                identifier=str(metadata[a][1]['id'][:6])+"_"+str(metadata[b][1]['id'][:6])
                mesh=_mesh_for(result,identifier)
                staged_meshes.append(mesh)
                original=metadata[a][2]
                if len(original.data.materials):
                    mesh.materials.append(original.data.materials[2] if len(original.data.materials)>2 and original.data.materials[2] is not None else original.data.materials[0])
                obj=bpy.data.objects.new("DWOS_W54_CORNER_INTERIOR_"+identifier,mesh)
                obj[JOIN_ROLE]=True
                obj["DWOS_W54_member_hosts"]=json.dumps([a,b])
                obj["DWOS_W54_member_fingerprints"]=json.dumps(
                    {a:_mesh_fingerprint(metadata[a][2]), b:_mesh_fingerprint(metadata[b][2])})
                obj["DWOS_W54_policy"]="W55_GENERAL_45_90_OPEN_SLEEVE_UNION"
                obj["DWOS_W54_intersection_area_m2"]=result["metric"]["intersection_area_m2"]
                obj["DWOS_W54_union_footprint_area_m2"]=result["metric"]["footprint_union_area_m2"]
                obj["DWOS_W54_uv_target_px_m"]="NO_UV_REQUIRED"
                stage_objects.append((obj,a,b))
            if created_collection:context.scene.collection.children.link(coll_out)
            for obj,a,b in stage_objects:
                coll_out.objects.link(obj)
            # Transaction starts here; restore old state on any error.
            for obj,a,b in stage_objects:
                for hname in (a,b):
                    host,state,original=metadata[hname]
                    for target,key,newval in ((host,JOIN_GROUP_PROP,obj.name),(host,STATE_PROP,"HP")):
                        changed_props.append((target,key,key in target,target.get(key)))
                        target[key]=newval
                for hname in (a,b):
                    original=metadata[hname][2]
                    changed_vis.append((original,original.hide_get()))
                    original.hide_set(True)
                obj.hide_set(False)
            if self.apply_opt_glass_preview:
                material,created=_opt_glass_preview()
                if created:created_material=material
                for host in hosts:
                    state=metadata[host.name][1]
                    glass=bpy.data.objects.get(state['objects'].get('GLASS',''))
                    if glass is None or len(glass.data.polygons)!=1 or len(glass.data.vertices)!=4:
                        raise ValueError("W54_OPT_GLASS_IS_NOT_SINGLE_PLANE")
                    if len(glass.data.materials)<2:
                        raise ValueError("W54_OPT_GLASS_MATERIAL_SLOT_MISSING")
                    previous=glass.data.materials[1]
                    if previous is not None and previous.name=="DWOS LAB | GLASS":
                        changed_glass.append((glass,previous))
                        glass.data.materials[1]=material
            self.report({'INFO'},f'W54: resolved {len(jobs)} orthogonal Interior corner pair(s)')
            return {'FINISHED'}
        except Exception as exc:
            for ob,old_mat in reversed(changed_glass):
                try:ob.data.materials[1]=old_mat
                except Exception:pass
            if created_material is not None and created_material.users==0:
                bpy.data.materials.remove(created_material)
            for obj,old_visible in reversed(changed_vis):
                try:obj.hide_set(old_visible)
                except Exception:pass
            for ob,key,had,value in reversed(changed_props):
                try:
                    if had:ob[key]=value
                    elif key in ob:del ob[key]
                except Exception:pass
            for obj,_,_ in stage_objects:
                if bpy.data.objects.get(obj.name) is not None:bpy.data.objects.remove(obj,do_unlink=True)
            for mesh in staged_meshes:
                if bpy.data.meshes.get(mesh.name) is not None and mesh.users==0:bpy.data.meshes.remove(mesh)
            self.report({'ERROR'},str(exc)[:180])
            return {'CANCELLED'}


class DWOS_OT_surface_reset_corner_joins(bpy.types.Operator):
    """Explicit reset, never silent deletion of authored joined geometry."""
    bl_idname='dwos.surface_reset_corner_joins'
    bl_label='DWOS W55 · Reset Old Shared Interior Groups'
    bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        try:
            groups=[ob for ob in bpy.data.objects if ob.get(JOIN_ROLE)]
            if not groups:return {'FINISHED'}
            if context.mode!='OBJECT':raise ValueError('W55_RESET_NEEDS_OBJECT_MODE')
            checks=[]
            from .surface_steps import SESSION_KEY
            for obj in groups:
                if obj.type!='MESH' or obj.data.users!=1 or obj.data.shape_keys or obj.library or obj.data.library:
                    raise ValueError('W55_RESET_PROTECTED_JOIN:'+obj.name)
                members=json.loads(obj["DWOS_W54_member_hosts"])
                if len(members)!=2:raise ValueError('W55_INVALID_JOIN_MEMBER_COUNT')
                for name in members:
                    host=bpy.data.objects.get(name)
                    if host is None or SESSION_KEY not in host:raise ValueError('W55_JOIN_HOST_MISSING')
                    if host.get(JOIN_GROUP_PROP)!=obj.name:raise ValueError('W55_UNEXPECTED_JOIN_OWNER')
                checks.append((obj,members))
            # Validate ALL before any mutation. Blender UNDO protects this operation.
            for obj,members in checks:
                for name in members:
                    host=bpy.data.objects[name]
                    for prop in (JOIN_GROUP_PROP,STATE_PROP):
                        if prop in host:del host[prop]
                    state=json.loads(host[SESSION_KEY])
                    original=bpy.data.objects.get(state['objects'].get('INTERIOR',''))
                    if original is not None:original.hide_set(False)
                bpy.data.objects.remove(obj,do_unlink=True)
            self.report({'INFO'},f'W55: reset {len(checks)} shared Interior groups, originals retained')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}

CLASSES=(DWOS_OT_surface_resolve_corner_interiors,DWOS_OT_surface_reset_corner_joins)
