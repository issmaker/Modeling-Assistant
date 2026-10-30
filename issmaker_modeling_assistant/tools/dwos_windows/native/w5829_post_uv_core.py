"""W58.29 metric UV shell kernel, single 0-1 tile, topology-connected 4m rings.
Long mullions get geometrical cross-section rings and UV island seams; NO UDIM.
No original HP authoring vertices touched when used through replace_post_island.
"""
from __future__ import annotations
import math,bmesh,bpy
from mathutils import Vector

TEXELS=4096.0
DENSITY=1000.0
METRES_PER_TILE=TEXELS/DENSITY
MAX_STRIP_M=4.0
PACK_GAP=0.007
PACK_MARGIN=0.010

def _plan(profile, height):
    points=[Vector((float(p[0]),float(p[1]))) for p in profile]
    if len(points)<3 or len(points)>32:raise ValueError('PROFILE_VERTEX_COUNT_UNSUPPORTED')
    if not math.isfinite(height) or height<=1e-5:raise ValueError('POST_HEIGHT_INVALID')
    if not all(math.isfinite(v) for p in points for v in p):raise ValueError('PROFILE_NONFINITE')
    sides=[(points[(i+1)%len(points)]-points[i]).length for i in range(len(points))]
    if min(sides)<1e-5:raise ValueError('PROFILE_EDGE_DEGENERATE')
    count=int(math.ceil(height/MAX_STRIP_M-1e-10))
    levels=[0.]+[min(height,4.*i) for i in range(1,count+1)]
    if abs(levels[-1]-height)>1e-7:levels[-1]=height
    offsets={}
    x=PACK_MARGIN
    for j in range(count):
        for i,edge in enumerate(sides):
            delta=edge/METRES_PER_TILE
            if x+delta+PACK_MARGIN>1.0:
                raise ValueError('RESOURCE_SINGLE_TILE_CAPACITY_EXCEEDED: narrow profile or fewer segments required')
            offsets[(j,i)]=x
            x+=delta+PACK_GAP
    return points,levels,offsets,x

def append_metric_shell(bm, uv_layer, profile, y_start, height):
    pts,levels,offsets,edge_end=_plan(profile,height)
    n=len(pts)
    rings=[]
    for offset in levels:
        rings.append([bm.verts.new((p.x,float(y_start+offset),p.y)) for p in pts])
    bm.verts.ensure_lookup_table()
    created=[]
    for j in range(len(levels)-1):
        dh=(levels[j+1]-levels[j])/METRES_PER_TILE
        for i in range(n):
            nxt=(i+1)%n
            quad=(rings[j][i],rings[j][nxt],rings[j+1][nxt],rings[j+1][i])
            face=bm.faces.new(quad)
            face.material_index=0
            u0=offsets[(j,i)]
            u1=u0+(pts[nxt]-pts[i]).length/METRES_PER_TILE
            v0=PACK_MARGIN;v1=v0+dh
            table={quad[0]:(u0,v0),quad[1]:(u1,v0),quad[2]:(u1,v1),quad[3]:(u0,v1)}
            for loop in face.loops:loop[uv_layer].uv=table[loop.vert]
            created.append(face)
    return {'segments':len(levels)-1,'profile_sides':n,'rings':len(rings),'post_vertices':n*len(rings),
            'post_faces':len(created),'max_strip_m':max((levels[i+1]-levels[i] for i in range(len(levels)-1)),default=0),
            'used_u':edge_end,'islands':len(created),'uv_0_1':True}

def make_metric_shell(name, profile, height, y_start=0.0):
    bm=bmesh.new()
    uv=bm.loops.layers.uv.new('UVMap')
    result=append_metric_shell(bm,uv,profile,y_start,height)
    mesh=bpy.data.meshes.new(name)
    bm.to_mesh(mesh);bm.free();mesh.update()
    return mesh,result

def replace_post_island(frame):
    old=frame.data
    count=int(frame.get('W5825_post_vertex_count',0))
    if count<6 or count%2:raise ValueError('POST_ISLAND_NOT_FOUND')
    source_v=len(old.vertices)-count
    source_f=len(old.polygons)-int(frame.get('W5825_post_face_count',8))
    base_uv=[tuple(loop.uv) for loop in old.uv_layers.active.data[:old.polygons[source_f].loop_start]]
    oldpost=list(old.vertices)[source_v:]
    height=max(v.co.y for v in oldpost)-min(v.co.y for v in oldpost)
    if height<=1e-5:raise ValueError('OLD_POST_HEIGHT_ZERO')
    y0=min(v.co.y for v in oldpost)
    first=oldpost[:count//2]
    profile=[(v.co.x,v.co.z) for v in first]
    # Validate the plan BEFORE modifying anything.
    _plan(profile,height)
    bm=bmesh.new();bm.from_mesh(old)
    uv=bm.loops.layers.uv.active
    if uv is None:bm.free();raise ValueError('FRAME_UV_MISSING')
    bm.verts.ensure_lookup_table()
    postverts=list(bm.verts)[source_v:]
    bmesh.ops.delete(bm,geom=postverts,context='VERTS')
    result=append_metric_shell(bm,uv,profile,y0,height)
    bm.to_mesh(old);bm.free();old.update()
    current=[tuple(loop.uv) for loop in old.uv_layers.active.data[:old.polygons[source_f].loop_start]]
    if base_uv!=current:raise ValueError('AUTHOR_FRAME_UV_CHANGED')
    frame['W5825_post_vertex_count']=result['post_vertices']
    frame['W5825_post_face_count']=result['post_faces']
    frame['W5829_post_uv_islands']=result['islands']
    frame['W5829_post_metric_uv_1000']=True
    frame['W5829_post_split_height_m']=4.0
    return result
