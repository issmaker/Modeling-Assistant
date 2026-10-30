"""Early, bpy-independent package integrity check for UV Wall.

Executed before production imports; it detects mixed ZIP installations and
reports an actionable error instead of ``cannot import name ...``.  It is a
file-consistency check, not a substitute for a real Blender enable test.
"""
import ast
import hashlib
import json
from pathlib import Path
import sys

try:
    from .version import VERSION, VERSION_STRING, INTEGRITY_MANIFEST
except ImportError:  # standalone offline tests
    VERSION = (2, 0, 0)
    VERSION_STRING = "2.0.0"
    INTEGRITY_MANIFEST = "release_integrity_200.json"

MANIFEST = INTEGRITY_MANIFEST
REQUIRED_EXPORTS = {
    'core/revisions.py': ('record_revision', 'parse_ledger', 'revision_payload'),
    'operators/revisions.py': ('revision_path_and_ledger', 'OBJECT_OT_uvwall_load_revision'),
    'core/revision_recovery.py': ('atomic_restore_definition',),
    'core/mesh_recovery.py': ('store_accepted_mesh', 'restore_accepted_mesh', 'recovery_status'),
    'operators/mesh_recovery.py': ('OBJECT_OT_uvwall_restore_mesh_snapshot',),
    'core/support_mesh_audit.py': ('inspect_mesh', 'height_hypothesis_snapshot'),
    'production/cap_surface_geometry.py': (
        'CapClipPoint', 'patch_diagonal', 'nonplanar_deviation', 'height_at_xy',
        'reconcile_patch_boundary_xy', 'ribbon_coordinates_xy', 'ribbon_height_at_xy',
        'polygon_nonplanar_deviation', 'split_local_nonplanar_face',
        'cap_tile_fragments', 'cap_tile_ranges', 'rotated_support', 'cap_cell_uv', 'is_convex_ccw',
    ),
    'core/definitions.py': (
        'ensure_source_uid', 'source_uid_collisions', 'unresolved_source_uid_collision', 'path_from_source_uid',
    ),
    'core/result_lifecycle.py': (
        'ownership_guard', 'assign_result_identity', 'ensure_result_uid', 'active_owned_results',
    ),
    'core/build_guard.py': (
        'build_transaction', 'transaction_is_active', 'reset_build_guard',
    ),
    'production/topology_first.py': ('stable_path_z_anchor','local_vertical_slabs','preweld_shared_vertices'),
    'production/height_correspondence.py': ('synchronize_wall_station_cuts',),
    'production/inner_height.py': ('resolve_path_deltas','height_at_station','validate_vertex_heights'),
    'production/secondary_points.py': ('parse_secondary_points','assign_secondary_points','selected_endcaps','resolve_secondary_points'),
    'production/cap_uv_integrity.py': ('tile_coordinates', 'reproject_cap_uv'),
    'production/planar_integrity.py': (
        'validate_ribbon_coverage', 'assert_nonoverlapping_ribbon_patches',
    ),
    'production/corner_trim.py': ('exact_miter_trim',),
    'production/segment_joint_graph.py': ('create_joint_graph', 'graph_diagnostics'),
    'operators/segment_joint_preview.py': ('build_preview', 'UVWALL_OT_segment_joint_preview', 'UVWALL_OT_cleanup_joint_previews', 'UVWALL_OT_joint_compare1175'),
    'production/corner_methods.py': ('select_frame',),
    'production/mirror_corner_integrity.py': ('audit_sweep_strips', 'require_valid_sweep'),
    'production/normal_math.py': ('section_signed_area_xz', 'quad_outward_decision'),
    'production/normal_integrity.py': ('orient_connected_shells',),
    'production/cap_boundary_plan.py': ('plan_shared_rim', 'extract_wall_rim'),
    'production/wall_topology.py': ('dissolve_redundant_wall_edges', 'convex_uv_ring', 'safe_affine_wall_patch', 'protected_path_corners_xy'),
    'production/wall_uv_field.py': ('FacadeField', 'rephase_uv_for_facade'),
    'production/vertex_minimal.py': ('straight_uv_vertex', 'remove_pass_through_boundary_vertices'),
    'production/corner_audit.py': ('inspect_wall_corners',),
    'core/blender_compat.py': ('version_family', 'assert_supported_environment'),
    'production/cornice.py': (
        'cornice_transport_up_vectors', 'cornice_corner_metrics', 'build_cornice',
    ),
    'core/material_intelligence.py': (
        'inspect_material_asset', 'material_image_candidates', 'expected_pixels',
    ),
    'core/problem_registry.py': (
        'add_problem', 'navigate_problem', 'scan_result_mesh', 'scan_cornice_result_metadata', 'serialize_problems',
    ),
    'viewport/resource_picker.py': (
        'SmartResourcePicker', 'validate_resource', 'resources_for', 'assign_resource',
    ),
}


