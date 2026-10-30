"""Conservative shared-edge contact classifier for Collision Inspector.

RF1 delegates the proof to the common Reliability Foundation.  No angular
floating tolerance can turn a near-coplanar pair into a hard geometry verdict.
"""
from __future__ import annotations
from ...core.reliability.predicates import exact_shared_edge_only_contact


def shared_edge_only_contact(indices_a, coords_a, indices_b, coords_b, *, angular_epsilon=1e-8):
    # ``angular_epsilon`` remains in the signature for backwards compatibility;
    # the hard proof itself is exact for the supplied binary coordinates.
    return exact_shared_edge_only_contact(indices_a, coords_a, indices_b, coords_b)
