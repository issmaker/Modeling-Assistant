"""Viewport preview budget. Does not affect production geometry or UV."""

def policy(vertex_count):
    n=max(0,int(vertex_count))
    if n <= 750:
        return {"fast":False,"preview_hz":45.0,"signature_hz":15.0,"marker_candidates":640}
    if n <= 2000:
        return {"fast":False,"preview_hz":16.0,"signature_hz":8.0,"marker_candidates":320}
    if n <= 5000:
        return {"fast":True,"preview_hz":9.0,"signature_hz":5.0,"marker_candidates":240}
    return {"fast":True,"preview_hz":6.0,"signature_hz":3.0,"marker_candidates":192}


def fast_path_lines(chains, *, include_points=False):
    """Exact authored path segments (no vertex decimation) for read-only HUD."""
    lines=[]; points=[]
    for chain in chains:
        coords=chain.get("points",())
        lines.extend(zip(coords,coords[1:]))
        if include_points:
            points.extend(coords[:-1] if chain.get("closed") else coords)
    return lines,points
