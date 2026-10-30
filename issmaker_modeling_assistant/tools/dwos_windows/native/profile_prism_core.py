"""DWOS 3.5 constant-cross-section vertical profile primitives.

Corner posts and branch-local vertical mullions are architectural profiles, not
pieces of the S/V/D ribbon.  Their 2D cross-section is authored once in a local
horizontal basis and extruded unchanged along the window V axis.

Pure module: no bpy/mathutils dependency.
"""
from __future__ import annotations
import math
try:
    from .adaptive_opening_core import map_svd_local, dot, cross, length, norm
    from .branch_core import branch_descriptors
except ImportError:
    from adaptive_opening_core import map_svd_local, dot, cross, length, norm
    from branch_core import branch_descriptors

EPS=1e-9


def _sub(a,b): return (a[0]-b[0],a[1]-b[1],a[2]-b[2])
def _add(a,b): return (a[0]+b[0],a[1]+b[1],a[2]+b[2])
def _mul(a,s): return (a[0]*s,a[1]*s,a[2]*s)


def _horizontal(v):
    q=(float(v[0]),0.0,float(v[2]))
    return norm(q) if length(q)>EPS else (0.0,0.0,1.0)


def profile_basis(topology,s,front_offset=0.0):
    """Return stable local (width, up, depth) basis at profile centre S.

    `depth` is sampled only at the profile centre and then frozen. Width is the
    horizontal perpendicular. This is the key difference from mapping both
    profile sides independently through a hard CORNER ribbon.
    """
    p0=tuple(map(float,map_svd_local(topology,float(s),0.0,float(front_offset))))
    p1=tuple(map(float,map_svd_local(topology,float(s),0.0,float(front_offset)+1.0)))
    depth=_horizontal(_sub(p1,p0))
    up=(0.0,1.0,0.0)
    width=cross(up,depth)
    width=_horizontal(width)
    # Deterministic sign: keep width roughly along increasing S.
    ds=max(1e-5,float(topology.get('width',1.0))*1e-6)
    lo=-float(topology.get('width',1.0))*.5
    hi=-lo
    sa=max(lo,min(hi,float(s)-ds)); sb=max(lo,min(hi,float(s)+ds))
    if sb-sa>EPS:
        a=map_svd_local(topology,sa,0.0,float(front_offset))
        b=map_svd_local(topology,sb,0.0,float(front_offset))
        tangent=_horizontal(_sub(b,a))
        if dot(width,tangent)<0: width=_mul(width,-1.0)
    return {'origin':p0,'width_axis':width,'up_axis':up,'depth_axis':depth}




def _host_vector_to_window_local(topology, vector):
    x=tuple(topology['axis_x']); y=tuple(topology['axis_y']); z=tuple(topology['axis_z'])
    v=tuple(map(float,vector))
    return (dot(v,x),dot(v,y),dot(v,z))



