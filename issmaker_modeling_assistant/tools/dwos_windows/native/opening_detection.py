from __future__ import annotations
import json, math, uuid
import bpy
import bmesh
from mathutils import Vector
from .numeric import format_meter
from .dwos_log import event as log_event, exception as log_exception
from .transform_safety_core import analyze_linear_transform
from .opening_core import nesting_depths_2d
from .adaptive_detection_core import discover_surface_openings, discover_selected_openings
from .opening_index import host_geometry_fingerprint, available_candidates

TOL_N=0.995


def _canonical(n: Vector):
    n=n.normalized()
    for c in n:
        if abs(c)>1e-6:
            return -n if c<0 else n
    return n


def _dominant_normal(mesh):
    groups={}
    for p in mesh.polygons:
        n=_canonical(p.normal)
        key=tuple(round(c,2) for c in n)
        groups[key]=groups.get(key,0.0)+p.area
    if not groups:
        return None
    key=max(groups,key=groups.get)
    return Vector(key).normalized()


def _poly_center_projection(mesh,p,n):
    return sum(mesh.vertices[i].co.dot(n) for i in p.vertices)/len(p.vertices)


def _plane_side_faces(mesh,n):
    """Return the two exterior planes for a solid wall when both sides exist."""
    pos=[]; neg=[]
    for p in mesh.polygons:
        d=p.normal.dot(n)
        if d>TOL_N: pos.append(p)
        elif d<-TOL_N: neg.append(p)
    if not pos or not neg:
        return [],[],None,None
    pos_proj=max(_poly_center_projection(mesh,p,n) for p in pos)
    neg_proj=min(_poly_center_projection(mesh,p,n) for p in neg)
    eps=max(1e-5,abs(pos_proj-neg_proj)*1e-4)
    pos=[p for p in pos if abs(_poly_center_projection(mesh,p,n)-pos_proj)<eps]
    neg=[p for p in neg if abs(_poly_center_projection(mesh,p,n)-neg_proj)<eps]
    return pos,neg,pos_proj,neg_proj


def _single_surface_plane(mesh,n):
    """Find the dominant coplanar surface for zero-thickness / facade test meshes."""
    candidates=[p for p in mesh.polygons if abs(p.normal.dot(n))>TOL_N]
    if not candidates:
        return [],None
    # Cluster by projected plane position. Tolerance is derived from object size but
    # remains tight enough not to merge two sides of a real solid wall.
    coords=[v.co.dot(n) for v in mesh.vertices]
    extent=(max(coords)-min(coords)) if coords else 0.0
    tol=max(1e-5, extent*1e-5)
    clusters=[]
    for p in candidates:
        z=_poly_center_projection(mesh,p,n)
        hit=None
        for cl in clusters:
            if abs(z-cl['z']) <= tol:
                hit=cl; break
        if hit is None:
            hit={'z':z,'faces':[],'area':0.0}; clusters.append(hit)
        hit['faces'].append(p); hit['area']+=p.area
        # area-weighted plane estimate makes triangulated surfaces stable
        hit['z']=(hit['z']*(hit['area']-p.area)+z*p.area)/hit['area']
    best=max(clusters,key=lambda c:c['area'])
    return best['faces'],best['z']


def _basis(n):
    ref=Vector((0,0,1)) if abs(n.z)<0.9 else Vector((1,0,0))
    x=ref.cross(n).normalized(); y=n.cross(x).normalized()
    return x,y


def _boundary_edges_for_plane(mesh, faces):
    face_ids={p.index for p in faces}
    edge_faces={}
    for p in mesh.polygons:
        for ei in p.edge_keys:
            key=tuple(sorted(ei)); edge_faces.setdefault(key,[]).append(p.index)
    out=[]
    for p in faces:
        for ekey in p.edge_keys:
            key=tuple(sorted(ekey)); adj=edge_faces.get(key,[])
            same=sum(1 for fi in adj if fi in face_ids)
            if same==1:
                out.append(key)
    return list(dict.fromkeys(out))