def _problem(root, message):
    return RuntimeError(
        f'UV Wall {VERSION_STRING}: installation is incomplete or contains mixed versions. '
        + message + '; package=' + str(root) + '. '
        f'Close ALL Blender windows. Use a clean {VERSION_STRING} ZIP installation to '
        'back up the old addon folder and replace it atomically. '
        'Do not merge or paste new files over an existing folder.'
    )


def validate_runtime_identity_contract(root):
    """Catch malformed critical-source entries *before* Blender registration.

    This is deliberately an AST-only, Blender-independent test: no `bpy`,
    imports, or mutable scene data are touched during startup preflight.
    Each pair names the actual module that defines the actual function.
    """
    root = Path(root).resolve()
    source = root / 'core/runtime_identity.py'
    try:
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        verifier = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == 'verify_critical_geometry_sources'
        )
        assignment = next(
            node for node in verifier.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == 'functions'
                    for target in node.targets)
        )
        entries = ast.literal_eval(assignment.value)
    except (OSError, UnicodeError, SyntaxError, ValueError, TypeError, StopIteration) as exc:
        raise _problem(root, f'Critical function guard unreadable: {exc}') from exc
    if not isinstance(entries, (list, tuple)) or not entries:
        raise _problem(root, 'Critical function guard contains no module/function pairs')
    seen = set()
    for index, entry in enumerate(entries, 1):
        if (not isinstance(entry, (tuple, list)) or len(entry) != 2
                or any(not isinstance(value, str) or not value for value in entry)):
            size = len(entry) if isinstance(entry, (tuple, list)) else 'invalid'
            raise _problem(root, f'Critical function entry #{index}: '
                           f'expected exactly 2 strings (module, function), got {size}')
        module_name, function_name = entry
        if not module_name.startswith('UV_Wall_Professional.'):
            raise _problem(root, f'Critical function entry #{index}: invalid module {module_name!r}')
        if tuple(entry) in seen:
            raise _problem(root, f'Critical function entry #{index}: duplicate {module_name}.{function_name}')
        seen.add(tuple(entry))
        relative = Path(*module_name.split('.')[1:]).with_suffix('.py')
        module_file = (root / relative).resolve()
        if not module_file.is_relative_to(root) or not module_file.is_file():
            raise _problem(root, f'Critical function entry #{index}: missing module file {module_name}')
        try:
            module_tree = ast.parse(module_file.read_text(encoding='utf-8'), filename=str(module_file))
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            raise _problem(root, f'Critical function entry #{index}: module unreadable: {exc}') from exc
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == function_name for node in module_tree.body):
            raise _problem(root, f'Critical function entry #{index}: '
                           f'{module_name}.{function_name} is not defined in that module')
    return len(entries)


