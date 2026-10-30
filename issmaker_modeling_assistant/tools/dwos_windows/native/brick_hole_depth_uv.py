"""DWOS W58.5: unfold profile reveals from authored front UV, using Brick Hole math.

Each reveal is anchored to an EXISTING front edge. Never restart UV at the
start of a reveal and never scale a face independently. Non-developable
surfaces require independent triangle charts; those cannot be fake quads.
"""
from __future__ import annotations
import json
import math
from collections import defaultdict
from mathutils import Vector
from ...brick_hole import unfold_reveal_quad, unfolded_third_uv, validate_triangle_uv_metric

def unwrap_frame_reveals(frame, resolution=4096):
    me=frame.data
    layer=me.uv_layers.active
    if layer is None:
        raise ValueError("DWOS_BRICK_DEPTH_MISSING_UV")
    roles=json.loads(str(frame.get("DWOS_face_roles","[]")))
    if len(roles)!=len(me.polygons):
        raise ValueError("DWOS_BRICK_DEPTH_ROLES_MISMATCH")
    front_edge={}
    for face in me.polygons:
        if roles[face.index] not in ("FRAME_EXTERIOR","FRAME_MULLION_VERTICAL"):
            continue
        ids=[me.loops[li].vertex_index for li in face.loop_indices]
        for k in range(len(ids)):
            a,b=ids[k],ids[(k+1)%len(ids)]
            uv_a=tuple(layer.data[face.loop_indices[k]].uv)
            uv_b=tuple(layer.data[face.loop_indices[(k+1)%len(ids)]].uv)
            other_index=(k+2)%len(ids)
            uv_third=tuple(layer.data[face.loop_indices[other_index]].uv)
            # Each edge records the first front triangle holding it,
            # preserving exactly the authored UV edge and winding.
            if frozenset((a,b)) not in front_edge:
                front_edge[frozenset((a,b))]=(a,b,uv_a,uv_b,uv_third)
    results=[]
    for face in me.polygons:
        if roles[face.index] not in (
            "FRAME_REVEAL","FRAME_MULLION_VERTICAL_SIDE_NEG",
            "FRAME_MULLION_VERTICAL_SIDE_POS"):
            continue
        ids=[me.loops[i].vertex_index for i in face.loop_indices]
        if face.loop_total==3:
            # W58.33: a local HP micro-edge seam can correctly turn one reveal
            # quad into a triangle. Anchor its real facade edge exactly as for
            # quads; unfold the third point from 3D edge lengths at 1000 px/m.
            front_matches=[i for i in range(3) if frozenset((ids[i],ids[(i+1)%3])) in front_edge]
            if len(front_matches)==0:
                _repair_events=[]
                try:_repair_events=json.loads(str(frame.get("W5833_hp_local_micro_edge_repairs","[]")))
                except (ValueError,TypeError):_repair_events=[]
                _linear=frame.matrix_world.to_3x3()
                _local_points=[tuple(_linear@me.vertices[idx].co) for idx in ids]
                _centroid=[sum(q[k] for q in _local_points)/3. for k in range(3)]
                _near_node=False
                for _event in _repair_events:
                    _mid=_event.get("midpoint_xyz") or ()
                    if len(_mid)==3:
                        _local_mid=tuple(_linear@Vector(_mid))
                        if math.dist(_centroid,_local_mid)<=.05:
                            _near_node=True;break
                if _near_node:
                    from ...brick_hole.uv_geometry_fallback_core import calculate_metric_island_uv
                    _island,_meta=calculate_metric_island_uv(_local_points,resolution=resolution)
                    for _li,_uv in zip(face.loop_indices,_island):
                        layer.data[_li].uv=_uv
                    results.append(face.index)
                    _triangle_islands=list(json.loads(str(me.get("DWOS_W5833_orphan_triangle_metric_faces","[]"))))
                    _triangle_islands.append(int(face.index))
                    me["DWOS_W5833_orphan_triangle_metric_faces"]=json.dumps(_triangle_islands)
                    continue
            if len(front_matches)!=1:
                edge_audit=[]
                for q in range(3):
                    ea,eb=ids[q],ids[(q+1)%3];key=frozenset((ea,eb))
                    adjacent=[]
                    for other in me.polygons:
                        if other.index==face.index:continue
                        ov=set(other.vertices)
                        if key.issubset(ov):
                            adjacent.append({'face':other.index,'role':roles[other.index],
                                'verts':list(other.vertices)})
                    edge_audit.append({'edge':[ea,eb],'front_edge':key in front_edge,'adjacent':adjacent[:6]})
                raise ValueError(f"DWOS_BRICK_DEPTH_TRIANGLE_NO_UNIQUE_SOURCE_EDGE:{face.index}:{front_matches}:ids={ids}:role={roles[face.index]}:edges={edge_audit}")
            i=front_matches[0];ia=i;ib=(i+1)%3;ip=(i+2)%3
            a_id,b_id=ids[ia],ids[ib]
            oa,ob,ouv_a,ouv_b,third=front_edge[frozenset((a_id,b_id))]
            if (oa,ob)==(a_id,b_id):ua,ub=ouv_a,ouv_b
            else:ua,ub=ouv_b,ouv_a
            uv_out=(ua[0]-third[0],ua[1]-third[1])
            edge=(ub[0]-ua[0],ub[1]-ua[1])
            sense=edge[0]*uv_out[1]-edge[1]*uv_out[0]
            if abs(sense)<1e-12:
                raise ValueError(f"DWOS_BRICK_DEPTH_TRIANGLE_SOURCE_UV_AMBIGUOUS:{face.index}")
            world=[tuple(frame.matrix_world@me.vertices[ids[j]].co) for j in (ia,ib,ip)]
            uv_third=unfolded_third_uv(world[0],world[1],world[2],ua,ub,
                                        resolution,1 if sense>0 else -1)
            uv_tri=[None,None,None];uv_tri[ia]=ua;uv_tri[ib]=ub;uv_tri[ip]=uv_third
            validate_triangle_uv_metric(world,uv_tri,resolution)
            for corner,uv_value in enumerate(uv_tri):
                layer.data[face.loop_indices[corner]].uv=uv_value
            if (math.dist(layer.data[face.loop_indices[ia]].uv,ua)>1e-7 or
                    math.dist(layer.data[face.loop_indices[ib]].uv,ub)>1e-7):
                raise ValueError(f"DWOS_BRICK_DEPTH_TRIANGLE_SOURCE_EDGE_MISMATCH:{face.index}")
            results.append(face.index)
            continue
        if face.loop_total!=4:
            raise ValueError(f"DWOS_BRICK_DEPTH_UNSUPPORTED_REVEAL:{face.index}:corners={face.loop_total}")
        front_matches=[i for i in range(4) if frozenset((ids[i],ids[(i+1)%4])) in front_edge]
        if len(front_matches)!=1:
            # Preserve generated depth UV only when it is demonstrably
            # metric, fits in 0-1, and has 4 genuine noncollapsed edges.
            def _already_metric():
                if len(front_matches)>1:return False
                loops=list(face.loop_indices)
                for q in range(4):
                    aa=loops[q];bb=loops[(q+1)%4]
                    uv_a=layer.data[aa].uv;uv_b=layer.data[bb].uv
                    if any(v<-1e-6 or v>1.000001 or not math.isfinite(v)
                           for v in (*uv_a,*uv_b)):return False
                    pa=frame.matrix_world@me.vertices[me.loops[aa].vertex_index].co
                    pb=frame.matrix_world@me.vertices[me.loops[bb].vertex_index].co
                    d=(pa-pb).length;du=(uv_a-uv_b).length
                    if d<=1e-7 or abs(du*resolution/d-1000)>5:return False
                return True
            if _already_metric():
                results.append(face.index)
                continue
            if len(front_matches)==0:
                from ...brick_hole.uv_geometry_fallback_core import (
                    calculate_micro_reveal_metric_uv)
                world_pts=[
                    tuple(frame.matrix_world@me.vertices[idx].co)
                    for idx in ids]
                try:
                    island,meta=calculate_micro_reveal_metric_uv(
                        world_pts,resolution=resolution)
                except ValueError:
                    pass  # The original strict reason remains authoritative.
                else:
                    for li,u in zip(face.loop_indices,island):
                        layer.data[li].uv=u
                    results.append(face.index)
                    prior=int(me.get('DWOS_W5831_orphan_metric_islands',0))
                    me['DWOS_W5831_orphan_metric_islands']=prior+1
                    continue
            debug=[(tuple(round(float(u),5) for u in layer.data[face.loop_indices[q]].uv),
                     tuple(round(float(v),7) for v in (frame.matrix_world@me.vertices[ids[q]].co)))
                    for q in range(4)]
            raise ValueError(f"DWOS_BRICK_DEPTH_NO_UNIQUE_ANCHOR:{face.index}:{front_matches}:DEBUG={debug}")
        i=front_matches[0]
        ia=i; ib=(i+1)%4; ip=(i-1)%4; iq=(i+2)%4
        a_id,b_id=ids[ia],ids[ib]
        oa,ob,ouv_a,ouv_b,third=front_edge[frozenset((a_id,b_id))]
        if (oa,ob)==(a_id,b_id):ua,ub=ouv_a,ouv_b
        else:ua,ub=ouv_b,ouv_a
        # The face must unfold onto the opposite UV side of the front
        # triangle; depth has to unfold OUT of the original facade.
        uv_out=(ua[0]-third[0],ua[1]-third[1])
        world=[tuple(frame.matrix_world@me.vertices[ids[j]].co) for j in (ia,ib,ip,iq)]
        chart=unfold_reveal_quad(world,ua,ub,uv_out,resolution,[(0,1,2),(2,1,3)])
        for local,face_corner in enumerate((ia,ib,ip,iq)):
            layer.data[face.loop_indices[face_corner]].uv=chart[local]
        # Enforce exact edge equality, plus numeric metric preflight in BH.
        if any(math.dist(layer.data[face.loop_indices[j]].uv, u)>1e-7
               for j,u in ((ia,ua),(ib,ub))):
            raise ValueError(f"DWOS_BRICK_DEPTH_SOURCE_EDGE_MISMATCH:{face.index}")
        results.append(face.index)
    if not results:
        raise ValueError("DWOS_BRICK_DEPTH_NO_REVEAL_FACES")
    me["DWOS_W58_5_brick_reveal_faces"]=len(results)
    me["DWOS_W5833_brick_triangle_reveal_faces"]=sum(
        1 for face_index in results if len(me.polygons[face_index].loop_indices)==3)
    me["DWOS_W58_5_brick_reveal_uv"]="BRICK_HOLE_UNFOLD_ANCHORED_V2_QUAD_TRIANGLE"
    me.update()
    return {"mapped_reveal_faces":len(results),"face_indices":results}


