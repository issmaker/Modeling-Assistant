"""Readonly Blender adapter for Brick Hole HUD axis eligibility (BH-03.2D)."""
import bmesh
from mathutils import Vector
from .direction_snap_core import axis_eligibility


def available_axes(context, face_basis=None):
    obj=context.edit_object
    if obj is None or obj.type!='MESH' or context.mode!='EDIT_MESH':
        return {key:(False,'Требуется Edit Mesh') for key in 'XYZ'}
    bm=bmesh.from_edit_mesh(obj.data)
    selection=getattr(getattr(context,'tool_settings',None),'mesh_select_mode',None)
    # Match Brick Hole's own current exclusive selection rule.
    from . import partition_brick_selection,face_world_normal,axis_vector
    faces,edges,_=partition_brick_selection(bm,selection)
    if not faces and not edges:
        return {key:(False,'Нет выделения') for key in 'XYZ'}
    if face_basis is not None:
        axis={key:axis_vector(face_basis,key) for key in 'XYZ'}
    else:
        axis={'X':(1.,0.,0.),'Y':(0.,1.,0.),'Z':(0.,0.,1.)}
    directions=[]
    normals=[]
    if edges:
        mat=obj.matrix_world.to_3x3()
        for edge in edges:
            directions.append(tuple(mat @ (edge.verts[1].co-edge.verts[0].co)))
    else:
        normals=[tuple(face_world_normal(obj,face)) for face in faces]
    return {key:axis_eligibility(vec,edge_tangents=directions,face_normals=normals)
            for key,vec in axis.items()}