def _loops_from_edges(edges):
    adj={}
    for a,b in edges:
        adj.setdefault(a,[]).append(b); adj.setdefault(b,[]).append(a)
    remaining={tuple(sorted(e)) for e in edges}
    loops=[]
    while remaining:
        e=next(iter(remaining)); start=e[0]; cur=start; prev=None; loop=[start]
        for _ in range(len(edges)+5):
            opts=[v for v in adj.get(cur,[]) if tuple(sorted((cur,v))) in remaining]
            if not opts: break
            if prev is None or len(opts)==1:
                nxt=opts[0]
            else:
                nxt=next((v for v in opts if v!=prev),opts[0])
            remaining.discard(tuple(sorted((cur,nxt))))
            prev,cur=cur,nxt
            if cur==start:
                loops.append(loop); break
            loop.append(cur)
    return [l for l in loops if len(l)>=4]


def _simplify_rect(mesh, loop, xax, yax):
    pts=[(mesh.vertices[i].co.dot(xax),mesh.vertices[i].co.dot(yax),i) for i in loop]
    clean=[]
    n=len(pts)
    for i,p in enumerate(pts):
        a=pts[i-1]; b=pts[(i+1)%n]
        ab=(b[0]-a[0],b[1]-a[1]); ap=(p[0]-a[0],p[1]-a[1])
        cross=abs(ab[0]*ap[1]-ab[1]*ap[0])
        if cross>1e-6*(abs(ab[0])+abs(ab[1])+1): clean.append(p)
    if len(clean)!=4: return None
    edges=[]
    for i,p in enumerate(clean):
        q=clean[(i+1)%4]; dx=q[0]-p[0]; dy=q[1]-p[1]; L=math.hypot(dx,dy)
        if L<1e-7: return None
        edges.append((dx/L,dy/L,L))
    if any(abs(edges[i][0]*edges[(i+1)%4][0]+edges[i][1]*edges[(i+1)%4][1])>2e-3 for i in range(4)): return None
    if abs(edges[0][2]-edges[2][2])>1e-4 or abs(edges[1][2]-edges[3][2])>1e-4: return None
    center=(sum(p[0] for p in clean)/4,sum(p[1] for p in clean)/4)
    points2=[(p[0],p[1]) for p in clean]
    area=abs(sum(points2[i][0]*points2[(i+1)%4][1]-points2[(i+1)%4][0]*points2[i][1] for i in range(4))*0.5)
    return {
        'full_loop':loop,
        'corners':[p[2] for p in clean],
        'center2':center,
        'edge_lengths':[e[2] for e in edges],
        'area':area,
        'points2':points2,
    }


def _opening_rects(mesh, loops, xax, yax, stats=None):
    """Classify holes from *all* closed contours, not just rectangular contours.

    The outside silhouette of a real facade can be stepped, have T-junctions,
    or contain many collinear vertices. Dropping its non-rectangular contour
    before calculating nesting depth accidentally classifies every window as
    a depth-0 island and silently loses the entire facade.

    Only the interior opening itself must pass strict rectangular validation;
    the enclosing silhouette may be an arbitrary simple polygon.
    """
    polys = []
    candidate_rectangles = []
    for loop in loops:
        points = [(mesh.vertices[i].co.dot(xax), mesh.vertices[i].co.dot(yax)) for i in loop]
        polys.append(points)
        candidate_rectangles.append(_simplify_rect(mesh, loop, xax, yax))
    depths = nesting_depths_2d(polys) if polys else []
    out = [rect for rect, depth in zip(candidate_rectangles, depths)
           if rect is not None and depth % 2 == 1]
    if stats is not None:
        stats.update(loops=len(loops), rects=sum(r is not None for r in candidate_rectangles),
                     nonrect=sum(r is None for r in candidate_rectangles),
                     nested=sum(depth % 2 for depth in depths), accepted=len(out))
    return out


