from __future__ import annotations
import json
import bpy, gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from .adaptive_opening_core import map_svd_host, map_svd_local
from ....core import lifecycle_lease as _lease

_HANDLE=None
_LEASE_OWNER="dwos.static_overlay"
_LEASE_TOKEN=None
_LIFECYCLE_ERRORS=[]

def _line(shader,coords,color):
    if len(coords)<2:return
    batch=batch_for_shader(shader,'LINES',{'pos':coords}); shader.bind(); shader.uniform_float('color',color); batch.draw(shader)

def _draw():
    ctx=bpy.context
    if not ctx.scene or not hasattr(ctx.scene,'dwos'): return
    shader=gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('LESS_EQUAL')
    try:
        # Detected openings.
        # Opening search overlay belongs ONLY to the actively selected source Host.
        # It must never remain visible above finished windows or unrelated objects.
        active=ctx.active_object
        if (ctx.scene.dwos.show_overlay and active is not None and
                active.type=='MESH' and active.name==ctx.scene.dwos.wall_name):
            host=bpy.data.objects.get(ctx.scene.dwos.wall_name)
            for c in ctx.scene.dwos.openings:
                if not c.enabled:continue
                try:
                    payload=json.loads(c.payload_json or '{}');topo=payload.get('adaptive_topology')
                except Exception:topo=None
                coords=[]
                if host is not None and isinstance(topo,dict) and topo.get('coordinate_system')=='SVD_RIBBON':
                    # Draw the real Host-authored boundary, not a flattened green box.
                    pts=[host.matrix_world@Vector(p) for p in topo.get('loop_points',[])]
                    for i,p in enumerate(pts):coords += [p,pts[(i+1)%len(pts)]] if pts else []
                else:
                    center=Vector(c.center);x=Vector(c.axis_x);y=Vector(c.axis_y)
                    try:w=float(c.width_text.replace(',','.'));h=float(c.height_text.replace(',','.'))
                    except:continue
                    pts=[center+x*(-w/2)+y*(-h/2),center+x*(w/2)+y*(-h/2),center+x*(w/2)+y*(h/2),center+x*(-w/2)+y*(h/2)]
                    for i,p in enumerate(pts):coords += [p,pts[(i+1)%4]]
                cls=(topo or {}).get('classification','PLANAR') if isinstance(topo,dict) else 'PLANAR'
                color={'CURVED':(0.15,0.85,1.0,1.0),'CORNER':(1.0,0.60,0.18,1.0)}.get(cls,(0.2,0.9,0.5,1.0))
                _line(shader,coords,color)
        obj=ctx.active_object
        if obj and getattr(obj,'dwos_window',None) and obj.dwos_window.is_window and not obj.dwos_window.finalized:
            s=obj.dwos_window; mw=obj.matrix_world
            try:w=float(s.width_text.replace(',','.')); h=float(s.height_text.replace(',','.')); z=float(s.frame_offset_text.replace(',','.'))
            except:return
            try:
                payload=json.loads(s.opening_payload_json or '{}');topo=payload.get('adaptive_topology')
            except Exception:topo=None
            coords=[]
            for sp in s.splits:
                try:p=float(sp.position_text.replace(',','.'))
                except:continue
                if isinstance(topo,dict) and topo.get('coordinate_system')=='SVD_RIBBON':
                    if sp.axis=='V':
                        ss=-w*.5+p;pts=[Vector(map_svd_local(topo,ss,-h*.5,z)),Vector(map_svd_local(topo,ss,h*.5,z))]
                        coords += [mw@pts[0],mw@pts[1]]
                    else:
                        vv=-h*.5+p;half=w*.5
                        cuts=[-half]+[float(st['s'])-half for st in topo.get('stations',[])[1:-1]]+[half]
                        pts=[mw@Vector(map_svd_local(topo,ss,vv,z)) for ss in cuts]
                        for a,b in zip(pts,pts[1:]):coords += [a,b]
                else:
                    if sp.axis=='V':a=Vector((-w/2+p,-h/2,z));b=Vector((-w/2+p,h/2,z))
                    else:a=Vector((-w/2,-h/2+p,z));b=Vector((w/2,-h/2+p,z))
                    coords += [mw@a,mw@b]
            _line(shader,coords,(1.0,0.65,0.1,1.0))
    finally:
        gpu.state.depth_test_set('NONE')

def _record_lifecycle(stage, exc):
    _LIFECYCLE_ERRORS.append(f'{stage}: {type(exc).__name__}: {exc}'[:250])
    del _LIFECYCLE_ERRORS[:-20]


def overlay_snapshot():
    return {'has_handle': _HANDLE is not None,
            'lease': _lease.snapshot(_LEASE_OWNER),
            'errors': list(_LIFECYCLE_ERRORS[-8:])}


def ensure_overlay():
    global _HANDLE, _LEASE_TOKEN
    if _HANDLE is not None and _lease.alive(_LEASE_OWNER, _LEASE_TOKEN):
        return
    if (_HANDLE is not None and _LEASE_TOKEN is not None
            and not _lease.snapshot(_LEASE_OWNER)['leased']):
        # driver_namespace was cleared on .blend load; keep the ONE existing
        # overlay rather than leaving it orphaned and adding a duplicate.
        _LEASE_TOKEN, _ = _lease.claim(_LEASE_OWNER)
        _lease.owned(_LEASE_OWNER, _LEASE_TOKEN)['handles'].append(_HANDLE)
        return
    _LEASE_TOKEN, prior = _lease.claim(_LEASE_OWNER)
    if prior:
        for old in prior.get('handles', ()):
            try:
                bpy.types.SpaceView3D.draw_handler_remove(old, 'WINDOW')
            except (ReferenceError, ValueError, RuntimeError) as exc:
                _record_lifecycle('old_overlay_remove', exc)
    try:
        _HANDLE = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
        row = _lease.owned(_LEASE_OWNER, _LEASE_TOKEN)
        row['handles'].append(_HANDLE)
    except Exception:
        _HANDLE = None
        _lease.retire(_LEASE_OWNER, _LEASE_TOKEN)
        _LEASE_TOKEN = None
        raise


def remove_overlay():
    global _HANDLE, _LEASE_TOKEN
    row = _lease.owned(_LEASE_OWNER, _LEASE_TOKEN)
    handles = list(row.get('handles', ())) if row else []
    if _HANDLE is not None and _HANDLE not in handles:
        handles.append(_HANDLE)
    _HANDLE = None
    for handle in handles:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
        except (ReferenceError, ValueError, RuntimeError) as exc:
            _record_lifecycle('overlay_remove', exc)
    _lease.retire(_LEASE_OWNER, _LEASE_TOKEN)
    _LEASE_TOKEN = None
