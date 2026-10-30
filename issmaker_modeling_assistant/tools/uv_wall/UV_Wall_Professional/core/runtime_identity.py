"""Trace exactly which UV Wall modules are executing, not a hard-coded label."""
import hashlib
import json
import pathlib
import sys
import uuid
from datetime import datetime, timezone

try:
    from .version import RUNTIME_MANIFEST
except ImportError:  # standalone offline tests
    RUNTIME_MANIFEST = "release_manifest_200.json"

MODULES = ('UV_Wall_Professional','UV_Wall_Professional.core.runtime_identity',
           'UV_Wall_Professional.core.revisions', 'UV_Wall_Professional.core.revision_recovery',
           'UV_Wall_Professional.operators.revisions',
           'UV_Wall_Professional.core.definitions',
           'UV_Wall_Professional.core.blender_compat','UV_Wall_Professional.core.result_lifecycle',
           'UV_Wall_Professional.core.build_guard',
           'UV_Wall_Professional.core.undo_handlers',
           'UV_Wall_Professional.core.support_logging',
           'UV_Wall_Professional.core.support_mesh_audit',
           'UV_Wall_Professional.operators.diagnostics',
           'UV_Wall_Professional.core.material_intelligence',
           'UV_Wall_Professional.core.problem_registry',
           'UV_Wall_Professional.viewport.resource_picker',
           'UV_Wall_Professional.viewport.parameter_windows',
           'UV_Wall_Professional.viewport.controller',
           'UV_Wall_Professional.viewport.cornice_direct',
           'UV_Wall_Professional.ui.preferences',
           'UV_Wall_Professional.ui.studio_dock',
           'UV_Wall_Professional.ui.dock_workspace_state',
           'UV_Wall_Professional.viewport.studio_dock_layout',
           'UV_Wall_Professional.ui.panel',
           'UV_Wall_Professional.operators.build_context','UV_Wall_Professional.operators.problems',
           'UV_Wall_Professional.production.validation',
           'UV_Wall_Professional.production.surface',
           'UV_Wall_Professional.production.cap_surface_geometry',
           'UV_Wall_Professional.production.surface_coverage',
           'UV_Wall_Professional.production.cornice',
           'UV_Wall_Professional.production.cap_uv_integrity',
           'UV_Wall_Professional.production.height_correspondence',
           'UV_Wall_Professional.production.planar_integrity',
           'UV_Wall_Professional.production.corner_trim',
           'UV_Wall_Professional.production.mirror_corner_integrity',
           'UV_Wall_Professional.production.corner_audit',
           'UV_Wall_Professional.production.wall_topology',
           'UV_Wall_Professional.production.topology_first',
           'UV_Wall_Professional.production.inner_height',
           'UV_Wall_Professional.production.secondary_points',
           'UV_Wall_Professional.production.wall',
           'UV_Wall_Professional.production.wall_uv_continuity',
           'UV_Wall_Professional.production.wall_uv_field',
           'UV_Wall_Professional.operators.build',
           'UV_Wall_Professional.production.segment_joint_graph',
           'UV_Wall_Professional.operators.segment_joint_preview')

