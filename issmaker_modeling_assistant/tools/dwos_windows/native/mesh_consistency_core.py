"""DWOS 3.7.13: read-only geometric consistency auditor (no Blender imports).

Operate on the *published* frame/glass mesh and polygon roles rather than
trusting cached controller metadata.  Suitable for staging and field diagnosis.

This intentionally does not assert a watertight frame: its outer Host contact
is open for welding. It also does not infer visual overlap from component count.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from math import isfinite

GLASS_ROLES = frozenset({'GLASS_EXTERIOR', 'GLASS_INTERIOR'})


def _mesh(m):
    return tuple(m.get('verts', ())), tuple(tuple(int(i) for i in f) for f in m.get('faces', ())), tuple(str(r) for r in m.get('roles', ()))


def _components(faces, selected):
    """Connected face patches by shared *mesh edge*, not just a vertex."""
    owners=defaultdict(list)
    selected=tuple(selected)
    for fi in selected:
        f=faces[fi]
        for a,b in zip(f, f[1:]+f[:1]):
            owners[(min(a,b), max(a,b))].append(fi)
    adj={i:set() for i in selected}
    for ids in owners.values():
        for other in ids[1:]:
            adj[ids[0]].add(other);adj[other].add(ids[0])
    seen=set(); groups=[]
    for seed in selected:
        if seed in seen:continue
        stack=[seed];seen.add(seed); group=[]
        while stack:
            cur=stack.pop();group.append(cur)
            for nxt in adj[cur]:
                if nxt not in seen:seen.add(nxt);stack.append(nxt)
        groups.append(group)
    return groups


def audit_glass(mesh, classification, *, expected_panels=None, allow_subpanes=False):
    """Verify actual per-role pane geometry. No pass for absent role metadata."""
    cls=str(classification).upper()
    result={'classification':cls,'status':'NOT_APPLICABLE','issues':[], 'pane_counts':{},'face_count':0}
    if cls not in ('CORNER','CURVED'):return result
    verts,faces,roles=_mesh(mesh)
    issues=[]
    if len(faces)!=len(roles):issues.append('GLASS_ROLE_COUNT_MISMATCH')
    if not faces:issues.append('GLASS_EMPTY')
    if not verts or any(len(v)!=3 or not all(isfinite(float(x)) for x in v) for v in verts):
        issues.append('GLASS_INVALID_VERTICES')
    for f in faces:
        if len(f)<3 or len(set(f))<3 or any(i<0 or i>=len(verts) for i in f):
            issues.append('GLASS_INVALID_FACE');break
    result['face_count']=len(faces)
    if issues:
        result.update(status='FAIL',issues=sorted(set(issues)))
        return result
    actual=set(roles)
    if actual!=GLASS_ROLES:issues.append('GLASS_MISSING_OR_EXTRA_ROLE')
    minimum=2 if cls=='CORNER' else 1
    target=(int(expected_panels) if (allow_subpanes and expected_panels is not None) else minimum)
    if target<minimum:
        issues.append('GLASS_BELOW_MINIMUM_FACADE_PANES')
    if not allow_subpanes and expected_panels is not None and int(expected_panels)!=minimum:
        issues.append('GLASS_EXPECTED_PANEL_METADATA_MISMATCH')
    groups_by_role={}
    for role in ('GLASS_EXTERIOR','GLASS_INTERIOR'):
        indexes=[i for i,r in enumerate(roles) if r==role]
        groups=_components(faces,indexes)
        groups_by_role[role]=groups
        result['pane_counts'][role]=len(groups)
        if len(groups)!=target:issues.append(role+'_PANE_COUNT')
    # Each geometric pane must contain nonzero-area polygons. This catches
    # collapsed/duplicated faces masquerading as the required pane count.
    for role,groups in groups_by_role.items():
        for group in groups:
            used=set(i for fi in group for i in faces[fi])
            if len(used)<3:
                issues.append(role+'_COLLAPSED_PANE')
            else:
                xs=[verts[i] for i in used]
                if max(max(float(p[k]) for p in xs)-min(float(p[k]) for p in xs) for k in range(3))<=1e-10:
                    issues.append(role+'_COLLAPSED_PANE')
    # An expected number of panes cannot be faked by cloning the same sheet
    # onto itself. Compare *physical* point clouds, not polygon indices.
    pane_clouds=[]
    for role,groups in groups_by_role.items():
        for group in groups:
            cloud=frozenset(tuple(round(float(c),7) for c in verts[i])
                            for fi in group for i in faces[fi])
            pane_clouds.append((role,cloud))
    for i,(role,cloud) in enumerate(pane_clouds):
        for other_role,other_cloud in pane_clouds[i+1:]:
            if cloud and cloud==other_cloud:
                issues.append('GLASS_COINCIDENT_PANES' if role==other_role else 'GLASS_COINCIDENT_LAYERS')
    result.update(status='FAIL' if issues else 'PASS',issues=sorted(set(issues)))
    return result


def audit_frame(mesh, classification, *, expected_components=None):
    cls=str(classification).upper()
    result={'classification':cls,'status':'NOT_APPLICABLE','issues':[]}
    if cls not in ('CORNER','CURVED'):return result
    verts,faces,roles=_mesh(mesh)
    issues=[]
    if not faces or not verts:issues.append('FRAME_EMPTY')
    if len(faces)!=len(roles):issues.append('FRAME_ROLE_COUNT_MISMATCH')
    if any(len(v)!=3 or not all(isfinite(float(x)) for x in v) for v in verts):issues.append('FRAME_INVALID_VERTICES')
    incidence=Counter();used=set()
    for f in faces:
        if len(f)<3 or len(set(f))<3 or any(i<0 or i>=len(verts) for i in f):
            issues.append('FRAME_INVALID_FACE');continue
        used.update(f)
        for a,b in zip(f,f[1:]+f[:1]):incidence[(min(a,b),max(a,b))]+=1
    # Field meshes can be edited after creation. Detect zero-area faces in the
    # published mesh even if the original generated mesh passed staging.
    for f in faces:
        if len(f)<3 or any(i<0 or i>=len(verts) for i in f):continue
        area_max=0.0
        for i in range(len(f)):
            a,b,c=(verts[f[j%len(f)]] for j in (i,i+1,i+2))
            u=[float(b[k])-float(a[k]) for k in range(3)]
            v=[float(c[k])-float(a[k]) for k in range(3)]
            area2=sum(x*x for x in (u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]))
            area_max=max(area_max,area2)
        if area_max<=4e-24:
            issues.append('FRAME_ZERO_AREA_FACE');break
    result['frame_vertices']=len(verts);result['frame_faces']=len(faces)
    result['nonmanifold_edges']=sum(v>2 for v in incidence.values())
    result['boundary_edges']=sum(v==1 for v in incidence.values())
    if result['nonmanifold_edges']:issues.append('FRAME_NONMANIFOLD_EDGE')
    if used!=set(range(len(verts))):issues.append('FRAME_UNUSED_VERTICES')
    # RC23: an explicitly tagged four-quad open shell is deliberately a *second*
    # edge-disconnected component, not a defect. Validate its real topology;
    # do not relax the test for any other disconnected face.
    independent_faces=[i for i,r in enumerate(roles) if r=='FRAME_CORNER_POST_SHELL']
    if independent_faces:
        try:
            from .corner_post_experiment_core import audit_open_shell
        except ImportError:
            from corner_post_experiment_core import audit_open_shell
        shell_gate=audit_open_shell(mesh)
        result['independent_corner_post']=shell_gate
        if shell_gate['status']!='PASS':issues.extend(shell_gate['issues'])
    # The frame is required to be one connected component for CORNER, even
    # when no mullions are present. CURVED can use different legacy pathways.
    if not any(i in issues for i in ('FRAME_INVALID_FACE','FRAME_EMPTY')):
        base_face_ids=[i for i in range(len(faces)) if i not in independent_faces]
        base_components=len(_components(faces,base_face_ids))
        components=len(_components(faces,range(len(faces))))
        result['frame_components']=components
        result['structural_frame_components']=base_components
        if cls=='CORNER' and (base_components!=1 or components!=(2 if independent_faces else 1)):
            issues.append('FRAME_DISCONNECTED')
        if expected_components is not None and int(expected_components)!=components:
            issues.append('FRAME_PUBLISHED_COMPONENTS_MISMATCH')
    result.update(status='FAIL' if issues else 'PASS',issues=sorted(set(issues)))
    return result


def audit_window(frame_mesh,glass_mesh,classification,*,expected_panels=None,expected_frame_components=None):
    frame=audit_frame(frame_mesh,classification,expected_components=expected_frame_components)
    glass=audit_glass(glass_mesh,classification,expected_panels=expected_panels)
    issues=['frame:'+s for s in frame['issues']]+['glass:'+s for s in glass['issues']]
    return {'classification':str(classification).upper(),'status':'FAIL' if issues else 'PASS' if frame['status']=='PASS' and glass['status']=='PASS' else 'NOT_APPLICABLE',
            'issues':issues,'frame':frame,'glass':glass}


# 3.7.13: staging-only test of the parametric Host boundary. The published
# Blender Mesh does not necessarily retain vertex_params, so do not use this
# routine to make unverifiable claims about an arbitrary edited Mesh.
def audit_staged_corner_boundary(mesh, topology, tolerance=2.0e-6):
    """Verify every open Frame edge belongs to the actual Host aperture rim.

    Checks the generated topology before Blender data-block mutation. An
    interior opening is a failed frame even if the Mesh remains connected and
    passes face-role counts. No destructive auto-repair is attempted here.
    """
    result={'status':'FAIL','issues':[], 'open_internal_edges':0,
            'open_host_edges':0,'nonmanifold_edges':0}
    if not isinstance(topology,dict) or topology.get('classification')!='CORNER':
        result['issues'].append('STAGING_NOT_CORNER');return result
    verts,faces,roles=_mesh(mesh)
    params=list(mesh.get('vertex_params') or [])
    if len(params)!=len(verts):
        result['issues'].append('STAGING_PARAMETER_LENGTH_MISMATCH');return result
    if not faces or len(faces)!=len(roles):
        result['issues'].append('STAGING_FRAME_INVALID');return result
    half_w=float(topology['width'])*.5
    half_h=float(topology['height'])*.5
    incidence=Counter()
    for face in faces:
        if len(face)<3 or any(not isinstance(i,int) or i<0 or i>=len(params) for i in face):
            result['issues'].append('STAGING_FACE_INVALID');return result
        for a,b in zip(face,face[1:]+face[:1]):
            incidence[tuple(sorted((a,b)))]+=1
    samples=[]
    for (a,b),count in incidence.items():
        if count>2:
            result['nonmanifold_edges']+=1
        elif count==1:
            pa,pb=params[a],params[b]
            valid=any(abs(float(pa[axis])-value)<=tolerance and
                      abs(float(pb[axis])-value)<=tolerance
                      for axis,value in ((0,-half_w),(0,half_w),(1,-half_h),(1,half_h)))
            if valid:result['open_host_edges']+=1
            else:
                result['open_internal_edges']+=1
                if len(samples)<8:samples.append([a,b])
    if result['nonmanifold_edges']:
        result['issues'].append('STAGING_NONMANIFOLD_EDGE')
    if result['open_internal_edges']:
        result['issues'].append('STAGING_OPEN_INTERNAL_EDGE')
    if result['open_host_edges']==0:
        result['issues'].append('STAGING_HOST_BOUNDARY_MISSING')
    result['samples']=samples
    result['status']='FAIL' if result['issues'] else 'PASS'
    return result
