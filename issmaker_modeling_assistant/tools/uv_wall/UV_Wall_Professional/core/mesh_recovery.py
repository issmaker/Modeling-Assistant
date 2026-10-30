"""Persistent *single-slot* recovery of an accepted UV Wall result Mesh.

This is deliberately separate from Blender Undo and the transactional builder.
Only Mesh datablocks are copied, and neither a Blender Object nor a long-lived
RNA pointer is stored in Python globals. No automatic restoration occurs.
"""
from __future__ import annotations

from array import array
import hashlib
import json
import struct
import uuid

RECOVERY_SCHEMA = 1
RECOVERY_NAME = 'uvwall_recovery_mesh_v1'
RECOVERY_OWNER = 'uvwall_recovery_owner_v1'
RECOVERY_RESULT = 'uvwall_recovery_result_v1'
RECOVERY_DIGEST = 'uvwall_recovery_digest_v1'
RECOVERY_TOKEN = 'uvwall_recovery_token_v1'
RECOVERY_REVISION = 'uvwall_recovery_revision_v1'
RECOVERY_STATS = 'uvwall_recovery_stats_v1'
RECOVERY_MARK = 'uvwall_recovery_backup_v1'
# Estimate, not a prediction of Blender's real allocation. Fail open for Build.
MAX_ESTIMATED_BYTES = 256 * 1024 * 1024


class RecoveryError(ValueError):
    def __init__(self, code, detail=''):
        super().__init__(f'{code}: {detail}' if detail else code)
        self.code = code


def _meta(obj, name, default=''):
    try:
        return obj.get(name, default)
    except (AttributeError, ReferenceError, RuntimeError):
        return default


def estimated_bytes(mesh):
    """Conservative guard; does not claim actual process peak RSS."""
    vertices = len(mesh.vertices)
    edges = len(mesh.edges)
    loops = len(mesh.loops)
    polygons = len(mesh.polygons)
    uvs = len(mesh.uv_layers)
    raw = vertices * 12 + edges * 8 + loops * (16 + uvs * 8) + polygons * 20
    return raw * 2


def _array_part(digest, label, values, prop, datatype):
    digest.update(label.encode('utf-8') + b'\0')
    digest.update(struct.pack('<Q', len(values)))
    buf = array(datatype, [0]) * (len(values) * (3 if prop == 'co' else 2 if prop == 'uv' else 1))
    values.foreach_get(prop, buf)
    # Windows Blender uses little endian; explicit representation for portability.
    if struct.pack('=I', 1) != struct.pack('<I', 1):
        buf.byteswap()
    digest.update(buf.tobytes())


def geometry_fingerprint(mesh):
    """Hash vertex positions, face topology, material indexes and UV coordinates.

    Additional custom attributes are preserved by Mesh.copy() but are not yet
    included in this fingerprint; the checksum is NOT a full Mesh forensic hash.
    """
    h = hashlib.sha256(b'ISSMAKER-WALL-RECOVERY-GEOMETRY-V1\0')
    _array_part(h, 'vertices.co', mesh.vertices, 'co', 'f')
    _array_part(h, 'loops.vertex_index', mesh.loops, 'vertex_index', 'i')
    for prop in ('loop_start', 'loop_total', 'material_index'):
        _array_part(h, f'polygons.{prop}', mesh.polygons, prop, 'i')
    h.update(struct.pack('<I', len(mesh.uv_layers)))
    for uv_layer in mesh.uv_layers:
        h.update(uv_layer.name.encode('utf-8') + b'\0')
        _array_part(h, 'uv', uv_layer.data, 'uv', 'f')
    return h.hexdigest()