def _candidate_axes(mesh, rect, n, xref):
    ids=rect['corners']
    p0=mesh.vertices[ids[0]].co
    p1=mesh.vertices[ids[1]].co
    p3=mesh.vertices[ids[3]].co
    e1=p1-p0; e2=p3-p0
    # X follows the rectangle edge that is closest to the wall's natural horizontal
    # basis. This avoids random width/height swaps due only to loop start order.
    if abs(e2.normalized().dot(xref)) > abs(e1.normalized().dot(xref)):
        ax=e2.normalized(); other=e1
    else:
        ax=e1.normalized(); other=e2
    if ax.dot(xref)<0: ax=-ax
    ay=n.cross(ax).normalized()
    # V2.1.2: ax has already been aligned with the deterministic facade basis.
    # NEVER flip ax/ay based on the arbitrary traversal direction of the other
    # rectangle edge: that reverses local Y and moves a lower transom to the top.
    coords=[mesh.vertices[i].co for i in ids]
    width=max(p.dot(ax) for p in coords)-min(p.dot(ax) for p in coords)
    height=max(p.dot(ay) for p in coords)-min(p.dot(ay) for p in coords)
    return ax,ay,width,height


def _make_candidate(mesh, rect, n, xax, yax, center_plane, wall_depth, wall_mode, back_rect=None, front_proj=None, back_proj=None):
    ax,ay,w,h=_candidate_axes(mesh,rect,n,xax)
    center2=rect['center2']
    center_local=xax*center2[0]+yax*center2[1]+n*center_plane
    return {
        'id':uuid.uuid4().hex[:12],
        'center_local':list(center_local),
        'axis_x_local':list(ax),
        'axis_y_local':list(ay),
        'axis_z_local':list(n),
        'width':w,
        'height':h,
        'wall_depth':wall_depth,
        'wall_mode':wall_mode,
        'front_proj':front_proj if front_proj is not None else center_plane,
        'back_proj':back_proj if back_proj is not None else center_plane,
        'front_loop':rect['full_loop'],
        'back_loop':back_rect['full_loop'] if back_rect else [],
        'front_corners':rect['corners'],
        'back_corners':back_rect['corners'] if back_rect else [],
    }


def _planar_patches(mesh):
    """Group coplanar faces *per orientation* rather than choosing one axis.

    A 4-sided facade shell is NOT a 22-metre-thick solid wall just because
    two remote faces have opposing normals. Solid pairing requires actual
    reveal faces connecting the boundary loops of the same aperture.
    """
    patches=[]
    if not mesh.polygons:
        return patches
    span=max((max(float(v.co[k]) for v in mesh.vertices) -
              min(float(v.co[k]) for v in mesh.vertices) for k in range(3)),default=1.0)
    # Blender 32-bit mesh coordinates on a ~22m facade can differ by ~0.13mm
    # after geometric modeling. Strict 0.02mm bucketing fragmented the provided
    # four facades into 12 patches. Cap tolerance to 0.5mm to avoid accidentally
    # merging distinct nearby planes on huge projects.
    tolerance=min(5e-4, max(1e-4, span*1e-5))
    for face in mesh.polygons:
        n=face.normal.normalized()
        if n.length_squared < 0.9:
            continue
        projections=[mesh.vertices[vi].co.dot(n) for vi in face.vertices]
        # Reject warped/non-planar source polygons from the planar detector.
        if not projections or max(projections)-min(projections)>tolerance:
            continue
        position=sum(projections)/len(projections)
        target=None
        for group in patches:
            if (n.dot(group['normal'])>=0.9999
                    and abs(position-group['position'])<=tolerance
                    and max(abs(mesh.vertices[vi].co.dot(group['normal'])-group['position'])
                            for vi in face.vertices)<=tolerance):
                target=group;break
        if target is None:
            target={'normal':n.copy(),'position':position,'faces':[],'area':0.0}
            patches.append(target)
        target['faces'].append(face)
        target['area']+=face.area
    for group in patches:
        n=group['normal']
        xax,yax=_basis(n)
        boundary=_boundary_edges_for_plane(mesh,group['faces'])
        loops=_loops_from_edges(boundary)
        stats={}
        group['rects']=_opening_rects(mesh,loops,xax,yax,stats=stats)
        group['detection_stats']=stats
        group['basis']=(xax,yax)
        group['boundary_edges']=len(boundary)
        group['plane_tolerance']=tolerance
    return patches