def _preserve_planar_metric_inner_uv(frame, resolution):
    """W58.12: validate and retain the existing metric back chart.

    HP back faces are independently staged into one coherent metric chart
    BEFORE Brick Hole unfold touches the facade/reveal. Do not propagate
    around an annular back ring: that creates an uncontrolled large seam.
    Author-authored front and anchored reveal loops remain bit-identical.
    """
    cls = str(frame.get('DWOS_adaptive_classification', 'PLANAR')).upper()
    if cls not in {'PLANAR', 'PLANAR_LEGACY'}:
        return None  # Adaptive CORNER/CURVED retain previous path.
    me = frame.data
    uv = me.uv_layers.active
    if uv is None:
        raise ValueError('DWOS_PLANAR_BACK_UV_MISSING')
    if (str(me.get('DWOS_uv_schema','')) != 'DWOS_PROFILE_METRIC_1000_PX_M_V2'
            or str(me.get('DWOS_uv_stage_state','')) != 'READY'):
        raise ValueError('DWOS_PLANAR_BACK_UV_NOT_CERTIFIED')
    roles = json.loads(str(frame.get('DWOS_face_roles','[]')))
    if len(roles) != len(me.polygons):
        raise ValueError('DWOS_PLANAR_BACK_UV_ROLE_COUNT_MISMATCH')
    back = {'FRAME_INTERIOR_BACK','FRAME_MULLION_VERTICAL_BACK'}
    shared = defaultdict(list)
    count = 0
    for f in me.polygons:
        if roles[f.index] not in back:
            continue
        count += 1
        verts = [me.loops[li].vertex_index for li in f.loop_indices]
        loop_indices = list(f.loop_indices)
        if len(verts) < 3:
            raise ValueError('DWOS_PLANAR_BACK_UV_BAD_FACE')
        for i, li in enumerate(loop_indices):
            nxt = loop_indices[(i+1) % len(loop_indices)]
            a,b = verts[i],verts[(i+1) % len(verts)]
            qa = tuple(float(x) for x in uv.data[li].uv)
            qb = tuple(float(x) for x in uv.data[nxt].uv)
            if any(not math.isfinite(t) or t < -1e-6 or t > 1+1e-6 for t in qa+qb):
                raise ValueError('DWOS_PLANAR_BACK_UV_OUTSIDE_SINGLE_TILE')
            d = ((frame.matrix_world @ me.vertices[a].co) -
                 (frame.matrix_world @ me.vertices[b].co)).length
            if d < 1e-8:
                raise ValueError('DWOS_PLANAR_BACK_UV_COLLAPSED_EDGE')
            pixels_per_m = math.dist(qa,qb)*resolution/d
            if abs(pixels_per_m-1000.) > 2.:
                raise ValueError('DWOS_PLANAR_BACK_UV_DENSITY_MISMATCH')
            shared[frozenset((a,b))].append({a:qa,b:qb})
    if count == 0:
        raise ValueError('DWOS_PLANAR_BACK_UV_NO_BACK_FACES')
    for edge, items in shared.items():
        if len(items) == 2 and any(math.dist(items[0][v],items[1][v])>1e-5 for v in edge):
            raise ValueError('DWOS_PLANAR_BACK_UV_INTERNAL_SEAM')
        if len(items) > 2:
            raise ValueError('DWOS_PLANAR_BACK_UV_NONMANIFOLD')
    me['DWOS_W58_5_brick_inner_faces'] = count
    me['DWOS_W58_5_brick_inner_uv'] = 'W58_12_PLANAR_BACK_GLOBAL_METRIC_SINGLE_TILE'
    return {'mapped_inner_faces': count,'passes':0,
            'mode':'PLANAR_BACK_METRIC_PRESERVED','certified':True}


