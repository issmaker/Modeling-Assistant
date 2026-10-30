"""Pure source-module UV atlas and LP cell provenance (Wave 34).

Per-loop UV ownership is authoritative; coincident Mesh vertex positions may
legitimately have different UV coordinates on two adjacent source modules.
No UV continuity is invented across author-defined module seams.
"""
from __future__ import annotations
import math


def _cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def _sub(a, b):
    return (a[0]-b[0], a[1]-b[1])


def barycentric(p, a, b, c):
    ab=_sub(b,a); ac=_sub(c,a); ap=_sub(p,a)
    det=_cross(ab,ac)
    if abs(det)<1e-12:
        raise ValueError('SOURCE_UV_ZERO_TRIANGLE')
    u=_cross(ap,ac)/det
    v=_cross(ab,ap)/det
    return (1-u-v,u,v)


def module_seams_from_edges(edges, tol=2e-5):
    """Extract axis-aligned internal source-face boundaries; nonaxial = BLOCKED."""
    cuts_x=[];cuts_y=[]
    for e in edges:
        a,b=e['xy']
        dx=b[0]-a[0];dy=b[1]-a[1]
        if math.hypot(dx,dy)<1e-9:
            raise ValueError('SOURCE_MODULE_ZERO_EDGE:'+str(e))
        if abs(dx)<=tol:
            cuts_x.append((a[0]+b[0])*.5)
        elif abs(dy)<=tol:
            cuts_y.append((a[1]+b[1])*.5)
        else:
            raise ValueError('SOURCE_MODULE_OBLIQUE_SEAM_UNSUPPORTED:'+str(e))
    return {'x':cuts_x,'y':cuts_y,'edges':edges}


def _test_point_in_triangle(p, tri, tol=2e-5):
    w=barycentric(p,*tri['xy'])
    # Physical tolerance, not barycentric tolerance: this handles 7µm
    # float32 projection jitter seen in supplied Blender 4.5 diagnostic.
    max_edge=max(math.dist(tri['xy'][j],tri['xy'][(j+1)%3]) for j in range(3))
    eps=tol/max(max_edge,1e-5)
    return min(w)>=-eps and max(w)<=1+eps


def _sample_atlas_triangle(tri,p):
    w=barycentric(p,*tri['xy'])
    return (sum(w[k]*tri['uv'][k][0] for k in range(3)),
            sum(w[k]*tri['uv'][k][1] for k in range(3)))


def locate_source_face(atlas, p, tol=2e-5):
    for tri in atlas['triangles']:
        if _test_point_in_triangle(p,tri,tol):
            return int(tri['face_id'])
    raise ValueError('LP_CELL_OUTSIDE_SOURCE_UV_ATLAS:'+str(tuple(round(x,7) for x in p)))


def face_uv_at(atlas,face_id,p,tol=2e-5):
    for tri in atlas['triangles']:
        if int(tri['face_id'])==face_id and _test_point_in_triangle(p,tri,tol):
            return _sample_atlas_triangle(tri,p)
    # An LP cell may touch a source module border within the projection's
    # ~7µm tolerance. Extrapolate only from the SAME source polygon's triangle
    # if the corner is within physical tolerance of that polygon.
    same=[tri for tri in atlas['triangles'] if int(tri['face_id'])==face_id]
    if same:
        candidate=min(same,key=lambda t:max(0.,-min(barycentric(p,*t['xy']))))
        w=barycentric(p,*candidate['xy'])
        extent=max(math.dist(candidate['xy'][j],candidate['xy'][(j+1)%3]) for j in range(3))
        if min(w)>=-tol/max(extent,1e-5):
            return _sample_atlas_triangle(candidate,p)
    raise ValueError('UV_MODULE_CELL_CROSSES_SOURCE_SEAM:'+str({'face':face_id,'xy':p}))


