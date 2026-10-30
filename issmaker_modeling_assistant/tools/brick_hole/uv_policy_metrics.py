"""Read-only source edge-density observation. Not a full 2D UV/Jacobian oracle.
World-space metres and active author-selected UV loop map, no scene writes.
"""
import math

def selected_edge_ppm(context, max_edges=32):
    import bmesh
    scene=context.scene
    obj=context.edit_object
    if obj is None or obj.type!='MESH' or context.mode!='EDIT_MESH':return None
    bm=bmesh.from_edit_mesh(obj.data)
    name=scene.brick_hole_uv_map
    layer=bm.loops.layers.uv.get(name) if name else bm.loops.layers.uv.active
    if layer is None:return None
    resolution=int(scene.brick_hole_resolution)
    values=[]
    for edge in bm.edges:
        if not edge.select or edge.hide:continue
        for face in tuple(edge.link_faces)[:1]:
            loop=next((l for l in face.loops if l.edge is edge and
                       l.link_loop_next.vert in edge.verts),None)
            if loop is None:continue
            delta=obj.matrix_world.to_3x3() @ (loop.link_loop_next.vert.co-loop.vert.co)
            metric=float(delta.length)
            uv0,uv1=loop[layer].uv,loop.link_loop_next[layer].uv
            uv_metric=math.hypot(float(uv1.x-uv0.x),float(uv1.y-uv0.y))
            if metric>1e-9 and uv_metric>1e-12:
                values.append(uv_metric*resolution/metric)
        if len(values)>=max_edges:break
    if not values:return None
    return {'count':len(values),'minimum':min(values),
            'maximum':max(values),'mean':sum(values)/len(values),
            'scope':'selected source edge loops only, NOT full-face Jacobian'}