def _bridging_reveal_faces(mesh, front_loop, back_loop):
    """Find physical host faces spanning both distinct aperture boundary loops.

    Mere coincidence of opening dimensions or opposite normals does not prove
    thickness. Requiring host-connected reveals prevents joining the front and
    back of an unrelated building/facade shell.
    """
    front=set(front_loop); back=set(back_loop)
    if not front or not back or front.intersection(back):
        return 0
    found=0
    for p in mesh.polygons:
        verts=set(p.vertices)
        if len(verts & front)>=2 and len(verts & back)>=2:
            found+=1
    return found


def _rect_match(mesh, rect1, rect2, xax, yax):
    def bounds(r):
        coords=[mesh.vertices[i].co for i in r['full_loop']]
        xx=[v.dot(xax) for v in coords]; yy=[v.dot(yax) for v in coords]
        return min(xx),max(xx),min(yy),max(yy)
    a=bounds(rect1);b=bounds(rect2)
    width=max(a[1]-a[0],a[3]-a[2],1e-6)
    diffs=[abs(a[k]-b[k]) for k in range(4)]
    return max(diffs)<=max(0.001,width*0.005)




def _adaptive_candidate(topology):
    center=Vector(topology['origin'])
    n=Vector(topology['axis_z']).normalized()
    front_proj=center.dot(n)
    return {
        'id':uuid.uuid4().hex[:12],
        'center_local':list(center),
        'axis_x_local':list(topology['axis_x']),
        'axis_y_local':list(topology['axis_y']),
        'axis_z_local':list(topology['axis_z']),
        'width':float(topology['width']),
        'height':float(topology['height']),
        'wall_depth':0.0,
        'wall_mode':'SURFACE',
        'front_proj':float(front_proj),
        'back_proj':float(front_proj),
        'front_loop':list(topology.get('loop_vertex_ids',[])),
        'back_loop':[],
        'front_corners':[],
        'back_corners':[],
        'adaptive_classification':topology.get('classification','PLANAR'),
        'coordinate_system':'SVD_RIBBON',
        'adaptive_topology':topology,
        'glass_policy':topology.get('glass_policy','UNSUPPORTED'),
    }


def _loop_overlap(a,b):
    aa=set(a or []);bb=set(b or [])
    return len(aa&bb)

