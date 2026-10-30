"""BH-04B.5: robust source-face preservation audit for BMesh transactions.

Blender BMesh wrapper identity is not a durable serialization of authored
topology. Freeze oriented face-loop signatures BEFORE topology edits, then
locate their unchanged equivalents by scanning the active BMesh AFTER edits.
An absent, extra, UV-altered, reversed or re-materialed source face fails.
No source BMesh element is mutated by this ledger.
"""
from collections import Counter


def _oriented_cycle(values):
    """Normalize cyclic loop starting point without ever reversing winding."""
    values=tuple(values)
    if len(values)<3:
        raise ValueError('SOURCE_LEDGER: face has fewer than 3 loops')
    return min(values[i:]+values[:i] for i in range(len(values)))


def source_face_signature(face, uv_layers):
    """World-independent local XYZ + ALL provided UV maps + material.

    The face is only read when this function runs; never retain it for
    reads after a mesh edit. UV maps are ordered by captured layer name.
    """
    rings=[]
    for loop in face.loops:
        pos=tuple(float(x) for x in loop.vert.co)
        maps=tuple((name,tuple(float(x) for x in loop[layer].uv))
                   for name,layer in uv_layers)
        rings.append((pos,maps))
    return (int(face.material_index), _oriented_cycle(rings))


def capture_source_face_ledger(bm, source_faces, uv_layers):
    """Capture source polygon signatures and their exact pre-edit counts.

    Duplicate physically coincident original faces are never silently
    collapsed. This is a topology/UV equivalence check, not a guarantee
    that Blender preserved Python BMFace pointer identity.
    """
    required=Counter(source_face_signature(f,uv_layers) for f in source_faces)
    if not required:
        raise ValueError('SOURCE_LEDGER: no source faces supplied')
    lengths={len(sig[1]) for sig in required}
    found=Counter()
    for face in bm.faces:
        if len(face.loops) not in lengths:
            continue
        sig=source_face_signature(face,uv_layers)
        if sig in required:
            found[sig]+=1
    if any(found[sig]!=required[sig] for sig in required):
        raise ValueError('SOURCE_LEDGER: ambiguous source faces before operation')
    return (required,tuple(uv_layers))


def verify_source_face_ledger(bm, ledger):
    """Fail-closed check of source face winding, XYZ, UV, material, count."""
    required,uv_layers=ledger
    lengths={len(sig[1]) for sig in required}
    found=Counter()
    for face in bm.faces:
        if len(face.loops) not in lengths:
            continue
        sig=source_face_signature(face,uv_layers)
        if sig in required:
            found[sig]+=1
    missing=sum(max(0,required[sig]-found[sig]) for sig in required)
    extra=sum(max(0,found[sig]-required[sig]) for sig in required)
    if missing or extra:
        raise ValueError('BH-04B: source face topology/material/UV not preserved '
                         f'(missing={missing}, extra={extra})')
    return {'faces_verified':sum(required.values()),
            'uv_maps_verified':len(uv_layers),
            'source_faces_matched':sum(found.values())}