def validate_required_exports_contract():
    """Fail closed on duplicated REQUIRED_EXPORTS keys before dict overwrite.

    Python silently overwrites duplicated keys in a dict literal. Validate the
    source expression itself, so protection cannot be weakened by this typo.
    This intentionally requires a literal dictionary in this startup guard.
    """
    source = Path(__file__).resolve()
    try:
        tree = ast.parse(source.read_text(encoding='utf-8'), filename=str(source))
        assignments = [node for node in tree.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'REQUIRED_EXPORTS'
                               for t in node.targets)]
        if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Dict):
            raise ValueError('REQUIRED_EXPORTS must be one literal dictionary')
        literal = assignments[0].value
        seen = set()
        for key_node, value_node in zip(literal.keys, literal.values):
            key = ast.literal_eval(key_node)
            values = ast.literal_eval(value_node)
            if not isinstance(key, str) or not key.endswith('.py'):
                raise ValueError(f'Invalid module key {key!r}')
            if key in seen:
                raise ValueError(f'Duplicate module key {key!r}')
            seen.add(key)
            if not isinstance(values, (tuple, list)) or not values or any(
                    not isinstance(v, str) or not v for v in values):
                raise ValueError(f'Empty or invalid exports {key!r}')
            if len(set(values)) != len(values):
                raise ValueError(f'Duplicate export in {key!r}')
        if seen != set(REQUIRED_EXPORTS):
            raise ValueError('Required export source disagrees with loaded dictionary')
    except (OSError, UnicodeError, SyntaxError, TypeError, ValueError) as exc:
        raise RuntimeError(f'UV Wall {VERSION_STRING}: invalid REQUIRED_EXPORTS contract: {exc}') from exc
    return len(seen)


def assert_package_integrity(root, version=VERSION):
    validate_required_exports_contract()
    root = Path(root).resolve()
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise _problem(root, 'Missing ' + MANIFEST)
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise _problem(root, 'Invalid manifest: ' + str(exc)) from exc
    expected_version = '.'.join(map(str, version))
    if manifest.get('version') != expected_version:
        raise _problem(root, f'Version mismatch root={expected_version} manifest={manifest.get("version")}')
    hashes = manifest.get('sha256')
    if not isinstance(hashes, dict) or len(hashes) < 4:
        raise _problem(root, 'Incomplete source hash manifest')
    for relative, expected in sorted(hashes.items()):
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise _problem(root, 'Missing module: ' + relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise _problem(root, f'SHA256 mismatch {relative}: expected={expected[:16]} actual={actual[:16]}')
    validate_runtime_identity_contract(root)
    for relative, required in REQUIRED_EXPORTS.items():
        path = root / relative
        try:
            parsed = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        except (SyntaxError, OSError, UnicodeError) as exc:
            raise _problem(root, f'Module invalid {relative}: {exc}') from exc
        exports = {node.name for node in parsed.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        missing = sorted(set(required) - exports)
        if missing:
            raise _problem(root, f'Missing exports in {relative}: {missing}')
    loaded_modules = {
        'production/topology_first.py': 'UV_Wall_Professional.production.topology_first',
        'production/cap_boundary_plan.py': 'UV_Wall_Professional.production.cap_boundary_plan',
        'production/cap_surface_geometry.py': 'UV_Wall_Professional.production.cap_surface_geometry',
        'core/definitions.py': 'UV_Wall_Professional.core.definitions',
        'core/result_lifecycle.py': 'UV_Wall_Professional.core.result_lifecycle',
        'core/build_guard.py': 'UV_Wall_Professional.core.build_guard',
        'production/normal_math.py': 'UV_Wall_Professional.production.normal_math',
        'production/normal_integrity.py': 'UV_Wall_Professional.production.normal_integrity',
        'production/cornice.py': 'UV_Wall_Professional.production.cornice',
        'production/corner_methods.py': 'UV_Wall_Professional.production.corner_methods',
        'production/mirror_corner_integrity.py': 'UV_Wall_Professional.production.mirror_corner_integrity',
        'core/material_intelligence.py': 'UV_Wall_Professional.core.material_intelligence',
        'core/problem_registry.py': 'UV_Wall_Professional.core.problem_registry',
        'viewport/resource_picker.py': 'UV_Wall_Professional.viewport.resource_picker',
    }
    for relative, module_name in loaded_modules.items():
        loaded = sys.modules.get(module_name)
        if loaded is None:
            continue
        path = Path(getattr(loaded, '__file__', '/missing/module.py')).resolve()
        if path != (root / relative).resolve():
            raise _problem(root, f'Old module already loaded from {path}')
        missing = [export for export in REQUIRED_EXPORTS[relative]
                   if getattr(loaded, export, None) is None]
        if missing:
            raise _problem(root, f'Stale in-memory module, missing {missing}')
    return True
