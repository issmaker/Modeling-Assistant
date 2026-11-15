"""Conservative one-triangle UV flip proposal, no writes or bpy dependency.

Only a relative minority single triangle with exactly one UV-connected
anchoring edge is eligible. Reflection preserves that shared edge, does NOT
touch adjacent faces or geometry, and must improve verified diagnostics.
Refuse rather than guess for multiple UV-connected edges, self-crossed ngons,
degenerate geometry, stale or incomplete issue reports.
"""
from __future__ import annotations
from .uv_flip_v2 import analyze_uv_faces


class UnsafeUVFix(ValueError):
    pass


def _same_uv(a,b,tol=1.e-7):
    return abs(a[0]-b[0])<=tol and abs(a[1]-b[1])<=tol


def propose_single_face_fix(faces, target=None):
    original=list(faces)
    report=analyze_uv_faces(original)
    if not report["complete"]:
        raise UnsafeUVFix("Incomplete analysis: too many overlapping UV candidates")
    candidates=tuple(report["local_flip_faces"])
    if not candidates:
        raise UnsafeUVFix("No confirmed local winding outlier; mirrored islands are advisory")
    if target is None:
        if len(candidates)!=1:
            raise UnsafeUVFix("Multiple candidates: select exactly one known UV face")
        target=candidates[0]
    if target not in candidates:
        raise UnsafeUVFix("Chosen face is not a confirmed local UV outlier")
    current=original[target]
    verts=tuple(current["vertices"])
    uv=tuple((float(p[0]),float(p[1])) for p in current["uv"])
    if len(verts)!=3 or len(uv)!=3:
        raise UnsafeUVFix("Only validated isolated triangular outliers are repairable")
    if any(c!="LOCAL_WINDING_OUTLIER" and c!="ISLAND_INTERIOR_OVERLAP"
           for c in report["issues"].get(target,())):
        raise UnsafeUVFix("Face has another unresolved UV defect")
    anchors=[]
    for side in range(3):
        i,j=side,(side+1)%3
        key=frozenset((verts[i],verts[j]))
        for fi,face in enumerate(original):
            if fi==target:continue
            others=tuple(face["vertices"])
            for k in range(len(others)):
                k1=(k+1)%len(others)
                if frozenset((others[k],others[k1]))!=key:
                    continue
                poly=tuple(face["uv"])
                look=dict(zip(others,poly))
                if (_same_uv(uv[i],look[verts[i]])
                    and _same_uv(uv[j],look[verts[j]])):
                    if report["face_states"][fi] in ("NORMAL","FLIPPED"):
                        anchors.append((i,j,fi))
    if len(anchors)!=1:
        raise UnsafeUVFix("UV face must have exactly one unambiguous shared anchor edge")
    i,j,neighbor=anchors[0]
    ax,ay=uv[i]
    bx,by=uv[j]
    dx,dy=bx-ax,by-ay
    length2=dx*dx+dy*dy
    if length2 < 1.e-15:
        raise UnsafeUVFix("Degenerate shared UV edge")
    other=({0,1,2}-{i,j}).pop()
    x,y=uv[other]
    t=((x-ax)*dx+(y-ay)*dy)/length2
    projection=(ax+t*dx,ay+t*dy)
    reflected=(2*projection[0]-x,2*projection[1]-y)
    new_uv=list(uv)
    new_uv[other]=reflected

    # Try proposal only in pure data, re-analyze the whole connected island,
    # including overlapping triangles. Nothing in Blender has been changed.
    proposed=[]
    for fi,face in enumerate(original):
        if fi==target:
            candidate=dict(face)
            candidate["uv"]=tuple(new_uv)
            candidate["triangles"]=(tuple(new_uv),)
            proposed.append(candidate)
        else:
            proposed.append(face)
    after=analyze_uv_faces(proposed)
    if not after["complete"] or target in after["local_flip_faces"]:
        raise UnsafeUVFix("Proposed reflection failed island-relative verification")
    new_issues=set(after["issues"].get(target,()))
    if new_issues:
        raise UnsafeUVFix("Proposed face still has UV defects: "+str(sorted(new_issues)))
    before_others={fi:set(codes) for fi,codes in report["issues"].items() if fi!=target}
    after_others={fi:set(codes) for fi,codes in after["issues"].items() if fi!=target}
    for fi,codes in after_others.items():
        if codes-before_others.get(fi,set()):
            raise UnsafeUVFix("Proposal would introduce a new issue in another polygon")
    if len(after["overlap_pairs"]) > len(report["overlap_pairs"]):
        raise UnsafeUVFix("Proposal creates new UV self-overlaps")
    return {
        "face":target,
        "neighbor":neighbor,
        "old_uv":uv,
        "new_uv":tuple(new_uv),
        "changed_corner":other,
        "issues_before":report["issues"].get(target,()),
        "issues_after":after["issues"].get(target,()),
        "neighbor_unchanged":True,
    }
