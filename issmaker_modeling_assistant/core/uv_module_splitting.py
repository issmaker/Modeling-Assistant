"""Pass15.19: conservative 4m/2m tile planning for *planar DWOS glazing*.

Pure Python. Only a whole axis-aligned XY quad per independent glass sheet is
eligible for topology-safe UV tessellation. This does not split panes into
separate Blender objects; each resulting pane is one connected polygon sheet.
Frame, CORNER/CURVED and Smart Interior are explicitly outside this contract.
Never use this as an unconditional mesh subdivision utility.
"""
from collections import Counter
from math import isfinite
from .generation_uv_quality import stage_face_uvs

SCHEMA = 'DWOS_PLANAR_GLASS_UV_PATCHES_V1'
MAX_PATCHES = 2048
_TOL = 1.e-7


def _signed_xy_area(points):
    return .5 * sum(a[0]*b[1]-b[0]*a[1]
                    for a, b in zip(points, points[1:]+points[:1]))


def _spans(lo, hi, stride):
    if hi <= lo:
        return ()
    count = int((hi - lo) // stride)
    # Avoid a near-zero segment if the last division is nearly exact.
    boundaries = [lo] + [lo + i*stride for i in range(1, count+1)
                         if lo + i*stride < hi - _TOL] + [hi]
    return tuple(boundaries)


def _edge_components(faces):
    owners = {}
    adjacent = [set() for _ in faces]
    for fi, face in enumerate(faces):
        for a, b in zip(face, face[1:]+face[:1]):
            key = (min(a,b),max(a,b))
            if key in owners:
                adjacent[fi].add(owners[key]);adjacent[owners[key]].add(fi)
            else:
                owners[key] = fi
    seen = set();groups = 0
    for fi in range(len(faces)):
        if fi in seen:continue
        groups += 1;stack = [fi];seen.add(fi)
        while stack:
            for neighbor in adjacent[stack.pop()]:
                if neighbor not in seen:
                    seen.add(neighbor);stack.append(neighbor)
    return groups


def _reject(verts, faces, roles, reason):
    return {'state':'UNCHANGED','reason':reason,'verts':verts,'faces':faces,
            'roles':roles,'patches':0,'schema':SCHEMA}


def split_planar_glass_for_uv(vertices, faces, roles, *, resolution=4096,
                              pixels_per_meter=1000, max_patches=MAX_PATCHES):
    """Split oversized, independent XY rectangular sheets *all-or-nothing*.

    Both FULL panes remain separate but each stays edge-connected. Does not
    operate on adaptive glass or on mesh that has existing author UV. The caller
    must prove it is the freshly staged PLANAR glass, before Blender publication.
    """
    original_v = tuple(tuple(float(x) for x in p) for p in vertices)
    original_f = tuple(tuple(int(i) for i in face) for face in faces)
    original_r = tuple(str(r) for r in roles)
    if resolution not in (2048,4096) or pixels_per_meter != 1000:
        raise ValueError('Only 2K/4K and 1000 px/metre supported')
    if not 1 <= max_patches <= MAX_PATCHES:
        raise ValueError('Invalid patch limit')
    unchanged = lambda reason: _reject(original_v,original_f,original_r,reason)
    if (len(original_f) not in (1,2) or len(original_f)!=len(original_r)
            or len(original_v)>8):
        return unchanged('NON_SIMPLE_PLANAR_GLASS')
    if set(original_r) not in ({'GLASS_SINGLE'}, {'GLASS_EXTERIOR','GLASS_INTERIOR'}):
        return unchanged('GLASS_ROLE_CONTRACT')
    if not all(len(p)==3 and all(isfinite(x) for x in p) for p in original_v):
        return unchanged('NONFINITE_GEOMETRY')
    if any(len(f)!=4 or len(set(f))!=4
           or any(i<0 or i>=len(original_v) for i in f) for f in original_f):
        return unchanged('NON_RECTANGULAR_FACE')
    if len(set(i for face in original_f for i in face))!=len(original_f)*4:
        return unchanged('SHARED_GLASS_LAYER_VERTICES')
    sheets = []
    total_patches = 0
    tile = resolution/float(pixels_per_meter)
    # Contract uses whole 4m/2m modules, leaving room for 8 px UV padding.
    stride = float(int(tile))
    needs_split = False
    for face, role in zip(original_f,original_r):
        pts = [original_v[i] for i in face]
        z = pts[0][2]
        if any(abs(p[2]-z)>_TOL for p in pts):
            return unchanged('NONPLANAR_GLASS_SHEET')
        lowx, highx = min(p[0] for p in pts),max(p[0] for p in pts)
        lowy, highy = min(p[1] for p in pts),max(p[1] for p in pts)
        if highx-lowx<=_TOL or highy-lowy<=_TOL:
            return unchanged('ZERO_AREA_GLASS')
        corners = {(round(x,7),round(y,7)) for x,y in
                   ((lowx,lowy),(highx,lowy),(highx,highy),(lowx,highy))}
        if {(round(p[0],7),round(p[1],7)) for p in pts}!=corners:
            return unchanged('NON_RECTANGULAR_GLASS_SHEET')
        signed_area = _signed_xy_area(pts)
        expected = (highx-lowx)*(highy-lowy)
        if abs(abs(signed_area)-expected)>max(1.e-7,expected*1.e-8):
            return unchanged('GLASS_WINDING_OR_AREA')
        xs, ys = _spans(lowx,highx,stride),_spans(lowy,highy,stride)
        num = (len(xs)-1)*(len(ys)-1)
        if num<=0 or total_patches+num>max_patches:
            return unchanged('PATCH_LIMIT')
        total_patches += num
        needs_split |= num>1
        sheets.append((z,xs,ys,1 if signed_area>0 else -1,role,expected))
    if not needs_split:
        return unchanged('FITS_SINGLE_MODULE')

    new_v=[];new_f=[];new_r=[]
    for z,xs,ys,sign,role,area_before in sheets:
        offset=len(new_v)
        nx,ny=len(xs),len(ys)
        # A shared grid makes each sheet one connected mesh component.
        for y in ys:
            for x in xs:
                new_v.append((x,y,z))
        sheet_faces=[]
        for j in range(ny-1):
            for i in range(nx-1):
                bl=offset+j*nx+i
                br=bl+1
                tl=bl+nx
                tr=tl+1
                quad=(bl,br,tr,tl) if sign>0 else (bl,tl,tr,br)
                new_f.append(quad);sheet_faces.append(quad);new_r.append(role)
        area_after=sum(abs(_signed_xy_area([new_v[k] for k in f])) for f in sheet_faces)
        if (abs(area_after-area_before)>max(1.e-7,area_before*1.e-8)
                or _edge_components(sheet_faces)!=1):
            return unchanged('SHEET_AREA_OR_CONNECTIVITY_CHANGED')
    if len(new_f)!=len(new_r) or _edge_components(new_f)!=len(sheets):
        return unchanged('PANE_COUNT_CHANGED')
    # The UV contract is the *same* tested pure core as Pass15.18.
    uv_plan = stage_face_uvs(new_v,new_f,resolution=resolution,
                              pixels_per_meter=pixels_per_meter)
    if uv_plan['state']!='READY':
        return unchanged('PATCH_UV_'+uv_plan['reason'])
    return {'state':'SPLIT','reason':'OK','verts':tuple(new_v),'faces':tuple(new_f),
            'roles':tuple(new_r),'patches':len(new_f),'sheets':len(sheets),
            'module_m':stride,'schema':SCHEMA,'uv_ready':True}


def module_coverage_report(vertices, faces, *, resolution=4096, max_faces=10000):
    """Read-only, conservative triage for ANY Mesh, including INTERIOR/SURFACE.

    Large or unsafe polygons get a diagnostic, not an automatic topology edit.
    Measurements are mesh-local; non-unit object transforms require caution.
    """
    if resolution not in (2048,4096):
        raise ValueError('Unsupported resolution')
    if len(faces)>max_faces:
        return {'state':'TOO_LARGE','faces':len(faces),'tested':0,
                'eligible':0,'oversized':0,'unsafe':0,'reasons':{'FACE_COUNT_LIMIT':len(faces)},
                'examples':()}
    reasons=Counter(); examples=[]; eligible=oversized=unsafe=0
    for i,face in enumerate(faces):
        # One face per pure evaluation limits memory and lets us label each face.
        pts=[vertices[int(k)] for k in face]
        local=tuple(range(len(pts)))
        result=stage_face_uvs(pts,[local],resolution=resolution)
        state,reason=result['state'],result['reason']
        if state=='READY':eligible+=1
        elif reason=='FACE_EXCEEDS_TILE':oversized+=1
        else:unsafe+=1
        reasons[reason]+=1
        if state!='READY' and len(examples)<16:
            examples.append((i,reason))
    return {'state':'REVIEW' if oversized or unsafe else 'READY','faces':len(faces),
            'tested':len(faces),'eligible':eligible,'oversized':oversized,
            'unsafe':unsafe,'reasons':dict(reasons),'examples':tuple(examples),
            'resolution':resolution,'pixels_per_meter':1000}
