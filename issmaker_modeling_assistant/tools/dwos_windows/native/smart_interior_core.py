"""DWOS Living Architecture: deterministic contact and open-cavity clipping core.

No Blender imports. The front/open rim of each canonical Interior is inviolate.
Planar corner-box footprints may be clipped to a shared XY contact line;
other non-separable contacts fail closed instead of producing broken geometry. All calculations are in world space.
"""
from __future__ import annotations
from collections import Counter, defaultdict
import math
try:
    from .spatial_safety_core import (mesh_intersections, self_intersections, bounds,
                                      aabb_overlap, dot, cross, sub, length)
    from .adaptive_validation import validate_mesh as validate_adaptive_mesh
except ImportError:  # Blender-free field tests
    from spatial_safety_core import (mesh_intersections, self_intersections, bounds,
                                     aabb_overlap, dot, cross, sub, length)
    from adaptive_validation import validate_mesh as validate_adaptive_mesh

POLICY='SMART_INTERIOR_CONTOUR_MITRE_V373'
EPS=1e-7

class UnresolvedInteriorContact(ValueError):
    """Known non-separable geometric contact; may be staged as PENDING."""
    def __init__(self,message,reason_code='CONTACT_NON_SEPARABLE'):
        super().__init__(message)
        self.reason_code=reason_code

class InteriorSolverUnavailable(UnresolvedInteriorContact):
    """Requested deterministic solver is not implemented (not damaged mesh)."""



def add(a,b):return tuple(x+y for x,y in zip(a,b))
def mul(a,k):return tuple(x*k for x in a)
def normalize(v):
    n=length(v)
    return mul(v,1/n) if n>EPS else None


def boundary_edges(mesh):
    count=Counter(); directed={}
    for face in mesh['faces']:
        for i,a in enumerate(face):
            b=face[(i+1)%len(face)];key=tuple(sorted((a,b)))
            count[key]+=1;directed[key]=(a,b)
    if any(v>2 for v in count.values()):raise ValueError('Interior contains non-manifold edge (>2 faces)')
    return [directed[k] for k,v in count.items() if v==1]


def front_rim(mesh):
    """All original open boundaries (including multi-strip adaptive apertures)."""
    edges=boundary_edges(mesh)
    if not edges:raise ValueError('Interior must be an open-front cavity, not a closed solid')
    return tuple(sorted({i for e in edges for i in e}))


def _edge_geometry(mesh):
    def point(i):return mesh['verts'][i]
    return Counter(tuple(sorted((tuple(round(t,6) for t in point(a)),tuple(round(t,6) for t in point(b))))) for a,b in boundary_edges(mesh))


def _validate_mesh(mesh):
    if not mesh.get('faces'):raise ValueError('Interior clipped away completely')
    if any(not math.isfinite(float(c)) for p in mesh['verts'] for c in p):raise ValueError('Interior has non-finite coordinates')
    validate_adaptive_mesh({'verts':mesh['verts'],'faces':mesh['faces'],'roles':mesh['roles']})
    if self_intersections(mesh['verts'],mesh['faces']):raise ValueError('Interior produced self-intersections')


def _basis_2d(normal):
    ref=(0.,0.,1.) if abs(normal[2])<0.8 else (0.,1.,0.)
    u=normalize(cross(normal,ref));v=normalize(cross(normal,u))
    return u,v


def _triangle_contains(p,a,b,c,sign,tol=1e-9):
    def cr(x,y,z):return (y[0]-x[0])*(z[1]-x[1])-(y[1]-x[1])*(z[0]-x[0])
    return all(sign*cr(x,y,p)>=-tol for x,y in ((a,b),(b,c),(c,a)))


def _ear_clip(loop,verts,normal):
    u,v=_basis_2d(normal)
    coords={i:(dot(verts[i],u),dot(verts[i],v)) for i in loop}
    area=sum(coords[loop[k]][0]*coords[loop[(k+1)%len(loop)]][1]-coords[loop[(k+1)%len(loop)]][0]*coords[loop[k]][1] for k in range(len(loop)))
    if abs(area)<1e-12:raise ValueError('Interior cap has zero area')
    sign=1 if area>0 else -1
    indices=list(loop);tris=[]
    for _ in range(len(loop)*len(loop)):
        if len(indices)==3:
            tris.append(tuple(indices));return tris
        found=False
        for k in range(len(indices)):
            a,b,c=(indices[(k-1)%len(indices)],indices[k],indices[(k+1)%len(indices)])
            pa,pb,pc=coords[a],coords[b],coords[c]
            area2=(pb[0]-pa[0])*(pc[1]-pa[1])-(pb[1]-pa[1])*(pc[0]-pa[0])
            if sign*area2<=1e-10:continue
            if any(_triangle_contains(coords[q],pa,pb,pc,sign) for q in indices if q not in (a,b,c)):
                continue
            tris.append((a,b,c));indices.pop(k);found=True;break
        if not found:break
    raise ValueError('Interior slice has complex/crossing cap; no safe triangulation')


