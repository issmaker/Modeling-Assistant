"""W58.17 — staged continuous Interior seam graph.

Creates actual geometric edges along mullion S/V boundaries WITHOUT
disconnecting shells. Scissors remain the only physical cut/separation.
No bpy Mesh/DataBlock mutation; validated staged polygon arrays only.
"""
from __future__ import annotations
import math
from collections import defaultdict

EPS=1.e-7

def _point(vec):
    return tuple(float(x) for x in vec)

def _plane(axis,position,topology):
    axis=str(axis).upper()
    position=float(position)
    if axis=='H':
        return (0.0,position,0.0),(0.0,1.0,0.0)
    if axis!='V':
        raise ValueError('W5817_INTERIOR_INVALID_AXIS')
    if topology is None:
        return (position,0.0,0.0),(1.0,0.0,0.0)
    from .adaptive_opening_core import map_svd_local
    from mathutils import Vector
    h=0.001
    mid=Vector(map_svd_local(topology,position,0.0,0.0))
    a=Vector(map_svd_local(topology,position-h,0.0,0.0))
    b=Vector(map_svd_local(topology,position+h,0.0,0.0))
    tangent=b-a
    tangent.y=0.0
    if tangent.length<1.e-7:
        raise ValueError('W5817_INTERIOR_ZERO_TANGENT')
    tangent.normalize()
    return _point(mid),_point(tangent)

def _area(verts,face):
    if len(face)<3:return 0.0
    a=verts[face[0]]
    ax,ay,az=a
    val=0.0
    for k in range(1,len(face)-1):
        p=verts[face[k]];q=verts[face[k+1]]
        u=(p[0]-ax,p[1]-ay,p[2]-az)
        v=(q[0]-ax,q[1]-ay,q[2]-az)
        cx=u[1]*v[2]-u[2]*v[1]
        cy=u[2]*v[0]-u[0]*v[2]
        cz=u[0]*v[1]-u[1]*v[0]
        val+=math.sqrt(cx*cx+cy*cy+cz*cz)*.5
    return val

def _cut_at_plane(vertices,faces,roles,groups,point,normal):
    vx=list(vertices)
    result_faces=[];result_roles=[];result_groups=[]
    cache={}
    changed=0
    def signed(index):
        v=vx[index]
        return sum((v[d]-point[d])*normal[d] for d in range(3))
    def intersection(a,b,da,db):
        if abs(da)<EPS:return a
        if abs(db)<EPS:return b
        edge=(min(a,b),max(a,b))
        if edge in cache:return cache[edge]
        t=da/(da-db)
        if not EPS<t<1.-EPS:
            raise ValueError('W5817_INTERIOR_ILL_CONDITIONED_INTERSECTION')
        u=vx[a];v=vx[b]
        c=tuple(u[d]+t*(v[d]-u[d]) for d in range(3))
        idx=len(vx);vx.append(c);cache[edge]=idx
        return idx
    for face,role,group in zip(faces,roles,groups):
        ids=list(face)
        signs=[signed(i) for i in ids]
        if not any(d>EPS for d in signs) or not any(d<-EPS for d in signs):
            result_faces.append(tuple(ids));result_roles.append(role);result_groups.append(group)
            continue
        children=[]
        for keep_positive in (False,True):
            child=[]
            for j,a in enumerate(ids):
                b=ids[(j+1)%len(ids)]
                da=signs[j];db=signs[(j+1)%len(ids)]
                ina=(da>=-EPS if keep_positive else da<=EPS)
                inb=(db>=-EPS if keep_positive else db<=EPS)
                if ina:child.append(a)
                if ina != inb:child.append(intersection(a,b,da,db))
            compact=[]
            for idx in child:
                if not compact or compact[-1]!=idx:compact.append(idx)
            if len(compact)>1 and compact[-1]==compact[0]:compact.pop()
            if len(set(compact))<3 or _area(vx,compact)<1.e-10:
                raise ValueError('W5817_INTERIOR_SUBDIVISION_DEGENERATE')
            children.append(tuple(compact))
        result_faces.extend(children)
        result_roles.extend((role,role))
        result_groups.extend((group,group))
        changed+=1
    if len(result_faces)>20000 or len(vx)>40000:
        raise ValueError('W5817_INTERIOR_SUBDIVISION_RESOURCE_LIMIT')
    return vx,result_faces,result_roles,result_groups,changed

def stage_continuous_interior(verts,faces,roles,*,topology,splits,zone_tree,branch_bands,width,height,frame_width,face_groups=None):
    """Adds real connected mesh edges at user split centers.

    Geometry remains the same surface; physical separation remains exclusively
    controlled by the existing Interior Scissors engine.
    """
    original_groups=list(face_groups) if face_groups else [None]*len(faces)
    if len(original_groups)!=len(faces):
        raise ValueError('W5817_INTERIOR_GROUP_MAP_MISMATCH')
    if not verts or not faces:
        return verts,faces,roles,{'mode':'NO_INTERIOR','applied':0,'cut_faces':0},original_groups
    bands=[]
    if zone_tree is not None:
        from .zone_core import layout
        bands.extend((str(row[0]),row[1],float(row[2])) for row in layout(zone_tree,width,height,frame_width)[1])
    else:
        bands.extend((str(i),sp.axis,float(sp.position)) for i,sp in enumerate(splits))
    bands.extend((str(row.get('id')),row.get('axis'),float(row.get('position'))) for row in branch_bands)
    if not bands:
        return verts,faces,roles,{'mode':'UNSPLIT_BASELINE','applied':0,'cut_faces':0},original_groups
    # Deduplicate coincident coordinates but preserve distinct branch-local bands.
    taken=set();planes=[]
    for sid,axis,value in bands:
        key=(str(axis),round(float(value),6))
        if key in taken:continue
        taken.add(key)
        pt,n=_plane(axis,value,topology)
        planes.append((sid,pt,n))
    x=list(verts);f=list(faces);r=list(roles);groups=list(original_groups)
    total=0;cuts=[]
    for sid,pt,n in planes:
        x,f,r,groups,affected=_cut_at_plane(x,f,r,groups,pt,n)
        cuts.append({'id':sid,'cut_faces':affected,'plane_point':list(pt),'plane_normal':list(n)})
        total+=affected
    if len(f)!=len(r) or len(f)!=len(groups):
        raise ValueError('W5817_INTERIOR_ROLE_COUNT')
    if total==0:
        raise ValueError('W5817_INTERIOR_USER_SPLIT_DID_NOT_INTERSECT_SHELL')
    return x,f,r,{'mode':'CONTINUOUS_CONNECTED_SEAM_V1',
                   'applied':len(planes),'cut_faces':total,'cuts':cuts},groups