def branch_profile_basis(topology, branch_id, s, front_offset=0.0):
    """Return an orthonormal basis owned by one CORNER facade branch.

    Regular branch mullions must never inherit the hard-junction miter/depth
    vector.  The body orientation comes from the real centerline segment of
    its branch; only the profile origin is sampled from S/V/D mapping.
    """
    bid=str(branch_id or '')
    desc=next((b for b in branch_descriptors(topology) if str(b.get('id'))==bid),None)
    if desc is None:
        raise ValueError(f'CORNER regular profile has unknown branch_id: {bid!r}')
    stations=list(topology.get('stations') or ())
    a=int(desc['station_start']); b=int(desc['station_end'])
    if b<=a or a<0 or b>=len(stations):
        raise ValueError('CORNER branch has invalid station range')
    raw_s=float(s)+float(topology.get('width',0.0))*.5
    candidates=[]
    for i in range(a,b):
        sa=float(stations[i]['s']); sb=float(stations[i+1]['s'])
        lo=min(sa,sb);hi=max(sa,sb)
        distance=0.0 if lo-EPS<=raw_s<=hi+EPS else min(abs(raw_s-lo),abs(raw_s-hi))
        candidates.append((distance,i))
    _dist,seg=min(candidates,key=lambda x:(x[0],x[1]))
    c0=tuple(map(float,stations[seg]['center'])); c1=tuple(map(float,stations[seg+1]['center']))
    tangent_world=_sub(c1,c0)
    tangent_local=_horizontal(_host_vector_to_window_local(topology,tangent_world))
    if length(tangent_local)<=EPS:
        tangent_local=_horizontal(_host_vector_to_window_local(topology,stations[seg].get('tangent',(1,0,0))))
    up=(0.0,1.0,0.0)
    depth=_horizontal(cross(tangent_local,up))
    # For increasing-S branch direction, cross(tangent, up) is the facade
    # exterior normal.  Do not read a hard-junction station normal here: that
    # station intentionally contains the miter/bisector normal we are removing.
    expected=depth
    # Re-derive width from depth/up to remove any residual numeric non-orthogonality,
    # while preserving increasing-S orientation.
    width=_horizontal(cross(up,depth))
    if dot(width,tangent_local)<0:width=_mul(width,-1.0)
    # The SVD depth-vector at the hard-corner station is a *miter*, not a
    # constant facade normal. Interpolating it over an otherwise straight
    # branch causes a millimetre-scale H/V seam when Frame Offset != 0.
    # Every point on a branch must use the same rigid branch depth axis.
    zero=tuple(map(float,map_svd_local(topology,float(s),0.0,0.0)))
    origin=_add(zero,_mul(depth,float(front_offset)))
    orth=max(abs(dot(width,depth)),abs(dot(width,up)),abs(dot(depth,up)))
    align=max(-1.0,min(1.0,dot(width,tangent_local)))
    angle=math.degrees(math.acos(align))
    return {
        'origin':origin,'width_axis':width,'up_axis':up,'depth_axis':depth,
        'basis_mode':'CORNER_BRANCH_ORTHONORMAL_V1','branch_id':bid,'segment_index':int(seg),
        'branch_tangent':tangent_local,'branch_exterior_normal':expected,
        'branch_angle_error_deg':float(angle),'orthogonality_error':float(orth),
        'corner_shape_propagation':False,
    }


def branch_id_for_s(topology,s):
    """Resolve centred-S to a deterministic CORNER branch id."""
    ss=float(s); desc=branch_descriptors(topology)
    if not desc:return ''
    best=None
    for b in desc:
        lo=float(b['s0']);hi=float(b['s1'])
        if lo-EPS<=ss<=hi+EPS:return str(b['id'])
        dist=min(abs(ss-lo),abs(ss-hi))
        if best is None or dist<best[0]:best=(dist,str(b['id']))
    return best[1] if best else ''

def branch_mapped_point(topology,branch_id,s,v,d,front_offset=0.0):
    """Map S/V/D using branch-owned orientation, never the corner miter depth."""
    b=branch_profile_basis(topology,branch_id,s,front_offset)
    p=b['origin']
    p=_add(p,_mul(b['up_axis'],float(v)))
    p=_add(p,_mul(b['depth_axis'],float(d)-float(front_offset)))
    return tuple(float(x) for x in p)

def vertical_profile_invariants(mesh):
    """Measure straightness/constant-section invariants of a rectangular V profile."""
    vs=mesh['verts']
    if len(vs)!=8:raise ValueError('Vertical profile invariant expects 8 vertices')
    widths=[math.dist(vs[0],vs[1]),math.dist(vs[3],vs[2]),math.dist(vs[4],vs[5]),math.dist(vs[7],vs[6])]
    depths=[math.dist(vs[0],vs[4]),math.dist(vs[1],vs[5]),math.dist(vs[3],vs[7]),math.dist(vs[2],vs[6])]
    vertical=[_sub(vs[j],vs[i]) for i,j in ((0,3),(1,2),(4,7),(5,6))]
    up=(0.0,1.0,0.0)
    straight=0.0
    for e in vertical:
        le=length(e)
        if le<=EPS:straight=float('inf');break
        # Distance of normalized edge from either +up or -up.
        q=tuple(x/le for x in e); straight=max(straight,min(length(_sub(q,up)),length(_add(q,up))))
    section=max(max(widths)-min(widths),max(depths)-min(depths))
    b=mesh.get('basis') or {}; orth=float(b.get('orthogonality_error',0.0)); angle=float(b.get('branch_angle_error_deg',0.0))
    return {'straightness_error':float(straight),'section_error_m':float(section),
            'orthogonality_error':orth,'branch_angle_error_deg':angle}