def clip_open_cavity(mesh,normal,threshold,*,keep_low=True):
    """Clip a two-manifold open cavity and cap new cut loops, preserving the mouth.

    The original front boundary MUST lie on the kept side. If not, no mutation.
    """
    n=normalize(normal)
    if n is None:raise ValueError('Invalid clipping normal')
    tol=1e-8
    signed=lambda p: (dot(n,p)-threshold)*(1 if keep_low else -1)
    original_mouth=_edge_geometry(mesh)
    mouth=front_rim(mesh)
    if all(signed(p)<=tol for p in mesh['verts']):
        return {'verts':list(mesh['verts']),'faces':list(mesh['faces']),'roles':list(mesh['roles'])}
    if any(signed(mesh['verts'][i])>-tol for i in mouth):
        raise ValueError('Smart Interior would alter the original opening rim')
    verts=[];faces=[];roles=[];lookup={}
    def idx(p):
        p=tuple(float(q) for q in p);key=tuple(round(q,9) for q in p)
        if key not in lookup:lookup[key]=len(verts);verts.append(p)
        return lookup[key]
    for face,role in zip(mesh['faces'],mesh['roles']):
        poly=[mesh['verts'][i] for i in face]
        clipped=[]
        for i,p in enumerate(poly):
            q=poly[(i+1)%len(poly)];dp=signed(p);dq=signed(q)
            if dp<=tol:clipped.append(p)
            if (dp < -tol and dq > tol) or (dp > tol and dq < -tol):
                t=dp/(dp-dq)
                clipped.append(add(p,mul(sub(q,p),t)))
        clean=[]
        for p in clipped:
            if not clean or length(sub(p,clean[-1]))>tol:clean.append(p)
        if len(clean)>=2 and length(sub(clean[0],clean[-1]))<=tol:clean.pop()
        if len(clean)<3:continue
        ids=tuple(idx(p) for p in clean)
        if len(set(ids))<3:continue
        faces.append(ids);roles.append(role)
    if not faces:raise ValueError('Smart Interior would remove the entire cavity')
    pre={'verts':verts,'faces':faces,'roles':roles}
    cut_edges=[(a,b) for a,b in boundary_edges(pre)
               if abs(dot(n,verts[a])-threshold)<=1e-6 and abs(dot(n,verts[b])-threshold)<=1e-6]
    if cut_edges:
        # The cap uses reversed boundary direction, producing opposing winding
        # along each shared edge. Reject ambiguous branches and holes.
        outgoing=defaultdict(list);incoming=Counter()
        for a,b in cut_edges:
            outgoing[b].append(a);incoming[a]+=1
        if any(len(v)!=1 for v in outgoing.values()) or any(incoming[v]!=1 for v in outgoing):
            raise ValueError('Smart Interior cap has branching/ambiguous boundaries')
        remaining=set(outgoing)
        while remaining:
            start=min(remaining);loop=[];at=start
            for _ in range(len(cut_edges)+1):
                if at in loop:
                    if at!=start:raise ValueError('Smart Interior cap has non-simple loop')
                    break
                loop.append(at);remaining.discard(at)
                at=outgoing[at][0]
            if at!=start or len(loop)<3:raise ValueError('Open cap chain in Smart Interior')
            for tri in _ear_clip(loop,verts,n):
                faces.append(tri);roles.append('INTERIOR_SMART_CONTACT')
    result={'verts':verts,'faces':faces,'roles':roles}
    if _edge_geometry(result)!=original_mouth:
        raise ValueError('Smart Interior would modify/open an original aperture boundary')
    _validate_mesh(result)
    return result