def operation_identity(package='UV_Wall_Professional'):
    """New operation ID and hash of the source files of actual loaded modules."""
    root = sys.modules.get(package)
    version = getattr(root, 'bl_info', {}).get('version', None)
    result = {
        'operation_id': uuid.uuid4().hex,
        'utc': datetime.now(timezone.utc).isoformat(),
        'version': '.'.join(map(str, version)) if version else 'UNKNOWN',
        'modules': [],
        'warnings': [],
    }
    root_path = getattr(root, '__file__', None)
    root_dir = pathlib.Path(root_path).resolve().parent if root_path else None
    for name in MODULES:
        module=sys.modules.get(name)
        if module is None:
            # A runtime identity snapshot may be taken before a lazily used
            # module is imported. That is not a damaged installation. Hash the
            # available source file and expose its state explicitly; only a
            # genuinely absent file is a warning.
            relative = pathlib.Path(*name.split('.')[1:]).with_suffix('.py')
            candidate = (root_dir / relative).resolve() if root_dir else None
            if candidate is not None and candidate.is_file():
                result['modules'].append({
                    'name': name, 'path': str(candidate),
                    'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    'state': 'AVAILABLE_NOT_IMPORTED',
                })
            else:
                result['warnings'].append('MISSING_MODULE_FILE: '+name)
            continue
        path=getattr(module,'__file__',None)
        item={'name':name,'path':str(path or '<unknown>'),'state':'LOADED'}
        try:
            if not path:
                raise OSError('missing module path')
            resolved=pathlib.Path(path).resolve()
            item['sha256']=hashlib.sha256(resolved.read_bytes()).hexdigest()
        except (OSError,ValueError) as e:
            item['sha256']=None
            result['warnings'].append(f'UNREADABLE: {name}: {e}')
        result['modules'].append(item)
    # Detect a mixed installation independently of mutable module bl_info.
    # The release manifest is generated after all code edits and included in ZIP.
    try:
        root_path=getattr(root, '__file__', None)
        if root_path:
            root_dir=pathlib.Path(root_path).resolve().parent
            # Never fall back to a previous version manifest on a new build.
            manifest_file = root_dir / RUNTIME_MANIFEST
            if manifest_file.exists():
                manifest=json.loads(manifest_file.read_text(encoding='utf-8'))
                result['release_id']=manifest.get('release_id','UNKNOWN')
                expected_version=manifest.get('version','UNKNOWN')
                if result['version']!=expected_version:
                    result['warnings'].append('VERSION_MISMATCH: runtime='+result['version']+' release='+expected_version)
                for item in result['modules']:
                    wanted=manifest.get('sha256',{}).get(item['name'])
                    if wanted is None:
                        result['warnings'].append('MANIFEST_MISSING_MODULE: '+item['name'])
                    elif wanted!=item.get('sha256'):
                        result['warnings'].append('SOURCE_HASH_MISMATCH: '+item['name'])
            else:
                result['warnings'].append('RELEASE_MANIFEST_MISSING')
    except (OSError,ValueError,TypeError) as error:
        result['warnings'].append('RELEASE_MANIFEST_UNREADABLE: '+str(error))
    return result

def log_identity(logger, package='UV_Wall_Professional'):
    identity=operation_identity(package)
    logger('RUNTIME_IDENTITY '+json.dumps(identity,ensure_ascii=False,sort_keys=True),'SNAPSHOT')
    for warning in identity['warnings']:
        if warning.startswith(('VERSION_MISMATCH','SOURCE_HASH_MISMATCH')):
            logger('RUNTIME_SOURCE_MISMATCH '+warning,'ERROR')
    return identity


def verify_loaded_function_source(module_name, function_name):
    """Verify that a loaded top-level function matches this installation's source.

    IMPORTANT: compile the *whole module*, exactly as Python import does.  Older
    1.5.24 code compiled only the isolated FunctionDef and marshalled that code
    object.  CPython can emit different bytecode for an isolated AST than for
    the same function compiled in its real module context, which caused false
    STALE_FUNCTION_BYTECODE failures in Blender 4.5.5.

    The package SHA256 manifest remains the primary disk-integrity check.  This
    runtime check detects an old in-memory function object without depending on
    line tables, filenames or source line numbers. Decorated functions are
    unwrapped with ``inspect.unwrap`` before code comparison, so wrappers from
    @contextmanager/@functools.wraps do not create false stale-code alarms.
    """
    import ast
    import inspect
    import types

    module = sys.modules.get(module_name)
    if module is None:
        return False, f'NOT_LOADED: {module_name}'
    loaded = getattr(module, function_name, None)
    if loaded is None:
        return False, f'MISSING_CALLABLE: {module_name}.{function_name}'
    try:
        # Decorators such as @contextmanager/@functools.wraps replace the
        # callable stored in the module with a wrapper. The wrapper bytecode
        # is expected to differ from the source FunctionDef even when the
        # installation is clean. Compare the original callable exposed through
        # __wrapped__ instead of treating the wrapper as stale.
        descriptor_unwrapped = loaded.__func__ if isinstance(loaded, (staticmethod, classmethod)) else loaded
        runtime_callable = inspect.unwrap(descriptor_unwrapped)
        if not hasattr(runtime_callable, '__code__'):
            return False, f'MISSING_CALLABLE_CODE: {module_name}.{function_name}'
        unwrap_depth = 0
        probe = descriptor_unwrapped
        seen = set()
        while hasattr(probe, '__wrapped__') and id(probe) not in seen:
            seen.add(id(probe))
            unwrap_depth += 1
            probe = probe.__wrapped__
        source_path = pathlib.Path(module.__file__).resolve()
        source_text = source_path.read_text(encoding='utf-8')
        tree = ast.parse(source_text, filename=str(source_path))
        # Compiling the whole module is essential: it reproduces import-time
        # symbol-table/compiler decisions instead of an artificial one-function
        # module used by the 1.5.24 verifier.
        module_code = compile(tree, str(source_path), 'exec', dont_inherit=True, optimize=-1)
        expected_code = next(
            c for c in module_code.co_consts
            if isinstance(c, types.CodeType) and c.co_name == function_name
        )

        def semantic_signature(code):
            """Line-number independent recursive code signature."""
            constants=[]
            for value in code.co_consts:
                if isinstance(value, types.CodeType):
                    constants.append(('code', semantic_signature(value)))
                else:
                    # repr gives a stable local value for normal Python
                    # constants without trying to marshal line metadata.
                    constants.append(('const', type(value).__name__, repr(value)))
            payload=(
                code.co_name,
                getattr(code, 'co_qualname', code.co_name),
                code.co_argcount,
                getattr(code, 'co_posonlyargcount', 0),
                code.co_kwonlyargcount,
                code.co_nlocals,
                code.co_flags,
                code.co_code,
                tuple(code.co_names),
                tuple(code.co_varnames),
                tuple(code.co_freevars),
                tuple(code.co_cellvars),
                tuple(constants),
            )
            return payload

        expected_sig = semantic_signature(expected_code)
        loaded_sig = semantic_signature(runtime_callable.__code__)
        if expected_sig != loaded_sig:
            # Keep hashes compact in user logs while hashing only semantic
            # content.  repr() is sufficient here because the payload is made
            # solely from deterministic tuples/bytes/strings/ints.
            expected_hash = hashlib.sha256(repr(expected_sig).encode('utf-8')).hexdigest()
            loaded_hash = hashlib.sha256(repr(loaded_sig).encode('utf-8')).hexdigest()
            return False, (f'STALE_FUNCTION_CODE: {module_name}.{function_name}; '
                           f'decorated={unwrap_depth > 0}; unwrap_depth={unwrap_depth}; '
                           f'loaded={loaded_hash[:16]}; disk={expected_hash[:16]}')

        runtime_module_name = getattr(runtime_callable, '__module__', module_name)
        loaded_path = pathlib.Path(getattr(sys.modules.get(runtime_module_name), '__file__', source_path)).resolve()
        if loaded_path != source_path:
            return False, (f'FUNCTION_MODULE_PATH_MISMATCH: {module_name}.{function_name}; '
                           f'loaded={loaded_path}; disk={source_path}')
        return True, 'OK'
    except (OSError, SyntaxError, ValueError, TypeError, StopIteration) as error:
        return False, f'FUNCTION_SOURCE_CHECK_FAILED: {module_name}.{function_name}: {error}'