def corner_profile_basis(topology,s,front_offset=0.0):
    """Stable rectangular basis for a hard-corner post.

    The post is a real vertical prism.  Its first horizontal axis follows the
    incoming frame branch; the second is the perpendicular horizontal axis
    oriented toward the outgoing branch.  It never uses the averaged/miter
    depth vector that warped the old corner member.
    """
    junctions=topology.get('corner_junctions') or []
    if not junctions:
        return profile_basis(topology,s,front_offset)
    width_total=float(topology.get('width',0.0))
    target=float(s)+width_total*.5
    j=min(junctions,key=lambda q:abs(float(q.get('s',target))-target))
    incoming=_horizontal(_host_vector_to_window_local(topology,j['incoming_tangent']))
    outgoing=_horizontal(_host_vector_to_window_local(topology,j['outgoing_tangent']))
    up=(0.0,1.0,0.0)
    width=incoming
    # A rectangular prism needs orthogonal section axes.  Project the outgoing
    # branch off the incoming axis, then orient the result toward that branch.
    q=(outgoing[0]-width[0]*dot(outgoing,width),0.0,outgoing[2]-width[2]*dot(outgoing,width))
    if length(q)<=EPS:
        q=cross(width,up)
    depth=_horizontal(q)
    if dot(depth,outgoing)<0: depth=_mul(depth,-1.0)
    origin=tuple(map(float,map_svd_local(topology,float(s),0.0,float(front_offset))))
    return {'origin':origin,'width_axis':width,'up_axis':up,'depth_axis':depth,'basis_mode':'CORNER_BRANCH_RECT'}

def build_vertical_profile_prism(topology,s,v0,v1,width,depth,front_offset=0.0,interior_sign=-1,
                                 *,profile_id='',kind='VERTICAL_MULLION',branch_id=''):
    """Build a rectangular constant-section vertical prism in window-local space."""
    s=float(s);v0=float(v0);v1=float(v1);w=float(width);dep=float(depth)
    if interior_sign not in (-1,1): raise ValueError('Profile interior_sign must be +/-1')
    if not all(math.isfinite(x) for x in (s,v0,v1,w,dep,float(front_offset))):
        raise ValueError('Profile has non-finite parameter')
    if v1-v0<=EPS or w<=EPS or dep<=EPS: raise ValueError('Profile dimensions must be positive')
    if str(kind)=='FRAME_CORNER_POST':
        basis=corner_profile_basis(topology,s,front_offset)
    elif topology.get('classification')=='CORNER' and branch_id:
        basis=branch_profile_basis(topology,branch_id,s,front_offset)
    else:
        basis=profile_basis(topology,s,front_offset)
    o=basis['origin'];u=basis['width_axis'];up=basis['up_axis'];d=basis['depth_axis']
    front=o
    back=_add(o,_mul(d,interior_sign*dep))
    def pt(center,uu,vv):
        return _add(_add(center,_mul(u,uu)),_mul(up,vv))
    hw=w*.5
    verts=[
        pt(front,-hw,v0),pt(front, hw,v0),pt(front, hw,v1),pt(front,-hw,v1),
        pt(back, -hw,v0),pt(back,  hw,v0),pt(back,  hw,v1),pt(back, -hw,v1),
    ]
    faces=[];roles=[]
    def addface(ids,role,desired):
        a,b,c=(verts[ids[i]] for i in range(3))
        n=cross(_sub(b,a),_sub(c,a))
        if length(n)<=EPS: raise ValueError('Profile produced zero-area face')
        ids=tuple(ids)
        if dot(n,desired)<0: ids=tuple(reversed(ids))
        faces.append(ids);roles.append(role)
    exterior=_mul(d,-interior_sign); interior=_mul(d,interior_sign)
    addface((0,1,2,3),kind,exterior)
    addface((5,4,7,6),kind+'_BACK',interior)
    addface((1,5,6,2),kind+'_SIDE_POS',u)
    addface((4,0,3,7),kind+'_SIDE_NEG',_mul(u,-1.0))
    addface((3,2,6,7),kind+'_TOP',up)
    addface((4,5,1,0),kind+'_BOTTOM',_mul(up,-1.0))
    out={
        'verts':[tuple(map(float,p)) for p in verts], 'faces':faces, 'roles':roles,
        'profile_id':str(profile_id), 'kind':str(kind), 's':s, 'v0':v0, 'v1':v1,
        'width':w,'depth':dep,'front_offset':float(front_offset),'branch_id':str(branch_id or ''),
        'basis':basis,
    }
    inv=vertical_profile_invariants(out);out['invariants']=inv
    if (not math.isfinite(inv['straightness_error']) or inv['straightness_error']>1e-8
            or inv['section_error_m']>1e-8 or inv['orthogonality_error']>1e-8
            or inv['branch_angle_error_deg']>1e-5):
        raise ValueError(f'Vertical profile invariant failed: {inv}')
    return out


