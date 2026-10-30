"""Wave 41: provenance-based occupied openings and live source invalidation.

Non-destructive, read-only helper for old/new opening discovery. The stable
geometric comparison intentionally does not rely on ephemeral candidate UUIDs.
All tolerances expressed in meters; conservative proof rather than guessing.
"""
from __future__ import annotations

import bmesh
import hashlib
import json
import math
from mathutils import Vector


def host_geometry_fingerprint(host):
    """Fingerprint live geometry, including Edit BMesh, and world transform."""
    if host is None or host.type != 'MESH':
        raise ValueError('HOST_NOT_MESH')
    h=hashlib.sha256()
    h.update(b'DWOS_OPENING_HOST_GEOMETRY_V1;')
    for row in host.matrix_world:
        h.update((','.join(f'{float(x):.8f}' for x in row)+';').encode('ascii'))
    if host.mode == 'EDIT':
        bm=bmesh.from_edit_mesh(host.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        h.update(f'V{len(bm.verts)};F{len(bm.faces)};'.encode('ascii'))
        for v in bm.verts:
            h.update((','.join(f'{float(c):.7f}' for c in v.co)+';').encode('ascii'))
        for f in bm.faces:
            h.update((','.join(str(v.index) for v in f.verts)+';').encode('ascii'))
        edges=[tuple(sorted((v.index for v in e.verts))) for e in bm.edges]
    else:
        mesh=host.data
        h.update(f'V{len(mesh.vertices)};F{len(mesh.polygons)};'.encode('ascii'))
        for v in mesh.vertices:
            h.update((','.join(f'{float(c):.7f}' for c in v.co)+';').encode('ascii'))
        for f in mesh.polygons:
            h.update((','.join(str(i) for i in f.vertices)+';').encode('ascii'))
        edges=[tuple(sorted((int(x) for x in e.vertices))) for e in mesh.edges]
    for edge in sorted(edges):
        h.update(f'E{edge[0]},{edge[1]};'.encode('ascii'))
    return h.hexdigest()


def _description(payload):
    if not isinstance(payload,dict):
        return None
    try:
        c=Vector(tuple(float(x) for x in payload['center_local']))
        z=Vector(tuple(float(x) for x in payload['axis_z_local']))
        w=float(payload['width'])
        h=float(payload['height'])
        if len(c)!=3 or len(z)!=3 or not all(math.isfinite(float(v)) for v in tuple(c)+tuple(z)+(w,h)):
            return None
        if z.length < 0.99 or w<=0 or h<=0:
            return None
        return (c,z.normalized(),w,h,str(payload.get('adaptive_classification','PLANAR')).upper())
    except (KeyError,ValueError,TypeError,OverflowError):
        return None


def occupied_window_for_candidate(payload,host,scene):
    """Return exact matching DWOS window, or None if not conclusively occupied.

    Requires same host + geometric center/width/height/classification, matching
    face normal, and a real published controller still near authored center.
    Old windows are supported through stored opening_payload_json.
    """
    candidate=_description(payload)
    if candidate is None:
        return None
    center,normal,width,height,classification=candidate
    for obj in scene.objects:
        if obj == host or obj.type != 'MESH':
            continue
        settings=getattr(obj,'dwos_window',None)
        if settings is None or not getattr(settings,'is_window',False):
            continue
        if str(getattr(settings,'host_name',''))!=host.name:
            continue
        try:
            old=json.loads(str(settings.opening_payload_json))
            desc=_description(old)
        except (ValueError,TypeError):
            continue
        if desc is None:
            continue
        other_center,other_normal,other_width,other_height,other_cls=desc
        size_eps=max(0.003,0.0002*max(width,height))
        center_eps=max(0.004,0.0002*max(width,height))
        if classification != other_cls:
            continue
        if abs(width-other_width)>size_eps or abs(height-other_height)>size_eps:
            continue
        if (center-other_center).length>center_eps:
            continue
        if normal.dot(other_normal)<0.995:
            continue
        # Verify the controller has not been manually moved elsewhere.
        world=host.matrix_world @ other_center
        if (obj.matrix_world.translation-world).length>max(.025,center_eps*3):
            continue
        return obj
    return None


def available_candidates(found,host,scene):
    available=[]
    occupied=[]
    for data in found:
        target=occupied_window_for_candidate(data,host,scene)
        if target is None:
            available.append(data)
        else:
            occupied.append((data,target))
    return available,occupied
