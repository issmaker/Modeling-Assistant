"""DWOS paired LOD publisher: staged source facades and scene-only visibility."""
from __future__ import annotations

import bpy
from .numeric import parse_meter_text
from .lp_facade_core import build_planar_lp
from .adaptive_opening_core import map_svd_local
from .material_system import ensure_frame_material, ensure_glass_material

HP_COLLECTION='ISSMAKER_WINDOWS_HP'
LP_COLLECTION='ISSMAKER_WINDOWS_LP'


def stage_planar_lp_mesh(frame):
    """Generate a new, unlinked mesh. Unsupported adaptive modes return None."""
    from .window_assembly import _adaptive_topology
    from .window_builder import _splits_from_settings
    from .zone_core import decode
    topology=_adaptive_topology(frame)
    if topology is not None and str(topology.get('classification','')).upper()!='PLANAR':
        return None
    if str(frame.get('DWOS_adaptive_classification','PLANAR')).upper() in {'CORNER','CURVED'}:
        return None
    settings=frame.dwos_window
    w=parse_meter_text(settings.width_text)
    h=parse_meter_text(settings.height_text)
    fw=parse_meter_text(settings.frame_width_text)
    front=float(frame.get('DWOS_frame_front_z',0.0))
    mapper=(lambda s,v,d:map_svd_local(topology,s,v,d)) if topology is not None else None
    design=build_planar_lp(w,h,fw,_splits_from_settings(settings,w,h),front=front,
                            zone_tree=decode(settings.zone_tree_json) if settings.zone_tree_json else None,
                            outward_sign=1 if settings.flip_interior else -1,mapper=mapper)
    me=bpy.data.meshes.new(frame.name+'_LP_mesh')
    try:
        me.from_pydata(design['verts'],[],design['faces'])
        # Do not call Mesh.validate() to silently delete triangles/faces: the
        # topology produced by the pure core is an exact contract.
        me.update()
        if len(me.polygons)!=design['face_count'] or len(me.vertices)!=len(design['verts']):
            raise RuntimeError('LP topology changed during Blender publication')
        me.materials.append(ensure_frame_material(settings))
        me.materials.append(ensure_glass_material())
        for poly,role in zip(me.polygons,design['roles']):
            poly.material_index=0 if role=='FRAME' else 1
        # HP and LP frame strips receive the same 1000px/m metric density.
        # GLASS faces intentionally receive no authored UV coordinates. Blender
        # Mesh UV layers always have loop entries for every face; (0,0) on
        # glass is only the neutral placeholder, not a glazing unwrap.
        width_uv=float(w)*1000./4096.
        height_uv=float(h)*1000./4096.
        pad=8./4096.
        if width_uv+2*pad <= 1. and height_uv+2*pad <= 1.:
            layer=me.uv_layers.new(name='UVMap')
            sign=1 if settings.flip_interior else -1
            min_u=min(sign*v[0] for v in design['metric_params'])
            min_v=min(v[1] for v in design['metric_params'])
            for poly,role in zip(me.polygons,design['roles']):
                if role!='FRAME':
                    continue
                for loop_id in poly.loop_indices:
                    vert_id=me.loops[loop_id].vertex_index
                    x,y=design['metric_params'][vert_id]
                    layer.data[loop_id].uv=((sign*x-min_u)*1000./4096.+pad,
                                            (y-min_v)*1000./4096.+pad)
            me['DWOS_LP_uv_state']='READY_1000_PX_M_FRAME_ONLY'
        else:
            me['DWOS_LP_uv_state']='UNMAPPED_TILE_EXCEEDED'
        me['DWOS_LP_SCHEMA']=design['version']
        me['DWOS_LP_frame_faces']=design['frame_faces']
        me['DWOS_LP_glass_faces']=design['glass_faces']
        me['DWOS_LP_coverage_m2']=float(design['coverage_m2'])
        return me
    except Exception:
        if me.users==0:
            bpy.data.meshes.remove(me)
        raise


def get_or_create_collection(scene,name,created_collections):
    # Always use the collection from *this* scene, even if another scene has
    # a collection with the same name. No alteration of foreign scene links.
    collection=scene.collection.children.get(name)
    if collection is None:
        collection=bpy.data.collections.new(name)
        scene.collection.children.link(collection)
        created_collections.append(collection)
    return collection


def apply_lod_visibility(scene,view_layer,mode):
    if mode not in {'HP','LP','BOTH','NONE'}:
        raise ValueError('Unsupported LOD view mode')
    before=[]
    try:
        for obj in scene.objects:
            layer=str(obj.get('DWOS_LOD_TYPE',''))
            if layer not in {'HP','LP'}:
                continue
            previous=obj.hide_get(view_layer=view_layer)
            before.append((obj,previous))
            obj.hide_set(not (mode=='BOTH' or layer==mode),view_layer=view_layer)
    except Exception:
        for obj,previous in reversed(before):
            try:obj.hide_set(previous,view_layer=view_layer)
            except Exception:pass
        raise
    return len(before)
