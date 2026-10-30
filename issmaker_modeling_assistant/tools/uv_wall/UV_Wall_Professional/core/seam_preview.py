"""Passive seam origin policy; no Blender/GPU dependencies."""

def uv_seam_origins(chains, generator_type):
    if generator_type != 'WALL':
        return []
    return [tuple(float(v) for v in c['points'][0])
            for c in chains if c.get('points')]
