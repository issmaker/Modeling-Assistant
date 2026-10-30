"""ISSMAKER Reliability Foundation.

Shared, Blender-independent contracts used by every geometry-producing tool.
The core rule is deliberately strict: only reproducible proof may hard-block
user geometry. Numerical ambiguity and resource limits are advisory classes.
"""
from .decision import (
    VALID, PROVEN_INVALID, UNCERTAIN, RESOURCE_LIMIT, SETUP_REQUIRED,
    INTERNAL_ERROR, Decision, proven_invalid, uncertain, valid,
    resource_limit, setup_required, internal_error,
)
from .precision import PrecisionPolicy, policy_from_points, policy_from_scale
from .predicates import (
    exact_segment_contact_2d, exact_segment_contact_3d,
    exact_shared_edge_only_contact, polygon_area_vector,
    signed_area_2d, localize_points, exact_triangles_coplanar,
    exact_segment_triangle_crossing_3d, exact_coplanar_triangle_positive_overlap_3d,
    exact_signed_area_2d,
)

KERNEL_VERSION = "RF1.0"

__all__ = [
    'VALID','PROVEN_INVALID','UNCERTAIN','RESOURCE_LIMIT','SETUP_REQUIRED',
    'INTERNAL_ERROR','Decision','PrecisionPolicy','policy_from_points',
    'policy_from_scale','exact_segment_contact_2d','exact_segment_contact_3d',
    'exact_shared_edge_only_contact','polygon_area_vector','signed_area_2d',
    'localize_points','exact_triangles_coplanar','exact_segment_triangle_crossing_3d',
    'exact_coplanar_triangle_positive_overlap_3d','exact_signed_area_2d','proven_invalid','uncertain','valid','resource_limit',
    'setup_required','internal_error','KERNEL_VERSION',
]