def pack_adaptive_back_uv_in_single_tile(frame, resolution=4096, margin_px=8):
    """W58.20: split an overlong UV chart into metric-connected subcharts.

    A whole smooth curved back strip wider than 4.096 m cannot fit in one
    4K tile without a UV seam. We must NOT shrink the UV or fail by calling
    valid 3D geometry invalid. The unified interval planner only splits UV
    adjacency when mathematically necessary; it leaves LP/front XYZ intact.
    Intentional seam counts are retained for diagnostics and GUI review.
    """
    kind = str(frame.get("DWOS_adaptive_classification","")).upper()
    if kind not in {"CURVED","CORNER"}:
        return {"state":"NOT_APPLICABLE","shifted":0,"components":0}
    from .uv_one_tile_core import pack_frame
    report = pack_frame(frame)
    frame.data["DWOS_W5820_uv_split_reason"]="ONE_TILE_METRIC_CHART_EXCEEDS_DOMAIN"
    frame.data["DWOS_W5820_uv_seam_links_added"]=int(report["newly_broken_links"])
    frame.data["DWOS_W58_13_back_component_count"]=int(report["groups"])
    frame.data["DWOS_W58_13_back_component_shifted"]=int(report["shifted_groups"])
    frame.data["DWOS_W58_13_back_uv_placement"]="W5820_ROLE_LOCKED_METRIC_CHART_SPLIT_V1"
    return {"state":"PACKED","shifted":int(report["shifted_groups"]),
            "components":int(report["groups"]),
            "newly_broken_links":int(report["newly_broken_links"])}