def detect_openings(obj):
    mesh=obj.data
    patches=_planar_patches(mesh)
    result=[]
    processed=set()
    candidates=sum(len(p['rects']) for p in patches)
    for pi,patch in enumerate(patches):
        n=patch['normal']
        xax,yax=patch['basis']
        # Front/back sign convention: deterministic canonical sign when both
        # faces exist; for a one-sided surface use its real source normal.
        for ri,rect in enumerate(patch['rects']):
            if (pi,ri) in processed:
                continue
            best=None
            for pj,back in enumerate(patches):
                if pi==pj or n.dot(back['normal'])>-0.9999:
                    continue
                depth=abs(patch['position'] - (-back['position']))
                if depth<=1e-5:
                    continue
                for rj,br in enumerate(back['rects']):
                    if (pj,rj) in processed or not _rect_match(mesh,rect,br,xax,yax):
                        continue
                    bridges=_bridging_reveal_faces(mesh,rect['full_loop'],br['full_loop'])
                    if bridges<2:
                        continue
                    candidate=(pj,rj,br,depth,bridges)
                    if best is None or depth<best[3]:
                        best=candidate
            if best is not None:
                # Avoid selecting the arbitrary reverse side as the facade.
                # The positive canonical side wins and is emitted only once.
                pj,rj,br,depth,bridges=best
                processed.add((pi,ri));processed.add((pj,rj))
                other=patches[pj]
                if n.dot(_canonical(n))>0:
                    front_patch,front_rect,back_rect=patch,rect,br
                else:
                    front_patch,front_rect,back_rect=other,br,rect
                fn=front_patch['normal'];fx,fy=front_patch['basis']
                fp=front_patch['position']
                bp=mesh.vertices[back_rect['corners'][0]].co.dot(fn)
                result.append(_make_candidate(mesh,front_rect,fn,fx,fy,fp,depth,'SOLID',
                                              back_rect,fp,bp))
            else:
                processed.add((pi,ri))
                result.append(_make_candidate(mesh,rect,n,xax,yax,patch['position'],0.0,'SURFACE'))
    # DWOS 3.1: discover holes whose boundary crosses several facade planes.
    # This pure topology pass is intentionally limited to one-sided/surface shells;
    # proven planar SOLID pairing above remains unchanged.  Resolve the new helper
    # lazily so legacy source-level regression harnesses (which deliberately exec
    # only the old planar functions) continue to exercise exactly that old path.
    adaptive_discover=globals().get('discover_surface_openings')
    if adaptive_discover is not None:
        try:
            verts=[tuple(float(x) for x in v.co) for v in mesh.vertices]
            faces=[tuple(int(i) for i in p.vertices) for p in mesh.polygons]
            normals=[tuple(float(x) for x in p.normal) for p in mesh.polygons]
            try:
                up_local=(obj.matrix_world.to_3x3().inverted() @ Vector((0,0,1))).normalized()
                up_hint=tuple(float(x) for x in up_local)
            except Exception:
                up_hint=(0.0,0.0,1.0)
            adaptive=adaptive_discover(verts,faces,face_normals=normals,up_hint=up_hint,source='AUTO_DETECTED')
            adaptive_added=0
            # On one-sided surface shells, boundary topology is more authoritative
            # than per-plane hole inference. Replace overlapping legacy SURFACE
            # candidates for every supported class and remove planar-patch false
            # positives that are not actual Host boundary loops. SOLID candidates
            # remain owned by the proven legacy reveal-pair detector.
            analyzed_loops=[row.get('loop_vertex_ids',[]) for row in adaptive.get('boundary_loops',[])]
            accepted_loops=[topo.get('loop_vertex_ids',[]) for topo in adaptive.get('openings',[])]
            if analyzed_loops:
                cleaned=[]
                for d in result:
                    if d.get('wall_mode')!='SURFACE':
                        cleaned.append(d);continue
                    loop=d.get('front_loop',[])
                    if any(_loop_overlap(loop,a)>=2 for a in accepted_loops):
                        continue  # replaced below by OpeningTopology candidate
                    if any(_loop_overlap(loop,a)>=2 for a in analyzed_loops):
                        continue  # real boundary, but unsupported/invalid: do not fabricate
                    # A legacy planar candidate with no relationship to any actual
                    # analyzed surface-hole boundary is a patch-fragment false hit.
                    continue
                result=cleaned
            for topo in adaptive['openings']:
                result.append(_adaptive_candidate(topo));adaptive_added+=1
            log_event('HOST_ADAPTIVE_OPENING_ANALYSIS',wall=obj.name,
                      boundary_edges=adaptive.get('boundary_edges',0),
                      face_components=adaptive.get('face_components',0),
                      detected=len(adaptive.get('openings',[])),added=adaptive_added,
                      curved=sum(o.get('classification')=='CURVED' for o in adaptive.get('openings',[])),
                      corner=sum(o.get('classification')=='CORNER' for o in adaptive.get('openings',[])),
                      diagnostics=adaptive.get('diagnostics',[])[:80],
                      adaptive_solid_support=adaptive.get('adaptive_solid_support',False))
        except Exception as adaptive_exc:
            # Detection remains fail-safe: a new adaptive analysis cannot destroy
            # the established planar result. Logging is optional in detached
            # source-level regression harnesses.
            logger=globals().get('log_exception')
            if logger is not None:
                logger('HOST_ADAPTIVE_OPENING_WARNING',adaptive_exc,wall=obj.name)

    log_event('HOST_PLANAR_PATCH_ANALYSIS', wall=obj.name, patches=len(patches),
              candidates=candidates, result=len(result),
              surface=sum(d['wall_mode']=='SURFACE' for d in result),
              solid=sum(d['wall_mode']=='SOLID' for d in result),
              adaptive=sum(bool(d.get('adaptive_topology')) for d in result),
              patch_normals=[tuple(round(float(v),3) for v in p['normal']) for p in patches],
              patch_stats=[{'faces':len(p['faces']), 'boundary_edges':p['boundary_edges'],
                            'opening_stats':p['detection_stats'],
                            'plane_tolerance':round(p['plane_tolerance'],7)} for p in patches[:40]])
    return result


