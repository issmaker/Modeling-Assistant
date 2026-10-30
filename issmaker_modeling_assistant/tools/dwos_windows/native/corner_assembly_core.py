"""DWOS 3.7.13 unified, angle-aware CORNER frame assembly.

Pure geometry layer (no bpy).  A CORNER frame is built as one host shell plus
branch-owned H/V profiles connected through explicit junction openings.  The
module intentionally does not create the architectural corner itself; the
field-proven 3.5.9 integrated corner remains the authoritative base host.
"""
from __future__ import annotations

from math import isfinite

try:
    from .adaptive_opening_core import map_svd_local, dot, cross, length, norm, sub
    from .profile_prism_core import branch_profile_basis, branch_mapped_point, branch_id_for_s
except ImportError:
    from adaptive_opening_core import map_svd_local, dot, cross, length, norm, sub
    from profile_prism_core import branch_profile_basis, branch_mapped_point, branch_id_for_s

EPS = 1.0e-8


def _add(a,b): return (a[0]+b[0],a[1]+b[1],a[2]+b[2])
def _mul(a,s): return (a[0]*s,a[1]*s,a[2]*s)
def _mid(a,b): return ((a[0]+b[0])*.5,(a[1]+b[1])*.5,(a[2]+b[2])*.5)


def _normalize_rows(rows):
    out=[]
    for r in rows or ():
        if isinstance(r,dict):
            q=dict(r)
        else:
            q={'id':r[0],'axis':r[1],'position':r[2],'thickness':r[3],
               'rect':r[4],'band':(r[5] if len(r)>5 else r[-1]),
               'branch_id':(r[6] if len(r)>6 else '')}
        q['id']=str(q.get('id',''));q['axis']=str(q.get('axis',''))
        q['branch_id']=str(q.get('branch_id',''))
        q['position']=float(q.get('position',0.0));q['thickness']=float(q.get('thickness',0.0))
        q['rect']=tuple(float(x) for x in q['rect']);q['band']=tuple(float(x) for x in q['band'])
        out.append(q)
    return out


def _host_vec_local(topology,v):
    ax=tuple(topology['axis_x']);ay=tuple(topology['axis_y']);az=tuple(topology['axis_z'])
    vv=tuple(float(x) for x in v)
    return (dot(vv,ax),dot(vv,ay),dot(vv,az))


def _horizontal(v):
    q=(float(v[0]),0.0,float(v[2]))
    return norm(q) if length(q)>EPS else (1.0,0.0,0.0)


def _junction_contract(topology,corner_posts,depth):
    cps=[p for p in (corner_posts or ()) if str(p.get('junction_kind','')).upper() in {'CONVEX','CONCAVE'}]
    js=list(topology.get('corner_junctions') or ())
    if not cps or not js:return None
    p=cps[0];ps=float(p['s']);hx=float(topology['width'])*.5
    target=ps+hx
    j=min(js,key=lambda x:abs(float(x.get('s',target))-target))
    inc=_horizontal(_host_vec_local(topology,j['incoming_tangent']))
    out=_horizontal(_host_vec_local(topology,j['outgoing_tangent']))
    axis_a=tuple(-x for x in inc);axis_b=out
    if abs(dot(axis_a,axis_b))>0.985:
        raise ValueError('Unified CornerAssembly junction too acute')
    return {'s':ps,'depth':float(depth),'axis_a':axis_a,'axis_b':axis_b,'junction':j,
            'side_a_s':ps-float(depth),'side_b_s':ps+float(depth)}


def _corner_point(topology,jc,s,v,d,front_offset,interior_sign):
    """Map a point, snapping the two integrated corner side planes exactly."""
    dep=jc['depth'];ps=jc['s'];tol=max(2e-7,dep*1e-6)
    front=float(front_offset);back=front+int(interior_sign)*dep
    if abs(float(s)-jc['side_a_s'])<=tol:
        hard=tuple(float(x) for x in map_svd_local(topology,ps,float(v),front))
        p=_add(hard,_mul(jc['axis_a'],dep))
        if abs(float(d)-back)<=tol:p=_add(p,_mul(jc['axis_b'],dep))
        return p
    if abs(float(s)-jc['side_b_s'])<=tol:
        hard=tuple(float(x) for x in map_svd_local(topology,ps,float(v),front))
        p=_add(hard,_mul(jc['axis_b'],dep))
        if abs(float(d)-back)<=tol:p=_add(p,_mul(jc['axis_a'],dep))
        return p
    return tuple(float(x) for x in map_svd_local(topology,float(s),float(v),float(d)))


def _face(ids,verts,desired):
    ids=tuple(ids)
    # Find a non-degenerate adjacent triple; quads can have a collinear run.
    best=None
    for k in range(len(ids)):
        a,b,c=(verts[ids[k]],verts[ids[(k+1)%len(ids)]],verts[ids[(k+2)%len(ids)]])
        n=cross(sub(b,a),sub(c,a));area=length(n)
        if best is None or area>best[0]:best=(area,n)
    if best is None or best[0]<=1e-12:raise ValueError('Unified CornerAssembly produced zero-area face')
    if dot(best[1],desired)<0:ids=tuple(reversed(ids))
    return ids


def _covered(mid,intervals,tol=2e-7):
    return any(float(a)-tol<=mid<=float(b)+tol for a,b in intervals)


def _contact_graph(rows):
    """Return explicit parent-surface openings derived from the Zone tree layout."""
    h=[r for r in rows if r['axis']=='H'];v=[r for r in rows if r['axis']=='V']
    h_open={r['id']:{'TOP':[],'BOTTOM':[]} for r in h}
    v_open={r['id']:{'SIDE_POS':[],'SIDE_NEG':[]} for r in v}
    tol=2e-7
    edges=[]
    for hr in h:
        hb=hr['band'];bid=hr['branch_id']
        for vr in v:
            if vr['branch_id']!=bid:continue
            vb=vr['band'];vrect=vr['rect'];vid=vr['id'];hid=hr['id']
            # V parent -> H child: H endpoint touches a V side wall.
            if abs(hb[0]-vb[1])<=tol:
                v_open[vid]['SIDE_POS'].append((hb[2],hb[3]));edges.append((vid,hid,'V_SIDE_POS'))
            if abs(hb[1]-vb[0])<=tol:
                v_open[vid]['SIDE_NEG'].append((hb[2],hb[3]));edges.append((vid,hid,'V_SIDE_NEG'))
            # H parent -> V child: V endpoint touches H top/bottom wall.
            if abs(vrect[2]-hb[3])<=tol:
                h_open[hid]['TOP'].append((vb[0],vb[1]));edges.append((hid,vid,'H_TOP'))
            if abs(vrect[3]-hb[2])<=tol:
                h_open[hid]['BOTTOM'].append((vb[0],vb[1]));edges.append((hid,vid,'H_BOTTOM'))
    return h_open,v_open,edges


