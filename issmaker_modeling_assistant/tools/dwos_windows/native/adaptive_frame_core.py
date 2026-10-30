"""DWOS 3.1 adaptive frame builder over S/V/D opening topology.

The outer Host-contact polyline is independent from internal split subdivision:
contact vertices come only from the detected opening topology, while mullions may
add arbitrary internal grid coordinates.  This preserves the V2.1.6 no-split-
vertices-on-wall-contact rule for curved/corner apertures.
"""
from __future__ import annotations

from math import isfinite
import struct

try:
    from .adaptive_opening_core import map_svd_local, sub, cross, dot, length, norm
    from .frame_core import EPS, SplitSpec, _uniq
    from .corner_rules import validate_split_rows
    from .profile_prism_core import build_vertical_profile_prism, build_corner_post_prism, build_curved_adaptive_mullion
except ImportError:  # pure regression tests
    from adaptive_opening_core import map_svd_local, sub, cross, dot, length, norm
    from frame_core import EPS, SplitSpec, _uniq
    from corner_rules import validate_split_rows
    from profile_prism_core import build_vertical_profile_prism, build_corner_post_prism, build_curved_adaptive_mullion


def adaptive_frame_grid(width, height, frame_width, splits, station_s, zone_tree=None, corner_posts=(), branch_bands=(), subdivision_rows=()):
    hx=width*0.5; hy=height*0.5
    fw=max(EPS,min(frame_width,min(width,height)*0.49))
    xs=[-hx,-hx+fw,hx-fw,hx]
    ys=[-hy,-hy+fw,hy-fw,hy]
    xs.extend(float(s) for s in station_s if -hx+EPS < float(s) < hx-EPS)
    bands=[]
    # 3.5.9: field-proven CONVEX corners use an integrated junction and must
    # not create cp_s +/- internal_width subdivisions.  CONCAVE corners keep the
    # previous isolated profile path until an equivalent user reference exists.
    corner_posts=[(float(p['s']),max(EPS,float(p['thickness'])),str(p.get('id','corner')),str(p.get('junction_kind','')).upper()) for p in (corner_posts or ())]
    for cp_s,cp_t,_cp_id,cp_kind in corner_posts:
        if cp_kind not in {'CONVEX','CONCAVE'} and -hx+fw+EPS < cp_s < hx-fw-EPS:
            xs.extend((cp_s-cp_t*.5,cp_s+cp_t*.5))
    if zone_tree:
        try:
            from .zone_core import layout
        except ImportError:
            from zone_core import layout
        _leaves,region_bands=layout(zone_tree,width,height,fw)
        # W58.7: V/H zone cells remain ONE shared mesh with the HP border.
        # All inner/front/back vertices belong to the same SVD-authorized grid.
        # A detached V prism produced proven upper belt collisions in W58.6.
        bands=[row[-1] for row in region_bands]
        for x0,x1,y0,y1 in bands:
            xs.extend((x0,x1));ys.extend((y0,y1))
    # 3.5: horizontal branch bands remain ribbon strips, while vertical
    # mullions become explicit constant-cross-section prisms.  Their band
    # boundaries still subdivide the grid so neighbouring H strips terminate
    # deterministically, but the V band itself is NOT filled by ribbon cells.
    for row in (branch_bands or ()):
        axis=row.get('axis') if isinstance(row,dict) else (row[1] if len(row)>1 else None)
        band=row.get('band') if isinstance(row,dict) else (row[5] if len(row)>5 else row[-1])
        if band is None:
            continue
        x0,x1,y0,y1=(float(v) for v in band)
        # Legacy/non-CORNER path: horizontal bands remain ribbon cells.
        if axis=='H':
            bands.append((x0,x1,y0,y1))
            xs.extend((x0,x1));ys.extend((y0,y1))
    # 3.5.12: Unified CornerAssembly needs the base host surfaces subdivided at
    # every future H/V contact boundary, but none of those rows may become
    # active ribbon cells.  They are only topological cut coordinates.
    for row in (subdivision_rows or ()):
        band=row.get('band') if isinstance(row,dict) else (row[5] if len(row)>5 else row[-1])
        if band is None:continue
        x0,x1,y0,y1=(float(v) for v in band)
        xs.extend((x0,x1));ys.extend((y0,y1))
    for split in ([] if zone_tree else splits):
        t=max(EPS,float(split.thickness))
        if split.axis=='V': xs.extend((split.position-t*0.5,split.position+t*0.5))
        elif split.axis=='H': ys.extend((split.position-t*0.5,split.position+t*0.5))
    xs=_uniq([max(-hx,min(hx,float(x))) for x in xs])
    ys=_uniq([max(-hy,min(hy,float(y))) for y in ys])

    def active(cx,cy):
        if cx<=-hx+fw+EPS or cx>=hx-fw-EPS or cy<=-hy+fw+EPS or cy>=hy-fw-EPS:
            return True
        for x0,x1,y0,y1 in bands:
            if x0-EPS<=cx<=x1+EPS and y0-EPS<=cy<=y1+EPS:return True
        for split in ([] if zone_tree else splits):
            if split.axis=='V' and abs(cx-split.position)<=split.thickness*0.5+EPS:return True
            if split.axis=='H' and abs(cy-split.position)<=split.thickness*0.5+EPS:return True
        # CORNER junctions are integrated explicitly after the base ribbon pass.
        # They are never represented as filled grid cells.
        return False
    cells={}
    for ix in range(len(xs)-1):
        for iy in range(len(ys)-1):
            cells[(ix,iy)]=active((xs[ix]+xs[ix+1])*0.5,(ys[iy]+ys[iy+1])*0.5)
    return xs,ys,cells



def _blender_float32(value):
    """The stored Blender Mesh vertex coordinates are IEEE754 float32."""
    return struct.unpack('<f', struct.pack('<f', float(value)))[0]


def _collapse_published_curved_stations(topology, stations):
    """Merge only *calculation* S stations whose mapped Mesh positions are
    indistinguishable after Blender float32 publication.

    Never modify the Host/authoritative topology, or silently merge genuinely
    distinct stations on a large-coordinate mesh. Those must fail explicitly.
    """
    src = _uniq(stations)
    out = []
    prev_key = None
    prev = None
    for s in src:
        point = map_svd_local(topology, float(s), 0.0, 0.0)
        key = tuple(_blender_float32(v) for v in point)
        if prev_key is not None and key == prev_key:
            # The Host may contain micro-duplicates (observed <0.1um in the
            # field). Only a micro-detail smaller than 10um is collapsible;
            # otherwise precision is inadequate and needs an explicit error.
            gap = length(sub(point, prev))
            if gap > 1.0e-5:
                raise ValueError(
                    'CURVED frame: distinct Host stations collapse at Blender '
                    f'float32 precision (distance={gap:.9g} m); recenter Host')
            continue
        out.append(float(s))
        prev_key = key
        prev = point
    return out


