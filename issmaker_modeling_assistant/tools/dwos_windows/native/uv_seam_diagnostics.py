"""ISSMAKER W58.13: non-destructive per-window UV edge and island audit."""
from __future__ import annotations
from collections import defaultdict, Counter
import json
import math

SCHEMA = "ISSMAKER_DWOS_UV_SEAM_AUDIT_W58_13"
BACK_ROLES = {"FRAME_INTERIOR_BACK", "FRAME_MULLION_VERTICAL_BACK"}

def _as_name(obj):
    return str(getattr(obj, "name", "<invalid>"))

def audit_mesh(obj, *, resolution=4096, target_density=1000.0, max_density_error=2.0):
    """Read-only. Never create UV or modify geometry; errors stay diagnostic."""
    report = {"name": _as_name(obj), "status": "NOT_VERIFIED",
              "faces": 0, "edge_samples": 0,
              "missing_uv": False, "nonfinite_uv": 0, "outside_tile_faces": 0,
              "density_violations": 0, "density_min": None,
              "density_max": None, "zero_length_edges": 0,
              "seam_edges": 0, "seam_types": {}, "seam_max_uv": 0.0,
              "non_manifold_edges": 0, "unknown_roles": False}
    if getattr(obj, "type", None) != "MESH" or getattr(obj, "data", None) is None:
        report["status"] = "NOT_MESH"
        return report
    mesh = obj.data
    report["faces"] = len(mesh.polygons)
    layer = mesh.uv_layers.active
    if layer is None:
        report["missing_uv"] = True
        report["status"] = "NO_UV"
        return report
    try:
        roles = json.loads(str(obj.get("DWOS_face_roles", "[]")))
        if len(roles) != len(mesh.polygons): roles = None
    except (ValueError, TypeError):
        roles = None
    report["unknown_roles"] = roles is None
    edges = defaultdict(list)
    min_ratio = float("inf"); max_ratio = -float("inf")
    for polygon in mesh.polygons:
        vids = list(polygon.vertices)
        loops = list(polygon.loop_indices)
        if len(vids) != len(loops) or not vids:
            report["status"] = "BAD_TOPOLOGY"
            return report
        role = str(roles[polygon.index]) if roles else "UNKNOWN_ROLE"
        outside = False
        for idx in range(len(vids)):
            j = (idx+1) % len(vids)
            a, b = vids[idx], vids[j]
            uv_a = tuple(float(x) for x in layer.data[loops[idx]].uv)
            uv_b = tuple(float(x) for x in layer.data[loops[j]].uv)
            if any(not math.isfinite(v) for v in uv_a+uv_b):
                report["nonfinite_uv"] += 1
                continue
            if any(v < -1e-6 or v > 1.0+1e-6 for v in uv_a):
                outside = True
            coords = obj.matrix_world
            p1 = coords @ mesh.vertices[a].co
            p2 = coords @ mesh.vertices[b].co
            dist = (p2-p1).length
            if dist < 1e-8:
                report["zero_length_edges"] += 1
                continue
            ratio = math.dist(uv_a,uv_b)*float(resolution)/dist
            if not math.isfinite(ratio):
                report["nonfinite_uv"] += 1
                continue
            report["edge_samples"] += 1
            min_ratio=min(min_ratio,ratio);max_ratio=max(max_ratio,ratio)
            if abs(ratio-target_density) > max_density_error:
                report["density_violations"] += 1
            edges[frozenset((a,b))].append((role,{a:uv_a,b:uv_b}))
        if outside:
            report["outside_tile_faces"] += 1
    if report["edge_samples"]:
        report["density_min"]=round(min_ratio,5)
        report["density_max"]=round(max_ratio,5)
    seam_types=Counter()
    for edge,sides in edges.items():
        if len(sides)>2:
            report["non_manifold_edges"] += 1
            continue
        if len(sides)!=2:
            continue
        (ra,pa),(rb,pb) = sides
        delta = max(math.dist(pa[v],pb[v]) for v in edge)
        if delta>1e-5:
            report["seam_edges"] += 1
            report["seam_max_uv"] = max(report["seam_max_uv"],delta)
            seam_types["+".join(sorted((ra,rb)))] += 1
    report["seam_types"] = dict(sorted(seam_types.items()))
    report["seam_max_uv"]=round(report["seam_max_uv"],7)
    issues=(report["nonfinite_uv"]+report["outside_tile_faces"]+
            report["density_violations"]+report["zero_length_edges"]+
            report["non_manifold_edges"])
    report["status"]="ISSUES" if issues else "METRIC_AND_TILE_VALID"
    # A UV seam is not necessarily a defect. Keep it in a separate category
    # and never silently discard exact UV cuts made by the author.
    return report

def audit_scene(scene):
    roots=sorted((o for o in scene.objects
                  if o.type == "MESH" and o.parent is None and
                  bool(getattr(getattr(o, "dwos_window", None), "is_window", False))),
                 key=lambda o:o.name)
    windows=[]
    total=Counter()
    seam=Counter()
    for root in roots:
        hp=audit_mesh(root)
        lp=next((c for c in root.children if c.name.startswith("DWOS_LP_")),None)
        lpr=audit_mesh(lp) if lp is not None else {"status":"LP_MISSING"}
        windows.append({"window":root.name,"hp":hp,"lp":lpr})
        for part in (hp,lpr):
            for key in ("edge_samples","outside_tile_faces","density_violations",
                        "seam_edges","zero_length_edges","nonfinite_uv",
                        "non_manifold_edges"):
                total[key] += int(part.get(key,0))
            seam.update(part.get("seam_types",{}))
    return {"schema":SCHEMA, "windows":len(roots), "totals":dict(total),
            "seam_type_totals":dict(sorted(seam.items())),
            "classification":"MEASUREMENT_ONLY",
            "warning":"UV discontinuities need material-aware evaluation; seam count is not an automatic failure",
            "window_details":windows}