def section_signature(mesh,t=0.5):
    """Return width/depth edge lengths of a rectangular prism (test/diagnostic helper)."""
    vs=mesh['verts']
    width=math.dist(vs[0],vs[1]); depth=math.dist(vs[0],vs[4])
    return (width,depth)


def _corner_junction(topology,s):
    junctions=list(topology.get('corner_junctions') or [])
    if not junctions:return None
    width_total=float(topology.get('width',0.0))
    target=float(s)+width_total*.5
    return min(junctions,key=lambda q:abs(float(q.get('s',target))-target))


def corner_post_junction_basis(topology,s,front_offset=0.0):
    """Return the two inward facade axes of the real hard-corner junction.

    3.5.5 used an incoming-tangent x facade-depth rectangle centred across the
    junction.  On a concave corner that places half of the post outside the
    second facade and makes the separator visibly miss the architectural node.
    3.5.6 anchors one *corner* of the post to the hard junction and grows the
    section only into the shared interior quadrant.  Both facade sides then see
    the configured mullion thickness at exactly the same junction.
    """
    j=_corner_junction(topology,s)
    if j is None:
        b=profile_basis(topology,s,front_offset)
        return {'origin':b['origin'],'axis_a':b['width_axis'],'axis_b':_mul(b['depth_axis'],-1.0),
                'up_axis':b['up_axis'],'basis_mode':'CORNER_FALLBACK'}
    # Keep frame_offset semantics from the ribbon miter, but use the exact hard
    # junction at offset zero as the authoritative architectural anchor.
    origin=tuple(map(float,map_svd_local(topology,float(s),0.0,float(front_offset))))
    ina=_horizontal(_host_vector_to_window_local(topology,tuple(-float(x) for x in j['incoming_exterior_normal'])))
    inb=_horizontal(_host_vector_to_window_local(topology,tuple(-float(x) for x in j['outgoing_exterior_normal'])))
    # Orthogonalize only numerical noise.  CORNER classification already means
    # a hard turn; the square post must stay rectangular, never wedge-shaped.
    q=(inb[0]-ina[0]*dot(inb,ina),0.0,inb[2]-ina[2]*dot(inb,ina))
    if length(q)>EPS:
        inb=_horizontal(q)
    if dot(inb,_horizontal(_host_vector_to_window_local(topology,tuple(-float(x) for x in j['outgoing_exterior_normal']))))<0:
        inb=_mul(inb,-1.0)
    return {'origin':origin,'axis_a':ina,'axis_b':inb,'up_axis':(0.0,1.0,0.0),
            'basis_mode':'CORNER_JUNCTION_QUADRANT_V2','junction_s':float(j['s'])}