def annotate_graph(graph, atlas):
    if not atlas or not atlas.get('triangles'):
        raise ValueError('LP_FIRST_REQUIRES_SOURCE_UV_ATLAS: restart with Step 1')
    counts={}
    for cell in graph['cells']:
        x0,x1,y0,y1=cell['rect']
        midpoint=((x0+x1)*.5,(y0+y1)*.5)
        face_id=locate_source_face(atlas,midpoint)
        uv=[face_uv_at(atlas,face_id,p) for p in cell['corners']]
        cell['module']=face_id
        cell['source_uv']=uv
        counts[face_id]=counts.get(face_id,0)+1
    graph['source_module_counts']=counts
    return graph


def module_front_uv_audit(vertices,faces,roles,parts,uvs,modules,resolution=4096,target=1000.,tolerance=.012):
    """Independent density, seam and role audit for LP-FIRST FRONT faces.

    Different module IDs are an intentional per-loop seam even if vertices
    have the same Mesh index. Source non-1000 UV is a WARN, not a fake PASS.
    """
    out={'measured_edges':0,'density_min':None,'density_max':None,
         'bad_edges':[],'unintended_seams':[],'source_module_seams':[],
         'module_ids':[], 'face_module_counts':{},'missing_provenance':[]}
    densities=[];seen={};intentional=set()
    for fi,(face,role,part,uv,module) in enumerate(zip(faces,roles,parts,uvs,modules)):
        if part!='FRONT' or role not in ('FRAME','MULLION'):
            continue
        if uv is None or module is None:
            out['missing_provenance'].append(fi);continue
        out['face_module_counts'][str(module)]=out['face_module_counts'].get(str(module),0)+1
        for vid,vec in zip(face,uv):
            prior=seen.setdefault(vid,[])
            for other_module,other_uv in prior:
                if math.dist(other_uv,vec)>1e-6:
                    key=(min(module,other_module),max(module,other_module),vid)
                    if other_module!=module:
                        if key not in intentional:
                            intentional.add(key)
                            out['source_module_seams'].append({'vertex':vid,'modules':[other_module,module]})
                    else:
                        out['unintended_seams'].append({'vertex':vid,'module':module})
            prior.append((module,vec))
        for a,b,ua,ub in zip(face,face[1:]+face[:1],uv,uv[1:]+uv[:1]):
            d=math.dist(vertices[a],vertices[b]);t=math.dist(ua,ub)
            if d<1e-8:continue
            dens=t*resolution/d;densities.append(dens)
            if abs(dens-target)>target*tolerance:
                out['bad_edges'].append({'face':fi,'module':module,'edge':[a,b],'density':round(dens,5)})
    out['measured_edges']=len(densities)
    if densities:
        out['density_min']=round(min(densities),5);out['density_max']=round(max(densities),5)
    out['module_ids']=sorted({x for x in modules if x is not None})
    out['status']=('FAIL' if out['unintended_seams'] or out['missing_provenance']
                   else 'WARN_SOURCE_UV_DENSITY' if out['bad_edges'] else 'PASS')
    return out


def reveal_metric_uv_audit(vertices,faces,roles,parts,uvs,resolution=4096,target=1000.,tolerance=.002):
    """Physically measure *new* GLASS_REVEAL surfaces separately from source UV.

    A source UV seam may be intentional but a stretched depth profile is not.
    """
    densities=[];bad=[];measured_faces=0
    for i,(face,role,part,uv) in enumerate(zip(faces,roles,parts,uvs)):
        if part!='GLASS_REVEAL' or role not in ('FRAME','MULLION'):continue
        measured_faces+=1
        if not uv or len(uv)!=len(face):
            bad.append({'face':i,'reason':'MISSING_REVEAL_UV'});continue
        for a,b,ua,ub in zip(face,face[1:]+face[:1],uv,uv[1:]+uv[:1]):
            physical=math.dist(vertices[a],vertices[b])
            if physical<1e-9:continue
            den=math.dist(ua,ub)*resolution/physical
            densities.append(den)
            if abs(den-target)>target*tolerance:
                bad.append({'face':i,'edge':[a,b],'px_m':round(den,6)})
    return {'measured_faces':measured_faces,'measured_edges':len(densities),
            'min_px_m':round(min(densities),6) if densities else None,
            'max_px_m':round(max(densities),6) if densities else None,
            'bad_edge_count':len(bad),'bad_edges':bad[:50]}
