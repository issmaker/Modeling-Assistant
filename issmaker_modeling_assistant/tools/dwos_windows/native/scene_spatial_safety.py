"""DWOS 3.5.3 Blender scene Interior collision preflight and audit operator."""
from __future__ import annotations
import bpy
from mathutils import Vector
from bpy.types import Operator
from .spatial_safety_core import self_intersections,mesh_intersections
from .dwos_log import event as log_event,exception as log_exception

def _world_verts(matrix,verts):
    return [tuple(matrix @ Vector(tuple(map(float,v)))) for v in verts]

def _mesh_world(obj):
    return ([tuple(obj.matrix_world @ v.co) for v in obj.data.vertices], [tuple(p.vertices) for p in obj.data.polygons])

def validate_staged_interior(frame_obj,verts,faces,scene=None):
    """Raise before commit; current published meshes therefore remain intact."""
    if not faces:return {'self_hits':0,'scene_hits':0,'other':''}
    own=self_intersections(verts,faces)
    if own:
        raise ValueError(f'INTERIOR SELF-INTERSECTION: {len(own)} пересечений внутри Interior. Операция отменена.')
    scene=scene or bpy.context.scene
    staged_world=_world_verts(frame_obj.matrix_world,verts)
    for other in scene.objects:
        if other.get('DWOS_role')!='INTERIOR' or other.parent is frame_obj or other.type!='MESH':continue
        ov,of=_mesh_world(other)
        hits=mesh_intersections(staged_world,faces,ov,of)
        if hits:
            owner=other.parent.name if other.parent else other.name
            raise ValueError(f'INTERIOR COLLISION: Interior {frame_obj.name} пересекается с {owner}. Операция отменена.')
    return {'self_hits':0,'scene_hits':0,'other':''}

def scan_scene(scene):
    rows=[]; interiors=[o for o in scene.objects if o.type=='MESH' and o.get('DWOS_role')=='INTERIOR']
    meshes={o.name:_mesh_world(o) for o in interiors}
    for o in interiors:
        v,f=meshes[o.name];selfh=self_intersections(v,f)
        if selfh:rows.append({'type':'SELF_INTERSECTION','window':o.parent.name if o.parent else o.name,'count':len(selfh)})
    for i,a in enumerate(interiors):
        va,fa=meshes[a.name]
        for b in interiors[i+1:]:
            vb,fb=meshes[b.name];hits=mesh_intersections(va,fa,vb,fb)
            if hits:rows.append({'type':'INTERIOR_INTERIOR_COLLISION','window':a.parent.name if a.parent else a.name,'other':b.parent.name if b.parent else b.name,'count':len(hits)})
    return rows

class DWOS_OT_validate_scene_interiors(Operator):
    bl_idname='dwos.validate_scene_interiors'
    bl_label='Проверить Interior сцены'
    bl_description='Самопересечения Interior и пересечения Interior разных окон DWOS'
    bl_options={'REGISTER'}
    def execute(self,context):
        try:rows=scan_scene(context.scene)
        except Exception as exc:
            log_exception('INTERIOR_SCENE_VALIDATION_FAILED',exc);self.report({'ERROR'},str(exc));return {'CANCELLED'}
        if rows:
            for row in rows:log_event('INTERIOR_SCENE_CONFLICT',**row)
            self.report({'WARNING'},f'Найдено конфликтов Interior: {len(rows)}. См. dwos-latest.log')
        else:
            log_event('INTERIOR_SCENE_VALIDATION_OK',windows=sum(1 for o in context.scene.objects if o.get('DWOS_role')=='INTERIOR'))
            self.report({'INFO'},'Interior: пересечений не найдено')
        return {'FINISHED'}
CLASSES=(DWOS_OT_validate_scene_interiors,)