def _require_result(result, data):
    if result is None or getattr(result, 'type', None) != 'MESH':
        raise RecoveryError('RECOVERY_SELECT_WALL', 'Выделите принятую стену')
    if not _meta(result, 'uvwall_generated') or _meta(result, 'uv_wall_mode') != 'WALL':
        raise RecoveryError('RECOVERY_NOT_ACCEPTED_WALL')
    if _meta(result, 'uvwall_detached') or _meta(result, 'uvwall_baked'):
        raise RecoveryError('RECOVERY_DETACHED_OR_BAKED')
    owner = str(_meta(result, 'uvwall_owner_uid') or '')
    result_uid = str(_meta(result, 'uvwall_result_uid') or '')
    if not owner or not result_uid:
        raise RecoveryError('RECOVERY_MISSING_IDENTITY')
    # A copied Object commonly inherits these IDs. Refuse an ambiguous owner.
    matches = 0
    for other in data.objects:
        try:
            if (other.type == 'MESH' and str(other.get('uvwall_result_uid') or '') == result_uid
                    and bool(other.get('uvwall_generated'))):
                matches += 1
                if matches > 1:
                    raise RecoveryError('RECOVERY_DUPLICATE_RESULT_UID', result_uid)
        except (ReferenceError, AttributeError, RuntimeError):
            continue
    if matches != 1:
        raise RecoveryError('RECOVERY_RESULT_NOT_REGISTERED')
    return owner, result_uid


def _find_backup(result, data, *, deep=True):
    owner, result_uid = _require_result(result, data)
    name = str(_meta(result, RECOVERY_NAME) or '')
    if not name:
        raise RecoveryError('RECOVERY_SNAPSHOT_NOT_FOUND')
    backup = data.meshes.get(name)
    if backup is None:
        raise RecoveryError('RECOVERY_SNAPSHOT_MISSING', name)
    if (not _meta(backup, RECOVERY_MARK) or
        str(_meta(backup, RECOVERY_OWNER)) != owner or
        str(_meta(backup, RECOVERY_RESULT)) != result_uid or
        str(_meta(backup, RECOVERY_TOKEN)) != str(_meta(result, RECOVERY_TOKEN)) or
        str(_meta(backup, RECOVERY_DIGEST)) != str(_meta(result, RECOVERY_DIGEST))):
        raise RecoveryError('RECOVERY_SNAPSHOT_IDENTITY_MISMATCH')
    if deep:
        try:
            if geometry_fingerprint(backup) != str(_meta(result, RECOVERY_DIGEST)):
                raise RecoveryError('RECOVERY_SNAPSHOT_TAMPERED')
        except (ValueError, TypeError, AttributeError, RuntimeError, OverflowError) as exc:
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError('RECOVERY_FINGERPRINT_FAILED', type(exc).__name__) from exc
    return backup, owner, result_uid


def recovery_status(result, data):
    """Cheap, redraw-safe metadata check. Deep checksum runs only on restore."""
    try:
        backup, _owner, _result_uid = _find_backup(result, data, deep=False)
        return {'available': True, 'code': 'READY', 'mesh': backup.name,
                'revision': int(_meta(result, RECOVERY_REVISION, -1))}
    except RecoveryError as exc:
        return {'available': False, 'code': exc.code}