def aperture_separation(a,b,clearance):
    """Find a plane separating original front mouths with given minimum gap."""
    ma=[a['verts'][i] for i in front_rim(a)];mb=[b['verts'][i] for i in front_rim(b)]
    ca=mul(tuple(map(sum,zip(*ma))),1/len(ma));cb=mul(tuple(map(sum,zip(*mb))),1/len(mb))
    direction=sub(cb,ca)
    candidates=[direction,(1,0,0),(0,1,0),(0,0,1)]
    # Bounded number of deterministic candidate normals for large CURVED strips.
    for p in ma[::max(1,len(ma)//24)]:
        for q in mb[::max(1,len(mb)//24)]:candidates.append(sub(q,p))
    # Also normals of aperture edges crossed with the source front plane.
    for mesh in (a,b):
        ed=boundary_edges(mesh)
        for aa,bb in ed:
            e=sub(mesh['verts'][bb],mesh['verts'][aa]);candidates.append(cross(e,direction))
    best=None
    for candidate in candidates:
        n=normalize(candidate)
        if n is None:continue
        for direction_sign in (1,-1):
            nn=mul(n,direction_sign)
            end_a=max(dot(nn,p) for p in ma)
            start_b=min(dot(nn,p) for p in mb)
            free=start_b-end_a
            if best is None or free>best[0]:best=(free,nn,end_a,start_b)
    if best is None or best[0]<clearance+1e-6:
        raise UnresolvedInteriorContact('Smart Interior: front openings cannot be separated with the selected clearance', 'FRONT_RIMS_NON_SEPARABLE')
    free,n,edge_a,edge_b=best
    return n,(edge_a+edge_b)*0.5,free


def _collision(a,b):
    if not a['faces'] or not b['faces'] or not aabb_overlap(bounds(a['verts']),bounds(b['verts'])):return False
    return bool(mesh_intersections(a['verts'],a['faces'],b['verts'],b['faces'],max_hits=1))


def collision_graph(meshes):
    """Deterministic X-sweep broad phase; narrow phase uses exact triangles.

    Never execute pairwise triangle checks on distant independent interiors.
    This is a broad-phase accelerator, not a geometric approximation.
    """
    rows=[]
    for name in sorted(meshes):
        mesh=meshes[name]
        if mesh.get('faces') and mesh.get('verts'):
            lo,hi=bounds(mesh['verts'])
            rows.append((lo[0],hi[0],name,(lo,hi)))
    rows.sort(key=lambda entry:(entry[0],entry[2]))
    pairs=[]
    for index,(_,xmax,a,ba) in enumerate(rows):
        for xmin,_,b,bb in rows[index+1:]:
            if xmin>xmax+EPS:break
            if aabb_overlap(ba,bb) and _collision(meshes[a],meshes[b]):
                pairs.append(tuple(sorted((a,b))))
    return sorted(pairs)


def contact_graph(meshes,*,clearance=0.0,classes=None):
    """Smart Interior group discovery, including sub-clearance planar cavities.

    A surface-triangle intersection alone does NOT identify nested, coincident,
    or almost-touching open cavities. For exact upright PLANAR BOX geometry,
    use footprint separation in world XY (only when Z ranges overlap). Other
    geometric types retain the existing strict triangle check.
    """
    pairs=set(collision_graph(meshes))
    gap=max(0.0,float(clearance))
    if gap<=0:return sorted(pairs)
    try:
        from .interior_footprint_core import footprint_clearance_xy
    except ImportError:
        from interior_footprint_core import footprint_clearance_xy
    classes=classes or {}
    ids=sorted(name for name,mesh in meshes.items() if mesh.get('verts') and mesh.get('faces'))
    eligible={'PLANAR','PLANAR_LEGACY'}
    bbs={name:bounds(meshes[name]['verts']) for name in ids}
    for i,a in enumerate(ids):
        if classes.get(a,'PLANAR') not in eligible:continue
        for b in ids[i+1:]:
            if (a,b) in pairs or classes.get(b,'PLANAR') not in eligible:continue
            lo,hi=bbs[a];lo2,hi2=bbs[b]
            if hi[2]<=lo2[2] or hi2[2]<=lo[2]:continue
            if any(hi[k]+gap<lo2[k] or hi2[k]+gap<lo[k] for k in (0,1)):
                continue
            d=footprint_clearance_xy(meshes[a],meshes[b])
            if d is not None and d+1e-6<gap:pairs.add((a,b))
    return sorted(pairs)


def connected_components(ids,pairs):
    adj={i:set() for i in ids}
    for a,b in pairs:adj[a].add(b);adj[b].add(a)
    out=[];seen=set()
    for i in sorted(ids):
        if i in seen:continue
        todo=[i];group=set()
        while todo:
            x=todo.pop()
            if x in group:continue
            group.add(x);todo.extend(adj[x]-group)
        seen|=group;out.append(tuple(sorted(group)))
    return out


def solve_group(canonical,*,clearance=0.002,mode='BALANCED',priority_id=None,classes=None):
    """Mutual deterministic halfspace resolution for aperture-separable cavities.

    For non-separable arrangements, refuse safely. The caller is responsible
    for world/local transforms and for transactional publication of all meshes.
    """
    if not math.isfinite(clearance) or clearance<0:raise ValueError('Clearance must be finite and nonnegative')
    if mode not in {'BALANCED','PRIORITY','CONTOUR_FOLLOW'}:raise ValueError('Unsupported Smart Interior mode')
    classes=classes or {}
    result={k:{'verts':list(v['verts']),'faces':list(v['faces']),'roles':list(v['roles'])} for k,v in canonical.items()}
    pairs=contact_graph(canonical,clearance=clearance,classes=classes)
    planes=[]
    if pairs:
        # Multiple physical Interior shells cannot be treated as a single
        # continuous prism by the legacy plane clipper. Detect the contact,
        # report it, and preserve the last valid mesh rather than merge shells.
        try:
            from .interior_footprint_core import _separate_shells
        except ImportError:
            from interior_footprint_core import _separate_shells
        if any(len(_separate_shells(result[key]))>1 for key in {name for pair in pairs for name in pair}):
            raise UnresolvedInteriorContact(
                'Smart Interior: конфликт разделённого Interior требует отдельной ручной обработки секций; объединённый Mesh не изменён',
                'MULTI_SHELL_CONTACT_UNSUPPORTED')
    for a,b in pairs:
        # Architectural planar windows around a wall corner are vertical
        # open-front extrusions. Their XY footprints can be mitered into
        # nonrectangular cavities BEFORE Blender publishes either window.
        # A shared 2D separator enforces the full requested physical gap.
        allow_planar_mitre=(str(classes.get(a,'PLANAR')) in {'PLANAR','PLANAR_LEGACY'} and
                            str(classes.get(b,'PLANAR')) in {'PLANAR','PLANAR_LEGACY'})
        planar_contour_eligible=False
        if mode in {'BALANCED','CONTOUR_FOLLOW'} and allow_planar_mitre:
            try:
                from .interior_footprint_core import mitre_pair
            except ImportError:  # standalone Blender-free regression harness
                from interior_footprint_core import mitre_pair
            try:
                from .interior_footprint_core import footprint
            except ImportError:
                from interior_footprint_core import footprint
            planar_contour_eligible=(footprint(result[a]) is not None and
                                     footprint(result[b]) is not None)
            contour=mitre_pair(result[a],result[b],clearance=clearance,
                               original_a=canonical[a],original_b=canonical[b])
            if contour is not None:
                result[a],result[b],details=contour
                planes.append((a,b,'XY_CONTOUR',details))
                continue
            if mode=='CONTOUR_FOLLOW':
                raise InteriorSolverUnavailable(
                    'Smart Interior: contact cannot be contour-mitered safely without altering the opening',
                    'CONTACT_CONTOUR_COMPLEX_UNSUPPORTED')
        if mode=='CONTOUR_FOLLOW':
            raise InteriorSolverUnavailable(
                'Smart Interior: CONTOUR_FOLLOW currently supports only vertical PLANAR BOX contacts',
                'CONTACT_CONTOUR_TYPE_UNSUPPORTED')
        try:
            n,mid,free=aperture_separation(canonical[a],canonical[b],clearance)
        except UnresolvedInteriorContact as exc:
            if planar_contour_eligible:
                raise UnresolvedInteriorContact(
                    'Smart Interior: угол нельзя согласовать с этим зазором без заметного изменения кромки Interior (предел подрезки 25 мм)',
                    'PLANAR_CORNER_CONTOUR_TRIM_LIMIT') from exc
            raise
        if mode=='BALANCED':
            try:
                result[a]=clip_open_cavity(result[a],n,mid-clearance*0.5,keep_low=True)
                result[b]=clip_open_cavity(result[b],n,mid+clearance*0.5,keep_low=False)
            except ValueError as exc:
                if str(exc).startswith(('Smart Interior would ', 'Smart Interior cap ',
                                        'Open cap chain', 'Interior cap has zero area',
                                        'Interior slice has complex/crossing cap',
                                        'Interior clipped away completely',
                                        'Interior produced self-intersections')):
                    raise UnresolvedInteriorContact(
                        'Smart Interior: contact cannot be clipped without damaging aperture: '+str(exc),
                        'CONTACT_CLIP_UNSUPPORTED') from exc
                raise
        elif priority_id in (a,b):
            # Preserve priority object's *whole* cavity, not only its mouth.
            protected=priority_id
            moving=b if protected==a else a
            if moving==b:
                bound=max(dot(n,p) for p in canonical[a]['verts'])+clearance
                result[b]=clip_open_cavity(result[b],n,bound,keep_low=False)
            else:
                bound=min(dot(n,p) for p in canonical[b]['verts'])-clearance
                result[a]=clip_open_cavity(result[a],n,bound,keep_low=True)
        else:
            raise ValueError('Priority mode requires an affected source window')
        planes.append((a,b,tuple(n),mid))
    remains=contact_graph(result,clearance=clearance,classes=classes)
    if remains:raise UnresolvedInteriorContact(f'Smart Interior insufficient gap/contacts after clipping: {remains}', 'CONTACT_REMAINS_AFTER_CLIP')
    for value in result.values():_validate_mesh(value)
    return result,{'policy':POLICY,'pairs':pairs,'planes':planes,'mode':mode,'clearance':clearance}
