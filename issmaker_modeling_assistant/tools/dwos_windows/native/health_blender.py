"""Low-cost Blender adapter for DWOS Health; no Mesh mutation during UI draw."""
from __future__ import annotations
import json
from .health_core import classify

LINKS_KEY='DWOS_smart_interior_contact_links_v372'


def _interior(frame):
    return next((o for o in getattr(frame,'children',()) if
                 getattr(o,'type',None)=='MESH' and o.get('DWOS_role')=='INTERIOR'),None)


def related_names(frame):
    cavity=_interior(frame)
    if cavity is None:
        return ()
    try:
        links=json.loads(cavity.get(LINKS_KEY,'[]') or '[]')
    except (ValueError,TypeError):
        return ()
    return tuple(sorted({name for name in links if isinstance(name,str)})) if isinstance(links,list) else ()


def health_of(frame,scene=None):
    if frame is None:
        return classify(is_window=False)
    settings=getattr(frame,'dwos_window',None)
    if not getattr(settings,'is_window',False):
        return classify(is_window=False)
    cavity=_interior(frame)
    classification=str(frame.get('DWOS_adaptive_classification','PLANAR'))
    # GLASS layer count is only asserted when the invariant metadata is present.
    glass=next((o for o in getattr(frame,'children',()) if
                getattr(o,'type',None)=='MESH' and o.get('DWOS_role')=='GLASS'),None)
    glass_layers=(glass.get('DWOS_curved_glass_layer_count') if glass and classification=='CURVED'
                  else frame.get('DWOS_corner_glass_panel_count') if classification=='CORNER' else None)
    lock=frame.get('DWOS_corner_full_geometry_locked') if classification=='CORNER' and 'DWOS_corner_full_geometry_locked' in frame else None
    links=related_names(frame)
    return classify(is_window=True,finalized=bool(settings.finalized),
        pending=bool(frame.get('DWOS_smart_interior_pending',False) or
                     (cavity and cavity.get('DWOS_smart_interior_pending',False))),
        reason_code=str(frame.get('DWOS_smart_interior_reason_code','')),
        validation_error=str(frame.get('DWOS_last_validation_error','')),
        contact_count=len(links), classification=classification,
        glass_layers=glass_layers,frame_geometry_locked=lock,
        smart_enabled=bool(getattr(getattr(scene,'dwos',None),'smart_interior_enabled',True)))