def build_corner_post_prism(topology,s,v0,v1,thickness,frame_depth,front_offset=0.0,interior_sign=-1,
                            *,profile_id='corner'):
    """Build the CORNER separator at the real branch junction.

    The visible section is thickness x thickness, anchored at the exact hard
    junction and extending only into the shared interior quadrant.  This makes
    the separator symmetric from both facade branches and prevents the old
    half-exterior / one-sided placement.  `frame_depth` is retained in metadata
    for diagnostics but does not distort the corner section.
    """
    s=float(s);v0=float(v0);v1=float(v1);t=float(thickness);fd=float(frame_depth)
    if interior_sign not in (-1,1):raise ValueError('Profile interior_sign must be +/-1')
    if not all(math.isfinite(x) for x in (s,v0,v1,t,fd,float(front_offset))):raise ValueError('Corner Post has non-finite parameter')
    if v1-v0<=EPS or t<=EPS or fd<=EPS:raise ValueError('Corner Post dimensions must be positive')
    b=corner_post_junction_basis(topology,s,front_offset)
    o=b['origin'];u=b['axis_a'];d=b['axis_b'];up=b['up_axis']
    def add3(a,bv):return _add(a,bv)
    p00=o;p10=_add(o,_mul(u,t));p01=_add(o,_mul(d,t));p11=_add(p10,_mul(d,t))
    def pv(p,v):return _add(p,_mul(up,v))
    verts=[pv(p00,v0),pv(p10,v0),pv(p10,v1),pv(p00,v1),
           pv(p01,v0),pv(p11,v0),pv(p11,v1),pv(p01,v1)]
    faces=[];roles=[]
    def addface(ids,role,desired):
        a0,b0,c0=(verts[ids[i]] for i in range(3));n=cross(_sub(b0,a0),_sub(c0,a0));ids=tuple(ids)
        if length(n)<=EPS:raise ValueError('Corner Post produced zero-area face')
        if dot(n,desired)<0:ids=tuple(reversed(ids))
        faces.append(ids);roles.append(role)
    addface((0,1,2,3),'FRAME_CORNER_POST',_mul(d,-1.0))
    addface((5,4,7,6),'FRAME_CORNER_POST_BACK',d)
    addface((1,5,6,2),'FRAME_CORNER_POST_SIDE_POS',u)
    addface((4,0,3,7),'FRAME_CORNER_POST_SIDE_NEG',_mul(u,-1.0))
    addface((3,2,6,7),'FRAME_CORNER_POST_TOP',up)
    addface((4,5,1,0),'FRAME_CORNER_POST_BOTTOM',_mul(up,-1.0))
    basis={'origin':o,'width_axis':u,'up_axis':up,'depth_axis':d,
           'axis_a':u,'axis_b':d,'basis_mode':b['basis_mode'],'junction_s':b.get('junction_s')}
    return {'verts':[tuple(map(float,p)) for p in verts],'faces':faces,'roles':roles,
            'profile_id':str(profile_id),'kind':'FRAME_CORNER_POST','s':s,'v0':v0,'v1':v1,
            'width':t,'depth':t,'frame_depth':fd,'front_offset':float(front_offset),'basis':basis,
            'anchor_policy':'HARD_JUNCTION_INTERIOR_QUADRANT_V2'}