def verify_critical_geometry_sources():
    """Build must not use old in-memory geometry or Weld functions."""
    functions = (
        ('UV_Wall_Professional.core.revision_recovery','atomic_restore_definition'),
        # Every entry is exactly (module, function); these functions are
        # defined in core.definitions, not in core.blender_compat. A third
        # item here made `for module, name in functions` crash during enable.
        ('UV_Wall_Professional.core.definitions', 'ensure_source_uid'),
        ('UV_Wall_Professional.core.definitions', 'unresolved_source_uid_collision'),
        ('UV_Wall_Professional.core.result_lifecycle', 'ownership_guard'),
        ('UV_Wall_Professional.core.result_lifecycle', 'active_owned_results'),
        ('UV_Wall_Professional.core.build_guard', 'build_transaction'),
        ('UV_Wall_Professional.production.surface', 'build_top_cap'),
        ('UV_Wall_Professional.production.cap_surface_geometry', 'height_at_xy'),
        ('UV_Wall_Professional.production.cap_surface_geometry', 'reconcile_patch_boundary_xy'),
        ('UV_Wall_Professional.production.validation', 'smart_regulation_weld'),
        ('UV_Wall_Professional.production.cornice', 'cornice_transport_up_vectors'),
        ('UV_Wall_Professional.production.cornice', 'cornice_corner_metrics'),
        ('UV_Wall_Professional.core.material_intelligence', 'inspect_material_asset'),
        ('UV_Wall_Professional.core.problem_registry', 'scan_result_mesh'),
        ('UV_Wall_Professional.core.problem_registry', 'scan_cornice_result_metadata'),
        ('UV_Wall_Professional.production.inner_height', 'height_at_station'),
        ('UV_Wall_Professional.production.secondary_points', 'parse_secondary_points'),
        ('UV_Wall_Professional.production.topology_first', 'local_vertical_slabs'),
        ('UV_Wall_Professional.production.topology_first', 'preweld_shared_vertices'),
        ('UV_Wall_Professional.production.segment_joint_graph', 'create_joint_graph'),
        ('UV_Wall_Professional.operators.segment_joint_preview', 'build_preview'),
    )
    failures = []
    for module, name in functions:
        ok, reason = verify_loaded_function_source(module, name)
        if not ok:
            failures.append(reason)
    return failures
