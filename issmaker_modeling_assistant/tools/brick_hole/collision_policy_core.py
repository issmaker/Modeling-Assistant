"""BH0466 explicit intersection policy; Blender-independent for QA."""

def intersections_enabled(scene):
    """Default to strict mode for historical scenes without the new field."""
    return bool(getattr(scene, 'brick_hole_check_intersections', True))