def _audit_published_curved_frame(mesh):
    """Fail closed if Blender publication would collapse generated frame faces.

    Do not change geometry or suppress the zero-area quality gate.
    """
    try:
        from .mesh_consistency_core import audit_frame
    except ImportError:
        from mesh_consistency_core import audit_frame
    frame_f32 = dict(mesh, verts=[tuple(_blender_float32(c) for c in v)
                                  for v in mesh['verts']])
    result = audit_frame(frame_f32, 'CURVED')
    if result['status'] != 'PASS':
        bad=[]
        for j,poly in enumerate(frame_f32['faces']):
            if len(poly)<3:continue
            from mathutils import Vector
            pts=[Vector(frame_f32['verts'][k]) for k in poly]
            maximum=max(((pts[(k+1)%len(pts)]-pts[k]).cross(pts[(k+2)%len(pts)]-pts[k])).length
                      for k in range(len(pts)))
            if maximum<=2e-12:
                bad.append({'index':j,'role':frame_f32.get('roles',[])[j] if len(frame_f32.get('roles',[]))>j else 'UNKNOWN',
                            'verts':list(poly),'xyz':[tuple(p) for p in pts],'area2':maximum})
                if len(bad)>3:break
        raise ValueError('CURVED frame cannot be published to Blender Mesh: '
                         + ', '.join(result['issues'])+' DEBUG='+repr(bad))
    return result

def _param(values, lo, hi):
    span=hi-lo
    if span<=EPS:return [0.0 for _ in values]
    return [(float(v)-lo)/span for v in values]


def _zipper_faces(outer_ids, outer_t, inner_ids, inner_t):
    """Triangulate a strip between two monotonic open polylines."""
    if len(outer_ids)<2 or len(inner_ids)<2:
        raise ValueError('Adaptive frame strip requires two points per edge')
    faces=[];i=j=0
    while i<len(outer_ids)-1 or j<len(inner_ids)-1:
        no=outer_t[i+1] if i<len(outer_ids)-1 else float('inf')
        ni=inner_t[j+1] if j<len(inner_ids)-1 else float('inf')
        if no <= ni:
            faces.append((outer_ids[i],outer_ids[i+1],inner_ids[j]));i+=1
        else:
            faces.append((outer_ids[i],inner_ids[j+1],inner_ids[j]));j+=1
    return faces


