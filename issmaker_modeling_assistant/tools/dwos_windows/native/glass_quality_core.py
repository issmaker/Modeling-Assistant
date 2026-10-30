"""Read-only physical plane alignment diagnostics for published CORNER glass.

Do not confuse pane counts with flatness or exact contacts. Blender independent.
The optional visual-grade check reports deviations; it never changes geometry.
"""
from __future__ import annotations
from math import acos, degrees, sqrt, isfinite
try:
    from .adaptive_opening_core import dot, cross, sub
except ImportError:
    from adaptive_opening_core import dot, cross, sub


def corner_glass_plane_quality(mesh,topology,*,tolerance_deg=.2):
    if not topology or topology.get('classification')!='CORNER':
        return {'status':'NOT_APPLICABLE','issues':[]}
    axes=[topology[k] for k in ('axis_x','axis_y','axis_z')]
    expected=[]
    for n in topology.get('segment_exterior_normals') or []:
        q=[dot(n,axis) for axis in axes];ll=sqrt(sum(c*c for c in q))
        if ll>1e-9:expected.append(tuple(c/ll for c in q))
    if not expected:
        return {'status':'UNVERIFIED','issues':['CORNER_GLASS_MISSING_NORMALS']}
    vertices=mesh.get('verts') or ();faces=mesh.get('faces') or ();roles=mesh.get('roles') or ()
    if len(faces)!=len(roles):
        return {'status':'UNVERIFIED','issues':['CORNER_GLASS_ROLE_MISMATCH']}
    worst=0.0;off=[];measured=0;worst_warp_m=0.0;warped_faces=[]
    for fi,(face,role) in enumerate(zip(faces,roles)):
        if role not in ('GLASS_EXTERIOR','GLASS_INTERIOR'):continue
        if len(face)<3:continue
        p0,p1,p2=(vertices[face[i]] for i in range(3))
        n=cross(sub(p1,p0),sub(p2,p0)); mag=sqrt(dot(n,n))
        if mag<=1e-10:continue
        u=tuple(c/mag for c in n)
        cos=max(min(1.0,abs(dot(u,ref))) for ref in expected)
        angle=degrees(acos(cos));worst=max(worst,angle);measured+=1
        if angle>tolerance_deg:off.append({'face':fi,'role':role,'deviation_deg':round(angle,5)})
        # Check all vertices, not just the first triangle. A twisted quad can
        # have an apparently valid normal while one corner is far off-plane.
        unit=tuple(c/mag for c in n)
        warp=max(abs(dot(sub(vertices[idx],p0),unit)) for idx in face[3:]) if len(face)>3 else 0.0
        worst_warp_m=max(worst_warp_m,warp)
        if warp>1.e-5:warped_faces.append({'face':fi,'warp_m':round(warp,7)})
    if not measured:return {'status':'UNVERIFIED','issues':['CORNER_GLASS_EMPTY_SHEET']}
    issues=[]
    if off:issues.append('CORNER_GLASS_FACET_NOT_ALIGNED_WITH_FACADE_PLANE')
    if warped_faces:issues.append('CORNER_GLASS_NONPLANAR_POLYGON')
    return {'status':'WARNING' if issues else 'PASS','issues':issues,
            'max_angle_deviation_deg':round(worst,5),'checked_faces':measured,
            'offending_faces':off[:30],'tolerance_deg':tolerance_deg,
            'max_polygon_warp_m':round(worst_warp_m,7),
            'warped_faces':warped_faces[:30]}