def _station_cuts(topology,s0,s1,extra=()):
    hx=float(topology['width'])*.5
    vals=[float(s0),float(s1)]+[float(x) for x in extra]
    for st in topology.get('stations') or ():
        ss=float(st['s'])-hx
        if s0+EPS<ss<s1-EPS:vals.append(ss)
    return sorted(set(round(x,10) for x in vals if s0-EPS<=x<=s1+EPS))


def _branch_postless_point(topology,branch_id,s,v,d):
    """Rigid branch depth axis; avoid SVD miter interpolation over the offset.

    Branch H/V contact profiles must share one plane for nonzero Frame Offset.
    The hard junction itself is joined separately by the miter connector.
    """
    p0=tuple(float(x) for x in map_svd_local(topology,float(s),float(v),0.0))
    normal=tuple(branch_profile_basis(topology,branch_id,s,0.0)['depth_axis'])
    return _add(p0,_mul(normal,float(d)))


def _build_h_profile(topology,row,depth,front_offset,interior_sign,jc,openings,*,cap_at_junction=True,postless=False):
    x0,x1,y0,y1=row['band'];extra=[]
    for a,b in openings['TOP']+openings['BOTTOM']:extra.extend((a,b))
    cuts=_station_cuts(topology,x0,x1,extra)
    zf=float(front_offset);zb=zf+int(interior_sign)*float(depth)
    verts=[];vid={}
    def point(s,v,d):
        tol=max(2e-7,float(depth)*1e-6)
        if jc is not None and (abs(float(s)-jc['side_a_s'])<=tol or abs(float(s)-jc['side_b_s'])<=tol):
            return _corner_point(topology,jc,s,v,d,zf,interior_sign)
        if postless:
            junc=tuple(topology.get('corner_junctions') or ())
            hs=float(topology['width'])*.5
            if any(abs(float(s)-(float(j['s'])-hs))<=tol for j in junc):
                return tuple(float(x) for x in map_svd_local(topology,float(s),float(v),float(d)))
            return _branch_postless_point(topology,row['branch_id'],s,v,d)
        return branch_mapped_point(topology,row['branch_id'],s,v,d,zf)
    def vtx(s,v,d):
        key=(round(float(s),10),round(float(v),10),round(float(d),10))
        if key not in vid:
            vid[key]=len(verts);verts.append(point(float(s),float(v),float(d)))
        return vid[key]
    faces=[];roles=[];up=(0.0,1.0,0.0)
    basis=branch_profile_basis(topology,row['branch_id'],(x0+x1)*.5,zf);inter_axis=tuple(basis['depth_axis'])
    exterior=tuple(-int(interior_sign)*x for x in inter_axis);inside=tuple(int(interior_sign)*x for x in inter_axis)
    for a,b in zip(cuts,cuts[1:]):
        if b-a<=EPS:continue
        fl0=vtx(a,y0,zf);fl1=vtx(b,y0,zf);fu1=vtx(b,y1,zf);fu0=vtx(a,y1,zf)
        bl0=vtx(a,y0,zb);bl1=vtx(b,y0,zb);bu1=vtx(b,y1,zb);bu0=vtx(a,y1,zb)
        mid=(a+b)*.5
        faces.append(_face((fl0,fl1,fu1,fu0),verts,exterior));roles.append('FRAME_MULLION_HORIZONTAL')
        faces.append(_face((bl1,bl0,bu0,bu1),verts,inside));roles.append('FRAME_MULLION_HORIZONTAL_BACK')
        if not _covered(mid,openings['TOP']):
            faces.append(_face((fu0,fu1,bu1,bu0),verts,up));roles.append('FRAME_MULLION_HORIZONTAL_TOP')
        if not _covered(mid,openings['BOTTOM']):
            faces.append(_face((bl0,bl1,fl1,fl0),verts,tuple(-x for x in up)));roles.append('FRAME_MULLION_HORIZONTAL_BOTTOM')
    junction_ring=None
    if jc is None:
        junctions=tuple(topology.get('corner_junctions') or ())
        hs=float(topology['width'])*0.5
        for j in junctions:
            js=float(j['s'])-hs
            if abs(x0-js)<2e-7 or abs(x1-js)<2e-7:
                f0=vtx(js,y0,zf);f1=vtx(js,y1,zf)
                b0=vtx(js,y0,zb);b1=vtx(js,y1,zb)
                junction_ring=(f0,f1,b0,b1)
                if cap_at_junction:
                    side= -1.0 if abs(x0-js)<2e-7 else 1.0
                    desired=tuple(side*v for v in basis['width_axis'])
                    faces.append(_face((f0,f1,b1,b0),verts,desired))
                    roles.append('FRAME_MULLION_HORIZONTAL_CORNER_END')
    return {'verts':verts,'faces':faces,'roles':roles,'contact_coords':[(key,verts[idx]) for key,idx in vid.items()], 'junction_ring':junction_ring,'profile_id':row['id'],'kind':'FRAME_MULLION_HORIZONTAL',
            'branch_id':row['branch_id'],'band':row['band'],'openings':openings,
            'width':float(row['thickness']),'depth':float(depth),'front_offset':float(front_offset),
            'basis':basis,'corner_shape_propagation':False}