def build_adaptive_frame_mesh(topology, frame_width, depth, splits, *, optimize=False,
                              interior_sign=-1, front_offset=0.0, zone_tree=None, corner_posts=(), branch_bands=(),
                              subdivision_rows=(), _unified_corner_stage=False):
    if not topology.get('build_supported') or topology.get('classification') not in {'PLANAR','CURVED','CORNER'}:
        raise ValueError('Adaptive opening topology is not buildable')
    if interior_sign not in (-1,1):raise ValueError('interior_sign must be -1 or +1')
    width=float(topology['width']);height=float(topology['height'])
    if min(width,height,frame_width,depth)<=EPS:raise ValueError('Adaptive frame dimensions must be positive')
    hx=width*0.5;hy=height*0.5
    fw=max(EPS,min(float(frame_width),min(width,height)*0.49))
    # 3.5.12 Unified CornerAssembly: build the field-proven 3.5.9 base frame
    # first, subdividing only its accepting surfaces, then attach H/V profiles
    # through one junction graph. This avoids the old H=ribbon / V=prism split.
    if (not _unified_corner_stage and topology.get('classification')=='CORNER' and branch_bands
            and any(str(p.get('junction_kind','')).upper() in {'CONVEX','CONCAVE'} for p in (corner_posts or ()))):
        base=build_adaptive_frame_mesh(
            topology,frame_width,depth,splits,optimize=optimize,interior_sign=interior_sign,
            front_offset=front_offset,zone_tree=None,corner_posts=corner_posts,branch_bands=(),
            subdivision_rows=branch_bands,_unified_corner_stage=True)
        try:
            from .corner_assembly_core import assemble_unified_corner
        except ImportError:
            from corner_assembly_core import assemble_unified_corner
        return assemble_unified_corner(base,topology,frame_width,depth,branch_bands,corner_posts,
                                       front_offset=front_offset,interior_sign=interior_sign)
    if (not _unified_corner_stage and topology.get('classification')=='CORNER'
            and branch_bands and not corner_posts):
        # Seamless joint: do NOT inherit the structural-post variant and do NOT
        # let old disconnected V prisms pass as a seemingly valid frame.
        base=build_adaptive_frame_mesh(
            topology,frame_width,depth,splits,optimize=optimize,interior_sign=interior_sign,
            front_offset=front_offset,zone_tree=None,corner_posts=(),branch_bands=(),
            subdivision_rows=branch_bands,_unified_corner_stage=True)
        try:
            from .corner_assembly_core import assemble_postless_corner
        except ImportError:
            from corner_assembly_core import assemble_postless_corner
        return assemble_postless_corner(base,topology,frame_width,depth,branch_bands,
                                        front_offset=front_offset,interior_sign=interior_sign)
    station_s=[float(s['s'])-hx for s in topology.get('stations',[])]
    original_station_count=len(_uniq(station_s))
    if topology.get('classification')=='CURVED':
        station_s=_collapse_published_curved_stations(topology,station_s)
    xs,ys,cells=adaptive_frame_grid(width,height,fw,splits,station_s,zone_tree,corner_posts,branch_bands,subdivision_rows)
    if topology.get('classification')=='CURVED':
        clean_xs=_collapse_published_curved_stations(topology,xs)
        if len(clean_xs)!=len(xs):
            # Splits/zone boundaries may introduce new micro-cells. Never
            # silently rewrite those cells, since their active flags matter.
            raise ValueError('CURVED frame: subdivision creates float32-microcells; '
                             'split positions must be separated before building')
    corner_posts_raw=list(corner_posts or ())
    if corner_posts_raw:
        if branch_bands:
            rows=[{'id':(r.get('id','') if isinstance(r,dict) else r[0]),
                   'axis':(r.get('axis') if isinstance(r,dict) else r[1]),
                   'position':(r.get('position') if isinstance(r,dict) else r[2]),
                   'thickness':(r.get('thickness') if isinstance(r,dict) else r[3]),
                   'rect':(r.get('rect') if isinstance(r,dict) else r[4])} for r in branch_bands]
        elif zone_tree:
            try:
                from .zone_core import layout
            except ImportError:
                from zone_core import layout
            _leaves,_bands=layout(zone_tree,width,height,fw)
            rows=[{'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],'rect':r[4]} for r in _bands]
        else:
            rect=(-hx+fw,hx-fw,-hy+fw,hy-fw)
            rows=[{'id':getattr(sp,'split_id',''),'axis':sp.axis,'position':sp.position,
                   'thickness':sp.thickness,'rect':rect} for sp in splits]
        conflicts=validate_split_rows(rows,corner_posts_raw,0.003)
        if conflicts:
            raise ValueError('Adaptive mullion overlaps Corner Post envelope')
    corner_posts=[(float(p['s']),max(EPS,float(p['thickness'])),str(p.get('id','corner')),str(p.get('junction_kind','')).upper()) for p in corner_posts_raw]
    def corner_post_at(s):
        return next((pid for ps,pt,pid,_kind in corner_posts if abs(float(s)-ps)<=pt*.5+EPS),None)
    z_front=float(front_offset);z_back=z_front+interior_sign*float(depth)
    exterior_sign=-interior_sign
    verts=[];params=[];vid={};faces=[];roles=[]

    def vertex_sv(s,v,d):
        key=(round(float(s),10),round(float(v),10),round(float(d),10))
        if key not in vid:
            p=map_svd_local(topology,float(s),float(v),float(d))
            if not all(isfinite(x) for x in p):raise ValueError('Adaptive mapping returned nonfinite coordinate')
            vid[key]=len(verts);verts.append(tuple(float(x) for x in p));params.append((float(s),float(v),float(d)))
        return vid[key]

    def vertex(ix,iy,layer):
        return vertex_sv(xs[ix],ys[iy],z_front if layer==0 else z_back)

    def local_s_dir(s,v,d):
        step=max(1e-5,width*1e-6)
        a=map_svd_local(topology,max(-hx,s-step),v,d)
        b=map_svd_local(topology,min(hx,s+step),v,d)
        q=sub(b,a)
        return norm(q) if length(q)>EPS else (1.0,0.0,0.0)
    # map_svd_local() is expressed in topology's own recovered frame; V is +local Y.
    up=(0.0,1.0,0.0)

    def depth_dir(s,v,d):
        p0=map_svd_local(topology,s,v,d)
        p1=map_svd_local(topology,s,v,d+1.0)
        q=sub(p1,p0)
        return norm(q) if length(q)>EPS else (0.0,0.0,1.0)

    def append_face(indices,role,desired):
        if len(set(indices))<3:return
        indices=tuple(indices)
        # Use the strongest adjacent triple for orientation.  Integrated CORNER
        # belts intentionally keep a junction vertex on an otherwise straight
        # boundary, so a valid quad may contain one collinear 3-vertex run.
        best=None
        for k in range(len(indices)):
            tri=(indices[k],indices[(k+1)%len(indices)],indices[(k+2)%len(indices)])
            p0,p1,p2=(verts[i] for i in tri)
            nn=cross(sub(p1,p0),sub(p2,p0));aa=length(nn)
            if best is None or aa>best[0]:best=(aa,nn,k)
        if best is None or best[0]<=1e-12:
            raise ValueError(f'Adaptive frame produced zero-area face: {role}')
        if dot(best[1],desired)<0:indices=tuple(reversed(indices))
        if len(indices)>3:
            # Rotate without changing winding so the validator's first triangle
            # is also non-collinear.
            candidates=[]
            for k in range(len(indices)):
                seq=indices[k:]+indices[:k]
                p0,p1,p2=(verts[i] for i in seq[:3])
                candidates.append((length(cross(sub(p1,p0),sub(p2,p0))),seq))
            indices=max(candidates,key=lambda row:row[0])[1]
        faces.append(tuple(indices));roles.append(role)

    # Contact polylines: Host-derived S stations only.  Internal split coordinates
    # are deliberately excluded from this outer ring.
    top_outer=_uniq([float(x['s']) for x in topology.get('top_contact',[])]+[-hx,hx])
    bottom_outer=_uniq([float(x['s']) for x in topology.get('bottom_contact',[])]+[-hx,hx])
    original_contact_counts=(len(top_outer),len(bottom_outer))
    if topology.get('classification')=='CURVED':
        top_outer=_collapse_published_curved_stations(topology,top_outer)
        bottom_outer=_collapse_published_curved_stations(topology,bottom_outer)
    inner_x=[x for x in xs if -hx+fw-EPS<=x<=hx-fw+EPS]
    inner_y=[y for y in ys if -hy+fw-EPS<=y<=hy-fw+EPS]
    left_outer=_uniq([float(x.get('v',0.0)) for x in topology.get('left_contact',[])]+[-hy,hy])
    right_outer=_uniq([float(x.get('v',0.0)) for x in topology.get('right_contact',[])]+[-hy,hy])

    def strip_horizontal(y_outer,y_inner,outer_x,inner_xs,layer,role):
        d=z_front if layer==0 else z_back
        oi=[vertex_sv(x,y_outer,d) for x in outer_x]
        ii=[vertex_sv(x,y_inner,d) for x in inner_xs]
        ot=_param(outer_x,-hx,hx);it=_param(inner_xs,-hx+fw,hx-fw)
        sign=exterior_sign if layer==0 else interior_sign
        for tri in _zipper_faces(oi,ot,ii,it):
            ss=sum(params[k][0] for k in tri)/3.0; vv=sum(params[k][1] for k in tri)/3.0
            append_face(tri,role,tuple(sign*x for x in depth_dir(ss,vv,d)))

    def strip_vertical(x_outer,x_inner,outer_y,inner_ys,layer,role):
        d=z_front if layer==0 else z_back
        oi=[vertex_sv(x_outer,y,d) for y in outer_y]
        ii=[vertex_sv(x_inner,y,d) for y in inner_ys]
        ot=_param(outer_y,-hy,hy);it=_param(inner_ys,-hy+fw,hy-fw)
        sign=exterior_sign if layer==0 else interior_sign
        for tri in _zipper_faces(oi,ot,ii,it):
            ss=sum(params[k][0] for k in tri)/3.0; vv=sum(params[k][1] for k in tri)/3.0
            append_face(tri,role,tuple(sign*x for x in depth_dir(ss,vv,d)))

    for layer,role in ((0,'FRAME_EXTERIOR'),(1,'FRAME_INTERIOR_BACK')):
        if layer==1 and optimize:continue
        strip_horizontal(-hy,-hy+fw,bottom_outer,inner_x,layer,role)
        strip_horizontal(hy,hy-fw,top_outer,inner_x,layer,role)
        strip_vertical(-hx,-hx+fw,left_outer,inner_y,layer,role)
        strip_vertical(hx,hx-fw,right_outer,inner_y,layer,role)

    nx=len(xs)-1;ny=len(ys)-1
    left_inner=-hx+fw;right_inner=hx-fw;bottom_inner=-hy+fw;top_inner=hy-fw
    for (ix,iy),enabled in cells.items():
        if not enabled:continue
        cx=(xs[ix]+xs[ix+1])*0.5;cy=(ys[iy]+ys[iy+1])*0.5
        # Outer belt front/back was already triangulated against Host-only contact.
        if left_inner+EPS < cx < right_inner-EPS and bottom_inner+EPS < cy < top_inner-EPS:
            cp_id=corner_post_at(cx)
            front_role='FRAME_CORNER_POST' if cp_id else 'FRAME_EXTERIOR'
            back_role='FRAME_CORNER_POST_BACK' if cp_id else 'FRAME_INTERIOR_BACK'
            a=vertex(ix,iy,0);b=vertex(ix+1,iy,0);c=vertex(ix+1,iy+1,0);d0=vertex(ix,iy+1,0)
            append_face((a,b,c,d0),front_role,tuple(exterior_sign*x for x in depth_dir(cx,cy,z_front)))
            if not optimize:
                a1=vertex(ix,iy,1);b1=vertex(ix+1,iy,1);c1=vertex(ix+1,iy+1,1);d1=vertex(ix,iy+1,1)
                append_face((a1,b1,c1,d1),back_role,tuple(interior_sign*x for x in depth_dir(cx,cy,z_back)))

        neighbors=[((-1,0),'LEFT'),((1,0),'RIGHT'),((0,-1),'BOTTOM'),((0,1),'TOP')]
        for (dx,dy),side in neighbors:
            if cells.get((ix+dx,iy+dy),False):continue
            if ((side=='LEFT' and ix==0) or (side=='RIGHT' and ix==nx-1)
                    or (side=='BOTTOM' and iy==0) or (side=='TOP' and iy==ny-1)):
                continue  # Host contact stays open.
            if side=='LEFT':
                quad=(vertex(ix,iy,0),vertex(ix,iy,1),vertex(ix,iy+1,1),vertex(ix,iy+1,0))
                desired=tuple(-x for x in local_s_dir(xs[ix],cy,(z_front+z_back)*.5))
            elif side=='RIGHT':
                quad=(vertex(ix+1,iy,0),vertex(ix+1,iy+1,0),vertex(ix+1,iy+1,1),vertex(ix+1,iy,1))
                desired=local_s_dir(xs[ix+1],cy,(z_front+z_back)*.5)
            elif side=='BOTTOM':
                quad=(vertex(ix,iy,0),vertex(ix+1,iy,0),vertex(ix+1,iy,1),vertex(ix,iy,1))
                desired=tuple(-x for x in up)
            else:
                quad=(vertex(ix,iy+1,0),vertex(ix,iy+1,1),vertex(ix+1,iy+1,1),vertex(ix+1,iy+1,0))
                desired=up
            reveal_role='FRAME_CORNER_POST_REVEAL' if corner_post_at(cx) else 'FRAME_REVEAL'
            append_face(quad,reveal_role,desired)

    # 3.5.9 INTEGRATED CORNER JUNCTION PASS -----------------------------
    # Field truth (MY_FRAME) showed that the architectural separator is not an
    # independent internal_width x internal_width box.  It is part of the frame
    # itself: a square whose side is frame depth.  One square vertex is the exact
    # front hard junction, the opposite vertex is the existing SVD back/miter
    # junction, and the two side vertices follow the two facade branches away
    # from the junction.  The square is stitched into top/bottom frame belts and
    # reveal walls, so no overlapping prism/caps remain.
    junction_meta=[]

    def _local_host_vector(v):
        ax=tuple(topology['axis_x']); ay=tuple(topology['axis_y']); az=tuple(topology['axis_z'])
        vv=tuple(float(x) for x in v)
        return (dot(vv,ax),dot(vv,ay),dot(vv,az))

    def _horizontal_unit(v):
        q=(float(v[0]),0.0,float(v[2]))
        return norm(q) if length(q)>EPS else (1.0,0.0,0.0)

    def _junction_for_s(ps):
        js=list(topology.get('corner_junctions') or ())
        if not js:return None
        target=float(ps)+hx
        return min(js,key=lambda j:abs(float(j.get('s',target))-target))

    def _nearest_side(values,ps,side):
        rows=[float(x) for x in values if (float(x)<ps-EPS if side<0 else float(x)>ps+EPS)]
        if not rows:
            raise ValueError('CORNER junction has no neighbouring station for integrated frame patch')
        return max(rows) if side<0 else min(rows)

    def _drop_local_faces(vertex_set, role):
        keep_faces=[];keep_roles=[];dropped=0
        target=set(vertex_set)
        for f,r in zip(faces,roles):
            if r==role and set(f).issubset(target):
                dropped+=1;continue
            keep_faces.append(f);keep_roles.append(r)
        faces[:]=keep_faces;roles[:]=keep_roles
        return dropped

    def _replace_reveal_front(front_id, back_id, replacement_left, replacement_right, ps):
        replaced=0
        for i,(f,r) in enumerate(list(zip(faces,roles))):
            if r not in {'FRAME_REVEAL','FRAME_CORNER_POST_REVEAL'}:continue
            if front_id not in f or back_id not in f:continue
            other=[k for k in f if k not in {front_id,back_id}]
            if not other:continue
            avg=sum(float(params[k][0]) for k in other)/len(other)
            rep=replacement_left if avg<ps else replacement_right
            newf=tuple(rep if k==front_id else k for k in f)
            if len(newf)>3:
                # A branch mullion may terminate exactly on the integrated
                # frame-depth junction plane. After replacing the legacy front
                # junction vertex this can make the previous first triangle
                # collinear even though the quad itself is valid. Rotate to the
                # strongest adjacent triple, preserving winding.
                ranked=[]
                for kk in range(len(newf)):
                    seq=newf[kk:]+newf[:kk]
                    p0,p1,p2=(verts[x] for x in seq[:3])
                    ranked.append((length(cross(sub(p1,p0),sub(p2,p0))),seq))
                newf=max(ranked,key=lambda row:row[0])[1]
            faces[i]=newf
            roles[i]='FRAME_REVEAL'
            replaced+=1
        return replaced

    def _add_raw_vertex(point,param):
        idx=len(verts);verts.append(tuple(float(x) for x in point));params.append(tuple(float(x) for x in param));return idx

    def _patch_corner_junction(ps,pid):
        if topology.get('classification')!='CORNER':return None
        j=_junction_for_s(ps)
        if j is None:raise ValueError('CORNER junction metadata missing')
        dep=float(depth)
        v0=bottom_inner;v1=top_inner
        f0=vertex_sv(ps,v0,z_front); f1=vertex_sv(ps,v1,z_front)
        b0=vertex_sv(ps,v0,z_back);  b1=vertex_sv(ps,v1,z_back)
        p_f0=verts[f0];p_f1=verts[f1]

        # The reference square uses the two real branch tangent directions
        # *away from the hard junction*: -incoming and +outgoing.  This is the
        # exact rule measured from MY_FRAME.  On mirrored/concave topology the
        # legacy SVD back-miter may point to the opposite diagonal; in that case
        # the integrated junction owns the central back vertex too and moves it
        # to the square's opposite corner instead of creating a crossing prism.
        inc=_horizontal_unit(_local_host_vector(j['incoming_tangent']))
        out=_horizontal_unit(_local_host_vector(j['outgoing_tangent']))
        axis_a=tuple(-x for x in inc)
        axis_b=out
        sign_a=-1.0;sign_b=1.0
        if abs(dot(axis_a,axis_b))>0.985:
            raise ValueError('CORNER junction too acute for integrated junction')
        a0=_add3(p_f0,axis_a,dep); a1=_add3(p_f1,axis_a,dep)
        c0=_add3(p_f0,axis_b,dep); c1=_add3(p_f1,axis_b,dep)
        ia0=_add_raw_vertex(a0,(ps,v0,z_front));ic0=_add_raw_vertex(c0,(ps,v0,z_front))
        ic1=_add_raw_vertex(c1,(ps,v1,z_front));ia1=_add_raw_vertex(a1,(ps,v1,z_front))

        # The opposite SVD/miter vertex must be the diagonal corner of the same
        # depth x depth square.  Do not move it: it also belongs to back frame.
        expected_b0=(p_f0[0]+axis_a[0]*dep+axis_b[0]*dep,
                     p_f0[1]+axis_a[1]*dep+axis_b[1]*dep,
                     p_f0[2]+axis_a[2]*dep+axis_b[2]*dep)
        expected_b1=(p_f1[0]+axis_a[0]*dep+axis_b[0]*dep,
                     p_f1[1]+axis_a[1]*dep+axis_b[1]*dep,
                     p_f1[2]+axis_a[2]*dep+axis_b[2]*dep)
        square_error=length(sub(verts[b0],expected_b0))
        # The reference junction is authoritative over the legacy averaged SVD
        # miter.  Reuse the same vertex ids so adjacent back/reveal faces stay
        # manifold and no duplicate geometry is introduced.
        verts[b0]=tuple(float(x) for x in expected_b0)
        verts[b1]=tuple(float(x) for x in expected_b1)

        ip=_nearest_side(inner_x,ps,-1);inext=_nearest_side(inner_x,ps,1)
        bp=_nearest_side(bottom_outer,ps,-1);bn=_nearest_side(bottom_outer,ps,1)
        tp=_nearest_side(top_outer,ps,-1);tn=_nearest_side(top_outer,ps,1)
        # Hard junction itself is a Host station and must exist on both contacts.
        bc=vertex_sv(ps,-hy,z_front);tc=vertex_sv(ps,hy,z_front)
        obp=vertex_sv(bp,-hy,z_front);obn=vertex_sv(bn,-hy,z_front)
        otp=vertex_sv(tp,hy,z_front);otn=vertex_sv(tn,hy,z_front)
        ibp=vertex_sv(ip,v0,z_front);ibn=vertex_sv(inext,v0,z_front)
        itp=vertex_sv(ip,v1,z_front);itn=vertex_sv(inext,v1,z_front)

        # Replace the local front-belt fan around the hard corner.  The 3.5.9
        # reference used exactly one inner point per side.  Branch-local splits
        # can add more inner S coordinates; keeping the old zipper fan together
        # with the fixed reference patch duplicated the Host outer edge on the
        # second branch (field 3.5.10: winding error at edge (1, 2)).
        left_vals=sorted(float(x) for x in inner_x if float(x)<ps-EPS)
        right_vals=sorted(float(x) for x in inner_x if float(x)>ps+EPS)
        dynamic_belt=(len(left_vals)>1 or len(right_vals)>1)

        def _drop_belt_fan(outer_a,outer_b,y_outer,y_inner,s_lo,s_hi):
            keep_f=[];keep_r=[];dropped=0
            for f,r in zip(faces,roles):
                if r!='FRAME_EXTERIOR':
                    keep_f.append(f);keep_r.append(r);continue
                pp=[params[k] for k in f]
                local=all(abs(float(d)-z_front)<=1e-7 and s_lo-1e-7<=float(ss)<=s_hi+1e-7
                          and (abs(float(v)-y_outer)<=1e-7 or abs(float(v)-y_inner)<=1e-7)
                          for ss,v,d in pp)
                touches=(outer_a in f or outer_b in f)
                has_outer=any(abs(float(v)-y_outer)<=1e-7 for _ss,v,_d in pp)
                has_inner=any(abs(float(v)-y_inner)<=1e-7 for _ss,v,_d in pp)
                if local and touches and has_outer and has_inner:
                    dropped+=1;continue
                keep_f.append(f);keep_r.append(r)
            faces[:]=keep_f;roles[:]=keep_r
            return dropped

        def _fan_side(side,outer_a,outer_b,hard_id,side_id,y_outer,y_inner,ordinary_vals,desired):
            if side>0:
                span=max(EPS,hx-ps)
                vals=[x for x in ordinary_vals if x>ps+dep+1e-7]
                inner=[hard_id,side_id]+[vertex_sv(x,y_inner,z_front) for x in vals]
                it=[0.0,min(.999999,dep/span)]+[min(.999999,max(0.0,(x-ps)/span)) for x in vals]
            else:
                span=max(EPS,ps+hx)
                vals=[x for x in ordinary_vals if x<ps-dep-1e-7]
                inner=[vertex_sv(x,y_inner,z_front) for x in vals]+[side_id,hard_id]
                it=[min(.999999,max(0.0,(x+hx)/span)) for x in vals]+[max(0.0,1.0-dep/span),1.0]
            # Remove accidental coincident consecutive points while preserving
            # the authoritative integrated side vertex.
            compact=[];ct=[]
            for idx,tv in zip(inner,it):
                if compact and length(sub(verts[idx],verts[compact[-1]]))<=1e-7:
                    compact[-1]=idx;ct[-1]=tv
                else:
                    compact.append(idx);ct.append(tv)
            for tri in _zipper_faces([outer_a,outer_b],[0.0,1.0],compact,ct):
                append_face(tri,'FRAME_EXTERIOR',desired)

        if dynamic_belt:
            # Rebuild the *entire* outer Host edge fan on each side that gained
            # internal subdivisions. This guarantees the Host edge is owned by
            # exactly one triangle and keeps arbitrary nested H/V zone trees
            # compatible with the integrated corner patch.
            _drop_belt_fan(obp,bc,-hy,v0,float(params[obp][0]),ps)
            _drop_belt_fan(bc,obn,-hy,v0,ps,float(params[obn][0]))
            _drop_belt_fan(otp,tc,hy,v1,float(params[otp][0]),ps)
            _drop_belt_fan(tc,otn,hy,v1,ps,float(params[otn][0]))
            left_des=tuple(exterior_sign*x for x in depth_dir((ip+ps)*.5,v0,z_front))
            right_des=tuple(exterior_sign*x for x in depth_dir((ps+inext)*.5,v0,z_front))
            left_des_t=tuple(exterior_sign*x for x in depth_dir((ip+ps)*.5,v1,z_front))
            right_des_t=tuple(exterior_sign*x for x in depth_dir((ps+inext)*.5,v1,z_front))
            _fan_side(-1,obp,bc,f0,ia0,-hy,v0,left_vals,left_des)
            _fan_side( 1,bc,obn,f0,ic0,-hy,v0,right_vals,right_des)
            _fan_side(-1,otp,tc,f1,ia1, hy,v1,left_vals,left_des_t)
            _fan_side( 1,tc,otn,f1,ic1, hy,v1,right_vals,right_des_t)
        else:
            # Exact 3.5.9 user-reference topology when no branch subdivision
            # exists near the hard junction.
            db=_drop_local_faces({obp,bc,obn,ibp,f0,ibn},'FRAME_EXTERIOR')
            dt=_drop_local_faces({otp,tc,otn,itp,f1,itn},'FRAME_EXTERIOR')
            if db<2 or dt<2:
                raise ValueError('CORNER integrated junction could not isolate front belt neighbourhood')
            left_des=tuple(exterior_sign*x for x in depth_dir((ip+ps)*.5,v0,z_front))
            right_des=tuple(exterior_sign*x for x in depth_dir((ps+inext)*.5,v0,z_front))
            append_face((obp,bc,f0),'FRAME_EXTERIOR',left_des)
            append_face((obp,f0,ia0,ibp),'FRAME_EXTERIOR',left_des)
            append_face((bc,obn,f0),'FRAME_EXTERIOR',right_des)
            append_face((obn,ibn,ic0,f0),'FRAME_EXTERIOR',right_des)
            left_des_t=tuple(exterior_sign*x for x in depth_dir((ip+ps)*.5,v1,z_front))
            right_des_t=tuple(exterior_sign*x for x in depth_dir((ps+inext)*.5,v1,z_front))
            append_face((otp,tc,f1),'FRAME_EXTERIOR',left_des_t)
            append_face((otp,f1,ia1,itp),'FRAME_EXTERIOR',left_des_t)
            append_face((tc,otn,f1),'FRAME_EXTERIOR',right_des_t)
            append_face((otn,itn,ic1,f1),'FRAME_EXTERIOR',right_des_t)

        rb=_replace_reveal_front(f0,b0,ia0,ic0,ps)
        rt=_replace_reveal_front(f1,b1,ia1,ic1,ps)
        if rb!=2 or rt!=2:
            raise ValueError(f'CORNER integrated junction reveal stitch incomplete: bottom={rb}, top={rt}')

        # 3.5.10: branch-owned H mullions may terminate directly on either
        # side plane of the integrated square. Legacy code ended them at the old
        # internal_width envelope and mapped their rear endpoint through SVD,
        # producing the visible wedge/overlap reported in field screenshots.
        # The new contract is a real T-junction: front endpoint lies on the
        # frame-depth side plane, rear endpoint lies on the square diagonal-back
        # line, the terminal cap is removed, and the post side wall is opened only
        # across the attached mullion band.
        side_a_s=ps-dep; side_b_s=ps+dep
        tol=max(1e-6,dep*1e-5)
        def _h_attachments(side_s, side):
            out=[]
            for row in ((branch_bands or subdivision_rows) or ()):
                axis=row.get('axis') if isinstance(row,dict) else row[1]
                if axis!='H':continue
                band=row.get('band') if isinstance(row,dict) else (row[5] if len(row)>5 else row[-1])
                if band is None:continue
                x0,x1,y0,y1=(float(v) for v in band)
                edge=x1 if side<0 else x0
                if abs(edge-side_s)<=tol:
                    out.append((min(y0,y1),max(y0,y1),str(row.get('id','') if isinstance(row,dict) else row[0])))
            return out
        attach_a=_h_attachments(side_a_s,-1); attach_b=_h_attachments(side_b_s,1)

        center=((p_f0[0]+verts[b0][0])*.5,(p_f0[1]+verts[b0][1])*.5,(p_f0[2]+verts[b0][2])*.5)
        attached=bool(attach_a or attach_b)
        attachment_caps_removed=0
        if not attached:
            # Exact 3.5.9 user-reference topology remains byte-for-purpose when
            # there are no corner-adjacent H mullions.
            ring=[(f0,ia0,f1,ia1,'FRAME_CORNER_POST'),
                  (ia0,b0,ia1,b1,'FRAME_CORNER_POST_SIDE_NEG'),
                  (b0,ic0,b1,ic1,'FRAME_CORNER_POST_BACK'),
                  (ic0,f0,ic1,f1,'FRAME_CORNER_POST_SIDE_POS')]
            for q0,q1,q0t,q1t,role in ring:
                mid=((verts[q0][0]+verts[q1][0])*.5,(verts[q0][1]+verts[q1][1])*.5,(verts[q0][2]+verts[q1][2])*.5)
                desired=_horizontal_unit(sub(mid,center))
                append_face((q0,q1,q1t,q0t),role,desired)
        else:
            hard_plan=(p_f0[0],p_f0[2]); a_plan=(a0[0],a0[2]); b_plan=(expected_b0[0],expected_b0[2]); c_plan=(c0[0],c0[2])

            def _set_plan(idx,plan,v):
                verts[idx]=(float(plan[0]),float(v),float(plan[1]))
                return idx

            def _replace_face_id(old_id,new_id):
                if old_id==new_id:return
                for fi,face in enumerate(faces):
                    if old_id in face:
                        faces[fi]=tuple(new_id if x==old_id else x for x in face)

            back_ids={}
            def _back_id(v):
                key=round(float(v),10)
                if key in back_ids:return back_ids[key]
                if abs(float(v)-v0)<=EPS:idx=b0
                elif abs(float(v)-v1)<=EPS:idx=b1
                else:idx=vertex_sv(ps,float(v),z_back)
                _set_plan(idx,b_plan,v);back_ids[key]=idx;return idx

            def _hard_id(v):
                if abs(float(v)-v0)<=EPS:return f0
                if abs(float(v)-v1)<=EPS:return f1
                return _set_plan(vertex_sv(ps,float(v),z_front),hard_plan,v)

            def _side_id(v,which):
                if which=='A':
                    if abs(float(v)-v0)<=EPS:return ia0
                    if abs(float(v)-v1)<=EPS:return ia1
                    return _set_plan(vertex_sv(side_a_s,float(v),z_front),a_plan,v)
                if abs(float(v)-v0)<=EPS:return ic0
                if abs(float(v)-v1)<=EPS:return ic1
                return _set_plan(vertex_sv(side_b_s,float(v),z_front),c_plan,v)

            # Re-route every attached H-mullion rear endpoint to the square's
            # diagonal-back line and remove its legacy terminal reveal cap.
            for which,side_s,bands in (('A',side_a_s,attach_a),('B',side_b_s,attach_b)):
                for y0,y1,_sid in bands:
                    ids=[]
                    for vv in (y0,y1):
                        front=_side_id(vv,which)
                        old_back=vertex_sv(side_s,float(vv),z_back)
                        back=_back_id(vv)
                        _replace_face_id(old_back,back)
                        ids.extend((front,back))
                    target=set(ids)
                    attachment_caps_removed+=_drop_local_faces(target,'FRAME_REVEAL')

            cuts=_uniq([v0,v1]+[v for a in (attach_a+attach_b) for v in a[:2]])
            cuts=[v for v in cuts if v0-EPS<=v<=v1+EPS]
            def _covered(v,bands):
                return any(a-EPS < v < b+EPS for a,b,_sid in bands)
            for lo,hi in zip(cuts,cuts[1:]):
                if hi-lo<=EPS:continue
                midv=(lo+hi)*.5
                h0,h1=_hard_id(lo),_hard_id(hi)
                a0i,a1i=_side_id(lo,'A'),_side_id(hi,'A')
                b0i,b1i=_back_id(lo),_back_id(hi)
                c0i,c1i=_side_id(lo,'B'),_side_id(hi,'B')
                # Branch A approaches the square through plane A->BACK.
                # Branch B approaches through plane BACK->C.  The two remaining
                # faces are longitudinal exterior faces and must stay present.
                segments=[(h0,a0i,h1,a1i,'FRAME_CORNER_POST',True),
                          (a0i,b0i,a1i,b1i,'FRAME_CORNER_POST_SIDE_NEG',not _covered(midv,attach_a)),
                          (b0i,c0i,b1i,c1i,'FRAME_CORNER_POST_BACK',not _covered(midv,attach_b)),
                          (c0i,h0,c1i,h1,'FRAME_CORNER_POST_SIDE_POS',True)]
                for q0,q1,q0t,q1t,role,enabled in segments:
                    if not enabled:continue
                    mid=((verts[q0][0]+verts[q1][0])*.5,(verts[q0][1]+verts[q1][1])*.5,(verts[q0][2]+verts[q1][2])*.5)
                    desired=_horizontal_unit(sub(mid,center))
                    append_face((q0,q1,q1t,q0t),role,desired)
        return {
            'profile_id':str(pid),'kind':'FRAME_CORNER_POST','s':float(ps),'v0':v0,'v1':v1,
            'width':dep,'depth':dep,'requested_internal_width':next((pt for s,pt,p,_kind in corner_posts if p==pid),0.0),
            'front_offset':z_front,'basis':{'axis_a':axis_a,'axis_b':axis_b,'up_axis':up,'incoming_sign':sign_a,'outgoing_sign':sign_b,
                'basis_mode':'CORNER_REFERENCE_BRANCH_SQUARE_V3'},
            'vertex_indices':[f0,ia0,b0,ic0,f1,ia1,b1,ic1],
            'vertex_count':8,'integrated':True,'square_error_m':float(square_error),
            'anchor_policy':'HARD_JUNCTION_ANGLE_PARALLELOGRAM_V4',
            'mullion_attachment_policy':'FRAME_DEPTH_SIDE_PLANE_T_JUNCTION_V1',
            'h_attachments_a':len(attach_a),'h_attachments_b':len(attach_b),
            'attachment_caps_removed':int(attachment_caps_removed),
        }

    def _add3(p,axis,scale):
        return (float(p[0])+float(axis[0])*scale,float(p[1])+float(axis[1])*scale,float(p[2])+float(axis[2])*scale)

    legacy_corner_posts=[]
    if corner_posts:
        for ps,pt,pid,kind in corner_posts:
            if kind in {'CONVEX','CONCAVE'}:
                meta=_patch_corner_junction(ps,pid)
                if meta:junction_meta.append(meta)
            else:
                legacy_corner_posts.append((ps,pt,pid,kind))

    # 3.5.11 BRANCH PROFILE T-JUNCTION CONTRACT ------------------------
    # Zone-tree hierarchy already tells us whether an H ribbon terminates on
    # the side of a V profile (V parent -> H child) or a V profile terminates
    # on the top/bottom surface of H (H parent -> V child).  Legacy geometry
    # kept both contact caps, producing coplanar/z-fighting squares and visible
    # steps.  Suppress only the contact cap; all exterior profile faces remain.
    _rows=[]
    for rr in (branch_bands or ()):
        if isinstance(rr,dict):
            _rows.append(rr)
        else:
            _rows.append({'id':rr[0],'axis':rr[1],'position':rr[2],'thickness':rr[3],
                          'rect':rr[4],'band':(rr[5] if len(rr)>5 else rr[-1]),
                          'branch_id':(rr[6] if len(rr)>6 else '')})
    _hrows=[r for r in _rows if r.get('axis')=='H']
    _vrows=[r for r in _rows if r.get('axis')=='V']
    _profile_tol=2e-7
    _h_side_contacts=[]
    _v_cap_contacts={}
    for hr in _hrows:
        hb=tuple(float(x) for x in hr.get('band'))
        hrect=tuple(float(x) for x in hr.get('rect'))
        hbid=str(hr.get('branch_id',''))
        for vr in _vrows:
            if str(vr.get('branch_id',''))!=hbid:continue
            vb=tuple(float(x) for x in vr.get('band'))
            vrect=tuple(float(x) for x in vr.get('rect'))
            vid_=str(vr.get('id',''))
            # V parent -> H child: H begins/ends exactly on a V side plane.
            if abs(hb[0]-vb[1])<=_profile_tol:
                _h_side_contacts.append((hb[0],hb[2],hb[3],str(hr.get('id','')),'V_SIDE_POS'))
            if abs(hb[1]-vb[0])<=_profile_tol:
                _h_side_contacts.append((hb[1],hb[2],hb[3],str(hr.get('id','')),'V_SIDE_NEG'))
            # H parent -> V child: V vertical extent begins/ends on H top/bottom.
            flags=_v_cap_contacts.setdefault(vid_,set())
            if abs(vrect[2]-hb[3])<=_profile_tol:flags.add('BOTTOM')
            if abs(vrect[3]-hb[2])<=_profile_tol:flags.add('TOP')

    def _remove_h_contact_reveal(s_contact,y0,y1):
        keep_f=[];keep_r=[];removed=0
        for f,r in zip(faces,roles):
            if r!='FRAME_REVEAL':
                keep_f.append(f);keep_r.append(r);continue
            pp=[params[i] for i in f]
            same_s=all(abs(float(ss)-float(s_contact))<=_profile_tol for ss,_v,_d in pp)
            vs=[float(v) for _ss,v,_d in pp]
            same_band=(min(vs)>=float(y0)-_profile_tol and max(vs)<=float(y1)+_profile_tol
                       and abs(min(vs)-float(y0))<=_profile_tol and abs(max(vs)-float(y1))<=_profile_tol)
            if same_s and same_band:
                removed+=1;continue
            keep_f.append(f);keep_r.append(r)
        faces[:]=keep_f;roles[:]=keep_r
        return removed

    hv_caps_removed=0
    for s_contact,y0,y1,_hid,_side in _h_side_contacts:
        hv_caps_removed+=_remove_h_contact_reveal(s_contact,y0,y1)

    # PROFILE PASS -------------------------------------------------------
    # CORNER separator geometry is already integrated above.  Ordinary
    # branch-local vertical mullions remain constant-section prisms, with their
    # contact cap removed when they terminate on an H ribbon.
    profile_meta=list(junction_meta)
    def append_profile(mesh, s_param, v0, v1, d_front, role_kind):
        off=len(verts)
        verts.extend(mesh['verts'])
        # Keep vertex_params length aligned for diagnostics; these params are
        # semantic only because the profile is intentionally NOT SVD-warped.
        if "vertex_params" in mesh:
            if len(mesh["vertex_params"])!=len(mesh["verts"]):
                raise ValueError("CURVED_PROFILE_PARAM_COUNT_MISMATCH")
            params.extend(mesh["vertex_params"])
        else:
            for vv in (v0,v0,v1,v1,v0,v0,v1,v1):
                params.append((float(s_param),float(vv),float(d_front)))
        faces.extend(tuple(off+i for i in f) for f in mesh['faces']);roles.extend(mesh['roles'])
        meta={k:mesh[k] for k in ('profile_id','kind','s','v0','v1','width','depth','front_offset','basis')}
        meta['vertex_start']=off;meta['vertex_count']=len(mesh['verts']);profile_meta.append(meta)

    clear_v0=-hy+fw; clear_v1=hy-fw
    for ps,pt,pid,_kind in legacy_corner_posts:
        pm=build_corner_post_prism(topology,ps,clear_v0,clear_v1,pt,float(depth),z_front,interior_sign,profile_id=pid)
        append_profile(pm,ps,clear_v0,clear_v1,z_front,'FRAME_CORNER_POST')

    for row in (branch_bands or ()):
        axis=row.get('axis') if isinstance(row,dict) else row[1]
        if axis!='V':continue
        sid=str(row.get('id','') if isinstance(row,dict) else row[0])
        pos=float(row.get('position') if isinstance(row,dict) else row[2])
        thick=float(row.get('thickness') if isinstance(row,dict) else row[3])
        rect=row.get('rect') if isinstance(row,dict) else row[4]
        rv0,rv1=float(rect[2]),float(rect[3])
        if topology.get('classification')=='CURVED':
            pm=build_curved_adaptive_mullion(
                topology,pos,rv0,rv1,thick,float(depth),z_front,
                interior_sign,profile_id=sid,kind='FRAME_MULLION_VERTICAL')
        else:
            pm=build_vertical_profile_prism(
                topology,pos,rv0,rv1,thick,float(depth),z_front,
                interior_sign,profile_id=sid,kind='FRAME_MULLION_VERTICAL')
        cap_flags=set(_v_cap_contacts.get(sid,set()))
        # Every branch-local V profile is physically attached at its vertical
        # ends.  Root/full-height V profiles terminate on the main frame inner
        # belt; nested profiles may terminate on an H ribbon.  Keeping a cap at
        # either contact creates coplanar faces and the light square/step seen
        # in the 3.5.10 field screenshot.
        if abs(rv0-clear_v0)<=_profile_tol:cap_flags.add('BOTTOM')
        if abs(rv1-clear_v1)<=_profile_tol:cap_flags.add('TOP')
        if cap_flags:
            kept=[(f,r) for f,r in zip(pm['faces'],pm['roles'])
                  if not (('TOP' in cap_flags and r=='FRAME_MULLION_VERTICAL_TOP')
                          or ('BOTTOM' in cap_flags and r=='FRAME_MULLION_VERTICAL_BOTTOM'))]
            pm['faces']=[f for f,_r in kept];pm['roles']=[r for _f,r in kept]
            pm['suppressed_contact_caps']=sorted(cap_flags)
        append_profile(pm,pos,rv0,rv1,z_front,'FRAME_MULLION_VERTICAL')

    # W58.30: Source/contact zipper occasionally emits a zero-thickness
    # triangle around float32-identical vertices. It is a numerical zero
    # interval, not an authored architectural panel. Cull ONLY microscopic
    # triangles whose shortest edge is <=10um. The frame quality oracle
    # still checks winding, manifoldness, and every retained polygon.
    _pruned_micro_triangles=[]
    _kept=[]
    for _fi,(_face,_role) in enumerate(zip(faces,roles)):
        _xyz=[tuple(_blender_float32(v) for v in verts[idx]) for idx in _face]
        _dist=[length(sub(_xyz[k],_xyz[(k+1)%len(_face)]))
               for k in range(len(_face))]
        _area2=max(length(cross(sub(_xyz[(k+1)%len(_face)],_xyz[k]),
                                 sub(_xyz[(k+2)%len(_face)],_xyz[k])))
                   for k in range(len(_face)))
        # Delete only physically zero-area source-zipper faces.
        if (min(_dist)<=1e-5 and _area2<=1e-6
                and _role in {'FRAME_EXTERIOR','FRAME_INTERIOR_BACK','FRAME_REVEAL'}):
            _pruned_micro_triangles.append((_fi,_role,min(_dist),_area2))
            continue
        _kept.append((_face,_role))
    if _pruned_micro_triangles:
        faces[:]=[f for f,r in _kept]
        roles[:]=[r for f,r in _kept]

    # 3.5.10 T-junction remapping can retire legacy SVD endpoint vertices.
    # Compact only genuinely unused vertices after every profile is appended,
    # preserving exact 3.5.9 topology when there is nothing to remove.
    used=sorted({i for face in faces for i in face})
    if len(used)!=len(verts):
        remap={old:new for new,old in enumerate(used)}
        verts[:]=[verts[i] for i in used];params[:]=[params[i] for i in used]
        faces[:]=[tuple(remap[i] for i in face) for face in faces]
        for meta in profile_meta:
            if 'vertex_indices' in meta:
                meta['vertex_indices']=[remap[i] for i in meta['vertex_indices'] if i in remap]
                meta['vertex_count']=len(meta['vertex_indices'])
            elif 'vertex_start' in meta:
                old_start=int(meta['vertex_start'])
                if old_start in remap: meta['vertex_start']=remap[old_start]

    result = {
        'verts':verts,'faces':faces,'roles':roles,'vertex_params':params,'xs':xs,'ys':ys,'cells':cells,
        'z_front':z_front,'z_back':z_back,'interior_sign':interior_sign,'exterior_sign':exterior_sign,
        'outer_contact_policy':'HOST_TOPOLOGY_1_TO_1','outer_contact_corners':len(top_outer)+len(bottom_outer),
        'contact_top_s':top_outer,'contact_bottom_s':bottom_outer,
        'contact_left_v':left_outer,'contact_right_v':right_outer,
        'adaptive_classification':topology.get('classification',''),
        'corner_posts':[{'s':float(p.get('s',0.0)),'thickness':float(p.get('thickness',0.0)),'id':str(p.get('id','corner')),
                         'junction_kind':str(p.get('junction_kind','')).upper(),
                         'physical_branch_extent':float(p.get('physical_branch_extent',float(p.get('thickness',0.0))*.5)),
                         'junction_contract':str(p.get('junction_contract','LEGACY_INTERNAL_WIDTH_V1'))} for p in corner_posts_raw],
        'prismatic_profiles':profile_meta,
        'profile_geometry_contract':('ANGLE_TRUE_JUNCTION_V4+CORNER_MULLION_T_JUNCTION_V1+CONSTANT_CROSS_SECTION_V2' if junction_meta else 'CONSTANT_CROSS_SECTION_V2'),
        'corner_mullion_attachment_policy':('FRAME_DEPTH_SIDE_PLANE_T_JUNCTION_V2' if junction_meta else ''),
        'branch_profile_attachment_policy':('BRANCH_CAP_SUPPRESSION_V1' if (_h_side_contacts or _vrows or any(_v_cap_contacts.values())) else ''),
        'branch_profile_h_contact_caps_removed':int(hv_caps_removed),
        'branch_profile_v_contact_caps_suppressed':int(sum(
            (1 if abs(float((r.get('rect') if isinstance(r,dict) else r[4])[2])-clear_v0)<=_profile_tol else 0)
            +(1 if abs(float((r.get('rect') if isinstance(r,dict) else r[4])[3])-clear_v1)<=_profile_tol else 0)
            +len(_v_cap_contacts.get(str(r.get('id','') if isinstance(r,dict) else r[0]),set()))
            for r in (branch_bands or ()) if (r.get('axis') if isinstance(r,dict) else r[1])=='V')),
        'corner_post_anchor_policy':('HARD_JUNCTION_ANGLE_PARALLELOGRAM_V4' if junction_meta else ('HARD_JUNCTION_INTERIOR_QUADRANT_V2' if legacy_corner_posts else '')),
    }
    if topology.get('classification')=='CURVED':
        result['w5830_pruned_micro_triangles']=_pruned_micro_triangles
        result['float32_station_merge_count']=original_station_count-len(station_s)
        result['float32_contact_merge_count']=(original_contact_counts[0]-len(top_outer)+
                                               original_contact_counts[1]-len(bottom_outer))
        result['float32_publication_audit']=_audit_published_curved_frame(result)
    if topology.get('classification')=='CORNER' and junction_meta:
        try:
            from .corner_assembly_core import (_orient_unified_corner_shell,
                                                _repair_overlapping_corner_exterior_triangles)
        except ImportError:
            from corner_assembly_core import (_orient_unified_corner_shell,
                                               _repair_overlapping_corner_exterior_triangles)
        result['corner_exterior_retriangulated']=_repair_overlapping_corner_exterior_triangles(result)
        result['corner_assembly_faces_oriented']=_orient_unified_corner_shell(result)
    return result