def _validate_host_source_contract(obj):
    """3.5.3: fail closed when base-mesh detection cannot preserve world metric."""
    m=obj.matrix_world.to_3x3()
    rows=[[float(m[i][j]) for j in range(3)] for i in range(3)]
    report=analyze_linear_transform(rows)
    if not report['rigid']:
        scale=' × '.join(f"{x:.6g}" for x in report['scales'])
        reasons=', '.join(report['reasons'])
        raise ValueError(
            f'Host имеет неприменённый Scale/Shear/Reflection ({scale}; {reasons}). '
            'DWOS 3.5.3 останавливает поиск, чтобы не создать окно неверного размера. '
            'Примените Transform Scale на копии/Host и повторите поиск.')
    modifiers=[m.name for m in getattr(obj,'modifiers',()) if getattr(m,'show_viewport',True)]
    if modifiers:
        shown=', '.join(modifiers[:6]) + ('…' if len(modifiers)>6 else '')
        raise ValueError(
            f'Host содержит активные модификаторы ({shown}). DWOS 3.5.3 анализирует base Mesh и '
            'не будет молча игнорировать evaluated geometry. Примените модификаторы на рабочей копии Host и повторите поиск.')
    return report


def _publish_candidates(context,obj,found,*,source_label='AUTO'):
    st=context.scene.dwos;st.openings.clear();st.wall_name=obj.name;st.show_overlay=True
    # The source revision is recorded for every scan; a later Mesh edit invalidates
    # candidates even if Blender's UI list and scene object stay unchanged.
    st.opening_source_fingerprint=host_geometry_fingerprint(obj)
    found,occupied=available_candidates(found,obj,context.scene)
    mw=obj.matrix_world;surface_count=0;solid_count=0;adaptive_count=0;classes={}
    for d in found:
        c=st.openings.add();c.candidate_id=d['id'];c.enabled=True
        c.width_text=format_meter(d['width']);c.height_text=format_meter(d['height']);c.wall_depth_text=format_meter(d['wall_depth'])
        c.center=mw @ Vector(d['center_local'])
        c.axis_x=(mw.to_3x3() @ Vector(d['axis_x_local'])).normalized()
        c.axis_y=(mw.to_3x3() @ Vector(d['axis_y_local'])).normalized()
        c.axis_z=(mw.to_3x3() @ Vector(d['axis_z_local'])).normalized()
        c.payload_json=json.dumps(d,separators=(',',':'))
        if d.get('wall_mode')=='SURFACE':surface_count+=1
        else:solid_count+=1
        if d.get('adaptive_topology'):adaptive_count+=1
        cls=d.get('adaptive_classification','PLANAR');classes[cls]=classes.get(cls,0)+1
        log_event('OPENING_FOUND',candidate=d['id'],width=c.width_text,height=c.height_text,
                  mode=d.get('wall_mode',''),classification=cls,source=source_label)
    st.status=f'Свободных проёмов: {len(found)}'
    if occupied:
        st.status+=f' · занято: {len(occupied)}'
        log_event('OPENING_ALREADY_OCCUPIED_SKIPPED',wall=obj.name,
                  count=len(occupied),windows=[window.name for _,window in occupied[:30]])
    if surface_count:st.status+=f' · поверхность: {surface_count}'
    if adaptive_count:st.status+=f' · adaptive: {adaptive_count}'
    if classes.get('CURVED'):st.status+=f" · curved: {classes['CURVED']}"
    if classes.get('CORNER'):st.status+=f" · corner: {classes['CORNER']}"
    return {'surface':surface_count,'solid':solid_count,'adaptive':adaptive_count,'classes':classes,'available':len(found),'occupied':len(occupied)}