def unfold_inner_profile_from_reveals(frame, resolution=4096):
    """Brick Hole triangle unfolding across the HP inner wall/back.

    Starts at UV-anchored reveal quads and crosses real shared mesh edges.
    The authored front faces and their UV are never changed.
    """
    preserved = _preserve_planar_metric_inner_uv(frame, resolution)
    if preserved is not None:
        return preserved
    from ...brick_hole import unfolded_third_uv, validate_triangle_uv_metric
    import collections
    me=frame.data
    layer=me.uv_layers.active
    roles=json.loads(str(frame.get("DWOS_face_roles","[]")))
    if len(roles)!=len(me.polygons):raise ValueError("BRICK_INNER_ROLE_MISMATCH")
    shared=collections.defaultdict(set)
    for poly in me.polygons:
        vids=[me.loops[i].vertex_index for i in poly.loop_indices]
        for a,b in zip(vids,vids[1:]+vids[:1]):
            shared[frozenset((a,b))].add(poly.index)
    known=set(p.index for p in me.polygons if roles[p.index] in (
        "FRAME_REVEAL","FRAME_MULLION_VERTICAL_SIDE_NEG",
        "FRAME_MULLION_VERTICAL_SIDE_POS"))
    pending=set(p.index for p in me.polygons if roles[p.index] in (
        "FRAME_INTERIOR_BACK","FRAME_MULLION_VERTICAL_BACK"))
    if not known or not pending:raise ValueError("BRICK_INNER_MISSING_SEEDS_OR_FACES")
    passes=0;resolved=0
    while pending and passes<=len(pending)+3:
        passes+=1
        changed=False
        for fi in sorted(tuple(pending)):
            face=me.polygons[fi]
            if face.loop_total not in (3,4):
                raise ValueError(f"BRICK_INNER_UNSUPPORTED_POLYGON:{face.loop_total}")
            ids=[me.loops[i].vertex_index for i in face.loop_indices]
            chosen=None
            for i in range(len(ids)):
                a,b=ids[i],ids[(i+1)%len(ids)]
                seed_candidates=sorted(
                    f for f in shared.get(frozenset((a,b)),()) if f in known)
                if seed_candidates:
                    # Prefer crossing the directly anchored reveal, then
                    # a back triangle whose UV has already been propagated.
                    seed=min(seed_candidates,key=lambda k:(roles[k]!="FRAME_REVEAL",k))
                    chosen=(i,a,b,seed);break
            if not chosen:continue
            i,a,b,seed=chosen
            ref=me.polygons[seed]
            loops=list(ref.loop_indices)
            ids_ref=[me.loops[j].vertex_index for j in loops]
            if a not in ids_ref or b not in ids_ref:continue
            ia=ids_ref.index(a);ib=ids_ref.index(b)
            others=[j for j in range(len(ids_ref)) if j not in (ia,ib)]
            if not others:continue
            uv_a=tuple(layer.data[loops[ia]].uv)
            uv_b=tuple(layer.data[loops[ib]].uv)
            uv_c=tuple(layer.data[loops[others[0]]].uv)
            sense=(uv_b[0]-uv_a[0])*(uv_c[1]-uv_a[1])-(uv_b[1]-uv_a[1])*(uv_c[0]-uv_a[0])
            if abs(sense)<1e-13:raise ValueError("BRICK_INNER_ANCHOR_ZERO_UV_AREA")
            if len(ids)==3:
                third=ids[(i+2)%3]
                pa=tuple(frame.matrix_world@me.vertices[a].co)
                pb=tuple(frame.matrix_world@me.vertices[b].co)
                pc=tuple(frame.matrix_world@me.vertices[third].co)
                uv_third=unfolded_third_uv(
                    pa,pb,pc,uv_a,uv_b,resolution,-1 if sense>0 else 1)
                validate_triangle_uv_metric(
                    (pa,pb,pc),(uv_a,uv_b,uv_third),resolution)
                coord={a:uv_a,b:uv_b,third:uv_third}
            else:
                # Same Brick Hole two-triangle quad unfolding used by reveal.
                # Choose adjacent depth counterparts by ring order; reject
                # physically incompatible faces rather than inventing UV.
                ip=(i-1)%4;iq=(i+2)%4
                pts=[tuple(frame.matrix_world@me.vertices[ids[k]].co)
                     for k in (i,(i+1)%4,ip,iq)]
                from ...brick_hole import unfold_reveal_quad, unfolded_third_uv, validate_triangle_uv_metric
                uv_out=(uv_a[0]-uv_c[0],uv_a[1]-uv_c[1])
                chart=unfold_reveal_quad(
                    pts,uv_a,uv_b,uv_out,resolution,[(0,1,2),(2,1,3)])
                coord={ids[i]:chart[0],ids[(i+1)%4]:chart[1],
                       ids[ip]:chart[2],ids[iq]:chart[3]}
            for li in face.loop_indices:
                vid=me.loops[li].vertex_index
                layer.data[li].uv=coord[vid]
            pending.remove(fi);known.add(fi);resolved+=1;changed=True
        if not changed:break
    if pending:
        raise ValueError(f"BRICK_INNER_UNREACHED_TRIANGLES:{sorted(pending)[:12]}")
    me["DWOS_W58_5_brick_inner_faces"]=resolved
    me["DWOS_W58_5_brick_inner_uv"]="BRICK_HOLE_EDGE_UNFOLD_CHAIN_V1"
    placement=pack_adaptive_back_uv_in_single_tile(frame,resolution)
    return {"mapped_inner_faces":resolved,"passes":passes,"back_uv_placement":placement}
