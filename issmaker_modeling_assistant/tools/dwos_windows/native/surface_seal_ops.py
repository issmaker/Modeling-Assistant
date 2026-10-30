"""W56: independently seal Interiors, removing UV; leave original HP and wall unchanged.

Explicit upgrade operator, transactionally swaps existing DWOS GLASS / INTERIOR
meshes ONLY. Requires detached old joins first, to avoid displaying stale
collision results. Blender undo owns final rollback, with additional Python
rollback for execution faults.
"""
from __future__ import annotations
import bpy,json
from bpy.props import StringProperty
from . import surface_steps as step

class DWOS_OT_surface_rebuild_closed_interior(bpy.types.Operator):
    bl_idname='dwos.surface_rebuild_closed_interior'
    bl_label='DWOS W56 · Seal Interior Rear / Clear Glass & Interior UV'
    bl_options={'REGISTER','UNDO'}
    collection_name:StringProperty(default='ISSMAKER | W53 NINE LP NO MULLIONS')
    def execute(self,context):
        staged=[];swapped=[]
        try:
            if context.mode!='OBJECT':raise ValueError('W56_OBJECT_MODE_REQUIRED')
            collection=bpy.data.collections.get(self.collection_name)
            if collection is None:raise ValueError('W56_WINDOW_SOURCE_COLLECTION_MISSING')
            hosts=sorted((o for o in collection.objects if o.type=='MESH' and step.SESSION_KEY in o),key=lambda x:x.name)
            if not hosts:raise ValueError('W56_NO_WINDOWS_TO_UPGRADE')
            from .surface_corner_union import JOIN_GROUP_PROP
            for host in hosts:
                if host.get(JOIN_GROUP_PROP) and bpy.data.objects.get(host.get(JOIN_GROUP_PROP)):
                    raise ValueError('W56_RESET_EXISTING_CORNER_GROUPS_FIRST')
                s=json.loads(host[step.SESSION_KEY])
                if int(s['step'])<6 or not s['parameters'].get('classic_glass_midframe'):
                    raise ValueError('W56_INCOMPATIBLE_DWOS_SESSION')
                if step._mesh_fingerprint(host)!=s.get('fingerprint_after'):
                    raise ValueError('W56_WALL_CAP_MESH_CHANGED')
                if step._uv_fingerprint(host)!=s.get('uv_fingerprint_after'):
                    raise ValueError('W56_WALL_CAP_UV_CHANGED')
                original=[]
                for role in ('INTERIOR','GLASS'):
                    ob=bpy.data.objects.get(s['objects'].get(role,''))
                    if ob is None or ob.type!='MESH' or ob.data.users!=1 or ob.data.shape_keys:
                        raise ValueError('W56_UNSAFE_OR_MISSING_'+role)
                    original.append((role,ob))
                mi=step._classic_interior_mesh(s,s['parameters'],int(s['step']))
                if mi is None:raise ValueError('W56_NO_INTERIOR_BUILT')
                staged.append(mi)
                mg=step._classic_glass_mesh(s,s['parameters'],int(s['step']))
                if mg is None:raise ValueError('W56_NO_GLASS_BUILT')
                staged.append(mg)
                if len(mi.polygons)<5 or len(mi.uv_layers) or len(mg.uv_layers) or len(mg.polygons)!=1:
                    raise ValueError('W56_SEAL_GEOMETRY_INVALID')
                old_glass=original[1][1].data
                if len(old_glass.materials)>1 and len(mg.materials)>1:
                    prev=old_glass.materials[1]
                    if prev is not None and prev.name=='DWOS W54 | OPT Glass Physical Preview':
                        mg.materials[1]=prev
                for (role,ob),newmesh in zip(original,(mi,mg)):
                    swapped.append((ob,ob.data))
                    ob.data=newmesh
            # Entire batch committed; old datablocks have zero users.
            for ob,old in swapped:
                if old.users==0 and bpy.data.meshes.get(old.name) is old:
                    bpy.data.meshes.remove(old)
            self.report({'INFO'},f'W56: sealed {len(hosts)} Interior, no front face, no glass/interior UV')
            return {'FINISHED'}
        except Exception as exc:
            for ob,old in reversed(swapped):
                try:ob.data=old
                except Exception:pass
            for mesh in staged:
                if mesh.users==0 and bpy.data.meshes.get(mesh.name) is mesh:bpy.data.meshes.remove(mesh)
            self.report({'ERROR'},str(exc)[:180])
            return {'CANCELLED'}
CLASSES=(DWOS_OT_surface_rebuild_closed_interior,)