class DWOS_OT_use_selected_openings(bpy.types.Operator):
    bl_idname='dwos.use_selected_openings'
    bl_label='Использовать выделенные контуры'
    bl_description='Принять замкнутые выбранные рёбра как намерение пользователя и классифицировать PLANAR/CURVED/CORNER без изменения Host'
    bl_options={'REGISTER','UNDO'}

    @classmethod
    def poll(cls,context):
        obj=context.active_object
        return bool(obj and obj.type=='MESH' and obj.mode=='EDIT' and not obj.dwos_window.is_window)

    def execute(self,context):
        obj=context.active_object
        try:
            _validate_host_source_contract(obj)
            bm=bmesh.from_edit_mesh(obj.data);bm.verts.ensure_lookup_table();bm.edges.ensure_lookup_table();bm.faces.ensure_lookup_table()
            verts=list(bm.verts);edges=list(bm.edges);vind={v:i for i,v in enumerate(verts)}
            coords=[tuple(float(x) for x in v.co) for v in verts]
            pairs=[(vind[e.verts[0]],vind[e.verts[1]]) for e in edges]
            selected=[i for i,e in enumerate(edges) if e.select]
            if not selected:raise ValueError('Выделите рёбра одного или нескольких оконных проёмов в Edit Mode')
            normals={}
            for i,e in enumerate(edges):
                if i in selected:normals[i]=[tuple(float(x) for x in f.normal) for f in e.link_faces]
            try:
                up_local=(obj.matrix_world.to_3x3().inverted() @ Vector((0,0,1))).normalized()
                up_hint=tuple(float(x) for x in up_local)
            except Exception:up_hint=(0.0,0.0,1.0)
            analysis=discover_selected_openings(coords,pairs,selected,edge_face_normals=normals,up_hint=up_hint,source='USER_SELECTED')
            found=[_adaptive_candidate(t) for t in analysis['openings']]
            if not found:
                raise ValueError('В выделении нет замкнутых поддерживаемых PLANAR/CURVED/CORNER контуров')
            stats=_publish_candidates(context,obj,found,source_label='USER_SELECTED')
            context.scene.dwos.status += f" · selection {analysis['component_count']}/{analysis['closed_count']}"
            rejected=analysis['component_count']-stats['available']
            log_event('USE_SELECTED_OPENINGS_OK',wall=obj.name,selected_edges=len(selected),
                      components=analysis['component_count'],closed=analysis['closed_count'],
                      accepted=len(found),rejected=rejected,classes=stats['classes'],
                      diagnostics=analysis['diagnostics'][:80])
            from .viewport import ensure_overlay
            ensure_overlay()
            self.report({'INFO'},f'Свободных проёмов: {stats["available"]}; занятых: {stats["occupied"]}; прочих: {rejected-stats["occupied"]}')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('USE_SELECTED_OPENINGS_FAILED',exc,wall=getattr(obj,'name',''))
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