def _build_v_profile(topology,row,depth,front_offset,interior_sign,openings,*,postless=False):
    """Build a regular V mullion in the orthonormal basis of its own branch.

    Critical 3.5.13 rule: the body is never SVD/miter warped.  Only its origin
    comes from the S/V/D host mapping; width/depth axes come from branch truth.
    """
    x0,x1,y0,y1=row['band'];extra=[]
    for a,b in openings['SIDE_POS']+openings['SIDE_NEG']:extra.extend((a,b))
    cuts=sorted(set([round(y0,10),round(y1,10)]+[round(float(x),10) for x in extra if y0-EPS<=x<=y1+EPS]))
    zf=float(front_offset);dep=float(depth);zb=zf+int(interior_sign)*dep
    basis=branch_profile_basis(topology,row['branch_id'],row['position'],zf)
    origin=(_branch_postless_point(topology,row['branch_id'],row['position'],0.0,zf)
            if postless else tuple(basis['origin']))
    u=tuple(basis['width_axis']);up=tuple(basis['up_axis']);d=tuple(basis['depth_axis'])
    center_s=float(row['position'])
    verts=[];vid={}
    def vtx(s,v,back=False):
        key=(round(float(s),10),round(float(v),10),bool(back))
        if key not in vid:
            p=_add(origin,_mul(u,float(s)-center_s))
            p=_add(p,_mul(up,float(v)))
            if back:p=_add(p,_mul(d,int(interior_sign)*dep))
            vid[key]=len(verts);verts.append(tuple(float(x) for x in p))
        return vid[key]
    faces=[];roles=[]
    exterior=_mul(d,-int(interior_sign));inside=_mul(d,int(interior_sign))
    for a,b in zip(cuts,cuts[1:]):
        if b-a<=EPS:continue
        f00=vtx(x0,a,False);f10=vtx(x1,a,False);f11=vtx(x1,b,False);f01=vtx(x0,b,False)
        b00=vtx(x0,a,True);b10=vtx(x1,a,True);b11=vtx(x1,b,True);b01=vtx(x0,b,True)
        faces.append(_face((f00,f10,f11,f01),verts,exterior));roles.append('FRAME_MULLION_VERTICAL')
        faces.append(_face((b10,b00,b01,b11),verts,inside));roles.append('FRAME_MULLION_VERTICAL_BACK')
        mid=(a+b)*.5
        if not _covered(mid,openings['SIDE_POS']):
            faces.append(_face((f10,b10,b11,f11),verts,u));roles.append('FRAME_MULLION_VERTICAL_SIDE_POS')
        if not _covered(mid,openings['SIDE_NEG']):
            faces.append(_face((b00,f00,f01,b01),verts,tuple(-x for x in u)));roles.append('FRAME_MULLION_VERTICAL_SIDE_NEG')
    # Measure the exact body invariants before welding/contact integration.
    straight=0.0;section=0.0
    # All generated points at identical (s, back) differ only along `up`; this
    # is stronger than a visual test and catches any future corner propagation.
    groups={}
    for (ss,vv,bb),idx in vid.items():groups.setdefault((ss,bb),[]).append((vv,idx))
    for group in groups.values():
        if len(group)<2:continue
        group=sorted(group);p0=verts[group[0][1]];p1=verts[group[-1][1]];e=sub(p1,p0);le=length(e)
        if le>EPS:
            q=tuple(x/le for x in e);straight=max(straight,min(length(sub(q,up)),length(_add(q,up))))
    widths=[];depths=[]
    for vv in cuts:
        try:
            fl=verts[vid[(round(float(x0),10),round(float(vv),10),False)]]
            fr=verts[vid[(round(float(x1),10),round(float(vv),10),False)]]
            bl=verts[vid[(round(float(x0),10),round(float(vv),10),True)]]
            br=verts[vid[(round(float(x1),10),round(float(vv),10),True)]]
        except KeyError:
            continue
        widths.extend((length(sub(fr,fl)),length(sub(br,bl))))
        depths.extend((length(sub(bl,fl)),length(sub(br,fr))))
    if widths and depths:
        section=max(max(widths)-min(widths),max(depths)-min(depths))
    orth=max(abs(dot(u,d)),abs(dot(u,up)),abs(dot(d,up)))
    angle=float(basis.get('branch_angle_error_deg',0.0))
    if straight>1e-8 or orth>1e-8 or angle>1e-5:
        raise ValueError(f'CORNER V profile basis invariant failed: straight={straight} orth={orth} branch_angle={angle}')
    invariants={'straightness_error':float(straight),'section_error_m':float(section),
                'orthogonality_error':float(orth),'branch_angle_error_deg':angle}
    return {'verts':verts,'faces':faces,'roles':roles,'contact_coords':[((ss,vv,zb if bb else zf),verts[idx]) for (ss,vv,bb),idx in vid.items()], 'profile_id':row['id'],'kind':'FRAME_MULLION_VERTICAL',
            'branch_id':row['branch_id'],'band':row['band'],'openings':openings,
            'width':float(row['thickness']),'depth':float(depth),'front_offset':float(front_offset),
            'basis':basis,'invariants':invariants,'corner_shape_propagation':False}


def _remove_host_reveal_openings(mesh,rows,tol=3e-7):
    """Open base-frame reveal patches exactly where branch profiles enter."""
    params=mesh.get('vertex_params') or []
    keep_f=[];keep_r=[];removed=0
    for f,r in zip(mesh['faces'],mesh['roles']):
        if r!='FRAME_REVEAL' or not params:
            keep_f.append(f);keep_r.append(r);continue
        pp=[params[i] for i in f]
        ss=[float(p[0]) for p in pp];vv=[float(p[1]) for p in pp]
        drop=False
        for row in rows:
            x0,x1,y0,y1=row['band']
            if row['axis']=='H':
                for sx in (x0,x1):
                    if max(abs(s-sx) for s in ss)<=tol and min(vv)>=y0-tol and max(vv)<=y1+tol:
                        drop=True;break
            else:
                for vy in (y0,y1):
                    if max(abs(v-vy) for v in vv)<=tol and min(ss)>=x0-tol and max(ss)<=x1+tol:
                        drop=True;break
            if drop:break
        if drop:removed+=1
        else:keep_f.append(f);keep_r.append(r)
    mesh['faces']=keep_f;mesh['roles']=keep_r
    return removed