def store_accepted_mesh(result, data, *, build_revision):
    """Publish a new backup only after a successful, validated Build.

    A snapshot allocation error cannot undo an already accepted Mesh commit.
    Return structured status to be logged as warning, never assert Build invalid.
    """
    try:
        owner, result_uid = _require_result(result, data)
        mesh = result.data
        estimate = estimated_bytes(mesh)
        if estimate > MAX_ESTIMATED_BYTES:
            return {'status': 'RESOURCE_LIMIT', 'estimated_bytes': estimate,
                    'limit_bytes': MAX_ESTIMATED_BYTES}
        digest = geometry_fingerprint(mesh)
        previous = str(_meta(result, RECOVERY_NAME) or '')
        candidate = None
        try:
            candidate = mesh.copy()
            candidate.name = f'ISSMAKER_RECOVERY_{result_uid[:16]}_{uuid.uuid4().hex[:8]}'
            candidate.use_fake_user = True
            token = uuid.uuid4().hex
            stats = {'vertices':len(mesh.vertices), 'faces':len(mesh.polygons),
                     'loops':len(mesh.loops), 'uv_layers':len(mesh.uv_layers)}
            for key, value in ((RECOVERY_MARK, True), (RECOVERY_OWNER, owner),
                               (RECOVERY_RESULT, result_uid), (RECOVERY_TOKEN, token),
                               (RECOVERY_DIGEST, digest), (RECOVERY_REVISION, int(build_revision)),
                               (RECOVERY_STATS, json.dumps(stats, separators=(',', ':')))):
                candidate[key] = value
            # Publish target metadata last. Roll back partial writes on exception.
            keys = (RECOVERY_NAME, RECOVERY_OWNER, RECOVERY_RESULT, RECOVERY_TOKEN,
                    RECOVERY_DIGEST, RECOVERY_REVISION, RECOVERY_STATS)
            old_props = {key: _meta(result, key, None) for key in keys}
            try:
                for key, value in ((RECOVERY_NAME, candidate.name), (RECOVERY_OWNER, owner),
                                   (RECOVERY_RESULT, result_uid), (RECOVERY_TOKEN, token),
                                   (RECOVERY_DIGEST, digest), (RECOVERY_REVISION, int(build_revision)),
                                   (RECOVERY_STATS, json.dumps(stats, separators=(',', ':')))):
                    result[key] = value
            except Exception:
                for key, old in old_props.items():
                    try:
                        if old is None:
                            if key in result: del result[key]
                        else:
                            result[key] = old
                    except Exception:
                        pass
                raise
            # Old backups are removed only if not referenced by another ID.
            if previous and previous != candidate.name:
                old = data.meshes.get(previous)
                if old is not None and (_meta(old, RECOVERY_MARK)
                                        and _meta(old, RECOVERY_OWNER) == owner
                                        and _meta(old, RECOVERY_RESULT) == result_uid):
                    try:
                        if old.users <= 1:
                            data.meshes.remove(old)
                    except (AttributeError, ReferenceError, RuntimeError):
                        pass  # A harmless orphan is preferable to lost geometry.
            return {'status': 'STORED', 'backup_name': candidate.name,
                    'estimated_bytes': estimate, 'fingerprint': digest[:16]}
        except Exception:
            if candidate is not None:
                try:
                    if candidate.users <= 1:
                        data.meshes.remove(candidate)
                except Exception:
                    pass
            raise
    except Exception as exc:
        return {'status': 'INTERNAL_ERROR', 'reason':f'{type(exc).__name__}:{exc}'}


def restore_accepted_mesh(result, data):
    """Transactional explicit restoration. The Blender operator owns UNDO."""
    backup, owner, result_uid = _find_backup(result, data, deep=True)
    previous_mesh = result.data
    replacement = None
    try:
        replacement = backup.copy()
        replacement.use_fake_user = False
        for key in (RECOVERY_MARK, RECOVERY_OWNER, RECOVERY_RESULT, RECOVERY_TOKEN,
                    RECOVERY_DIGEST, RECOVERY_REVISION, RECOVERY_STATS):
            if key in replacement:
                del replacement[key]
        result.data = replacement
        if result.data is not replacement:
            raise RecoveryError('RECOVERY_MESH_ASSIGNMENT_FAILED')
        return {'status':'RESTORED', 'mesh': replacement.name,
                'owner_uid':owner, 'result_uid':result_uid,
                'revision': int(_meta(result, RECOVERY_REVISION, -1))}
    except Exception:
        try: result.data = previous_mesh
        except (ReferenceError, RuntimeError, AttributeError): pass
        if replacement is not None:
            try:
                if replacement.users == 0:
                    data.meshes.remove(replacement)
            except Exception:
                pass
        raise