def build_curved_adaptive_mullion(topology,s,v0,v1,width,depth,front_offset=0.0,
                                  interior_sign=-1,*,profile_id="",
                                  kind="FRAME_MULLION_VERTICAL"):
    """Extrude a sharp-edged mullion from the *actual* SVD facade, not a rigid prism.

    At each authored path station, use an exact intersection of the two inward
    profile-offset lines (miter). Every front node lies on the source facade.
    Each depth section has the true normal distance DEPTH to its corresponding
    facade segment, rather than using a global/bisector depth direction.
    No rounding or beveling. The user wall is not changed.
    """
    s=float(s);v0=float(v0);v1=float(v1)
    width=float(width);depth=float(depth);front_offset=float(front_offset)
    if interior_sign not in (-1,1) or min(width,depth,v1-v0)<=EPS:
        raise ValueError("CURVED_MULLION_INVALID_DIMENSIONS")
    if topology.get("classification")!="CURVED":
        raise ValueError("CURVED_MULLION_WRONG_CLASS")
    lo=s-width*.5;hi=s+width*.5
    extent=float(topology["width"])*.5
    if lo<-extent-1e-8 or hi>extent+1e-8:
        raise ValueError("CURVED_MULLION_OUTSIDE_SELECTED_OPENING")
    stations=sorted(set(float(st["s"])-extent for st in topology["stations"]))
    cuts=[lo]+[t for t in stations if lo+1.e-6<t<hi-1.e-6]+[hi]
    up=(0.,1.,0.)
    unitwidth=max(1.e-5,float(topology["width"])*1.e-6)
    sign_axis=None
    def tangent(x,side):
        if side=="LEFT":
            a=max(-extent,x-unitwidth);b=x
        elif side=="RIGHT":
            a=x;b=min(extent,x+unitwidth)
        else:
            a=max(-extent,x-unitwidth);b=min(extent,x+unitwidth)
        d=_sub(map_svd_local(topology,b,0.,0.),map_svd_local(topology,a,0.,0.))
        if length(d)<EPS:raise ValueError("CURVED_MULLION_ZERO_FACADE_TANGENT")
        return norm(d)
    def local_normal(t):
        nn=cross(t,up)
        if length(nn)<EPS:raise ValueError("CURVED_MULLION_NONHORIZONTAL_PATH")
        return norm(nn)
    def miter_normal(x):
        left=local_normal(tangent(x,"LEFT"))
        right=local_normal(tangent(x,"RIGHT"))
        # Choose interior-facing source normal consistent with the SVD map.
        dmap=_sub(map_svd_local(topology,x,0.,1.),
                  map_svd_local(topology,x,0.,0.))
        if dot(left,dmap)<0:left=_mul(left,-1.)
        if dot(right,dmap)<0:right=_mul(right,-1.)
        average=_add(left,right)
        if length(average)<1.e-5:
            raise ValueError("CURVED_MULLION_OPPOSITE_NORMALS")
        bisector=norm(average)
        scale=dot(bisector,left)
        if scale<0.2:
            raise ValueError("CURVED_MULLION_MITER_INFEASIBLE")
        return _mul(bisector,1./scale)
    out_verts=[];params=[]
    for x in cuts:
        node=map_svd_local(topology,x,0.,front_offset)
        m=miter_normal(x)
        # Signed profile depth: physical perpendicular distance is exact.
        back=_add(node,_mul(m,interior_sign*depth))
        for anchor,layer in ((node,"FRONT"),(back,"BACK")):
            for v in (v0,v1):
                out_verts.append(_add(anchor,(0.,v,0.)))
                params.append((float(x),float(v),float(front_offset if layer=="FRONT"
                                    else front_offset+interior_sign*depth)))
    faces=[];roles=[]
    def addface(ids,role):
        if len(set(ids))!=len(ids):raise ValueError("CURVED_MULLION_DUPLICATE_FACE_VERTEX")
        xyz=[out_verts[k] for k in ids]
        tri=cross(_sub(xyz[1],xyz[0]),_sub(xyz[2],xyz[0]))
        if length(tri)<1.e-9:raise ValueError("CURVED_MULLION_ZERO_FACE")
        faces.append(tuple(ids));roles.append(role)
    for k in range(len(cuts)-1):
        a=k*4;b=(k+1)*4
        addface((a,a+1,b+1,b),kind)
        addface((b+2,b+3,a+3,a+2),kind+"_BACK")
        addface((a+1,a+3,b+3,b+1),kind+"_TOP")
        addface((a+2,a,b,b+2),kind+"_BOTTOM")
    addface((2,3,1,0),kind+"_SIDE_NEG")
    q=(len(cuts)-1)*4
    addface((q,q+1,q+3,q+2),kind+"_SIDE_POS")
    # Enforce source facade contact and exact depth normal, independent of
    # topology class labels, viewport projection, and object material.
    max_contact=0.
    for k,x in enumerate(cuts):
        for v in (v0,v1):
            idx=k*4+(0 if v==v0 else 1)
            expected=map_svd_local(topology,x,v,front_offset)
            max_contact=max(max_contact,math.dist(out_verts[idx],expected))
    if max_contact>1.e-6:raise ValueError("CURVED_MULLION_FRONT_HOST_CONTACT_FAILED")
    basis=profile_basis(topology,s,front_offset)
    basis["basis_mode"]="CURVED_SHARP_SEGMENT_OWNED_DEPTH_V1"
    return {'verts':[tuple(v) for v in out_verts],'faces':faces,'roles':roles,
            'vertex_params':params,'profile_id':str(profile_id),'kind':kind,
            's':s,'v0':v0,'v1':v1,'width':width,'depth':depth,
            'front_offset':front_offset,'basis':basis,
            'join_policy':'EXACT_SHARP_NO_BEVEL_NO_ROUND',
            'facade_contact_error_m':max_contact,
            'sections':len(cuts),'section_positions':cuts}