def _orthogonalize_base_inner_back(mesh,topology,frame_width,depth,front_offset,interior_sign,jc):
    """Move only the clear-opening BACK rim onto branch planes.

    Outer Host contact stays untouched.  The small transition therefore lives
    inside the main frame belt, while the visible clear opening and all regular
    mullions share one physical branch plane.
    """
    params=mesh.get('vertex_params') or [];hy=float(topology['height'])*.5;hx=float(topology['width'])*.5
    fw=float(frame_width);back=float(front_offset)+int(interior_sign)*float(depth)
    li=-hx+fw;ri=hx-fw;bi=-hy+fw;ti=hy-fw;tol=max(3e-7,float(depth)*1e-6)
    moved=0
    for i,p in enumerate(params):
        s,v,d=(float(p[0]),float(p[1]),float(p[2]))
        if abs(d-back)>tol:continue
        on_rim=(abs(s-li)<=tol or abs(s-ri)<=tol or abs(v-bi)<=tol or abs(v-ti)<=tol)
        if not on_rim:continue
        # Integrated square owns the junction envelope and is already exact.
        if jc is not None and jc['side_a_s']-tol < s < jc['side_b_s']+tol:continue
        if jc is None and any(abs(s-(float(j['s'])-hx))<=tol for j in (topology.get('corner_junctions') or ())):
            continue  # Postless miter: keep shared corner vertex exactly on Host mapping.
        bid=branch_id_for_s(topology,s)
        if not bid:continue
        np=branch_mapped_point(topology,bid,s,v,back,front_offset)
        if length(sub(mesh['verts'][i],np))>tol:
            mesh['verts'][i]=np;moved+=1
    return moved

def _append_profile(mesh,profile):
    off=len(mesh['verts']);count=len(profile['verts']);mesh['verts'].extend(profile['verts'])
    mesh.setdefault('vertex_params',[]).extend([(0.0,0.0,0.0)]*count)
    mesh['faces'].extend(tuple(off+i for i in f) for f in profile['faces']);mesh['roles'].extend(profile['roles'])
    return {'profile_id':profile['profile_id'],'kind':profile['kind'],'branch_id':profile['branch_id'],
            'vertex_start':off,'vertex_count':count,'_old_vertex_indices':list(range(off,off+count)),
            'band':profile['band'],'openings':profile['openings'],'width':profile.get('width'),
            'depth':profile.get('depth'),'front_offset':profile.get('front_offset',0.0),
            'basis':profile.get('basis'),'_junction_ring_indices':([off+i for i in profile['junction_ring']] if profile.get('junction_ring') else None),'invariants':profile.get('invariants'),
            'corner_shape_propagation':profile.get('corner_shape_propagation',False)}


def _weld(mesh,tol=2e-7):
    verts=mesh['verts'];old_params=list(mesh.get('vertex_params') or [(0.0,0.0,0.0)]*len(verts))
    bucket={};remap={};new=[];new_params=[]
    def key(p):return tuple(int(round(float(x)/tol)) for x in p)
    for i,p in enumerate(verts):
        k=key(p);found=None
        for dx in (-1,0,1):
            for dy in (-1,0,1):
                for dz in (-1,0,1):
                    q=bucket.get((k[0]+dx,k[1]+dy,k[2]+dz))
                    if q is not None and length(sub(new[q],p))<=tol:
                        found=q;break
                if found is not None:break
            if found is not None:break
        if found is None:
            found=len(new);new.append(tuple(float(x) for x in p));new_params.append(old_params[i]);bucket[k]=found
        remap[i]=found
    new_faces=[];new_roles=[];seen={};dedup=0;deg=0
    for f,r in zip(mesh['faces'],mesh['roles']):
        nf=tuple(remap[i] for i in f)
        # remove consecutive duplicates after welding
        compact=[]
        for x in nf:
            if not compact or compact[-1]!=x:compact.append(x)
        if len(compact)>1 and compact[0]==compact[-1]:compact.pop()
        if len(set(compact))<3:deg+=1;continue
        sig=tuple(sorted(set(compact)))
        if sig in seen:
            dedup+=1;continue
        seen[sig]=r;new_faces.append(tuple(compact));new_roles.append(r)
    mesh['verts']=new;mesh['faces']=new_faces;mesh['roles']=new_roles
    mesh['vertex_params']=new_params
    return ({'vertices_merged':len(verts)-len(new),'duplicate_faces_removed':dedup,'degenerate_faces_removed':deg},remap)


def _component_count(mesh):
    faces=mesh['faces'];verts_used={i for f in faces for i in f}
    if not verts_used:return 0
    adj={i:set() for i in verts_used}
    for f in faces:
        for a,b in zip(f,f[1:]+f[:1]):adj[a].add(b);adj[b].add(a)
    seen=set();count=0
    for root in verts_used:
        if root in seen:continue
        count+=1;stack=[root];seen.add(root)
        while stack:
            u=stack.pop()
            for v in adj[u]:
                if v not in seen:seen.add(v);stack.append(v)
    return count


