"""Mesh-level actual Blender seam marking with no geometric or UV mutation."""

def mark_mesh_uv_seams(mesh, *, include_material=True, include_boundary=True, epsilon=2e-6):
    layer=mesh.uv_layers.active
    if layer is None:
        return {'marked':0,'uv':0,'material':0,'boundary':0,'already':sum(bool(e.use_seam) for e in mesh.edges)}
    uses={}
    for poly in mesh.polygons:
        loops=list(poly.loop_indices)
        for j,idx in enumerate(loops):
            nex=loops[(j+1)%len(loops)]
            edge=int(mesh.loops[idx].edge_index)
            v=int(mesh.loops[idx].vertex_index); w=int(mesh.loops[nex].vertex_index)
            uv0=tuple(layer.data[idx].uv); uv1=tuple(layer.data[nex].uv)
            if v>w: v,w=w,v; uv0,uv1=uv1,uv0
            uses.setdefault(edge,[]).append((v,w,uv0,uv1,int(poly.material_index)))
    marked=uv=material=boundary=0
    for edge_id, samples in uses.items():
        if len(samples)>2:
            continue  # Non-manifold is an error, not a made-up module boundary.
        is_boundary=len(samples)==1
        is_material=len(samples)==2 and include_material and samples[0][4]!=samples[1][4]
        is_uv=len(samples)==2 and any(abs(a-b)>epsilon
             for i in (2,3) for a,b in zip(samples[0][i],samples[1][i]))
        if is_uv:uv+=1
        if is_material:material+=1
        if is_boundary:boundary+=1
        if (is_uv or is_material or (include_boundary and is_boundary)) and not mesh.edges[edge_id].use_seam:
            mesh.edges[edge_id].use_seam=True;marked+=1
    mesh.update()
    return {'marked':marked,'uv':uv,'material':material,'boundary':boundary,
            'already':sum(bool(e.use_seam) for e in mesh.edges)}