class DWOS_OT_use_selected_cap_faces(bpy.types.Operator):
    bl_idname='dwos.use_selected_cap_faces'
    bl_label='Использовать грани проёма'
    bl_description='Определить замкнутый контур по границе выделенных граней углубления, без изменения Mesh'
    bl_options={'REGISTER'}

    @classmethod
    def poll(cls,context):
        obj=context.active_object
        return bool(obj and obj.type=='MESH' and obj.mode=='EDIT'
                    and not obj.dwos_window.is_window)

    def execute(self,context):
        obj=context.active_object
        try:
            _validate_host_source_contract(obj)
            bm=bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table();bm.edges.ensure_lookup_table();bm.faces.ensure_lookup_table()
            faces=[f for f in bm.faces if f.select]
            if not faces:
                raise ValueError('Выделите грани дна проёма, созданные Brick Hole (Edit Mode)')
            if len(faces) >= len(bm.faces)*.5:
                raise ValueError('Выделено более половины стены. Оставьте только грани одного или нескольких проёмов')
            edge_ids=[e.index for e in bm.edges
                      if sum(1 for f in e.link_faces if f.select)==1]
            if len(edge_ids)<4:
                raise ValueError('Нет замкнутой границы выбранных граней. Выделите только дно проёма')
            verts=list(bm.verts); vind={v:i for i,v in enumerate(verts)}
            coords=[tuple(map(float,v.co)) for v in verts]
            pairs=[(vind[e.verts[0]],vind[e.verts[1]]) for e in bm.edges]
            normals={i:[tuple(map(float,f.normal)) for f in bm.edges[i].link_faces]
                     for i in edge_ids}
            try:
                up_hint=tuple(map(float,(obj.matrix_world.to_3x3().inverted()
                                          @ Vector((0,0,1))).normalized()))
            except (ValueError,ZeroDivisionError):
                up_hint=(0.,0.,1.)
            analysis=discover_selected_openings(
                coords,pairs,edge_ids,edge_face_normals=normals,
                up_hint=up_hint,source='USER_SELECTED_CAP_FACES')
            found=[_adaptive_candidate(t) for t in analysis['openings']]
            if not found:
                raise ValueError('Грани не образуют поддерживаемый замкнутый контур. Проверьте выделение и нормали')
            stats=_publish_candidates(context,obj,found,source_label='USER_SELECTED_CAP_FACES')
            context.scene.dwos.status+=f' · грани: {len(faces)}, контуры: {len(found)}'
            log_event('USE_SELECTED_CAP_FACES_OK',wall=obj.name,selected_faces=len(faces),
                      boundary_edges=len(edge_ids),accepted=len(found),classes=stats['classes'])
            from .viewport import ensure_overlay
            ensure_overlay()
            self.report({'INFO'},f'Принято проёмов из граней: {len(found)}. Перейдите в Object Mode для создания')
            return {'FINISHED'}
        except Exception as exc:
            log_exception('USE_SELECTED_CAP_FACES_FAILED',exc,wall=getattr(obj,'name',''))
            self.report({'ERROR'},str(exc));return {'CANCELLED'}


class DWOS_OT_find_openings(bpy.types.Operator):
    bl_idname='dwos.find_openings'; bl_label='Найти проёмы'; bl_options={'REGISTER','UNDO'}
    def execute(self,context):
        obj=context.active_object
        if not obj or obj.type!='MESH':
            self.report({'ERROR'},'Выберите объект стены Mesh'); return {'CANCELLED'}
        if obj.dwos_window.is_window:
            self.report({'ERROR'},'Выберите стену, а не окно'); return {'CANCELLED'}
        try:
            _validate_host_source_contract(obj)
        except Exception as exc:
            log_exception('HOST_SOURCE_CONTRACT_REJECTED',exc,wall=obj.name)
            self.report({'ERROR'},str(exc)); return {'CANCELLED'}
        log_event('FIND_OPENINGS_BEGIN', wall=obj.name, verts=len(obj.data.vertices), edges=len(obj.data.edges), faces=len(obj.data.polygons))
        try:
            found=detect_openings(obj)
        except Exception as exc:
            log_exception('FIND_OPENINGS_FAILED', exc, wall=obj.name)
            self.report({'ERROR'},f'Поиск проёмов: {exc}')
            return {'CANCELLED'}
        stats=_publish_candidates(context,obj,found,source_label='AUTO')
        st=context.scene.dwos
        if not found and not stats['occupied']:
            st.status=('Автопоиск не подтвердил замкнутый проём. '
                       'В Edit Mode выделите рёбра настоящего контура и нажмите '
                       '«Использовать выделенные контуры».')
            log_event('FIND_OPENINGS_AMBIGUOUS_OR_EMPTY',wall=obj.name,
                      advice='Use selected edges in Edit Mode; no frame is generated')
        log_event('FIND_OPENINGS_OK',wall=obj.name,total=len(found),surface=stats['surface'],
                  solid=stats['solid'],adaptive=stats['adaptive'],classes=stats['classes'])
        self.report({'INFO'},st.status)
        from .viewport import ensure_overlay
        ensure_overlay()
        return {'FINISHED'}