def _stitch_corner_inner_belt_t_junctions(mesh, topology, frame_width):
    """Stitch collinear T-junctions at the corner's accepting belt contour.

    The legacy integrated corner emits a long edge while the subdivided reveal
    has two shorter edges on the exact same line. This is an open topological
    seam, NOT a triangular surface hole. Split the long edge at existing
    boundary vertices. No vertex moves and no new face area is invented.
    """
    from collections import defaultdict
    target=float(topology['height'])*.5-float(frame_width)
    verts=mesh['verts'];tolerance=2e-7;total=0
    for _pass in range(5):
        owners=defaultdict(list)
        for fi,f in enumerate(mesh['faces']):
            for idx,(a,b) in enumerate(zip(f,f[1:]+f[:1])):
                owners[tuple(sorted((a,b)))].append((fi,idx,a,b))
        open_edges=[entries[0] for entries in owners.values() if len(entries)==1]
        rim_edges=[];rim_vertices=set()
        for fi,pos,a,b in open_edges:
            if abs(verts[a][1]-verts[b][1])>tolerance:continue
            if abs(abs(verts[a][1])-target)>tolerance:continue
            rim_edges.append((fi,pos,a,b))
            rim_vertices.update((a,b))
        proposals={}
        for fi,pos,a,b in rim_edges:
            v0=verts[a];v1=verts[b];delta=sub(v1,v0);sq=dot(delta,delta)
            if sq<=tolerance*tolerance:continue
            splits=[]
            for c in rim_vertices-{a,b}:
                v=verts[c];rel=sub(v,v0);u=dot(rel,delta)/sq
                if not (1e-7<u<1.0-1e-7):continue
                projection=tuple(v0[k]+u*delta[k] for k in range(3))
                if length(sub(projection,v))<=tolerance:
                    splits.append((u,c))
            if splits:
                splits.sort()
                proposals[(fi,pos)]=[c for _,c in splits]
        if not proposals:break
        by_face=defaultdict(dict)
        for (fi,pos),extra in proposals.items():by_face[fi][pos]=extra
        for fi,bypos in by_face.items():
            original=mesh['faces'][fi];extended=[]
            for pos,idx in enumerate(original):
                extended.append(idx)
                extended.extend(bypos.get(pos,()))
            if len(set(extended))!=len(extended):
                raise ValueError('CORNER seam stitch would repeat vertex in a polygon')
            # Keep valid first-triangle normals even when two adjacent points
            # lie on a single straight edge.
            best=0.0;rotation=0
            for k in range(len(extended)):
                a,b,c=(verts[extended[(k+i)%len(extended)]] for i in range(3))
                area=length(cross(sub(b,a),sub(c,a)))
                if area>best:best=area;rotation=k
            if best<=1e-12:
                raise ValueError('CORNER seam stitch would create degenerate face')
            mesh['faces'][fi]=tuple(extended[rotation:]+extended[:rotation])
        total+=sum(len(x) for x in proposals.values())
    return total


def _repair_overlapping_corner_exterior_triangles(mesh, *, tolerance=5e-6):
    """Retessellate corner band overlaps caused by intermediate Host stations.

    On a real six-metre CORNER fixture a FRAME_EXTERIOR triangle incorrectly
    spans from the junction to the *second* inner-rim station, while another
    exterior triangle already owns that segment. Together with FRAME_REVEAL
    this creates a three-face nonmanifold edge. Trim just the extra triangle
    back to the existing outer-rim vertex at the first inner station.

    No points are moved, no faces removed, and Host corner vertices remain
    authored. Only a proven EXTERIOR+EXTERIOR+REVEAL overlap is eligible.
    Unknown configurations fail the original manifold validator unchanged.
    """
    from collections import defaultdict
    from math import dist
    verts=mesh['verts'];params=mesh.get('vertex_params') or []
    if len(params)!=len(verts):return 0
    faces=mesh['faces'];roles=mesh['roles'];owners=defaultdict(list)
    for fi,f in enumerate(faces):
        for a,b in zip(f,f[1:]+f[:1]):
            owners[tuple(sorted((a,b)))].append(fi)
    repairs=[]
    for edge,fs in sorted(owners.items()):
        if len(fs)!=3 or sorted(roles[fi] for fi in fs)!=['FRAME_EXTERIOR','FRAME_EXTERIOR','FRAME_REVEAL']:
            continue
        a,b=edge; lo,hi=sorted((float(params[a][0]),float(params[b][0])))
        ext=[fi for fi in fs if roles[fi]=='FRAME_EXTERIOR']
        if any(len(faces[fi])!=3 for fi in ext):continue
        thirds=[next((v for v in faces[fi] if v not in edge),None) for fi in ext]
        if any(v is None for v in thirds):continue
        low=[(fi,v) for fi,v in zip(ext,thirds) if float(params[v][0])<lo-tolerance]
        high=[(fi,v) for fi,v in zip(ext,thirds) if float(params[v][0])>hi+tolerance]
        if len(low)!=1 or len(high)!=1:continue
        fi,outer=low[0]
        # Find the EXISTING outer-rim vertex at the first inner station.
        op=params[outer]
        possible=[i for i,p in enumerate(params) if i not in (outer,a,b)
                  and abs(float(p[0])-lo)<tolerance
                  and abs(float(p[1])-float(op[1]))<tolerance
                  and abs(float(p[2])-float(op[2]))<tolerance]
        if len(possible)!=1:continue
        outer_target=possible[0]
        far=a if float(params[a][0])>float(params[b][0]) else b
        nf=tuple(outer_target if v==far else v for v in faces[fi])
        if len(set(nf))!=3:continue
        oldcoords=[verts[v] for v in faces[fi]]
        newcoords=[verts[v] for v in nf]
        def area2(cs):
            u=tuple(cs[1][j]-cs[0][j] for j in range(3))
            v=tuple(cs[2][j]-cs[0][j] for j in range(3))
            c=(u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
            return sum(x*x for x in c)
        if area2(newcoords)<1e-18 or area2(oldcoords)<1e-18:continue
        # Preserve the exterior-anchor orientation against adjacent existing
        # faces.  The replacement swaps the supporting triangle to the other
        # side of its original edge and therefore may need reversed winding.
        def orientation_score(poly):
            score=0
            for x,y in zip(poly,poly[1:]+poly[:1]):
                for fj,fjpoly in enumerate(faces):
                    if fj==fi:continue
                    for aa,bb in zip(fjpoly,fjpoly[1:]+fjpoly[:1]):
                        if (aa,bb)==(x,y):score-=1
                        elif (aa,bb)==(y,x):score+=1
            return score
        if orientation_score(tuple(reversed(nf)))>orientation_score(nf):
            nf=tuple(reversed(nf))
        repairs.append((fi,nf,edge))
    if not repairs:return 0
    old=[(fi,faces[fi]) for fi,_,_ in repairs]
    for fi,nf,_ in repairs:faces[fi]=nf
    # Fail-closed: a local retriangulation may NOT create a new over-owned edge.
    incidence=defaultdict(int)
    for f in faces:
        for a,b in zip(f,f[1:]+f[:1]):incidence[tuple(sorted((a,b)))]+=1
    if any(v>2 for v in incidence.values()):
        for fi,of in old:faces[fi]=of
        return 0
    # Some original exterior patch triangles on the newly exposed outer edge
    # have the same directed edge as the corrected patch. Reorientation of
    # ONLY these explicitly identified faces is allowed by the shell solver.
    allowed=set()
    for fi,nf,_ in repairs:
        for a,b in zip(nf,nf[1:]+nf[:1]):
            for fj,f in enumerate(faces):
                if fj==fi or roles[fj]!='FRAME_EXTERIOR':continue
                if any((x,y)==(a,b) for x,y in zip(f,f[1:]+f[:1])):
                    allowed.add(fj)
    mesh['corner_allowed_exterior_reorientation']=sorted(allowed)
    mesh['corner_exterior_retriangulated']=len(repairs)
    mesh['corner_exterior_retriangulated_edges']=[list(e) for _,_,e in repairs]
    return len(repairs)


def _orient_unified_corner_shell(mesh):
    """Propagate opposite-edge winding from the existing Host frame surfaces.

    This is strictly topology correction, not geometry replacement: no point,
    polygon set or semantic role changes. A non-orientable or non-manifold mesh
    remains an error rather than being silently published.
    """
    from collections import defaultdict
    owners=defaultdict(list)
    faces=mesh['faces'];roles=mesh['roles']
    for fi,f in enumerate(faces):
        for a,b in zip(f,f[1:]+f[:1]):
            owners[tuple(sorted((a,b)))].append((fi,a<b))
    adjacency=defaultdict(list)
    for edge,uses in owners.items():
        if len(uses)>2:
            raise ValueError(f'CORNER shell non-manifold edge {edge}')
        if len(uses)==2:
            (fi,fa),(fj,fb)=uses
            if fi==fj:raise ValueError('CORNER shell face repeats an edge')
            same=(fa==fb)
            adjacency[fi].append((fj,same));adjacency[fj].append((fi,same))
    flips={}
    for seed in range(len(faces)):
        if seed in flips:continue
        # Determine each edge-connected component's existing exterior anchor.
        component={seed};stack=[seed]
        while stack:
            fi=stack.pop()
            for fj,_ in adjacency[fi]:
                if fj not in component:component.add(fj);stack.append(fj)
        anchors=sorted(fi for fi in component if roles[fi] in {'FRAME_EXTERIOR','FRAME_INTERIOR_BACK'})
        root=anchors[0] if anchors else min(component)
        flips[root]=False;stack=[root]
        while stack:
            fi=stack.pop()
            for fj,same in adjacency[fi]:
                needed=bool(flips[fi]^same)
                if fj in flips:
                    if flips[fj]!=needed:
                        raise ValueError('CORNER shell winding constraints conflict')
                else:
                    flips[fj]=needed;stack.append(fj)
        allowed=set(mesh.get('corner_allowed_exterior_reorientation',()))
        for fi in anchors:
            if flips[fi] and fi not in allowed:
                raise ValueError('CORNER shell reference exterior/back winding contradicts geometry')
    for fi,needs_flip in flips.items():
        if needs_flip:faces[fi]=tuple(reversed(faces[fi]))
    return sum(flips.values())


def assemble_unified_corner(base_mesh,topology,frame_width,depth,branch_rows,corner_posts,
                            *,front_offset=0.0,interior_sign=-1):
    """Attach all CORNER branch profiles to a subdivided field-proven base frame."""
    rows=_normalize_rows(branch_rows)
    if not rows:return base_mesh
    if topology.get('classification')!='CORNER':raise ValueError('Unified CornerAssembly only supports CORNER')
    jc=_junction_contract(topology,corner_posts,depth)
    if jc is None:raise ValueError('Unified CornerAssembly requires an integrated two-branch angle junction')
    mesh={k:(list(v) if isinstance(v,list) else v) for k,v in base_mesh.items()}
    mesh['verts']=list(base_mesh['verts']);mesh['faces']=list(base_mesh['faces']);mesh['roles']=list(base_mesh['roles'])
    mesh['vertex_params']=list(base_mesh.get('vertex_params') or [(0.0,0.0,0.0)]*len(mesh['verts']))
    rim_moved=_orthogonalize_base_inner_back(mesh,topology,frame_width,depth,front_offset,interior_sign,jc)
    host_removed=_remove_host_reveal_openings(mesh,rows)
    h_open,v_open,edges=_contact_graph(rows)
    pmeta=[];contact_maps=[]
    # Deterministic parent-first order is not required geometrically; H first
    # makes debug dumps easier to compare with the zone tree.
    for r in rows:
        if r['axis']=='H':
            profile=_build_h_profile(topology,r,depth,front_offset,interior_sign,jc,h_open[r['id']])
            pmeta.append(_append_profile(mesh,profile))
            contact_maps.extend(profile.get('contact_coords') or ())
    for r in rows:
        if r['axis']=='V':
            profile=_build_v_profile(topology,r,depth,front_offset,interior_sign,v_open[r['id']])
            pmeta.append(_append_profile(mesh,profile))
            contact_maps.extend(profile.get('contact_coords') or ())
    # Angle-agnostic contact reconciliation: after mapping a CORNER to the
    # physical facade planes, the host's accepting clear-opening rim and a
    # branch-owned V mullion must share the same vertices even when the main
    # frame has a positive/negative front offset. Only accepting inner rim
    # positions are adjusted; the outer Host weld boundary never moves.
    from math import dist as _dist
    rows_by_param = {}
    cell = 1e-5
    for pp, xyz in contact_maps:
        k=tuple(int(round(float(q)/cell)) for q in pp)
        rows_by_param.setdefault(k,[]).append((pp,xyz))
    hx=float(topology['width'])*.5;hy=float(topology['height'])*.5
    fw=float(frame_width);tol=max(3e-7,float(depth)*1e-6)
    aligned=0
    for i,p in enumerate(mesh['vertex_params'][:len(base_mesh['verts'])]):
        ss,vv,dd=(float(q) for q in p)
        if not (abs(abs(vv)-(hy-fw))<=tol or abs(abs(ss)-(hx-fw))<=tol):
            continue
        key=tuple(int(round(q/cell)) for q in (ss,vv,dd))
        candidates=[]
        for ka in (-1,0,1):
            for kb in (-1,0,1):
                for kc in (-1,0,1):
                    for pp,xyz in rows_by_param.get((key[0]+ka,key[1]+kb,key[2]+kc),()):
                        if max(abs(float(x)-float(y)) for x,y in zip(pp,(ss,vv,dd)))<=tol:
                            candidates.append(xyz)
        if not candidates:continue
        target=candidates[0]
        if any(_dist(target,q)>tol for q in candidates[1:]):
            raise ValueError('CORNER joint has inconsistent profile contact coordinates')
        if _dist(mesh['verts'][i],target)>tol:
            mesh['verts'][i]=target;aligned+=1
    weld,remap=_weld(mesh)
    seams_closed=_stitch_corner_inner_belt_t_junctions(mesh,topology,frame_width)
    faces_oriented=_orient_unified_corner_shell(mesh)
    for meta in pmeta:
        ids=[]
        for old in meta.pop('_old_vertex_indices',[]):
            ni=remap.get(old)
            if ni is not None and ni not in ids:ids.append(ni)
        meta['vertex_indices']=ids;meta['vertex_count']=len(ids)
        if ids:meta['vertex_start']=min(ids)
    components=_component_count(mesh)
    mesh['prismatic_profiles']=list(base_mesh.get('prismatic_profiles') or [])+pmeta
    mesh['profile_geometry_contract']='ANGLE_TRUE_CORNER_ASSEMBLY_V3'
    mesh['corner_mullion_attachment_policy']='UNIFIED_CORNER_JUNCTION_GRAPH_V1'
    mesh['branch_profile_attachment_policy']='ORTHOGONAL_BRANCH_PROFILE_AND_WELD_V2'
    mesh['corner_assembly_host_faces_removed']=int(host_removed)
    mesh['corner_assembly_branch_back_rim_vertices_moved']=int(rim_moved+aligned)
    mesh['corner_assembly_graph_edges']=[tuple(x) for x in edges]
    mesh['corner_assembly_graph_edge_count']=len(edges)
    mesh['corner_assembly_profile_count']=len(rows)
    mesh['corner_assembly_components']=components
    mesh['corner_assembly_weld']=weld
    mesh['corner_assembly_closed_belt_seams']=seams_closed
    mesh['corner_assembly_faces_oriented']=faces_oriented
    vmeta=[m for m in pmeta if m.get('kind')=='FRAME_MULLION_VERTICAL']
    def _mx(key):
        vals=[float((m.get('invariants') or {}).get(key,0.0)) for m in vmeta]
        return max(vals) if vals else 0.0
    mesh['corner_v_profile_policy']='BRANCH_ORTHONORMAL_BODY_LOCAL_JUNCTION_V1'
    mesh['corner_v_profile_count']=len(vmeta)
    mesh['corner_v_max_branch_angle_error_deg']=_mx('branch_angle_error_deg')
    mesh['corner_v_max_straightness_error']=_mx('straightness_error')
    mesh['corner_v_max_section_error_m']=_mx('section_error_m')
    mesh['corner_v_max_orthogonality_error']=_mx('orthogonality_error')
    mesh['corner_v_corner_shape_propagation']=False
    if (mesh['corner_v_max_branch_angle_error_deg']>1e-5 or mesh['corner_v_max_straightness_error']>1e-8
            or mesh['corner_v_max_section_error_m']>1e-8 or mesh['corner_v_max_orthogonality_error']>1e-8):
        raise ValueError('Unified CornerAssembly V-profile invariant gate failed')
    mesh['corner_post_anchor_policy']='HARD_JUNCTION_ANGLE_PARALLELOGRAM_V4'
    if components!=1:
        raise ValueError(f'Unified CornerAssembly is not one connected frame component: {components}')
    return mesh


def assemble_postless_corner(base_mesh, topology, frame_width, depth, branch_rows,
                             *, front_offset=0.0, interior_sign=-1):
    """Build a genuine NO-POST CORNER frame from branch-connected profiles.

    The hard-corner ribbon stays open (no structural separator or cosmetic cap).
    The inner-back belt is fitted to branch planes, then mullions are stitched
    into the base graph. This never changes the ON integrated-junction builder.
    """
    if topology.get('classification')!='CORNER':
        raise ValueError('Postless assembly requires CORNER')
    if len(topology.get('corner_junctions') or ())!=1:
        raise ValueError('Postless assembly requires one hard corner')
    rows=_normalize_rows(branch_rows)
    mesh={k:(list(v) if isinstance(v,list) else v) for k,v in base_mesh.items()}
    mesh['verts']=list(base_mesh['verts'])
    mesh['faces']=list(base_mesh['faces'])
    mesh['roles']=list(base_mesh['roles'])
    mesh['vertex_params']=list(base_mesh['vertex_params'])
    moved=_orthogonalize_base_inner_back(mesh,topology,frame_width,depth,front_offset,interior_sign,None)
    removed=_remove_host_reveal_openings(mesh,rows)
    h_open,v_open,edges=_contact_graph(rows)
    # Join two H profiles arriving at the SAME hard corner and height band as
    # one mitred junction. Independent caps at both sides create edge incidence
    # 3 and are not valid mesh topology. Unmatched H receives one end cap.
    half=float(topology['width'])*.5
    junction_s=float(topology['corner_junctions'][0]['s'])-half
    arriving={}
    for r in rows:
        if r['axis']!='H':continue
        x0,x1,y0,y1=r['band']
        if abs(x0-junction_s)<2e-7 or abs(x1-junction_s)<2e-7:
            arriving.setdefault((round(y0,7),round(y1,7)),[]).append(r['id'])
    paired_ids={r for ids in arriving.values() if len(ids)==2 for r in ids}
    pmeta=[];contact_maps=[]
    for r in rows:
        if r['axis']=='H':
            profile=_build_h_profile(topology,r,depth,front_offset,interior_sign,None,h_open[r['id']],
                                     cap_at_junction=r['id'] not in paired_ids,postless=True)
            pmeta.append(_append_profile(mesh,profile))
            contact_maps.extend(profile.get('contact_coords') or ())
    for r in rows:
        if r['axis']=='V':
            profile=_build_v_profile(topology,r,depth,front_offset,interior_sign,v_open[r['id']],postless=True)
            pmeta.append(_append_profile(mesh,profile))
            contact_maps.extend(profile.get('contact_coords') or ())
    # At nonzero front offset, the SVD corner miter interpolation shifts the
    # base inner rim relative to the rigid branch-owned profile basis. Snap
    # only accepting rim vertices (never the outer Host contact) to the precise
    # profile end. Preserve a single exact coordinate before the global weld.
    contacts={}
    # Geometry library rounds profile station coordinates when subdividing.
    # Equality/hash rounding is not a robust contact test at halfway bins.
    step=1.0e-5
    def _contact_key(param):return tuple(int(round(float(q)/step)) for q in param)
    for pp,pt in contact_maps:
        contacts.setdefault(_contact_key(pp),[]).append((tuple(float(q) for q in pp),tuple(float(q) for q in pt)))
    hx=float(topology['width'])*.5;hy=float(topology['height'])*.5
    fw=float(frame_width);tol=max(3e-7,float(depth)*1e-6)
    snapped=0
    for i,p in enumerate(mesh['vertex_params'][:len(base_mesh['verts'])]):
        ss,vv,dd=(float(v) for v in p)
        if not (abs(abs(vv)-(hy-fw))<=tol or abs(abs(ss)-(hx-fw))<=tol):continue
        key=_contact_key((ss,vv,dd));choices=[]
        for a in (-1,0,1):
            for b in (-1,0,1):
                for c in (-1,0,1):
                    for pp,pt in contacts.get((key[0]+a,key[1]+b,key[2]+c),()):
                        if max(abs(float(x)-float(y)) for x,y in zip(pp,(ss,vv,dd)))<=tol:choices.append(pt)
        if not choices:continue
        target=choices[0]
        if any(length(sub(q,target))>tol for q in choices[1:]):
            raise ValueError('Postless CORNER has inconsistent joint profile coordinates')
        if length(sub(mesh['verts'][i],target))>tol:
            mesh['verts'][i]=target;snapped+=1
    # Three mitred connector faces: back quad + top/bottom triangles. Their
    # shared front edge is already owned by the two H exterior faces.
    hmeta={m['profile_id']:m for m in pmeta if m['kind']=='FRAME_MULLION_HORIZONTAL'}
    for ids in arriving.values():
        if len(ids)!=2:continue
        ra=hmeta[ids[0]]['_junction_ring_indices'];rb=hmeta[ids[1]]['_junction_ring_indices']
        if not ra or not rb:raise ValueError('Missing H-profile miter endpoints')
        fa0,fa1,ba0,ba1=ra;fb0,fb1,bb0,bb1=rb
        if length(sub(mesh['verts'][fa0],mesh['verts'][fb0]))>2e-7 or length(sub(mesh['verts'][fa1],mesh['verts'][fb1]))>2e-7:
            raise ValueError('Postless H-profile front endpoints do not meet')
        v=mesh['verts']
        for face,role in (
            ((fa0,ba0,bb0),'FRAME_MULLION_HORIZONTAL_MITRE_BOTTOM'),
            ((fa1,bb1,ba1),'FRAME_MULLION_HORIZONTAL_MITRE_TOP'),
            ((ba0,ba1,bb1,bb0),'FRAME_MULLION_HORIZONTAL_MITRE_BACK')):
            # Seam-oriented winding is more reliable than an averaged plane
            # normal: convex/concave corner branches use opposite orientations.
            def score(seq):
                own={(a,b) for a,b in zip(seq,seq[1:]+seq[:1])}
                existing={(a,b) for ff in mesh['faces'] for a,b in zip(ff,ff[1:]+ff[:1])}
                return sum((b,a) in existing for a,b in own)-sum((a,b) in existing for a,b in own)
            aa=tuple(face);bb=tuple(reversed(aa))
            if score(bb)>score(aa):aa=bb
            mesh['faces'].append(aa);mesh['roles'].append(role)
    weld,remap=_weld(mesh)
    for meta in pmeta:
        ids=[]
        for old in meta.pop('_old_vertex_indices',[]):
            idx=remap.get(old)
            if idx is not None and idx not in ids:ids.append(idx)
        meta['vertex_indices']=ids
        meta['vertex_count']=len(ids)
    components=_component_count(mesh)
    mesh['prismatic_profiles']=list(base_mesh.get('prismatic_profiles') or [])+pmeta
    mesh['profile_geometry_contract']='POSTLESS_CORNER_MITRE_BRANCH_GRAPH_V1'
    mesh['corner_post_anchor_policy']='NONE_POSTLESS_MITRE'
    mesh['corner_assembly_components']=components
    mesh['corner_assembly_weld']=weld
    mesh['corner_assembly_profile_count']=len(rows)
    mesh['corner_assembly_graph_edge_count']=len(edges)
    mesh['corner_assembly_host_faces_removed']=removed
    mesh['corner_assembly_branch_back_rim_vertices_moved']=moved+snapped
    if components!=1:
        raise ValueError(f'Postless CORNER components disconnected: {components}; no geometry published')
    # Critical extra mesh gate: a surface can pass the normal manifold check
    # while retaining an open internal profile end at the hard junction.
    # Every open edge of a production frame must lie on the Host aperture rim.
    from collections import Counter
    incidence=Counter(tuple(sorted((a,b))) for f in mesh['faces'] for a,b in zip(f,f[1:]+f[:1]))
    params=mesh['vertex_params'];hx=float(topology['width'])*.5;hy=float(topology['height'])*.5
    def _on_host_rim(a,b):
        pa,pb=params[a],params[b]
        return any(abs(float(pa[axis])-limit)<2e-6 and abs(float(pb[axis])-limit)<2e-6
                   for axis,limit in ((0,-hx),(0,hx),(1,-hy),(1,hy)))
    open_internal=[edge for edge,count in incidence.items() if count==1 and not _on_host_rim(*edge)]
    if open_internal:
        raise ValueError(f'Postless CORNER has {len(open_internal)} open internal edges; no geometry published')
    mesh['corner_assembly_open_internal_edges']=0
    return mesh

